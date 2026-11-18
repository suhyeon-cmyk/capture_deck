"""
온라인 강의 연속 자동 녹화 (해커스 나의 강의실 + StarPlayer)

자동녹화용 Edge(edge_cdp)에서 '나의 강의실 → 강좌 선택'까지 해 두면
강의 목록에서 지정한 LESSON(제 1 강, 1~3, 1, 2, 3 ...)을 차례로 열어 → 처음부터 재생 → 전체화면 → 녹화 → 강의가 끝나면 저장 → 다음 강의.
"""
import re
import threading
import time

import edge_cdp

LIST_HOST = "myclass.hackers.com"   # 나의 강의실 (강의 목록)
PLAYER_HOST = "class.hackers.com"   # 강의 팝업

# 강의 목록 읽기: [{idx, no, title, mins}]
READ_LIST_JS = r"""
Array.from(document.querySelectorAll('tr[name=lecture]')).map(tr => {
  const a = tr.querySelector('a.classroom-view');
  if (!a) return null;
  const no = parseInt((tr.cells[0].innerText.match(/\d+/) || ['0'])[0]);
  const len = (tr.querySelector('.td_lecture_playtime') || {}).innerText || '';
  const m = len.match(/\((\d+)분\)/);
  return {idx: tr.getAttribute('lectureidx'), no: no, title: a.innerText.trim().replace(/\s+/g, ' '),
          mins: m ? parseInt(m[1]) : null};
}).filter(x => x)
"""

# 강의 링크 클릭 (사이트가 강의 팝업을 열어 줌)
CLICK_JS = r"""
(() => {
  const tr = document.querySelector('tr[name=lecture][lectureidx="%s"]');
  const a = tr && tr.querySelector('a.classroom-view');
  if (!a) return false;
  a.scrollIntoView({block: 'center'});
  a.click();
  return true;
})()
"""

# 강의 팝업 상태
POPUP_STATE_JS = r"""
(() => {
  if (typeof goLesson === 'function' && document.querySelector('.intropage')) return {step: 'intro'};
  const f = document.getElementById('StarPlayerForm');
  if (!f) return {step: 'loading'};
  let w, d;
  try { w = f.contentWindow; d = f.contentDocument; } catch (e) { return {step: 'loading'}; }
  if (!d) return {step: 'loading'};
  const resume = Array.from(d.querySelectorAll('a.btn_type')).find(a => a.innerText.includes('처음부터'));
  if (resume && resume.offsetParent) return {step: 'resume'};
  const v = d.getElementById('starplayer');
  if (!v) return {step: 'loading'};
  return {step: 'player', t: v.currentTime, dur: v.duration || 0, paused: v.paused, ended: v.ended,
          done: w.comple == 1 || w.complete === true, fs: !!d.fullscreenElement,
          rate: v.playbackRate, src: !!v.getAttribute('src') || !!v.currentSrc};
})()
"""

RESUME_JS = r"""
(() => {
  const d = document.getElementById('StarPlayerForm').contentDocument;
  const a = Array.from(d.querySelectorAll('a.btn_type')).find(a => a.innerText.includes('처음부터'));
  a.click();
  return true;
})()
"""

# 재생 준비: 일시정지 + 맨 앞으로 + 1배속 + 소리 켬
PREPARE_JS = r"""
(() => {
  const w = document.getElementById('StarPlayerForm').contentWindow;
  const v = w.document.getElementById('starplayer');
  if (!v.paused) v.pause();
  v.currentTime = 0;
  try { w.player.setRate(1); } catch (e) { v.playbackRate = 1; }
  v.muted = false;
  if (v.volume < 1) { try { w.player.setVolume(1); } catch (e) { v.volume = 1; } }
  return true;
})()
"""

PLAY_JS = r"""
(() => {
  const d = document.getElementById('StarPlayerForm').contentDocument;
  const v = d.getElementById('starplayer');
  if (v.paused) { const b = d.querySelector('a.btn_play'); if (b) b.click(); else v.play(); }
  return true;
})()
"""

RATE_JS = r"""
(() => {
  const w = document.getElementById('StarPlayerForm').contentWindow;
  try { w.player.setRate(1); } catch (e) { w.document.getElementById('starplayer').playbackRate = 1; }
  return true;
})()
"""

