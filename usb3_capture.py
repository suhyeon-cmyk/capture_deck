"""
CaptureDeck - USB3 캡쳐 프로그램 (OBS 대체용, Windows)
- 실행하면 저장된/자동 감지된 장치로 바로 미리보기 + 오디오 레벨 표시
- 영상 + 오디오 캡쳐, MP4 녹화
- 장치 신호가 끊기면 자동 재연결
- 마지막 설정을 자동 저장 (매번 세팅할 필요 없음)
- 녹화 복구: MP4로 합치지 못하고 남은 임시 파일(.tmp.mkv + .tmp.wav)을 다시 MP4로
- 용량 줄이기: 영상을 다시 인코딩해 용량을 줄임 (자르기 저장 시 한 번에 적용도 가능)

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


# ---------------------------------------------------------------- 설정
DEFAULTS = {
    "video": "", "audio": "", "resolution": "1920x1080", "fps": "60",
    "out_dir": str(Path.home() / "Videos" / "Captures"), "monitor": False,
    "sched_mode": "range", "sched_start": "09:00:00", "sched_end": "10:00:00", "sched_minutes": "60",
    "sched_duration": "",  # 지금부터 녹화할 시간 "H:M:S" (비어 있으면 sched_minutes 사용 - 예전 설정 호환)
    "trim_after": True, "geometry": "", "zoomed": False, "tab": "0",
    # 용량 줄이기 (자르기 탭의 '용량 줄이기 설정으로 저장'도 이 설정을 씀)
    "enc_quality": "보통 (추천)", "enc_res": "원본", "enc_fps": "30fps", "enc_gpu": False, "trim_compress": True,
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


def parse_clock(text):
    """'HH:MM' 또는 'HH:MM:SS' → 'HH:MM:SS' (형식이 틀리면 ValueError)"""
    text = text.strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M:%S")
        except ValueError:
            pass
    raise ValueError(text)


def schedule_window(start_hms, end_hms, now):
    """'HH:MM:SS' 두 개 → 지금 진행 중이거나 다음에 올 (시작, 종료) datetime. 자정 넘김 지원"""
    sh, sm, ss = map(int, parse_clock(start_hms).split(":"))
    eh, em, es = map(int, parse_clock(end_hms).split(":"))
    s = now.replace(hour=sh, minute=sm, second=ss, microsecond=0)
    e = now.replace(hour=eh, minute=em, second=es, microsecond=0)
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


NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def media_info(path):
    """ffmpeg로 파일 정보 확인 → {ok: 열리는지, dur: 길이 초, fps, w, h} (모르면 None)"""
    r = {"ok": False, "dur": None, "fps": None, "w": None, "h": None}
    try:
        p = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path],
                           capture_output=True, creationflags=NO_WINDOW, timeout=60)
    except Exception:
        return r
    info = p.stderr.decode("utf-8", "replace")
    r["ok"] = "Stream #" in info
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if m:
        r["dur"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    v = re.search(r"Stream #.*Video:.*?(\d{2,5})x(\d{2,5})", info)
    if v:
        r["w"], r["h"] = int(v.group(1)), int(v.group(2))
    f = re.search(r"Stream #.*Video:.*?([\d.]+) fps", info)
    if f:
        r["fps"] = float(f.group(1))
    return r


def probe_media(path):
    """→ (열리는지, 길이 초 또는 None)"""
    r = media_info(path)
    return r["ok"], r["dur"]


# ---- 용량 줄이기 (재인코딩) 설정
ENC_QUALITY = (("고화질", 21), ("보통 (추천)", 24), ("작게", 27))  # x264 CRF (낮을수록 고화질·큰 용량)
ENC_RES = ("원본", "1080p", "720p")
ENC_FPS = ("원본", "30fps")
BELOW_NORMAL = 0x00004000 if sys.platform == "win32" else 0  # 녹화가 우선이도록 낮은 우선순위로 인코딩


def nvenc_available():
    """GPU 인코더(NVENC)를 쓸 수 있는지 (처음 한 번 확인하는 데 몇 초 걸릴 수 있음)"""
    video_encoder_args()
    return _ENCODER[1] == "h264_nvenc"


def compress_args(quality, res, fps, gpu, info):
    """용량 줄이기 설정 → ffmpeg 출력 옵션 (영상 필터 + 영상/소리 인코더). info는 media_info() 결과"""
    crf = dict(ENC_QUALITY).get(quality, 24)
    vf = []
    if fps == "30fps" and (info.get("fps") or 60) > 31:
        vf.append("fps=30")
    target = {"1080p": 1080, "720p": 720}.get(res)
    if target and (info.get("h") or 9999) > target:  # 줄이기만 하고 키우지는 않음
        vf.append(f"scale=-2:{target}:flags=lanczos")
    if gpu:
        vargs = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(crf + 2), "-b:v", "0"]
    else:
        vargs = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]
    return ((["-vf", ",".join(vf)] if vf else []) + vargs
            + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"])


def mux_to_mp4(vid, wav, out, progress=None):
    """녹화 임시 파일(영상 .mkv + 소리 .wav) → .mp4
    영상은 재인코딩 없이 복사, 소리만 AAC로 변환. 다 만든 뒤에만 out 이름으로 바꾸므로
    중간에 끊겨도 깨진 out 파일이 생기지 않음.
    progress(처리한 초, 전체 초 또는 None)로 진행 상황 전달. → (성공 여부, 오류 내용, 명령)"""
    part = os.path.splitext(out)[0] + ".part.mp4"
    total = probe_media(vid)[1] if progress else None
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-progress", "pipe:1", "-i", vid]
    if wav:
        cmd += ["-i", wav, "-map", "0:v", "-map", "1:a"]
    # (+faststart는 큰 파일을 한 번 더 통째로 다시 쓰므로 사용하지 않음)
    cmd += ["-c:v", "copy"]
    if wav:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += [part]
    err = []
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, errors="replace", creationflags=NO_WINDOW)
        # 오류 출력이 많아도 막히지 않도록 따로 읽음
        t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
        t.start()
        for line in p.stdout:
            if progress and line.startswith("out_time_us="):
                v = line.strip()[12:]
                if v.isdigit():
                    progress(int(v) / 1e6, total)
        ok = p.wait() == 0 and os.path.exists(part)
        t.join(5)
        if ok:
            os.replace(part, out)
    except Exception as ex:
        err.append(repr(ex))
        ok = False
    if not ok:
        try:
            os.remove(part)
        except OSError:
            pass
    return ok, ("".join(err) if not ok else ""), cmd


def write_mux_log(base, cmd, err):
    """변환 실패 원인 확인용 로그 (base.log)"""
    try:
        with open(base + ".log", "w", encoding="utf-8") as f:
            f.write(" ".join(cmd) + "\n\n" + err)
    except OSError:
        pass


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


# ---------------------------------------------------------------- 영상 표시 영역
class FitView(tk.Frame):
    """남는 공간을 모두 차지하고, 영상을 비율 그대로 최대한 크게 가운데에 표시하는 검은 영역
    (자기 크기를 요구하지 않으므로 아래 조작부 높이는 그대로 두고 창 크기에 따라 늘었다 줄었다 함)"""

    def __init__(self, master, min_w=320, min_h=180, on_resize=None):
        super().__init__(master, bg="black", width=min_w, height=min_h)
        self.label = tk.Label(self, bg="black", bd=0)
        self.label.place(relx=0.5, rely=0.5, anchor="center")
        self.size = (min_w, min_h)  # 다른 스레드(재생)에서도 읽으므로 tkinter 호출 없이 값으로 보관
        self.photo = None
        self.on_resize = on_resize
        self._job = None
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, ev):
        size = (max(16, ev.width), max(16, ev.height))
        if size == self.size:
            return
        self.size = size
        if self.on_resize:  # 창 크기를 끄는 동안 계속 다시 그리지 않도록 잠깐 기다렸다가 한 번만
            if self._job:
                self.after_cancel(self._job)
            self._job = self.after(120, self.on_resize)

    def to_rgb(self, frame):
        """BGR 프레임 → 영역에 맞춘 크기의 RGB (어느 스레드에서나 호출 가능)"""
        h, w = frame.shape[:2]
        aw, ah = self.size
        s = min(aw / w, ah / h)
        size = (max(1, int(w * s)), max(1, int(h * s)))
        interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
        return cv2.cvtColor(cv2.resize(frame, size, interpolation=interp), cv2.COLOR_BGR2RGB)

    def show(self, rgb):
        img = Image.fromarray(rgb)
        if self.photo is not None and (self.photo.width(), self.photo.height()) == img.size:
            self.photo.paste(img)  # 기존 이미지에 덮어쓰기가 새로 만드는 것보다 빠름
        else:
            self.photo = ImageTk.PhotoImage(img)
            self.label.config(image=self.photo)


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

    def __init__(self, master, get_dir):
        super().__init__(master)
        self.get_dir = get_dir  # 파일 열기 기본 폴더
        self.get_compress = None    # '용량 줄이기' 설정 (App이 연결)
        self.send_to_encode = None  # '용량 줄이기' 탭으로 보내기 (App이 연결)
        self.last_out = None        # 마지막으로 잘라 저장한 파일
        self.path = None
        self.cap = None
        self.fps, self.dur, self.frame_ms = 60, 0, 1000 / 60
        self.pos = 0
        self.start_ms = self.end_ms = 0
        self.seek_job = None
        self.proc = None
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
        self.last_out = None
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

        # 영상 영역은 아래 조작부를 뺀 나머지 공간을 모두 차지 (아래 조작부들을 먼저 아래쪽에 붙임)
        self.view = FitView(self, on_resize=self._redraw)

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
        # 자르면서 '용량 줄이기' 탭의 설정(화질/해상도/프레임)으로 한 번에 인코딩
        self.var_compress = tk.BooleanVar(value=True)
        ttk.Checkbutton(b, text="용량 줄이기 설정으로 저장", variable=self.var_compress).pack(side="left", padx=(8, 0))
        self.btn_send = ttk.Button(b, text="용량 줄이기 탭으로 보내기", command=self._send)
        self.btn_send.pack(side="right")
        self.prog = ttk.Progressbar(b, maximum=100, length=200)
        self.prog.pack(side="left", padx=8)
        self.lbl_status = ttk.Label(b, text="원본은 그대로 두고 '_trim' 파일로 새로 저장해요")
        self.lbl_status.pack(side="left")
        # 영상 영역: 위 파일 줄과 아래 조작부 사이의 남는 공간을 모두 차지
        self.view.pack(fill="both", expand=True, padx=8, pady=8, before=self.scale)

    # ---- 이동
    def _show(self, ms):
        if not self.cap:
            return
        self.cap.set(cv2.CAP_PROP_POS_MSEC, ms)
        ok, frame = self.cap.read()
        if not ok:
            return
        self._display(self.view.to_rgb(frame))

    def _display(self, rgb):
        self.view.show(rgb)

    def _redraw(self):
        """창 크기가 바뀌면 멈춰 있는 화면을 새 크기로 다시 그림 (재생 중이면 다음 프레임부터 자동으로 맞춰짐)"""
        if self.cap and not self.playing:
            self._show(self.pos)

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
                rgb = self.view.to_rgb(frame)
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
               "-ss", f"{self.start_ms / 1000:.3f}", "-i", self.path, "-t", f"{length / 1000:.3f}"]
        opts = self.get_compress() if (self.var_compress.get() and self.get_compress) else None
        if opts:  # '용량 줄이기' 탭 설정으로 자르면서 한 번에 줄임
            info = {"fps": self.fps, "h": self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)}
            cmd += compress_args(*opts, info)
        else:
            cmd += video_encoder_args() + ["-g", str(round(self.fps)), "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", out]
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
        if ok:
            self.last_out = out
            size = fmt_size(os.path.getsize(out)) if os.path.exists(out) else ""
            self.lbl_status.config(text=f"저장 완료: {os.path.basename(out)} ({size})")
        else:
            self.lbl_status.config(text=f"저장 실패: {err.strip()[:80]}")

    def _send(self):
        """방금 잘라 저장한 파일(없으면 열린 영상)을 '용량 줄이기' 탭 목록에 넣음"""
        path = self.last_out if self.last_out and os.path.exists(self.last_out) else self.path
        if not path:
            messagebox.showinfo("용량 줄이기", "먼저 '영상 열기'로 영상을 열어 주세요.", parent=self)
            return
        if self.send_to_encode:
            self.send_to_encode(path)

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


def fmt_secs(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    return f"{n / 1024 ** 2:.0f} MB" if n >= 1024 ** 2 else f"{n / 1024:.0f} KB"


class RecoverPanel(ttk.Frame):
    """'녹화 복구' 탭: MP4로 합치지 못하고 남은 임시 파일(.tmp.mkv 영상 + .tmp.wav 소리)을 다시 MP4로 만듦"""
    COLS = (("file", "녹화 파일", 230), ("time", "녹화 시각", 140), ("len", "길이", 80),
            ("size", "크기", 80), ("audio", "소리", 60), ("mp4", "기존 MP4", 110), ("state", "상태", 230))

    def __init__(self, master, get_dir, get_active, on_idle):
        super().__init__(master)
        self.get_dir = get_dir        # 기본 폴더 (캡쳐 저장 폴더)
        self.get_active = get_active  # 지금 녹화/변환 중인 파일 (목록에서 제외)
        self.on_idle = on_idle        # 복구 작업이 모두 끝났을 때 호출
        self.busy = False
        self.scan_id = 0
        self._build()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="폴더").pack(side="left")
        self.var_dir = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.var_dir, width=60)
        ent.pack(side="left", padx=6)
        ent.bind("<Return>", lambda e: self.refresh())
        self.btn_dir = ttk.Button(top, text="폴더 선택", command=self.choose_dir)
        self.btn_dir.pack(side="left")
        self.btn_refresh = ttk.Button(top, text="새로 고침", command=self.refresh)
        self.btn_refresh.pack(side="left", padx=4)
        ttk.Button(top, text="폴더 열기", command=self.open_dir).pack(side="left")

        ttk.Label(self, foreground="#555", justify="left", text=(
            "녹화 중 프로그램이 꺼지거나 MP4 변환이 실패하면 영상(.tmp.mkv)과 소리(.tmp.wav)가 따로 남아요. "
            "여기서 다시 하나의 MP4로 합칠 수 있어요.\n"
            "기존 MP4가 깨져 있으면 복구한 파일로 바꾸고, 정상이면 '_복구.mp4'로 따로 저장해요.")
        ).pack(fill="x", padx=8, pady=(0, 6))

        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(box, columns=[c[0] for c in self.COLS], show="headings", height=14)
        for key, title, w in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, anchor="w" if key in ("file", "state") else "center",
                             stretch=key in ("file", "state"))
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.recover_selected())

        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=8)
        self.btn_sel = ttk.Button(bot, text="선택 항목 복구", command=self.recover_selected)
        self.btn_sel.pack(side="left")
        self.btn_all = ttk.Button(bot, text="전체 복구", command=self.recover_all)
        self.btn_all.pack(side="left", padx=6)
        self.var_del = tk.BooleanVar(value=True)
        self.chk_del = ttk.Checkbutton(bot, text="복구에 성공하면 임시 파일(.tmp.mkv / .tmp.wav) 삭제",
                                       variable=self.var_del)
        self.chk_del.pack(side="left", padx=12)
        self.prog = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.prog.pack(fill="x", padx=8)
        self.lbl_status = ttk.Label(self, text="")
        self.lbl_status.pack(fill="x", padx=8, pady=(4, 8))

    def _ui(self, fn, *args):
        """작업 스레드 → 화면 갱신 (창이 이미 닫혔으면 무시)"""
        try:
            self.after(0, fn, *args)
        except (RuntimeError, tk.TclError):
            pass

    def _set(self, base, col, text):
        if self.tree.exists(base):
            self.tree.set(base, col, text)

    # ---- 폴더
    def shown(self):
        """탭이 열릴 때 호출 - 처음엔 캡쳐 저장 폴더로 시작"""
        if not self.var_dir.get().strip():
            self.var_dir.set(self.get_dir())
        if not self.busy:
            self.refresh()

    def choose_dir(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.var_dir.get() or self.get_dir())
        if d:
            self.var_dir.set(os.path.normpath(d))
            self.refresh()

    def open_dir(self):
        d = self.var_dir.get().strip()
        if os.path.isdir(d):
            os.startfile(d)

    # ---- 목록
    def refresh(self):
        if self.busy:
            return
        self.scan_id += 1
        self.tree.delete(*self.tree.get_children())
        self.prog.config(value=0)
        d = self.var_dir.get().strip()
        if not os.path.isdir(d):
            self.lbl_status.config(text="폴더를 찾을 수 없어요.")
            return
        active = self.get_active()
        bases = []
        for name in sorted(os.listdir(d), reverse=True):  # 최근 녹화가 위로
            if not name.lower().endswith(".tmp.mkv"):
                continue
            base = os.path.join(d, name[:-len(".tmp.mkv")])
            if active and os.path.normcase(base) == os.path.normcase(active):
                continue  # 지금 녹화/변환 중인 파일
            m = re.search(r"(\d{8})_(\d{6})$", base)
            when = (datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
                    if m else "")
            wav = base + ".tmp.wav"
            size = os.path.getsize(base + ".tmp.mkv") + (os.path.getsize(wav) if os.path.exists(wav) else 0)
            self.tree.insert("", "end", iid=base, values=(
                os.path.basename(base), when, "확인 중...", fmt_size(size),
                "있음" if os.path.exists(wav) else "없음",
                "확인 중..." if os.path.exists(base + ".mp4") else "없음", "복구 대기"))
            bases.append(base)
        self.lbl_status.config(text=f"복구할 수 있는 녹화 {len(bases)}개" if bases
                               else "복구할 녹화가 없어요. (남아 있는 임시 파일 없음)")
        if bases:
            threading.Thread(target=self._probe_all, args=(self.scan_id, bases), daemon=True).start()

    def _probe_all(self, scan_id, bases):
        """길이와 기존 MP4가 열리는지 확인 (오래 걸릴 수 있어 별도 스레드)"""
        for base in bases:
            if scan_id != self.scan_id:
                return
            dur = probe_media(base + ".tmp.mkv")[1]
            self._ui(self._set, base, "len", fmt_secs(dur) if dur else "알 수 없음")
            if os.path.exists(base + ".mp4"):
                ok = probe_media(base + ".mp4")[0]
                self._ui(self._set, base, "mp4", "정상" if ok else "깨짐 (열리지 않음)")

    # ---- 복구
    def recover_selected(self):
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("녹화 복구", "목록에서 복구할 녹화를 골라 주세요.", parent=self)
            return
        self._start(sel)

    def recover_all(self):
        self._start(list(self.tree.get_children()))

    def _start(self, bases):
        if self.busy or not bases:
            return
        self.busy = True
        self.scan_id += 1  # 진행 중인 확인 작업은 멈춤
        for w in (self.btn_sel, self.btn_all, self.btn_refresh, self.btn_dir, self.chk_del):
            w.config(state="disabled")
        threading.Thread(target=self._run, args=(bases, self.var_del.get()), daemon=True).start()

    def _run(self, bases, delete_tmp):
        done = 0
        n = len(bases)
        for i, base in enumerate(bases):
            vid, wav, mp4 = base + ".tmp.mkv", base + ".tmp.wav", base + ".mp4"
            if not os.path.exists(vid):
                self._ui(self._set, base, "state", "영상 파일이 없어요")
                continue
            name = os.path.basename(base)
            self._ui(self._set, base, "state", "복구 중...")
            self._ui(self.lbl_status.config, {"text": f"복구 중 ({i + 1}/{n}): {name}"})
            out = mp4
            if os.path.exists(mp4) and probe_media(mp4)[0]:  # 정상 MP4는 덮어쓰지 않음
                out, k = base + "_복구.mp4", 2
                while os.path.exists(out):
                    out, k = f"{base}_복구{k}.mp4", k + 1

            def progress(sec, total, base=base, i=i):
                if total:
                    pct = min(100.0, sec / total * 100)
                    self._ui(self._set, base, "state", f"복구 중... {pct:.0f}%")
                    self._ui(self.prog.config, {"value": (i + pct / 100) / n * 100})
                else:
                    self._ui(self._set, base, "state", f"복구 중... {fmt_secs(sec)}")

            ok, err, cmd = mux_to_mp4(vid, wav if os.path.exists(wav) else None, out, progress)
            if ok:
                done += 1
                if delete_tmp:
                    for f in (vid, wav, base + ".log"):
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                self._ui(self._set, base, "state", f"완료 → {os.path.basename(out)}")
                self._ui(self._set, base, "mp4", "정상")
            else:
                write_mux_log(base, cmd, err)
                self._ui(self._set, base, "state", f"실패 (원인: {name}.log)")
        self._ui(self._finish, done, n)

    def _finish(self, done, n):
        self.busy = False
        for w in (self.btn_sel, self.btn_all, self.btn_refresh, self.btn_dir, self.chk_del):
            w.config(state="normal")
        self.prog.config(value=100 if done == n else self.prog["value"])
        self.lbl_status.config(text=f"복구 완료: {done}개 성공" + (f", {n - done}개 실패" if done < n else ""))
        self.on_idle()


class EncodePanel(ttk.Frame):
    """'용량 줄이기' 탭: 녹화/자른 영상을 다시 인코딩해 용량을 줄임 (원본은 그대로 두고 '_small.mp4'로 저장)"""
    COLS = (("file", "파일", 300), ("len", "길이", 80), ("size", "원본 크기", 90),
            ("out", "줄인 크기", 130), ("state", "상태", 250))

    def __init__(self, master, get_dir, cfg, on_busy):
        super().__init__(master)
        self.get_dir = get_dir  # 파일 추가 기본 폴더
        self.cfg = cfg
        self.on_busy = on_busy  # 인코딩 시작/끝 알림 (절전 방지용)
        self.busy = False
        self.proc = None
        self.stop_flag = threading.Event()
        self.info = {}          # 경로 → media_info 결과
        self.gpu_ok = False
        self.worker = None
        self.current = None
        self.removed = set()    # 인코딩 중 목록에서 뺀 항목 (건너뜀)
        self._build()
        threading.Thread(target=self._check_gpu, daemon=True).start()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.btn_add = ttk.Button(top, text="파일 추가...", command=self.add_dialog)
        self.btn_add.pack(side="left")
        self.btn_remove = ttk.Button(top, text="목록에서 빼기", command=self.remove_selected)
        self.btn_remove.pack(side="left", padx=4)
        self.btn_clear = ttk.Button(top, text="끝난 항목 지우기", command=self.clear_done)
        self.btn_clear.pack(side="left")
        ttk.Label(top, foreground="#555",
                  text="원본은 그대로 두고 같은 폴더에 '_small.mp4'로 저장해요").pack(side="left", padx=12)

        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(box, columns=[c[0] for c in self.COLS], show="headings", height=12)
        for key, title, w in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, anchor="w" if key in ("file", "state") else "center",
                             stretch=key in ("file", "state"))
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        o = ttk.LabelFrame(self, text="설정")
        o.pack(fill="x", padx=8, pady=8)
        ttk.Label(o, text="화질").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=4)
        self.var_q = tk.StringVar(value=self.cfg.get("enc_quality") or ENC_QUALITY[1][0])
        if self.var_q.get() not in dict(ENC_QUALITY):
            self.var_q.set(ENC_QUALITY[1][0])
        qf = ttk.Frame(o)
        qf.grid(row=0, column=1, sticky="w")
        self.opt_widgets = []
        for name, _ in ENC_QUALITY:
            rb = ttk.Radiobutton(qf, text=name, value=name, variable=self.var_q)
            rb.pack(side="left", padx=(0, 10))
            self.opt_widgets.append(rb)
        ttk.Label(o, text="해상도").grid(row=0, column=2, sticky="w", padx=(16, 4))
        self.cb_res = ttk.Combobox(o, values=ENC_RES, state="readonly", width=7)
        self.cb_res.set(self.cfg.get("enc_res") if self.cfg.get("enc_res") in ENC_RES else "원본")
        self.cb_res.grid(row=0, column=3, sticky="w")
        ttk.Label(o, text="프레임").grid(row=0, column=4, sticky="w", padx=(16, 4))
        self.cb_fps = ttk.Combobox(o, values=ENC_FPS, state="readonly", width=7)
        self.cb_fps.set(self.cfg.get("enc_fps") if self.cfg.get("enc_fps") in ENC_FPS else "30fps")
        self.cb_fps.grid(row=0, column=5, sticky="w")
        self.var_gpu = tk.BooleanVar(value=False)
        self.chk_gpu = ttk.Checkbutton(o, text="GPU로 빠르게 (확인 중...)", variable=self.var_gpu, state="disabled")
        self.chk_gpu.grid(row=0, column=6, sticky="w", padx=(16, 8))
        self.opt_widgets += [self.cb_res, self.cb_fps]
        ttk.Label(o, foreground="#555", justify="left", text=(
            "강의 영상 45분 기준 (원본 약 5.2GB): 고화질 약 1.1GB · 보통 약 0.7GB · 작게+720p 약 0.3GB\n"
            "강의처럼 움직임이 적은 영상은 30fps로 줄여도 티가 거의 안 나요. 녹화 중에도 쓸 수 있지만 "
            "녹화가 우선이라 인코딩은 느려질 수 있어요.")).grid(row=1, column=0, columnspan=7, sticky="w",
                                                         padx=8, pady=(0, 6))

        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8)
        self.btn_start = ttk.Button(bot, text="용량 줄이기 시작", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bot, text="중지", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.prog = ttk.Progressbar(bot, mode="determinate", maximum=100)
        self.prog.pack(side="left", fill="x", expand=True, padx=8)
        self.lbl_status = ttk.Label(self, text="'파일 추가'로 줄일 영상을 고르세요. 자르기 탭에서 보낼 수도 있어요.")
        self.lbl_status.pack(fill="x", padx=8, pady=(4, 8))

    def _ui(self, fn, *args):
        try:
            self.after(0, fn, *args)
        except (RuntimeError, tk.TclError):
            pass

    def _set(self, iid, col, text):
        if self.tree.exists(iid):
            self.tree.set(iid, col, text)

    def _check_gpu(self):
        ok = nvenc_available()
        def done():
            self.gpu_ok = ok
            if ok:
                self.chk_gpu.config(text="GPU로 빠르게 (NVENC)", state="disabled" if self.busy else "normal")
                self.var_gpu.set(bool(self.cfg.get("enc_gpu", False)))
            else:
                self.chk_gpu.config(text="GPU 사용 불가 (NVIDIA 드라이버 확인)")
        self._ui(done)

    def options(self):
        return self.var_q.get(), self.cb_res.get(), self.cb_fps.get(), bool(self.var_gpu.get())

    def save_options(self):
        q, r, f, g = self.options()
        self.cfg.update(enc_quality=q, enc_res=r, enc_fps=f, enc_gpu=g)

    # ---- 목록
    def add_dialog(self):
        paths = filedialog.askopenfilenames(
            parent=self, title="용량을 줄일 영상 선택", initialdir=self.get_dir(),
            filetypes=[("영상 파일", "*.mp4 *.mkv *.mov *.avi"), ("모든 파일", "*.*")])
        self.add_files(paths)

    def add_files(self, paths):
        new = []
        for p in paths:
            p = os.path.normpath(p)
            if self.tree.exists(p) or not os.path.isfile(p):
                continue
            self.tree.insert("", "end", iid=p, values=(
                os.path.basename(p), "확인 중...", fmt_size(os.path.getsize(p)), "", "대기"))
            new.append(p)
        if new:
            self.lbl_status.config(text=f"{len(new)}개 추가했어요. 설정을 고른 뒤 '용량 줄이기 시작'을 누르세요.")
            threading.Thread(target=self._probe, args=(new,), daemon=True).start()

    def _probe(self, paths):
        for p in paths:
            info = media_info(p)
            self.info[p] = info
            self._ui(self._set, p, "len", fmt_secs(info["dur"]) if info["dur"] else "알 수 없음")
            if not info["ok"]:
                self._ui(self._set, p, "state", "열 수 없는 파일")

    def remove_selected(self):
        for iid in self.tree.selection():
            if not (self.busy and iid == self.current):
                self.removed.add(iid)
                self.tree.delete(iid)

    def clear_done(self):
        for iid in self.tree.get_children():
            if self.tree.set(iid, "state").startswith("완료"):
                self.tree.delete(iid)

    # ---- 인코딩
    def start(self):
        if self.busy:
            return
        todo = [i for i in self.tree.get_children() if not self.tree.set(i, "state").startswith("완료")]
        if not todo:
            messagebox.showinfo("용량 줄이기", "줄일 영상이 없어요. '파일 추가'로 영상을 골라 주세요.", parent=self)
            return
        self.save_options()
        save_config(self.cfg)
        self.busy = True
        self.current = None
        self.removed.clear()
        self.stop_flag.clear()
        self._set_inputs("disabled")
        self.btn_stop.config(state="normal")
        self.on_busy(True)
        self.worker = threading.Thread(target=self._run, args=(todo, self.options()), daemon=True)
        self.worker.start()

    def _set_inputs(self, state):
        widgets = [self.btn_start, self.btn_add, self.btn_clear, *self.opt_widgets]
        if self.gpu_ok:
            widgets.append(self.chk_gpu)
        for w in widgets:
            w.config(state=("readonly" if isinstance(w, ttk.Combobox) and state == "normal" else state))

    def _out_path(self, src):
        base = os.path.splitext(src)[0]
        out, k = base + "_small.mp4", 2
        while os.path.exists(out):
            out, k = f"{base}_small{k}.mp4", k + 1
        return out

    def _run(self, todo, opts):
        done = failed = 0
        before = after = 0
        n = len(todo)
        for i, src in enumerate(todo):
            if self.stop_flag.is_set():
                break
            if src in self.removed:
                continue
            if not os.path.isfile(src):
                self._ui(self._set, src, "state", "파일이 없어요")
                failed += 1
                continue
            self.current = src
            info = self.info.get(src) or media_info(src)
            self.info[src] = info
            out = self._out_path(src)
            part = os.path.splitext(out)[0] + ".part.mp4"
            cmd = ([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-progress", "pipe:1",
                    "-i", src, "-map", "0:v:0", "-map", "0:a?"]
                   + compress_args(*opts, info) + ["-movflags", "+faststart", part])
            name = os.path.basename(src)
            self._ui(self.lbl_status.config, {"text": f"줄이는 중 ({i + 1}/{n}): {name}"})
            self._ui(self._set, src, "state", "인코딩 중...")
            err = []
            t0 = time.time()
            try:
                self.proc = p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                                 errors="replace", creationflags=NO_WINDOW | BELOW_NORMAL)
                et = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
                et.start()
                last = 0.0
                for line in p.stdout:
                    if line.startswith("out_time_us=") and line.strip()[12:].isdigit() and info["dur"]:
                        pct = min(100.0, int(line.strip()[12:]) / 1e6 / info["dur"] * 100)
                        now = time.time()
                        if now - last > 0.5:
                            last = now
                            left = (now - t0) / pct * (100 - pct) if pct > 1 else None
                            eta = f" (남은 시간 약 {fmt_secs(left)})" if left else ""
                            self._ui(self._set, src, "state", f"인코딩 중 {pct:.0f}%{eta}")
                            self._ui(self.prog.config, {"value": (i + pct / 100) / n * 100})
                ok = p.wait() == 0 and os.path.exists(part) and not self.stop_flag.is_set()
                et.join(5)
                if ok:
                    os.replace(part, out)
            except Exception as ex:
                err.append(repr(ex))
                ok = False
            self.proc = None
            if not ok:
                try:
                    os.remove(part)
                except OSError:
                    pass
            if self.stop_flag.is_set():
                self._ui(self._set, src, "state", "중지됨")
                break
            if ok:
                done += 1
                a, b = os.path.getsize(src), os.path.getsize(out)
                before, after = before + a, after + b
                self._ui(self._set, src, "out", f"{fmt_size(b)} ({b / a * 100:.0f}%)")
                self._ui(self._set, src, "state", f"완료 → {os.path.basename(out)}")
            else:
                failed += 1
                msg = " ".join("".join(err).split())[:80] or "알 수 없는 오류"
                self._ui(self._set, src, "state", f"실패: {msg}")
        self._ui(self._finish, done, failed, before, after)

    def _finish(self, done, failed, before, after):
        self.busy = False
        self.current = None
        self._set_inputs("normal")
        self.btn_stop.config(state="disabled")
        if self.stop_flag.is_set():
            self.prog.config(value=0)
            self.lbl_status.config(text="중지했어요. 만들던 파일은 지웠어요.")
        else:
            self.prog.config(value=100 if done and not failed else self.prog["value"])
            text = f"완료: {done}개"
            if failed:
                text += f", 실패 {failed}개"
            if before:
                text += f"  ·  {fmt_size(before)} → {fmt_size(after)} ({after / before * 100:.0f}%)"
            self.lbl_status.config(text=text)
        self.on_busy(False)

    def stop(self):
        if not self.busy:
            return
        self.stop_flag.set()
        p = self.proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass

    def close(self):
        """프로그램 종료 시 호출. 인코딩 중에 사용자가 취소하면 False"""
        if not self.busy:
            return True
        if not messagebox.askyesno("종료", "용량 줄이기 인코딩 중이에요.\n중단하고 종료할까요? (만들던 파일은 지워져요)",
                                   parent=self):
            return False
        self.stop()
        if self.worker is not None:
            self.worker.join(5)  # 만들던 파일(.part.mp4) 정리를 기다림
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
        self.close_pending = False  # MP4 변환 중 창을 닫으면 변환 후 종료
        self.last_audio_try = 0.0
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
        self.recover = RecoverPanel(self.nb, get_dir=lambda: self.var_dir.get(),
                                    get_active=lambda: self.rec_base if (self.recording or self.busy) else None,
                                    on_idle=self._maybe_close)
        self.nb.add(self.recover, text="  녹화 복구  ")
        self.encode = EncodePanel(self.nb, get_dir=lambda: self.var_dir.get(), cfg=self.cfg,
                                  on_busy=self._on_encode_busy)
        self.nb.add(self.encode, text="  용량 줄이기  ")
        self.trim.get_compress = self.encode.options
        self.trim.send_to_encode = self._send_to_encode
        self.trim.var_compress.set(bool(self.cfg.get("trim_compress", True)))
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab)
        self.root.bind("<space>", self._on_space)

        # 미리보기: 아래 조작부(높이 고정)를 뺀 나머지 공간을 모두 차지
        self.preview = FitView(r)
        self.preview.pack(fill="both", expand=True, padx=8, pady=8)
        self.prev_next = 0.0  # 다음 미리보기를 그릴 시각 (그리는 데 걸린 시간에 맞춰 조절)

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
        self.var_s_start = tk.StringVar(value=self._clock_or(self.cfg["sched_start"], "09:00:00"))
        self.var_s_end = tk.StringVar(value=self._clock_or(self.cfg["sched_end"], "10:00:00"))
        self.ent_s_start = ttk.Entry(r1, textvariable=self.var_s_start, width=9, justify="center")
        self.ent_s_start.pack(side="left", padx=(6, 2))
        ttk.Label(r1, text="부터").pack(side="left")
        self.ent_s_end = ttk.Entry(r1, textvariable=self.var_s_end, width=9, justify="center")
        self.ent_s_end.pack(side="left", padx=(6, 2))
        ttk.Label(r1, text="까지  (24시간, 예: 14:30:00)").pack(side="left")
        # 방식 2: 지금부터 몇 시간 몇 분 몇 초간
        r2 = ttk.Frame(s)
        r2.grid(row=1, column=1, sticky="w")
        self.rb_dur = ttk.Radiobutton(r2, text="지금부터", value="duration", variable=self.var_s_mode)
        self.rb_dur.pack(side="left")
        dh, dm, ds = self._initial_duration()
        self.var_s_h, self.var_s_m, self.var_s_s = (tk.StringVar(value=str(v)) for v in (dh, dm, ds))
        self.dur_entries = []
        for i, (var, unit) in enumerate(((self.var_s_h, "시간"), (self.var_s_m, "분"), (self.var_s_s, "초간"))):
            ent = ttk.Entry(r2, textvariable=var, width=4, justify="center")
            ent.pack(side="left", padx=(6 if i == 0 else 4, 2))
            ttk.Label(r2, text=unit).pack(side="left")
            self.dur_entries.append(ent)
        # 지금부터 녹화하면 몇 시 몇 분 몇 초에 끝나는지 실시간 표시
        self.lbl_s_until = ttk.Label(r2, text="", foreground="#1a5fb4")
        self.lbl_s_until.pack(side="left", padx=(10, 0))
        self.btn_sched = ttk.Button(s, text="예약", command=self.toggle_schedule)
        self.btn_sched.grid(row=0, column=2, padx=12, sticky="ew")
        self.btn_sched_end = ttk.Button(s, text="종료 시간 변경", command=self.change_sched_end, state="disabled")
        self.btn_sched_end.grid(row=1, column=2, padx=12, sticky="ew")
        for e in (self.ent_s_end, *self.dur_entries):  # 예약 중 Enter로도 종료 시간 변경
            e.bind("<Return>", lambda ev: self.change_sched_end() if self.sched else None)
        self.lbl_sched = ttk.Label(s, text="예약 없음")
        self.lbl_sched.grid(row=0, column=3, rowspan=2, sticky="w")
        self.sched_inputs = (self.rb_range, self.rb_dur, self.ent_s_start, self.ent_s_end, *self.dur_entries)

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
    @staticmethod
    def _clock_or(text, default):
        try:
            return parse_clock(text)
        except ValueError:
            return default

    def _initial_duration(self):
        """저장된 '지금부터' 시간 → (시, 분, 초). 예전 설정(분 단위)도 읽음"""
        try:
            h, m, s = (int(x) for x in self.cfg.get("sched_duration", "").split(":"))
            return h, m, s
        except ValueError:
            pass
        try:
            total = max(0, int(self.cfg.get("sched_minutes", "60"))) * 60
        except ValueError:
            total = 3600
        return total // 3600, total % 3600 // 60, total % 60

    def _get_duration(self):
        """'지금부터' 입력칸 → 녹화할 초 (빈칸은 0, 잘못된 값이면 None)"""
        try:
            h, m, s = (int(v.get().strip() or 0) for v in (self.var_s_h, self.var_s_m, self.var_s_s))
        except ValueError:
            return None
        if min(h, m, s) < 0:
            return None
        total = h * 3600 + m * 60 + s
        return total if total > 0 else None

    def _save_duration(self, total):
        self.cfg["sched_duration"] = f"{total // 3600}:{total % 3600 // 60}:{total % 60}"

    @staticmethod
    def _fmt_end(e, now):
        return ("내일 " if e.date() > now.date() else "") + f"{e:%H:%M:%S}"

    def _update_until_label(self):
        """'지금부터' 시간으로 녹화하면 끝나는 시각을 실시간으로 표시"""
        text = ""
        if self.var_s_mode.get() == "duration" or (self.sched and self.sched_mode == "duration"):
            total = self._get_duration()
            now = datetime.now()
            if total is None:
                text = "→ 시간을 확인해 주세요"
            else:
                base = self.sched[0] if (self.sched and self.sched_mode == "duration") else now
                text = f"→ {self._fmt_end(base + timedelta(seconds=total), now)}에 종료"
        if self.lbl_s_until.cget("text") != text:
            self.lbl_s_until.config(text=text)

    def toggle_schedule(self):
        if self.sched:
            self.cancel_schedule()
            return
        mode = self.var_s_mode.get()
        now = datetime.now()
        if mode == "duration":
            total = self._get_duration()
            if total is None:
                messagebox.showwarning("예약", "녹화할 시간을 시간/분/초 숫자로 입력해 주세요. (예: 0시간 30분 15초)")
                return
            self.sched = (now, now + timedelta(seconds=total))
            self.cfg["sched_mode"] = mode
            self._save_duration(total)
        else:
            try:
                a = parse_clock(self.var_s_start.get())
                b = parse_clock(self.var_s_end.get())
            except ValueError:
                messagebox.showwarning("예약", "시간을 HH:MM:SS 형식으로 입력해 주세요. (예: 09:00:00, 14:30:15)")
                return
            if a == b:
                messagebox.showwarning("예약", "시작 시간과 종료 시간이 같아요.")
                return
            self.var_s_start.set(a)
            self.var_s_end.set(b)
            self.sched = schedule_window(a, b, now)
            self.cfg.update(sched_mode=mode, sched_start=a, sched_end=b)
        save_config(self.cfg)
        self.sched_mode = mode
        # 이미 녹화 중이고 예약 구간 안이면, 지금 녹화를 예약 녹화로 이어받아 종료 시간에 멈춤
        if self.recording and self.sched[0] <= now < self.sched[1]:
            self.sched_rec = True
        self.btn_sched.config(text="예약 취소")
        for w in self.sched_inputs:
            w.config(state="disabled")
        # 종료 시간(또는 지금부터 시간)은 예약/녹화 중에도 바꿀 수 있게 열어 둠
        for w in (self.dur_entries if mode == "duration" else (self.ent_s_end,)):
            w.config(state="normal")
        self.btn_sched_end.config(state="normal")
        keep_awake(True)
        self._check_schedule()

    def change_sched_end(self):
        if not self.sched:
            return
        s, e = self.sched
        now = datetime.now()
        if self.sched_mode == "duration":
            total = self._get_duration()
            if total is None:
                messagebox.showwarning("종료 시간 변경", "녹화할 시간을 시간/분/초 숫자로 입력해 주세요. (예: 0시간 30분 15초)")
                return
            new_e = s + timedelta(seconds=total)  # 예약 시작 시각부터 N시간 N분 N초
            self._save_duration(total)
        else:
            try:
                t = datetime.strptime(parse_clock(self.var_s_end.get()), "%H:%M:%S")
            except ValueError:
                messagebox.showwarning("종료 시간 변경", "시간을 HH:MM:SS 형식으로 입력해 주세요. (예: 14:30:15)")
                return
            new_e = s.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            if new_e <= s:  # 시작보다 이른 시각이면 다음 날
                new_e += timedelta(days=1)
            self.var_s_end.set(f"{new_e:%H:%M:%S}")
            self.cfg["sched_end"] = f"{new_e:%H:%M:%S}"
        if new_e <= now:
            messagebox.showwarning("종료 시간 변경",
                                   f"{new_e:%H:%M:%S}은 이미 지난 시간이에요. 지금 멈추려면 '녹화 중지'를 눌러 주세요.")
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
        if not self.recording and not self.busy:  # MP4 변환 중이면 끝날 때까지 절전 방지 유지
            keep_awake(False)

    def _check_schedule(self):
        if not self.sched:
            return
        s, e = self.sched
        now = datetime.now()
        end = self._fmt_end(e, now)
        if self.recording and self.sched_rec:
            left = max(0, int((e - now).total_seconds()))
            self.lbl_sched.config(text=f"예약 녹화 중 - {end}에 종료 "
                                       f"(남은 시간 {left // 3600}시간 {left % 3600 // 60}분 {left % 60}초)")
            if now >= e:
                self.stop_recording()
                self.cancel_schedule()
                self.lbl_sched.config(text=f"예약 녹화 완료 ({s:%m/%d %H:%M:%S} ~ {e:%H:%M:%S})")
            return
        if self.recording:  # 수동 녹화 중이면 끝날 때까지 기다림
            self.lbl_sched.config(text=f"예약: {s:%m/%d %H:%M:%S} ~ {end}  (수동 녹화 중)")
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
        h, m, sec = wait // 3600, wait % 3600 // 60, wait % 60
        self.lbl_sched.config(text=f"예약됨: {s:%m/%d %H:%M:%S} ~ {end}  (시작까지 {h}시간 {m}분 {sec}초)")

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
        self.recording = False  # 절전 방지는 MP4 변환이 끝난 뒤(_mux_done)에 풀어 줌
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
        ok, err, cmd = mux_to_mp4(vid, wav if has_audio else None, out)
        if ok:
            for f in (vid, wav):
                try:
                    os.remove(f)
                except OSError:
                    pass
        else:
            write_mux_log(base, cmd, err)
        msg = (f"저장 완료: {out}" if ok
               else f"변환 실패 - '녹화 복구' 탭에서 다시 시도할 수 있어요 (원인: {os.path.basename(base)}.log)")
        if not sync:
            self.root.after(0, self._mux_done, msg, out if ok else None)
        return ok

    def _mux_done(self, msg, out=None):
        self.busy = False
        self.lbl_status.config(text=msg)
        if not self.recording and not self.sched:
            keep_awake(False)
        if self.close_pending:  # 변환 중에 창을 닫았으면 이제 종료
            self._maybe_close()
            return
        if out and self.var_trim_after.get() and not self.recording and self.trim.load(out):
            self.nb.select(self.trim)

    def on_trim_after(self):
        self.cfg["trim_after"] = self.var_trim_after.get()
        save_config(self.cfg)

    # ---- 주기 업데이트
    def tick(self):
        # 미리보기 (캡쳐 탭이 보일 때만)
        # 미리보기가 크면 그리는 데 시간이 걸리므로, 그 시간에 맞춰 그리는 횟수를 줄임
        # (화면 그리기가 녹화를 방해하지 않도록 녹화 중에는 더 여유를 둠)
        now = time.perf_counter()
        if self.video and self.nb.index("current") == 0 and now >= self.prev_next:
            frame = self.video.get_frame()
            if frame is not None and self.video.status == "영상 정상":
                self.preview.show(self.preview.to_rgb(frame))
                cost = time.perf_counter() - now
                self.prev_next = now + max(0.03, cost * (4 if self.recording else 1.5))
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
        self._update_until_label()
        if self.recording:
            t = int(time.time() - self.rec_start)
            self.lbl_time.config(text=f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}")
        self.root.after(33, self.tick)

    def _on_tab(self, ev=None):
        self.trim.pause()
        if self.nb.select() == str(self.recover):
            self.recover.shown()

    def _send_to_encode(self, path):
        self.encode.add_files([path])
        self.nb.select(self.encode)

    def _on_encode_busy(self, busy):
        """인코딩 중에는 절전 방지 (끝나면 녹화/예약/변환 중이 아닐 때만 해제)"""
        if busy:
            keep_awake(True)
        elif not (self.recording or self.sched or self.busy):
            keep_awake(False)

    def _maybe_close(self):
        """변환/복구 중에 창을 닫았던 경우, 모두 끝났으면 종료"""
        if self.close_pending and not self.busy and not self.recover.busy:
            self.on_close()

    def on_close(self):
        if self.busy or self.recover.busy:  # MP4 변환/복구 중에 끄면 파일이 깨지므로 끝날 때까지 기다림
            if self.close_pending:
                return
            what = "녹화 파일을 MP4로 변환" if self.busy else "녹화를 복구"
            if messagebox.askyesno("종료", f"{what}하고 있어요.\n"
                                         "지금 끄면 파일이 깨질 수 있어요. 끝나면 자동으로 종료할까요?"):
                self.close_pending = True
                self.lbl_status.config(text="작업이 끝나면 자동으로 종료해요...")
                self.recover.lbl_status.config(text="복구가 끝나면 자동으로 종료해요...")
            return
        if self.recording and not messagebox.askyesno("종료", "녹화 중이에요. 녹화를 저장하고 종료할까요?"):
            return
        if not self.trim.close():
            return
        if not self.encode.close():
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
        """마지막 창 크기·위치·최대화와 탭 복원 (화면 밖이면 위치는 무시)"""
        self.root.update_idletasks()
        # 아래 조작부가 잘리지 않는 최소 크기 (영상 영역은 작게 줄어들 수 있음)
        min_w, min_h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.minsize(min_w, min_h)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        vx, vy, vw, vh = 0, 0, sw, sh
        if sys.platform == "win32":  # 모니터 여러 대를 합친 전체 화면 범위
            import ctypes
            gsm = ctypes.windll.user32.GetSystemMetrics
            vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)
        # 예전 설정은 위치만("+x+y"), 지금은 크기까지("WxH+x+y") 저장
        m = re.fullmatch(r"(?:(\d+)x(\d+))?\+(-?\d+)\+(-?\d+)", self.cfg.get("geometry", ""))
        w, h = (int(m.group(1)), int(m.group(2))) if m and m.group(1) else (0, 0)
        if w < min_w or h < min_h:  # 처음 실행: 화면의 80% 크기
            w, h = max(min_w, int(sw * 0.8)), max(min_h, int(sh * 0.8))
        w, h = min(w, vw), min(h, vh)
        geo = f"{w}x{h}"
        if m:
            x, y = int(m.group(3)), int(m.group(4))
            if vx - 50 <= x < vx + vw - 100 and vy <= y < vy + vh - 100:
                geo += f"+{x}+{y}"
        self.root.geometry(geo)
        if self.cfg.get("zoomed"):
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass
        try:
            self.nb.select(int(self.cfg.get("tab", 0)))
        except (ValueError, tk.TclError):
            pass

    def _window_geometry(self):
        """저장할 창 크기·위치. 최대화 상태면 최대화 여부만 바꾸고 원래 크기는 그대로 둠"""
        zoomed = self.root.state() == "zoomed"
        if zoomed:
            return {"zoomed": True}
        return {"zoomed": False, "geometry": self.root.wm_geometry()}

    def save_all(self):
        """현재 화면의 모든 설정을 settings.ini에 저장"""
        self.cfg.update(
            video=self.cb_video.get(), audio=self.cb_audio.get(),
            resolution=self.cb_res.get(), fps=self.cb_fps.get(),
            out_dir=self.var_dir.get(), monitor=self.var_mon.get(),
            sched_mode=self.var_s_mode.get(), sched_start=self.var_s_start.get().strip(),
            sched_end=self.var_s_end.get().strip(),
            sched_duration=":".join(v.get().strip() or "0" for v in (self.var_s_h, self.var_s_m, self.var_s_s)),
            trim_after=self.var_trim_after.get(), trim_compress=self.trim.var_compress.get(),
            **self._window_geometry(),
            tab=str(self.nb.index("current")))
        self.encode.save_options()
        save_config(self.cfg)


if __name__ == "__main__":
    threading.Thread(target=video_encoder_args, daemon=True).start()  # 인코더 확인을 미리 해 둠
    root = tk.Tk()
    App(root)
    root.mainloop()
