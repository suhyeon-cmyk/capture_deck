"""
CaptureDeck - USB3 캡쳐 프로그램 (OBS 대체용, Windows)
- 실행하면 저장된/자동 감지된 장치로 바로 미리보기 + 오디오 레벨 표시
- 영상 + 오디오 캡쳐, MP4 녹화
- 장치 신호가 끊기면 자동 재연결
- 마지막 설정을 자동 저장 (매번 세팅할 필요 없음)

설치:  pip install opencv-python sounddevice numpy pillow pygrabber imageio-ffmpeg
실행:  python usb3_capture.py
exe:   build.bat 실행 → dist/CaptureDeck.exe
"""
import configparser
import json
import os
import re
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
import sounddevice as sd
from PIL import Image, ImageTk

try:
    from pygrabber.dshow_graph import FilterGraph
except Exception:
    FilterGraph = None
import imageio_ffmpeg

APP_NAME = "CaptureDeck"
# 설정 파일: exe(또는 스크립트)와 같은 폴더의 settings.ini
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG_PATH = APP_DIR / "settings.ini"
OLD_CONFIG_PATH = Path.home() / ".usb3_capture_config.json"  # 예전 버전 설정 (있으면 한 번 가져옴)


def resource_path(name):
    """exe(PyInstaller)로 묶였을 때는 압축 해제 폴더, 아니면 스크립트 폴더 기준 경로"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


KEYWORDS = ["usb3", "usb 3", "video"]  # 비디오 장치 자동 감지용 키워드
AUDIO_KEYWORDS = ["usb3", "usb 3", "plus video", "video", "디지털 오디오", "digital audio"]
RESOLUTIONS = ["1920x1080", "1280x720", "3840x2160", "1024x768", "640x480"]
FPS_LIST = ["60", "30", "25", "24"]
PREVIEW_W, PREVIEW_H = 960, 540


# ---------------------------------------------------------------- 설정
DEFAULTS = {
    "video": "", "audio": "", "resolution": "1920x1080", "fps": "60",
    "out_dir": str(Path.home() / "Videos" / "Captures"), "monitor": False,
    "sched_mode": "range", "sched_start": "09:00", "sched_end": "10:00", "sched_minutes": "60",
    "trim_after": True, "geometry": "", "tab": "0",
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(CONFIG_PATH, encoding="utf-8")
            for k, v in cp["settings"].items():
                if k in DEFAULTS:
                    cfg[k] = v.lower() in ("1", "true", "yes") if isinstance(DEFAULTS[k], bool) else v
        except Exception:
            pass
    else:
        try:
            cfg.update(json.loads(OLD_CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    cp = configparser.ConfigParser(interpolation=None)
    cp["settings"] = {k: str(cfg.get(k, v)) for k, v in DEFAULTS.items()}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(f"; {APP_NAME} 설정 - 프로그램이 자동으로 저장해요\n")
            cp.write(f)
    except OSError:
        pass  # 쓰기 권한 없는 폴더(예: Program Files)면 저장하지 않음


# ---------------------------------------------------------------- 장치 목록
def list_video_devices():
    """DirectShow 비디오 장치 이름 목록 (인덱스 = OpenCV 인덱스)"""
    if FilterGraph is None:
        return []
    try:
        return list(FilterGraph().get_input_devices())
    except Exception:
        return []


_FORMATS = {}


def video_formats(name):
    """캡처 장치가 지원하는 {해상도: (최소 fps, 최대 fps)} (MJPG 우선, 큰 해상도 순). 알아낼 수 없으면 빈 dict"""
    if name in _FORMATS:
        return _FORMATS[name]
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-f", "dshow",
                            "-list_options", "true", "-i", f"video={name}"],
                           capture_output=True, creationflags=flags, timeout=15)
        text = p.stderr.decode("utf-8", errors="replace")
    except Exception:
        return {}
    found = {"mjpeg": {}, "other": {}}
    for line in text.splitlines():
        m = re.search(r"min s=\d+x\d+ fps=([\d.]+) max s=(\d+)x(\d+) fps=([\d.]+)", line)
        if not m:
            continue
        lo, w, h, hi = float(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
        d = found["mjpeg" if "vcodec=mjpeg" in line else "other"]
        old = d.get((w, h), (lo, hi))
        d[(w, h)] = (min(old[0], lo), max(old[1], hi))
    d = found["mjpeg"] or found["other"]  # 녹화는 MJPG로 요청하므로 MJPG 목록 우선
    out = {f"{w}x{h}": (round(lo), round(hi))
           for (w, h), (lo, hi) in sorted(d.items(), key=lambda x: -x[0][0] * x[0][1])}
    if out:
        _FORMATS[name] = out
    return out


def list_audio_devices():
    """(sounddevice 인덱스, 이름) 목록 - MME 호스트 API 우선"""
    try:
        apis = sd.query_hostapis()
        mme = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] <= 0:
                continue
            if mme is not None and d["hostapi"] != mme:
                continue
            out.append((i, d["name"]))
        return out
    except Exception:
        return []


def pick_by_keyword(names, saved, keywords=KEYWORDS):
    if saved and saved in names:
        return saved
    for n in names:
        if any(k in n.lower() for k in keywords):
            return n
    return names[0] if names else ""


def schedule_window(start_hm, end_hm, now):
    """'HH:MM' 두 개 → 지금 진행 중이거나 다음에 올 (시작, 종료) datetime. 자정 넘김 지원"""
    sh, sm = map(int, start_hm.split(":"))
    eh, em = map(int, end_hm.split(":"))
    s = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    e = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if e <= s:
        e += timedelta(days=1)
    day = timedelta(days=1)
    if s - day <= now < e - day:  # 어제 시작해서 자정을 넘긴 구간 안
        return s - day, e - day
    if now >= e:
        s, e = s + day, e + day
    return s, e


def keep_awake(on):
    """예약/녹화 중에는 PC 절전 모드 진입을 막음 (화면은 꺼질 수 있음)"""
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


_ENCODER = None


def video_encoder_args():
    """NVIDIA GPU 인코더(NVENC)가 되면 사용, 아니면 CPU x264 ultrafast"""
    global _ENCODER
    if _ENCODER is None:
        nvenc = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            p = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error",
                                "-f", "lavfi", "-i", "color=s=256x256", "-frames:v", "1"]
                               + nvenc + ["-f", "null", "-"],
                               capture_output=True, creationflags=flags, timeout=15)
            ok = p.returncode == 0
        except Exception:
            ok = False
        _ENCODER = nvenc if ok else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20"]
    return _ENCODER + ["-pix_fmt", "yuv420p"]


# ---------------------------------------------------------------- 영상 워커
class VideoWorker(threading.Thread):
    def __init__(self, index, width, height, fps):
        super().__init__(daemon=True)
        self.index, self.w, self.h, self.fps = index, width, height, fps
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.frame = None
        self.status = "연결 중..."
        self.actual_size = (0, 0)
        # 녹화 (별도 스레드 + ffmpeg 파이프로 인코딩 → 캡처 스레드는 읽기만 함)
        self.rec_thread = None
        self.rec_stop = threading.Event()
        self.rec_stop_at = 0.0
        self.rec_error = ""

    def _open(self):
        cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return None
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.h)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
        except cv2.error:  # 장치가 아직 다른 곳에서 해제되는 중 → 잠시 뒤 다시 시도
            cap.release()
            return None
        return cap

    def run(self):
        cap = None
        last_ok = time.time()
        while not self.stop_flag.is_set():
            if cap is None:
                cap = self._open()
                if cap is None:
                    self.status = "장치를 찾는 중... (USB 연결 확인)"
                    time.sleep(1)
                    continue
                last_ok = time.time()
            ok, frame = cap.read()
            if ok and frame is not None:
                last_ok = time.time()
                self.status = "영상 정상"
                self.actual_size = (frame.shape[1], frame.shape[0])
                with self.lock:
                    self.frame = frame
            elif time.time() - last_ok > 2:
                cap.release()
                cap = None
                self.status = "신호 없음 - 재연결 중..."
            else:
                time.sleep(0.01)
        if cap is not None:
            cap.release()

    def _rec_loop(self, path, t0):
        with self.lock:
            frame = self.frame
        if frame is None:
            self.rec_error = "영상 프레임 없음"
            return
        h, w = frame.shape[:2]
        w, h = w - w % 2, h - h % 2  # yuv420p는 짝수 크기 필요
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}",
               "-framerate", str(self.fps), "-i", "-"] + video_encoder_args() + [
               # cv2 BGR→I420 변환은 BT.601(제한 범위) → 플레이어가 BT.709로 잘못 해석하지 않게 표시
               "-colorspace", "smpte170m", "-color_range", "tv",
               "-color_primaries", "bt709", "-color_trc", "bt709",
               "-g", str(self.fps), path]  # 1초마다 키프레임 → 편집 창에서 빠르게 이동
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        # 실제 시간 기준으로 고정 fps에 맞춰 최신 프레임을 기록 (영상 길이 = 실제 녹화 시간)
        # 기준 시각 = start_rec 호출 시점 (오디오 녹음 시작과 같음). ffmpeg 시작 지연만큼은 첫 프레임으로 채움
        step = 1.0 / self.fps
        next_t = t0
        try:
            # 중지 신호를 받아도 중지 시각까지 밀린 프레임은 마저 기록 (영상 길이 = 오디오 길이)
            while not (self.rec_stop.is_set() and next_t >= self.rec_stop_at):
                delay = next_t - time.time()
                if delay > 0:
                    time.sleep(delay)
                with self.lock:
                    frame = self.frame
                if frame.shape[1] != w or frame.shape[0] != h:
                    frame = cv2.resize(frame, (w, h))
                p.stdin.write(cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).tobytes())
                next_t += step
            p.stdin.close()
        except OSError:
            self.rec_error = p.stderr.read().decode(errors="replace").strip()[:200] or "인코더 오류"
        p.wait()

    def get_frame(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def start_rec(self, path):
        self.rec_stop.clear()
        self.rec_error = ""
        self.rec_thread = threading.Thread(target=self._rec_loop, args=(path, time.time()), daemon=True)
        self.rec_thread.start()

    def stop_rec(self):
        """녹화 중지 신호만 보냄 (인코더 마무리는 wait_rec에서 기다림)"""
        self.rec_stop_at = time.time()
        self.rec_stop.set()

    def wait_rec(self):
        if self.rec_thread is not None:
            self.rec_thread.join()
            self.rec_thread = None

    def stop(self):
        self.stop_rec()
        self.stop_flag.set()


# ---------------------------------------------------------------- 오디오 워커
class AudioWorker:
    def __init__(self, device_index):
        self.device = device_index
        self.stream = None
        self.out = None
        self.level = 0.0
        self.wav = None
        self.lock = threading.Lock()
        self.monitor = False
        self.buf = bytearray()
        self.sr = 44100
        self.ch = 2
        self.error = ""

    @property
    def running(self):
        try:
            return self.stream is not None and self.stream.active
        except Exception:
            return False

    def start(self):
        try:
            info = sd.query_devices(self.device)
            self.sr = int(info["default_samplerate"])
            self.ch = max(1, min(2, int(info["max_input_channels"])))
            self.stream = sd.InputStream(
                device=self.device, samplerate=self.sr, channels=self.ch,
                dtype="int16", callback=self._cb)
            self.stream.start()
            self.error = ""
            return True
        except Exception as e:
            self.error = str(e)
            self._close_stream()
            return False

    def _cb(self, indata, frames, t, status):
        peak = float(np.abs(indata).max()) / 32768.0
        self.level = max(self.level, peak)
        data = indata.tobytes()
        with self.lock:
            if self.wav is not None:
                self.wav.writeframes(data)
            if self.monitor:
                self.buf += data
                limit = self.sr * self.ch * 2 // 2  # 최대 0.5초 버퍼
                if len(self.buf) > limit:
                    del self.buf[:len(self.buf) - limit]

    def _out_cb(self, outdata, frames, t, status):
        need = frames * self.ch * 2
        with self.lock:
            chunk = bytes(self.buf[:need])
            del self.buf[:need]
        if len(chunk) < need:
            chunk += b"\x00" * (need - len(chunk))
        outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, self.ch)

    def set_monitor(self, on):
        self.monitor = on
        if on and self.out is None and self.running:
            try:
                self.out = sd.OutputStream(
                    samplerate=self.sr, channels=self.ch, dtype="int16",
                    callback=self._out_cb)
                self.out.start()
            except Exception as e:
                self.error = f"모니터링 실패: {e}"
                self.out = None
        elif not on and self.out is not None:
            try:
                self.out.stop()
                self.out.close()
            except Exception:
                pass
            self.out = None
            with self.lock:
                self.buf.clear()

    def start_rec(self, path):
        w = wave.open(path, "wb")
        w.setnchannels(self.ch)
        w.setsampwidth(2)
        w.setframerate(self.sr)
        with self.lock:
            self.wav = w

    def stop_rec(self):
        with self.lock:
            w, self.wav = self.wav, None
        if w is not None:
            w.close()

    def _close_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def stop(self):
        self.stop_rec()
        self.set_monitor(False)
        self._close_stream()


# ---------------------------------------------------------------- 영상 자르기
def fmt_ms(ms):
    ms = max(0, int(round(ms)))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def parse_ms(text):
    """'HH:MM:SS.mmm', 'MM:SS.mmm', 'SS.mmm' → 밀리초"""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(text)
    sec = 0.0
    for p in parts:
        sec = sec * 60 + float(p)
    if sec < 0:
        raise ValueError(text)
    return int(round(sec * 1000))


class TrimPanel(ttk.Frame):
    """'영상 자르기' 탭: 영상의 앞뒤를 밀리초 단위로 잘라 새 파일로 저장 (원본은 그대로 둠)"""
    VIEW_W, VIEW_H = PREVIEW_W, PREVIEW_H

    def __init__(self, master, get_dir):
        super().__init__(master)
        self.get_dir = get_dir  # 파일 열기 기본 폴더
        self.path = None
        self.cap = None
        self.fps, self.dur, self.frame_ms = 60, 0, 1000 / 60
        self.pos = 0
        self.start_ms = self.end_ms = 0
        self.seek_job = None
        self.proc = None
        self.photo = None
        # 재생
        self.playing = False
        self.play_stop = threading.Event()
        self.play_threads = []
        self.play_frame = None      # (시각 ms, RGB 이미지) - 재생 스레드가 넣고 화면 갱신이 꺼내 씀
        self.play_t0 = self.play_start = 0.0
        self._build()
        self._update_range()

    def open_dialog(self):
        path = filedialog.askopenfilename(
            parent=self, title="자를 영상 선택", initialdir=self.get_dir(),
            filetypes=[("영상 파일", "*.mp4 *.mkv *.mov *.avi"), ("모든 파일", "*.*")])
        if path:
            self.load(path)

    def load(self, path):
        if self.proc:
            messagebox.showinfo("영상 자르기", "저장이 끝난 뒤에 다른 영상을 열 수 있어요.", parent=self)
            return False
        self.pause()
        cap = cv2.VideoCapture(path)
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if not cap.isOpened() or n <= 0:
            cap.release()
            messagebox.showwarning("영상 자르기", f"영상을 열 수 없어요:\n{path}", parent=self)
            return False
        if self.cap:
            self.cap.release()
        self.cap, self.path = cap, path
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 60
        self.dur = int(n * 1000 / self.fps)
        self.frame_ms = 1000 / self.fps
        self.lbl_file.config(text=os.path.basename(path))  # 전체 경로는 창이 넓어져서 이름만
        self.scale.config(to=max(1, self.dur))
        self.start_ms, self.end_ms = 0, self.dur
        self._update_range()
        self.prog.config(value=0)
        self.lbl_status.config(text="원본은 그대로 두고 '_trim' 파일로 새로 저장해요")
        self.goto(0)
        return True

    def _build(self):
        t = ttk.Frame(self)
        t.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Button(t, text="영상 열기...", command=self.open_dialog).pack(side="left")
        self.lbl_file = ttk.Label(t, text="열린 영상 없음 - '영상 열기'로 파일을 고르거나, 녹화가 끝나면 자동으로 열려요")
        self.lbl_file.pack(side="left", padx=8)

        self.view = tk.Label(self, bg="black")
        self.view.pack(padx=8, pady=8)
        blank = ImageTk.PhotoImage(Image.new("RGB", (self.VIEW_W, self.VIEW_H), "black"))
        self.view.config(image=blank)
        self.view.image = blank

        self.var_pos = tk.DoubleVar(value=0)
        self.scale = ttk.Scale(self, from_=0, to=max(1, self.dur), variable=self.var_pos,
                               command=self._on_scale)
        self.scale.pack(fill="x", padx=8)
        # 막대 빈 곳을 클릭하면 그 위치로 바로 이동 (기본 동작은 조금씩만 움직임). 클릭한 채로 끌기도 가능
        self.scale.bind("<Button-1>", self._on_scale_click)

        n = ttk.Frame(self)
        n.pack(pady=4)
        # 기본 글꼴은 ⏸를 네모 테두리 안에 그려서, 테두리 없이 그리는 Segoe UI Symbol 사용
        ttk.Style().configure("Play.TButton", font=("Segoe UI Symbol", 9))
        self.btn_play = ttk.Button(n, text="▶ 재생", width=11, style="Play.TButton", command=self.toggle_play)
        self.btn_play.pack(side="left", padx=(2, 10))
        for text, d in (("◀◀ 1초", -1000), ("◀ 1프레임", "-f"), ("1프레임 ▶", "+f"), ("1초 ▶▶", 1000)):
            ttk.Button(n, text=text, width=10, command=lambda d=d: self.step(d)).pack(side="left", padx=2)
        self.lbl_pos = ttk.Label(n, text="", font=("Consolas", 12))
        self.lbl_pos.pack(side="left", padx=12)
        self.var_goto = tk.StringVar()
        e = ttk.Entry(n, textvariable=self.var_goto, width=13, justify="center")
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self._goto_text())
        ttk.Button(n, text="이동", command=self._goto_text).pack(side="left", padx=2)

        g = ttk.Frame(self)
        g.pack(padx=8, pady=4)
        self.var_start = tk.StringVar()
        self.var_end = tk.StringVar()
        for row, (label, var, which) in enumerate((("시작", self.var_start, "start"),
                                                    ("끝", self.var_end, "end"))):
            ttk.Label(g, text=label).grid(row=row, column=0, sticky="w")
            ent = ttk.Entry(g, textvariable=var, width=14, justify="center", font=("Consolas", 11))
            ent.grid(row=row, column=1, padx=4, pady=2)
            ent.bind("<Return>", lambda ev, w=which: self._apply_text(w))
            ent.bind("<FocusOut>", lambda ev, w=which: self._apply_text(w))
            ttk.Button(g, text="현재 위치로 지정", command=lambda w=which: self.set_mark(w)
                       ).grid(row=row, column=2, padx=2)
            ttk.Button(g, text="이 위치 보기", command=lambda w=which: self.goto(
                self.start_ms if w == "start" else max(0, self.end_ms - self.frame_ms))
                       ).grid(row=row, column=3, padx=2)
        ttk.Label(g, text="형식: 시:분:초.밀리초 (예: 00:01:23.456)", foreground="gray"
                  ).grid(row=0, column=4, padx=12, sticky="w")
        self.lbl_len = ttk.Label(g, text="")
        self.lbl_len.grid(row=1, column=4, padx=12, sticky="w")

        b = ttk.Frame(self)
        b.pack(fill="x", padx=8, pady=(4, 8))
        self.btn_save = ttk.Button(b, text="잘라서 저장", command=self.save)
        self.btn_save.pack(side="left")
        self.prog = ttk.Progressbar(b, maximum=100, length=200)
        self.prog.pack(side="left", padx=8)
        self.lbl_status = ttk.Label(b, text="원본은 그대로 두고 '_trim' 파일로 새로 저장해요")
        self.lbl_status.pack(side="left")

    # ---- 이동
    def _show(self, ms):
        if not self.cap:
            return
        self.cap.set(cv2.CAP_PROP_POS_MSEC, ms)
        ok, frame = self.cap.read()
        if not ok:
            return
        h, w = frame.shape[:2]
        s = min(self.VIEW_W / w, self.VIEW_H / h)
        self._display(cv2.cvtColor(cv2.resize(frame, (int(w * s), int(h * s))), cv2.COLOR_BGR2RGB))

    def _display(self, rgb):
        img = Image.fromarray(rgb)
        if self.photo is not None and (self.photo.width(), self.photo.height()) == img.size:
            self.photo.paste(img)  # 기존 이미지에 덮어쓰기가 새로 만드는 것보다 약 2배 빠름 (재생 시 중요)
        else:
            self.photo = ImageTk.PhotoImage(img)
            self.view.config(image=self.photo)

    def goto(self, ms):
        if not self.cap:
            return
        self.pause()
        self.pos = int(min(max(0, ms), max(0, self.dur - self.frame_ms)))
        self.var_pos.set(self.pos)
        self.lbl_pos.config(text=f"{fmt_ms(self.pos)} / {fmt_ms(self.dur)}")
        self._show(self.pos)

    def _on_scale_click(self, ev):
        if not self.cap or "slider" in self.scale.identify(ev.x, ev.y):
            return  # 손잡이를 잡은 경우는 기본 끌기 동작
        value = self.scale.get(ev.x, ev.y)
        self.var_pos.set(value)  # 손잡이를 클릭 위치로 옮김 → 이어서 기본 동작이 끌기로 처리
        self._on_scale(value)

    def _on_scale(self, value):
        if not self.cap:
            self.var_pos.set(0)
            return
        self.pause()
        self.pos = int(float(value))
        self.lbl_pos.config(text=f"{fmt_ms(self.pos)} / {fmt_ms(self.dur)}")
        # 드래그 중에는 멈췄을 때만 프레임을 디코딩 (끊김 방지)
        if self.seek_job:
            self.after_cancel(self.seek_job)
        self.seek_job = self.after(60, lambda: self._show(self.pos))

    def step(self, d):
        if d == "-f":
            d = -self.frame_ms
        elif d == "+f":
            d = self.frame_ms
        self.goto(self.pos + d)

    # ---- 재생 / 일시정지 (영상과 소리를 같은 시계에 맞춰 재생)
    def toggle_play(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def _clock(self):
        return self.play_start + (time.perf_counter() - self.play_t0) * 1000

    def play(self):
        if not self.cap or self.playing:
            return
        start = self.pos if self.pos < self.dur - self.frame_ms * 2 else 0  # 끝에서 누르면 처음부터
        self.playing = True
        self.btn_play.config(text="⏸ 일시정지")
        self.play_stop.clear()
        self.play_frame = None
        self.play_req = self.play_start = start  # 요청 위치 / 영상이 찾은 실제 시작 위치
        self.play_t0 = float("inf")  # 영상이 첫 프레임을 준비하면 정해짐
        self.play_ready = threading.Event()
        self.play_threads = [threading.Thread(target=t, daemon=True)
                             for t in (self._play_video, self._play_audio)]
        for t in self.play_threads:
            t.start()
        self._play_tick()

    def pause(self):
        if not self.playing:
            return
        self.playing = False
        self.play_stop.set()
        for t in self.play_threads:
            t.join(timeout=1)
        self.play_threads = []
        self.btn_play.config(text="▶ 재생")
        self.goto(self.pos)  # 멈춘 위치의 프레임을 정확히 다시 표시

    def _play_video(self):
        cap = cv2.VideoCapture(self.path)
        cap.set(cv2.CAP_PROP_POS_MSEC, self.play_start)  # 위치 찾기에 시간이 걸리므로 시계보다 먼저
        first = True
        shown_at = 0.0
        try:
            while not self.play_stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    break
                ts = cap.get(cv2.CAP_PROP_POS_MSEC)
                if first:  # 첫 프레임이 준비된 뒤 시계 출발 (소리도 이때 맞춰 나옴)
                    first = False
                    self.play_start = ts
                    self.play_t0 = time.perf_counter() + 0.12
                    self.play_ready.set()
                # 늦은 프레임은 건너뜀 (소리와 맞추기). 단 0.1초 넘게 화면이 멈춰 있지는 않게
                elif ts < self._clock() - self.frame_ms * 2 and time.perf_counter() - shown_at < 0.1:
                    continue
                shown_at = time.perf_counter()
                h, w = frame.shape[:2]
                s = min(self.VIEW_W / w, self.VIEW_H / h)
                rgb = cv2.cvtColor(cv2.resize(frame, (int(w * s), int(h * s))), cv2.COLOR_BGR2RGB)
                while not self.play_stop.is_set() and self._clock() < ts:
                    time.sleep(0.002)
                self.play_frame = (ts, rgb)
        finally:
            cap.release()

    def _play_audio(self):
        sr, ch = 48000, 2
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error",
                              "-ss", f"{self.play_req / 1000:.3f}", "-i", self.path, "-vn",
                              "-f", "s16le", "-ac", str(ch), "-ar", str(sr), "-"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
        stream = None
        try:
            stream = sd.RawOutputStream(samplerate=sr, channels=ch, dtype="int16", latency="low")
            stream.start()
            # 영상 첫 프레임 준비를 기다린 뒤, 스피커에서 소리가 나오는 시각이 영상과 같도록 출력 지연만큼 먼저 보냄
            while not self.play_ready.wait(0.05):
                if self.play_stop.is_set() or not self.play_threads[0].is_alive():
                    return
            # 영상의 실제 시작 위치가 요청보다 조금 뒤면 그만큼 소리도 건너뜀
            skip = int(max(0.0, self.play_start - self.play_req) / 1000 * sr) * ch * 2
            while skip > 0:
                data = p.stdout.read(min(skip, 65536))
                if not data:
                    break
                skip -= len(data)
            delay = self.play_t0 - stream.latency - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            chunk = sr * ch * 2 // 50  # 20ms
            while not self.play_stop.is_set():
                data = p.stdout.read(chunk)
                if not data:
                    break
                stream.write(data)
        except Exception:
            pass  # 소리 장치 문제나 오디오 없는 영상이면 영상만 재생
        finally:
            if stream is not None:
                stream.abort()
                stream.close()
            p.kill()

    def _play_tick(self):
        if not self.playing:
            return
        f = self.play_frame
        if f is not None:
            self.play_frame = None
            ts, rgb = f
            self._display(rgb)
            self.pos = int(ts)
            self.var_pos.set(self.pos)
            self.lbl_pos.config(text=f"{fmt_ms(self.pos)} / {fmt_ms(self.dur)}")
        if not self.play_threads[0].is_alive() and self.play_frame is None:
            self.pause()  # 영상 끝
            return
        self.after(8, self._play_tick)

    def _goto_text(self):
        try:
            self.goto(parse_ms(self.var_goto.get()))
        except ValueError:
            messagebox.showwarning("이동", "시간 형식이 올바르지 않아요. (예: 00:01:23.456)", parent=self)

    # ---- 구간
    def set_mark(self, which):
        if not self.cap:
            return
        if which == "start":
            self.start_ms = self.pos
        else:
            self.end_ms = int(min(self.dur, self.pos + self.frame_ms))  # 지금 보이는 프레임까지 포함
        self._update_range()

    def _apply_text(self, which):
        var = self.var_start if which == "start" else self.var_end
        try:
            ms = min(parse_ms(var.get()), self.dur)
        except ValueError:
            self._update_range()  # 잘못 입력하면 이전 값으로 되돌림
            return
        if which == "start":
            self.start_ms = ms
        else:
            self.end_ms = ms
        self._update_range()

    def _update_range(self):
        self.var_start.set(fmt_ms(self.start_ms))
        self.var_end.set(fmt_ms(self.end_ms))
        length = self.end_ms - self.start_ms
        if length > 0:
            self.lbl_len.config(text=f"잘라낸 길이: {fmt_ms(length)}", foreground="")
        else:
            self.lbl_len.config(text="끝 시간이 시작 시간보다 뒤여야 해요", foreground="red")

    # ---- 저장
    def save(self):
        if self.proc:
            return
        if not self.cap:
            messagebox.showinfo("자르기", "먼저 '영상 열기'로 자를 영상을 골라 주세요.", parent=self)
            return
        length = self.end_ms - self.start_ms
        if length <= 0:
            messagebox.showwarning("자르기", "끝 시간이 시작 시간보다 뒤여야 해요.", parent=self)
            return
        base, ext = os.path.splitext(self.path)
        out = base + "_trim" + ext
        i = 2
        while os.path.exists(out):
            out = f"{base}_trim{i}{ext}"
            i += 1
        # 밀리초 단위로 정확히 자르기 위해 재인코딩 (복사 방식은 키프레임 단위로만 잘림)
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-progress", "pipe:1",
               "-ss", f"{self.start_ms / 1000:.3f}", "-i", self.path, "-t", f"{length / 1000:.3f}",
               ] + video_encoder_args() + ["-g", str(round(self.fps)),
                                           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, creationflags=flags)
        self.btn_save.config(state="disabled")
        self.lbl_status.config(text="저장 중...")
        threading.Thread(target=self._wait_save, args=(self.proc, length, out), daemon=True).start()

    def _wait_save(self, p, length, out):
        def ui(fn, *args):  # 창이 이미 닫혔으면 무시
            try:
                self.after(0, fn, *args)
            except (RuntimeError, tk.TclError):
                pass
        for line in p.stdout:
            if line.startswith("out_time_us=") and line.strip()[12:].isdigit():
                pct = min(100, int(line.strip()[12:]) / 1000 / length * 100)
                ui(self.prog.config, {"value": pct})
        err = p.stderr.read()
        ok = p.wait() == 0 and os.path.exists(out)
        ui(self._save_done, ok, out, err)

    def _save_done(self, ok, out, err):
        self.proc = None
        self.btn_save.config(state="normal")
        self.prog.config(value=100 if ok else 0)
        self.lbl_status.config(text=f"저장 완료: {os.path.basename(out)}" if ok
                               else f"저장 실패: {err.strip()[:80]}")

    def close(self):
        """프로그램 종료 시 호출. 저장 중에 사용자가 취소하면 False"""
        self.pause()
        if self.proc and not messagebox.askyesno("종료", "영상 자르기 저장 중이에요. 중단하고 종료할까요?", parent=self):
            return False
        if self.proc:
            self.proc.kill()
        if self.cap:
            self.cap.release()
        return True


# ---------------------------------------------------------------- 메인 앱
class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        try:
            root.iconbitmap(resource_path("capturedeck.ico"))
        except tk.TclError:
            pass
        self.cfg = load_config()
        self.video = None
        self.audio = None
        self.recording = False
        self.rec_start = 0.0
        self.rec_base = None
        self.busy = False
        self.last_audio_try = 0.0
        self.photo = None
        self.sched = None          # 예약된 (시작, 종료) datetime
        self.sched_rec = False     # 지금 녹화가 예약으로 시작된 것인지

        self.video_names = []
        self.audio_devs = []
        self._build_ui()
        self._restore_window()
        self.refresh_devices(auto=True)
        self.tick()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- UI
    def _build_ui(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True)
        r = ttk.Frame(self.nb)
        self.nb.add(r, text="  영상 캡쳐  ")
        self.trim = TrimPanel(self.nb, get_dir=lambda: self.var_dir.get())
        self.nb.add(self.trim, text="  영상 자르기  ")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.trim.pause())
        self.root.bind("<space>", self._on_space)

        self.preview = tk.Label(r, bg="black", width=PREVIEW_W, height=PREVIEW_H)
        self.preview.config(width=PREVIEW_W, height=PREVIEW_H)
        self.preview.pack(padx=8, pady=8)
        # 라벨 크기를 픽셀로 고정
        blank = ImageTk.PhotoImage(Image.new("RGB", (PREVIEW_W, PREVIEW_H), "black"))
        self.preview.config(image=blank)
        self.preview.image = blank

        f = ttk.Frame(r)
        f.pack(fill="x", padx=8)
        ttk.Label(f, text="비디오 장치").grid(row=0, column=0, sticky="w")
        self.cb_video = ttk.Combobox(f, state="readonly", width=38)
        self.cb_video.grid(row=0, column=1, padx=4, pady=2)
        ttk.Label(f, text="오디오 장치").grid(row=1, column=0, sticky="w")
        self.cb_audio = ttk.Combobox(f, state="readonly", width=38)
        self.cb_audio.grid(row=1, column=1, padx=4, pady=2)

        ttk.Label(f, text="해상도").grid(row=0, column=2, padx=(12, 0), sticky="w")
        self.cb_res = ttk.Combobox(f, values=RESOLUTIONS, width=11, state="readonly")
        self.cb_res.set(self.cfg["resolution"])
        self.cb_res.grid(row=0, column=3, padx=4)
        ttk.Label(f, text="FPS").grid(row=1, column=2, padx=(12, 0), sticky="w")
        self.cb_fps = ttk.Combobox(f, values=FPS_LIST, width=11, state="readonly")
        self.cb_fps.set(self.cfg["fps"])
        self.cb_fps.grid(row=1, column=3, padx=4)

        ttk.Button(f, text="장치 새로고침", command=lambda: self.refresh_devices(auto=False)
                   ).grid(row=0, column=4, padx=8)
        self.var_mon = tk.BooleanVar(value=self.cfg.get("monitor", False))
        ttk.Checkbutton(f, text="소리 듣기(모니터링)", variable=self.var_mon,
                        command=self.on_monitor).grid(row=1, column=4, padx=8)

        for cb in (self.cb_video, self.cb_audio, self.cb_res, self.cb_fps):
            cb.bind("<<ComboboxSelected>>", lambda e: self.on_settings_changed())

        a = ttk.Frame(r)
        a.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(a, text="오디오 레벨").pack(side="left")
        self.level_bar = ttk.Progressbar(a, maximum=100, length=400)
        self.level_bar.pack(side="left", padx=8)

        o = ttk.Frame(r)
        o.pack(fill="x", padx=8, pady=6)
        ttk.Label(o, text="저장 폴더").pack(side="left")
        self.var_dir = tk.StringVar(value=self.cfg["out_dir"])
        ttk.Entry(o, textvariable=self.var_dir, width=60).pack(side="left", padx=6)
        ttk.Button(o, text="폴더 선택", command=self.choose_dir).pack(side="left")
        ttk.Button(o, text="폴더 열기", command=self.open_dir).pack(side="left", padx=4)

        s = ttk.Frame(r)
        s.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(s, text="예약 녹화").grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 8))
        self.var_s_mode = tk.StringVar(value=self.cfg["sched_mode"])
        # 방식 1: 몇 시 몇 분부터 몇 시 몇 분까지
        r1 = ttk.Frame(s)
        r1.grid(row=0, column=1, sticky="w")
        self.rb_range = ttk.Radiobutton(r1, text="시간 지정", value="range", variable=self.var_s_mode)
        self.rb_range.pack(side="left")
        self.var_s_start = tk.StringVar(value=self.cfg["sched_start"])
        self.var_s_end = tk.StringVar(value=self.cfg["sched_end"])
        self.ent_s_start = ttk.Entry(r1, textvariable=self.var_s_start, width=6, justify="center")
        self.ent_s_start.pack(side="left", padx=(6, 2))
        ttk.Label(r1, text="부터").pack(side="left")
        self.ent_s_end = ttk.Entry(r1, textvariable=self.var_s_end, width=6, justify="center")
        self.ent_s_end.pack(side="left", padx=(6, 2))
        ttk.Label(r1, text="까지  (24시간, 예: 14:30)").pack(side="left")
        # 방식 2: 지금부터 몇 분간
        r2 = ttk.Frame(s)
        r2.grid(row=1, column=1, sticky="w")
        self.rb_dur = ttk.Radiobutton(r2, text="지금부터", value="duration", variable=self.var_s_mode)
        self.rb_dur.pack(side="left")
        self.var_s_min = tk.StringVar(value=self.cfg["sched_minutes"])
        self.ent_s_min = ttk.Entry(r2, textvariable=self.var_s_min, width=6, justify="center")
        self.ent_s_min.pack(side="left", padx=(6, 2))
        ttk.Label(r2, text="분간").pack(side="left")
        self.btn_sched = ttk.Button(s, text="예약", command=self.toggle_schedule)
        self.btn_sched.grid(row=0, column=2, padx=12, sticky="ew")
        self.btn_sched_end = ttk.Button(s, text="종료 시간 변경", command=self.change_sched_end, state="disabled")
        self.btn_sched_end.grid(row=1, column=2, padx=12, sticky="ew")
        for e in (self.ent_s_end, self.ent_s_min):  # 예약 중 Enter로도 종료 시간 변경
            e.bind("<Return>", lambda ev: self.change_sched_end() if self.sched else None)
        self.lbl_sched = ttk.Label(s, text="예약 없음")
        self.lbl_sched.grid(row=0, column=3, rowspan=2, sticky="w")
        self.sched_inputs = (self.rb_range, self.rb_dur, self.ent_s_start, self.ent_s_end, self.ent_s_min)

        b = ttk.Frame(r)
        b.pack(fill="x", padx=8, pady=(0, 8))
        self.btn_rec = ttk.Button(b, text="● 녹화 시작", command=self.toggle_rec)
        self.btn_rec.pack(side="left")
        self.lbl_time = ttk.Label(b, text="00:00:00", font=("Consolas", 14))
        self.lbl_time.pack(side="left", padx=12)
        self.lbl_status = ttk.Label(b, text="")
        self.lbl_status.pack(side="left", padx=8)
        self.var_trim_after = tk.BooleanVar(value=self.cfg.get("trim_after", True))
        ttk.Checkbutton(b, text="녹화 종료 후 바로 편집", variable=self.var_trim_after,
                        command=self.on_trim_after).pack(side="right", padx=8)

    # ---- 장치
    def refresh_devices(self, auto):
        if not auto:
            _FORMATS.clear()  # '장치 새로고침'이면 지원 해상도도 다시 확인
        self.video_names = list_video_devices()
        self.audio_devs = list_audio_devices()
        self.cb_video["values"] = self.video_names
        self.cb_audio["values"] = [n for _, n in self.audio_devs]
        self.cb_video.set(pick_by_keyword(self.video_names, self.cfg["video"] if auto else self.cb_video.get()))
        self.cb_audio.set(pick_by_keyword([n for _, n in self.audio_devs],
                                          self.cfg["audio"] if auto else self.cb_audio.get(),
                                          AUDIO_KEYWORDS))
        self.restart_devices()

    def on_settings_changed(self):
        if self.recording:
            messagebox.showinfo("알림", "녹화 중에는 설정을 바꿀 수 없어요.")
            self.cb_video.set(self.cfg["video"])
            self.cb_audio.set(self.cfg["audio"])
            self.cb_res.set(self.cfg["resolution"])
            self.cb_fps.set(self.cfg["fps"])
            return
        self.restart_devices()

    def restart_devices(self):
        if self.video:
            self.video.stop()
            self.video.join(timeout=3)  # 장치를 완전히 놓은 뒤에 새로 열기
            self.video = None
        if self.audio:
            self.audio.stop()
            self.audio = None
        self.update_formats()
        self.cfg.update(video=self.cb_video.get(), audio=self.cb_audio.get(),
                        resolution=self.cb_res.get(), fps=self.cb_fps.get(),
                        out_dir=self.var_dir.get(), monitor=self.var_mon.get())
        save_config(self.cfg)

        vname = self.cb_video.get()
        if vname in self.video_names:
            w, h = map(int, self.cb_res.get().split("x"))
            self.video = VideoWorker(self.video_names.index(vname), w, h, int(self.cb_fps.get()))
            self.video.start()
        self.start_audio()

    def update_formats(self):
        """해상도/FPS 목록을 선택한 캡처 장치가 실제 지원하는 값으로 맞춤"""
        fmts = video_formats(self.cb_video.get()) if self.cb_video.get() else {}
        if not fmts:  # 지원 목록을 알 수 없으면 기본 목록
            self.cb_res["values"] = RESOLUTIONS
            self.cb_fps["values"] = FPS_LIST
            return
        res = list(fmts)
        self.cb_res["values"] = res
        if self.cb_res.get() not in res:
            self.cb_res.set("1920x1080" if "1920x1080" in res else res[0])
        lo, hi = fmts[self.cb_res.get()]
        fps = [f for f in FPS_LIST if lo <= int(f) <= hi] or [str(hi)]
        self.cb_fps["values"] = fps
        if self.cb_fps.get() not in fps:
            self.cb_fps.set(fps[0])

    def start_audio(self):
        self.last_audio_try = time.time()
        aname = self.cb_audio.get()
        idx = next((i for i, n in self.audio_devs if n == aname), None)
        if idx is None:
            return
        self.audio = AudioWorker(idx)
        if self.audio.start():
            self.audio.set_monitor(self.var_mon.get())

    def on_monitor(self):
        self.cfg["monitor"] = self.var_mon.get()
        save_config(self.cfg)
        if self.audio:
            self.audio.set_monitor(self.var_mon.get())

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get())
        if d:
            self.var_dir.set(d)
            self.cfg["out_dir"] = d
            save_config(self.cfg)

    def open_dir(self):
        d = self.var_dir.get()
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    # ---- 녹화
    def toggle_rec(self):
        if self.busy:
            return
        if not self.recording:
            self.start_recording()
        else:
            self.stop_recording()
            if self.sched_rec:  # 예약 녹화를 직접 멈추면 예약도 끝냄
                self.cancel_schedule()
                self.lbl_sched.config(text="예약 녹화를 직접 중지했어요")

    # ---- 예약
    def toggle_schedule(self):
        if self.sched:
            self.cancel_schedule()
            return
        if self.recording:
            messagebox.showinfo("예약", "녹화 중에는 예약할 수 없어요. 녹화를 먼저 멈춰 주세요.")
            return
        mode = self.var_s_mode.get()
        if mode == "duration":
            try:
                minutes = int(self.var_s_min.get().strip())
                if minutes <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("예약", "녹화할 시간을 1 이상의 분 단위 숫자로 입력해 주세요. (예: 30)")
                return
            now = datetime.now()
            self.sched = (now, now + timedelta(minutes=minutes))
            self.cfg.update(sched_mode=mode, sched_minutes=str(minutes))
        else:
            a, b = self.var_s_start.get().strip(), self.var_s_end.get().strip()
            try:
                a = datetime.strptime(a, "%H:%M").strftime("%H:%M")
                b = datetime.strptime(b, "%H:%M").strftime("%H:%M")
            except ValueError:
                messagebox.showwarning("예약", "시간을 HH:MM 형식으로 입력해 주세요. (예: 09:00, 14:30)")
                return
            if a == b:
                messagebox.showwarning("예약", "시작 시간과 종료 시간이 같아요.")
                return
            self.var_s_start.set(a)
            self.var_s_end.set(b)
            self.sched = schedule_window(a, b, datetime.now())
            self.cfg.update(sched_mode=mode, sched_start=a, sched_end=b)
        save_config(self.cfg)
        self.sched_mode = mode
        self.btn_sched.config(text="예약 취소")
        for w in self.sched_inputs:
            w.config(state="disabled")
        # 종료 시간(또는 분)은 예약/녹화 중에도 바꿀 수 있게 열어 둠
        (self.ent_s_min if mode == "duration" else self.ent_s_end).config(state="normal")
        self.btn_sched_end.config(state="normal")
        keep_awake(True)
        self._check_schedule()

    def change_sched_end(self):
        if not self.sched:
            return
        s, e = self.sched
        now = datetime.now()
        if self.sched_mode == "duration":
            try:
                minutes = int(self.var_s_min.get().strip())
                if minutes <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("종료 시간 변경", "1 이상의 분 단위 숫자로 입력해 주세요. (예: 30)")
                return
            new_e = s + timedelta(minutes=minutes)  # 예약 시작 시각부터 N분
            self.cfg["sched_minutes"] = str(minutes)
        else:
            try:
                t = datetime.strptime(self.var_s_end.get().strip(), "%H:%M")
            except ValueError:
                messagebox.showwarning("종료 시간 변경", "시간을 HH:MM 형식으로 입력해 주세요. (예: 14:30)")
                return
            new_e = s.replace(hour=t.hour, minute=t.minute)
            if new_e <= s:  # 시작보다 이른 시각이면 다음 날
                new_e += timedelta(days=1)
            self.var_s_end.set(f"{new_e:%H:%M}")
            self.cfg["sched_end"] = f"{new_e:%H:%M}"
        if new_e <= now:
            messagebox.showwarning("종료 시간 변경",
                                   f"{new_e:%H:%M}은 이미 지난 시간이에요. 지금 멈추려면 '녹화 중지'를 눌러 주세요.")
            return
        save_config(self.cfg)
        self.sched = (s, new_e)
        self._check_schedule()

    def cancel_schedule(self):
        self.sched = None
        self.sched_rec = False
        self.btn_sched.config(text="예약")
        self.btn_sched_end.config(state="disabled")
        self.lbl_sched.config(text="예약 없음")
        for w in self.sched_inputs:
            w.config(state="normal")
        if not self.recording:
            keep_awake(False)

    def _check_schedule(self):
        if not self.sched:
            return
        s, e = self.sched
        now = datetime.now()
        end = ("내일 " if e.date() > now.date() else "") + f"{e:%H:%M}"
        if self.recording and self.sched_rec:
            left = max(0, int((e - now).total_seconds()))
            self.lbl_sched.config(text=f"예약 녹화 중 - {end}에 종료 "
                                       f"(남은 시간 {left // 3600}시간 {left % 3600 // 60}분 {left % 60}초)")
            if now >= e:
                self.stop_recording()
                self.cancel_schedule()
                self.lbl_sched.config(text=f"예약 녹화 완료 ({s:%m/%d %H:%M} ~ {e:%H:%M})")
            return
        if self.recording:  # 수동 녹화 중이면 끝날 때까지 기다림
            self.lbl_sched.config(text=f"예약: {s:%m/%d %H:%M} ~ {end}  (수동 녹화 중)")
            return
        if now >= e:  # 구간을 놓침 (예: 영상 신호 없음) → 예약 해제
            self.cancel_schedule()
            self.lbl_sched.config(text="예약 시간이 지나서 취소됐어요 (영상 신호 확인)")
            return
        if now >= s:
            if not self.busy and self.start_recording(quiet=True):
                self.sched_rec = True
            else:
                self.lbl_sched.config(text=f"예약 시작 대기 - 영상 신호를 기다리는 중 ({end}까지)")
            return
        wait = int((s - now).total_seconds())
        h, m = wait // 3600, wait % 3600 // 60
        self.lbl_sched.config(text=f"예약됨: {s:%m/%d %H:%M} ~ {end}  (시작까지 {h}시간 {m}분)")

    def start_recording(self, quiet=False):
        if not self.video or self.video.get_frame() is None:
            if not quiet:
                messagebox.showwarning("녹화 불가", "영상 신호가 아직 들어오지 않았어요.")
            return False
        out_dir = self.var_dir.get()
        os.makedirs(out_dir, exist_ok=True)
        self.cfg["out_dir"] = out_dir
        save_config(self.cfg)
        self.rec_base = os.path.join(out_dir, "capture_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.has_audio = bool(self.audio and self.audio.running)
        if self.has_audio:
            self.audio.start_rec(self.rec_base + ".tmp.wav")
        self.video.start_rec(self.rec_base + ".tmp.mkv")
        self.rec_start = time.time()
        self.recording = True
        self.btn_rec.config(text="■ 녹화 중지")
        keep_awake(True)
        return True

    def stop_recording(self):
        self.recording = False
        if not self.sched:
            keep_awake(False)
        self.video.stop_rec()
        if self.audio:
            self.audio.stop_rec()
        self.btn_rec.config(text="● 녹화 시작")
        self.busy = True
        self.lbl_status.config(text="MP4로 변환 중... 잠시만 기다려 주세요")
        threading.Thread(target=self._mux, args=(self.rec_base, self.has_audio), daemon=True).start()

    def _mux(self, base, has_audio, sync=False):
        vid, wav, out = base + ".tmp.mkv", base + ".tmp.wav", base + ".mp4"
        self.video.wait_rec()
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", vid]
        if has_audio:
            cmd += ["-i", wav]
        # 영상은 녹화 중 이미 H.264로 인코딩됨 → 재인코딩 없이 복사
        cmd += ["-c:v", "copy", "-movflags", "+faststart"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        cmd += [out]
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            p = subprocess.run(cmd, capture_output=True, creationflags=flags)
            ok = p.returncode == 0 and os.path.exists(out)
        except Exception:
            ok = False
        if ok:
            for f in (vid, wav):
                try:
                    os.remove(f)
                except OSError:
                    pass
        msg = f"저장 완료: {out}" if ok else f"변환 실패 - 임시 파일이 남아 있어요: {vid}"
        if not sync:
            self.root.after(0, self._mux_done, msg, out if ok else None)
        return ok

    def _mux_done(self, msg, out=None):
        self.busy = False
        self.lbl_status.config(text=msg)
        if out and self.var_trim_after.get() and not self.recording and self.trim.load(out):
            self.nb.select(self.trim)

    def on_trim_after(self):
        self.cfg["trim_after"] = self.var_trim_after.get()
        save_config(self.cfg)

    # ---- 주기 업데이트
    def tick(self):
        # 미리보기 (캡쳐 탭이 보일 때만)
        if self.video and self.nb.index("current") == 0:
            frame = self.video.get_frame()
            if frame is not None and self.video.status == "영상 정상":
                h, w = frame.shape[:2]
                s = min(PREVIEW_W / w, PREVIEW_H / h)
                small = cv2.resize(frame, (int(w * s), int(h * s)))
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))
                self.preview.config(image=self.photo)
        # 오디오 레벨 / 재연결
        if self.audio and self.audio.running:
            lv = self.audio.level
            self.audio.level = 0.0
            self.level_bar["value"] = min(100, (lv ** 0.5) * 100)
        else:
            self.level_bar["value"] = 0
            if not self.recording and self.cb_audio.get() and time.time() - self.last_audio_try > 2:
                if self.audio:
                    self.audio.stop()
                self.start_audio()
        # 상태 표시
        if not self.busy:
            v = self.video.status if self.video else "비디오 장치 없음"
            a = "오디오 정상" if (self.audio and self.audio.running) else "오디오 연결 안 됨"
            if self.audio and self.audio.error and not self.audio.running:
                a += f" ({self.audio.error[:40]})"
            self.lbl_status.config(text=f"{v}  |  {a}")
        self._check_schedule()
        if self.recording:
            t = int(time.time() - self.rec_start)
            self.lbl_time.config(text=f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}")
        self.root.after(33, self.tick)

    def on_close(self):
        if self.recording and not messagebox.askyesno("종료", "녹화 중이에요. 녹화를 저장하고 종료할까요?"):
            return
        if not self.trim.close():
            return
        if self.recording:
            self.recording = False
            self.video.stop_rec()
            if self.audio:
                self.audio.stop_rec()
            self.lbl_status.config(text="MP4로 변환 중...")
            self.root.update()
            self._mux(self.rec_base, self.has_audio, sync=True)
        if self.video:
            self.video.stop()
        if self.audio:
            self.audio.stop()
        self.save_all()
        self.root.destroy()

    def _on_space(self, ev):
        """자르기 탭에서 스페이스바 = 재생/일시정지 (입력칸에 글자 입력 중일 때는 제외)"""
        if self.nb.index("current") == 1 and not isinstance(ev.widget, (tk.Entry, ttk.Entry)):
            self.trim.toggle_play()

    def _restore_window(self):
        """마지막 창 위치와 탭 복원 (화면 밖이면 위치는 무시)"""
        m = re.fullmatch(r"\+(-?\d+)\+(-?\d+)", self.cfg.get("geometry", ""))
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            vx, vy, vw, vh = 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            if sys.platform == "win32":  # 모니터 여러 대를 합친 전체 화면 범위
                import ctypes
                gsm = ctypes.windll.user32.GetSystemMetrics
                vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)
            if vx - 50 <= x < vx + vw - 100 and vy <= y < vy + vh - 100:
                self.root.geometry(f"+{x}+{y}")
        try:
            self.nb.select(int(self.cfg.get("tab", 0)))
        except (ValueError, tk.TclError):
            pass

    def save_all(self):
        """현재 화면의 모든 설정을 settings.ini에 저장"""
        self.cfg.update(
            video=self.cb_video.get(), audio=self.cb_audio.get(),
            resolution=self.cb_res.get(), fps=self.cb_fps.get(),
            out_dir=self.var_dir.get(), monitor=self.var_mon.get(),
            sched_mode=self.var_s_mode.get(), sched_start=self.var_s_start.get().strip(),
            sched_end=self.var_s_end.get().strip(), sched_minutes=self.var_s_min.get().strip(),
            trim_after=self.var_trim_after.get(),
            geometry=f"+{self.root.winfo_x()}+{self.root.winfo_y()}",
            tab=str(self.nb.index("current")))
        save_config(self.cfg)


if __name__ == "__main__":
    threading.Thread(target=video_encoder_args, daemon=True).start()  # 인코더 확인을 미리 해 둠
    root = tk.Tk()
    App(root)
    root.mainloop()