# 녹화 중에는 재생기 조작 막대·안내 문구를 안 보이게 (투명하게만 해서 버튼 클릭은 그대로 됨)
HIDE_UI_JS = r"""
(() => {
  const d = document.getElementById('StarPlayerForm').contentDocument;
  if (d.getElementById('cd-hide-ui')) return true;
  const st = d.createElement('style');
  st.id = 'cd-hide-ui';
  st.textContent = '#controller-container2, #controller-container2 *, [class*=tooltip]' +
    ' { opacity: 0 !important; transition: none !important; }' +
    ' html, body, body * { cursor: none !important; }';
  d.head.appendChild(st);
  return true;
})()
"""

# 전체화면 버튼의 페이지 좌표 (진짜 마우스 클릭으로 눌러야 전체화면이 됨)
FS_POS_JS = r"""
(() => {
  const f = document.getElementById('StarPlayerForm');
  const b = f.contentDocument.querySelector('a.btn_fullscreen');
  if (!b) return null;
  const fr = f.getBoundingClientRect(), br = b.getBoundingClientRect();
  return [fr.left + f.clientLeft + br.left + br.width / 2, fr.top + f.clientTop + br.top + br.height / 2];
})()
"""


# ---------------------------------------------------------------- 강의 목록
def list_page():
    """강의 목록이 열린 탭"""
    for t in edge_cdp.pages():
        if LIST_HOST in t.get("url", ""):
            return t
    return None


def read_lectures():
    """나의 강의실에 펼쳐 둔 강의 목록. Edge가 안 떠 있거나 목록이 없으면 RuntimeError"""
    if not edge_cdp.is_running():
        raise RuntimeError("자동녹화용 Edge가 열려 있지 않아요. 'Edge 열기'를 먼저 눌러 주세요.")
    t = list_page()
    if not t:
        raise RuntimeError("Edge에서 '나의 강의실' 페이지를 찾지 못했어요.")
    s = edge_cdp.Session(t)
    try:
        lecs = s.eval(READ_LIST_JS) or []
    finally:
        s.close()
    if not lecs:
        raise RuntimeError("강의 목록이 보이지 않아요. 나의 강의실에서 강좌를 눌러 강의 목록을 펼쳐 주세요.")
    return lecs


_LESSON = r"(?:제\s*)?(\d+)\s*강?"                      # 1 / 1강 / 제 1 강 / 제1강
_LESSON_TOKEN = re.compile(rf"{_LESSON}(?:\s*[-~]\s*{_LESSON})?")  # 하나 또는 범위 (1~3, 제 1 강 ~ 제 3 강)


def lesson_numbers(line):
    """줄 전체가 강 번호(쉼표로 여러 개, 범위 가능)면 번호 목록, 아니면 None
    예: '제 1 강, 제 2 강' / '1,2,3' / '1~3' / '제 1 강~제 3 강' / '제 1 강,' """
    parts = [p.strip() for p in line.split(",")]
    if not any(parts):
        return None
    nums = []
    for p in parts:
        if not p:
            continue  # '제 1 강,' 처럼 끝에 쉼표
        m = _LESSON_TOKEN.fullmatch(p)
        if not m:
            return None
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        nums += list(range(a, b + 1) if a <= b else range(a, b - 1, -1))
    return nums


def match_lines(lines, lecs):
    """입력한 LESSON(번호 / 쉼표 / 범위)을 강의로 바꿈 → [(입력, 강의 또는 None)]
    LESSON 형식이 아닌 줄은 (그 줄, None)"""
    by_no = {}
    for lec in lecs:
        by_no.setdefault(lec["no"], lec)
    out = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        nums = lesson_numbers(line)
        if nums is None:
            out.append((line, None))
        else:
            out += [(f"제 {n} 강", by_no.get(n)) for n in nums]
    return out


def safe_name(lec):
    """저장 파일 이름 = LESSON (예: '제 1 강')"""
    return f"제 {lec['no']} 강"


# ---------------------------------------------------------------- 자동 녹화
class Stopped(Exception):
    pass


class AutoRecorder(threading.Thread):
    """
    강의를 차례로 재생하며 녹화. 녹화 시작/중지는 hooks로 프로그램 본체에 맡김.
    hooks: start_rec(name) -> bool, stop_rec(), is_busy() -> bool,
           status(i, text), log(text), finished(summary)   (모두 이 스레드에서 호출 → 본체가 메인 스레드로 넘김)
    """

    def __init__(self, lectures, hooks, gap=5):
        super().__init__(daemon=True)
        self.lectures = lectures
        self.h = hooks
        self.gap = gap
        self.stop_flag = threading.Event()
        self.recording = False

    def stop(self):
        self.stop_flag.set()

    # -- 공통
    def _sleep(self, sec):
        if self.stop_flag.wait(sec):
            raise Stopped()

    def _wait(self, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            r = cond()
            if r:
                return r
            self._sleep(0.5)
        raise RuntimeError(f"{what} 시간 초과")

    @staticmethod
    def _popups():
        return [t for t in edge_cdp.pages() if PLAYER_HOST in t.get("url", "") and LIST_HOST not in t.get("url", "")
                or "classSatisfactionSurvey" in t.get("url", "")]

    def _close_popups(self):
        """강의 창 닫기 → 닫혔으면 True (닫으면 강의 목록 페이지가 새로고침됨)"""
        closed = False
        for t in self._popups():
            try:
                edge_cdp.close_target(t["id"])
                closed = True
            except Exception:
                pass
        return closed

    def _wait_idle(self):
        """이전 녹화의 MP4 변환이 끝날 때까지"""
        while self.h["is_busy"]():
            self._sleep(0.5)

    # -- 실행
    def run(self):
        ok = fail = 0
        try:
            for i, lec in enumerate(self.lectures):
                if self.stop_flag.is_set():
                    break
                self.h["status"](i, "준비 중")
                try:
                    self._one(i, lec)
                    ok += 1
                    self.h["status"](i, "완료")
                except Stopped:
                    self.h["status"](i, "중지됨")
                    break
                except Exception as e:
                    fail += 1
                    self.h["status"](i, f"실패: {e}")
                    self.h["log"](f"{lec['no']}강 실패: {e}")
                finally:
                    if self.recording:
                        self.h["stop_rec"]()
                        self.recording = False
                    try:
                        self._close_popups()
                    except Exception:
                        pass
                if i + 1 < len(self.lectures) and not self.stop_flag.is_set():
                    try:
                        self._sleep(self.gap)
                    except Stopped:
                        break
            # 마지막 녹화 변환까지 기다림
            while self.h["is_busy"]():
                time.sleep(0.5)
        finally:
            stopped = self.stop_flag.is_set()
            left = len(self.lectures) - ok - fail
            msg = f"자동 녹화 {'중지' if stopped else '끝'} - 완료 {ok}개" + (f", 실패 {fail}개" if fail else "") + \
                  (f", 남은 강의 {left}개" if stopped and left > 0 else "")
            self.h["finished"](msg)

    def _one(self, i, lec):
        st = lambda text: self.h["status"](i, text)  # noqa: E731
        self._wait_idle()
        if self._close_popups():
            self._sleep(3)  # 목록 페이지가 새로고침되기 시작할 때까지

        # 1. 강의 목록에서 강의 클릭 (팝업을 닫은 뒤 목록 페이지가 새로고침되므로 다시 나타날 때까지 기다림)
        st("강의 여는 중")
        main_t = self._wait(list_page, 20, "강의 목록 페이지 찾기")

        def find_row():
            s = edge_cdp.Session(list_page())
            try:
                return any(x["idx"] == lec["idx"] for x in (s.eval(READ_LIST_JS) or []))
            except Exception:
                return False
            finally:
                s.close()

        self._wait(find_row, 30, "강의 목록 표시")
        s = edge_cdp.Session(list_page())
        try:
            s.send("Page.enable")
            if not s.eval(CLICK_JS % lec["idx"], user_gesture=True):
                raise RuntimeError("강의 목록에서 강의를 찾지 못함")
        finally:
            s.close()
        pop = self._wait(lambda: (self._popups() or [None])[0], 20, "강의 창 열기")

        # 강의 창을 강의 목록 창과 같은 모니터로 (전체화면이 그 모니터에 뜨도록)
        try:
            _, b = edge_cdp.window_bounds(main_t["id"])
            edge_cdp.move_window(pop["id"], b["left"] + 40, b["top"] + 40, 1360, 768)
        except Exception:
            pass

        ps = edge_cdp.Session(pop)
        try:
            ps.send("Page.enable")
            self._play(ps, i, lec, st)
        finally:
            ps.close()

    def _state(self, ps):
        try:
            return ps.eval(POPUP_STATE_JS) or {"step": "loading"}
        except Exception:  # 페이지 이동 중
            return {"step": "loading"}

    def _play(self, ps, i, lec, st):
        # 2. 인트로 → '처음부터 보기' → 영상 준비될 때까지
        st("영상 불러오는 중")
        end = time.time() + 60
        while True:
            if time.time() > end:
                raise RuntimeError("영상을 불러오지 못함")
            s = self._state(ps)
            if s["step"] == "intro":
                try:
                    ps.eval("goLesson(); true")
                except Exception:
                    pass
                self._sleep(2)
            elif s["step"] == "resume":
                ps.eval(RESUME_JS, user_gesture=True)
                self._sleep(2)
            elif s["step"] == "player" and s["dur"] > 0:
                break
            else:
                self._sleep(0.5)
        dur = s["dur"]
        if "length" in self.h:
            self.h["length"](i, dur)  # 표의 길이를 실제 영상 길이(초 단위)로
        self._sleep(1)
        ps.eval(PREPARE_JS)
        self._sleep(1)

        # 3. 전체화면 (진짜 마우스 클릭)
        st("전체화면 전환")
        ps.bring_to_front()
        for _ in range(3):
            pos = ps.eval(FS_POS_JS)
            if pos:
                ps.click(*pos)
            self._sleep(1.5)
            if self._state(ps).get("fs"):
                break
        else:
            self.h["log"](f"{lec['no']}강: 전체화면으로 바꾸지 못해서 창 상태로 녹화해요")
        ps.eval(PREPARE_JS)  # 전체화면 전환 중 재생이 시작됐으면 다시 맨 앞으로
        ps.eval(HIDE_UI_JS)  # 0:00부터 조작 막대가 안 보이게
        self._sleep(1)

        # 4. 녹화 시작 → 재생
        st("녹화 시작")
        for _ in range(20):  # 영상 신호가 잠깐 끊겨 있으면 기다림
            if self.h["start_rec"](safe_name(lec)):
                break
            self._sleep(1)
        else:
            raise RuntimeError("녹화를 시작하지 못함 (영상 신호 확인)")
        self.recording = True
        self._sleep(1)
        ps.eval(PLAY_JS, user_gesture=True)

        # 5. 끝날 때까지 지켜봄
        started = time.time()
        paused_since = None
        last_t = 0.0
        rate_fixed = False
        fs_retry = 0
        while True:
            # 끝 무렵에는 0.1초마다 확인 (끝 판정은 목록의 '분'이 아니라 실제 영상 재생 위치로 함)
            self._sleep(0.1 if dur - last_t < 3 else 0.5)
            s = self._state(ps)
            if s["step"] != "player":
                if not any(t["id"] == ps.target["id"] for t in edge_cdp.pages()):
                    if last_t >= dur - 1:
                        break  # 끝까지 재생한 뒤 창이 닫힘
                    raise RuntimeError(f"강의 창이 닫힘 ({int(last_t)}초 / {int(dur)}초)")
                continue  # 잠깐 읽지 못함 → 다시 확인
            # 재생기가 '재생 끝'을 알리거나 영상이 끝까지 재생됐을 때만 멈춤 (조금이라도 남으면 계속 녹화)
            if s["done"] or s["ended"] or (s["dur"] and s["t"] >= s["dur"] - 0.02):
                break
            last_t = s["t"]
            left = max(0, int(dur - last_t))
            st(f"녹화 중 {int(last_t) // 60}:{int(last_t) % 60:02d} / {int(dur) // 60}:{int(dur) % 60:02d}"
               f" (남은 {left // 60}분 {left % 60}초)")
            if not s["paused"] and not rate_fixed and s["rate"] != 1:
                ps.eval(RATE_JS)  # 플레이어가 예전 배속을 되살린 경우
                rate_fixed = True
            if s["paused"]:
                paused_since = paused_since or time.time()
                if time.time() - paused_since > 3:  # 멈춰 있으면 다시 재생
                    ps.eval(PLAY_JS, user_gesture=True)
                    paused_since = time.time()
            else:
                paused_since = None
                if not s["fs"] and fs_retry < 3 and last_t > 3:  # 전체화면이 풀렸으면 다시
                    fs_retry += 1
                    pos = ps.eval(FS_POS_JS)
                    if pos:
                        ps.click(*pos)
            if time.time() - started > dur * 1.5 + 600:
                raise RuntimeError("예상보다 너무 오래 걸려서 멈춤")
        for msg in ps.dialogs:
            self.h["log"](f"{lec['no']}강 알림창: {msg[:80]}")
        # 6. 녹화 중지 (강의 창은 run()에서 닫음)
        # 캡쳐 보드를 거쳐 들어오는 화면은 실제보다 조금 늦으므로 0.5초 더 녹화해서 마지막 장면이 잘리지 않게 함
        time.sleep(0.5)
        self.h["stop_rec"]()
        self.recording = False
        st("저장 중")
