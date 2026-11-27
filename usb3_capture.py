"""
CaptureDeck - USB3 캡쳐 프로그램 (OBS 대체용, Windows)
- 실행하면 저장된/자동 감지된 장치로 바로 미리보기 + 오디오 레벨 표시
- 영상 + 오디오 캡쳐, MP4 녹화
- 장치 신호가 끊기면 자동 재연결
- 마지막 설정을 자동 저장 (매번 세팅할 필요 없음)
- 복구: 녹화·자르기·인코딩 중 끊기거나 실패해서 생긴 파일(합치지 못한 녹화, .part 파일, 손상된 영상)을 고침
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
import tkinter.font as tkfont
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
    "out_dir": str(Path.home() / "Videos" / "Captures"), "monitor": False, "monitor_out": "",
    "sched_mode": "range", "sched_start": "09:00:00", "sched_end": "10:00:00", "sched_minutes": "60",
    "sched_duration": "",  # 지금부터 녹화할 시간 "H:M:S" (비어 있으면 sched_minutes 사용 - 예전 설정 호환)
    "trim_after": True, "geometry": "", "zoomed": False, "tab": "0",
    "mode": "auto",  # (예전 시작 화면 설정 - 지금은 자동 녹화 창이 바로 열림)
    "auto_geometry": "", "auto_zoomed": False,  # 자동 녹화 화면의 창 크기·위치 (직접 녹화와 따로 기억)
    # 용량 줄이기 (자르기 탭의 '용량 줄이기 설정으로 저장'도 이 설정을 씀)
    "auto_list": "", "auto_gap": "5",  # 자동 녹화: 강의 목록(줄바꿈은 \n으로 저장), 강의 사이 쉬는 시간
    # 자동 녹화 작업: 진행 방식(each 녹화 완료 후 바로 인코딩 / all 모든 녹화 후 / rec 녹화만), 인코딩 완료 후 원본 제거
    # (auto_enc·auto_enc_when은 예전 설정 - auto_flow가 비어 있을 때 한 번 읽음)
    "auto_flow": "", "auto_enc": False, "auto_enc_when": "", "auto_enc_quality": "보통 (추천)",
    "auto_enc_res": "원본", "auto_enc_fps": "30fps", "auto_enc_gpu": False, "auto_enc_delete": False,
    "auto_enc_speed_cpu": "", "auto_enc_speed_gpu": "",  # 지난 인코딩에서 잰 속도 (남은 시간 계산용)
    # 자동 녹화 파일 이름: 공백(keep 그대로 / remove 제거 / under '_'로), 영상 길이(분) 넣기
    "auto_name_space": "keep", "auto_name_len": False,
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


DEFAULT_OUT = "Windows 기본 출력 장치"


def list_output_devices():
    """'소리 듣기'에 쓸 출력 장치 [(sounddevice 인덱스 또는 None=Windows 기본, 이름)] - MME"""
    out = [(None, DEFAULT_OUT)]
    try:
        apis = sd.query_hostapis()
        mme = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] <= 0 or (mme is not None and d["hostapi"] != mme):
                continue
            if "사운드 매퍼" in d["name"] or "Sound Mapper" in d["name"]:
                continue
            out.append((i, d["name"]))
    except Exception:
        pass
    return out


def default_output_name():
    """Windows 기본 출력 장치 이름 (모르면 '')"""
    try:
        return sd.query_devices(kind="output")["name"]
    except Exception:
        return ""


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
    r = {"ok": False, "dur": None, "fps": None, "w": None, "h": None, "abr": None, "tag": False}
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
    a = re.search(r"Stream #.*Audio:.*?(\d+) kb/s", info)
    if a:
        r["abr"] = int(a.group(1))
    r["tag"] = ENC_TAG in info
    return r


ENC_TAG = "CaptureDeck encoded"  # 용량 줄이기로 만든 파일에 넣는 표시 (파일 정보의 comment)


def is_encoded(info):
    """용량 줄이기를 거친 파일인지: 표시가 있거나, 소리가 128k (녹화 파일은 192k)"""
    return bool(info.get("tag") or (info.get("abr") and info["abr"] < 160))


def probe_media(path):
    """→ (열리는지, 길이 초 또는 None)"""
    r = media_info(path)
    return r["ok"], r["dur"]


# ---- 용량 줄이기 (재인코딩) 설정
ENC_QUALITY = (("고화질", 21), ("보통 (추천)", 24), ("작게", 27))  # x264 CRF (낮을수록 고화질·큰 용량)
ENC_RES = ("원본", "1080p", "720p")
ENC_FPS = ("원본", "30fps")
BELOW_NORMAL = 0x00004000 if sys.platform == "win32" else 0  # 녹화가 우선이도록 낮은 우선순위로 인코딩


def to_recycle_bin(path):
    """파일을 휴지통으로 (실패하면 False, 파일은 그대로)"""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    # FO_DELETE=3, FOF_ALLOWUNDO(휴지통)|FOF_NOCONFIRMATION|FOF_SILENT|FOF_NOERRORUI
    op = SHFILEOPSTRUCTW(None, 3, os.path.abspath(path) + "\0", None, 0x40 | 0x10 | 0x04 | 0x400, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not os.path.exists(path)


def suspend_process(proc, pause):
    """실행 중인 프로세스를 잠시 멈추거나(pause=True) 다시 실행 (Windows만, 실패하면 False)"""
    if sys.platform != "win32" or proc is None or proc.poll() is not None:
        return False
    import ctypes
    k32, nt = ctypes.windll.kernel32, ctypes.windll.ntdll
    h = k32.OpenProcess(0x0800, False, proc.pid)  # PROCESS_SUSPEND_RESUME
    if not h:
        return False
    try:
        return (nt.NtSuspendProcess(h) if pause else nt.NtResumeProcess(h)) == 0
    finally:
        k32.CloseHandle(h)


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
            + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-metadata", f"comment={ENC_TAG}"])


TOLERANT_IN = ["-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt"]  # 깨진 부분은 건너뛰고 읽기
REPAIR_VIDEO = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


def run_ffmpeg(args, part, progress=None, total=None, stop=None):
    """ffmpeg 실행 (진행 상황은 -progress로 받음) → (성공 여부, 오류 내용)
    part가 만들어지고 열려야 성공. stop(threading.Event)이 켜지면 중단"""
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-progress", "pipe:1"] + args
    err = []
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, errors="replace", creationflags=NO_WINDOW)
        # 오류 출력이 많아도 막히지 않도록 따로 읽음
        t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
        t.start()
        for line in p.stdout:
            if stop is not None and stop.is_set():
                p.kill()
                break
            if progress and line.startswith("out_time_us="):
                v = line.strip()[12:]
                if v.isdigit():
                    progress(int(v) / 1e6, total)
        ok = p.wait() == 0 and os.path.exists(part) and os.path.getsize(part) > 0
        t.join(5)
        if stop is not None and stop.is_set():
            ok = False
            err.append("중지됨")
    except Exception as ex:
        err.append(repr(ex))
        ok = False
    return ok, "".join(err)


def _finish_part(ok, part, out, need_dur=True):
    """만든 임시 파일이 정말 열리는지 확인 → 맞으면 out 이름으로, 아니면 지움"""
    if ok:
        info = media_info(part)
        ok = info["ok"] and (info["dur"] or 0) > 0 if need_dur else info["ok"]
    if ok:
        os.replace(part, out)
    else:
        try:
            os.remove(part)
        except OSError:
            pass
    return ok


def fix_wav_header(path):
    """녹음 중에 꺼져서 WAV 머리말의 크기가 실제와 다르면 고침 (아니면 소리가 짧게/없게 읽힘) → 고쳤으면 True"""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return False
            # 'data' 덩어리 위치: 덩어리를 차례로 따라가고, 중간 크기가 깨져 있으면 앞부분에서 직접 찾음
            f.seek(0)
            first = f.read(65536)
            pos = 12
            while pos + 8 <= size:
                f.seek(pos)
                cid = f.read(4)
                csz = int.from_bytes(f.read(4), "little")
                if cid != b"data" and (len(cid) < 4 or not cid.isascii() or pos + 8 + csz > size):
                    k = first.find(b"data", 12)
                    if k < 0:
                        return False
                    pos = k
                    f.seek(pos + 4)
                    cid, csz = b"data", int.from_bytes(f.read(4), "little")
                if cid == b"data":
                    real = min(size - pos - 8, 0xFFFFFFFF)
                    changed = False
                    if csz != real:
                        f.seek(pos + 4)
                        f.write(real.to_bytes(4, "little"))
                        changed = True
                    riff = min(size - 8, 0xFFFFFFFF)
                    if int.from_bytes(head[4:8], "little") != riff:
                        f.seek(4)
                        f.write(riff.to_bytes(4, "little"))
                        changed = True
                    return changed
                pos += 8 + csz + (csz & 1)
    except OSError:
        pass
    return False


def mux_to_mp4(vid, wav, out, progress=None, reencode=False, note=None, stop=None):
    """녹화 임시 파일(영상 .mkv + 소리 .wav) → .mp4
    1) 영상은 그대로 복사, 소리만 AAC로  2) 실패하면 깨진 부분을 건너뛰며 다시
    3) reencode면 영상도 새로 인코딩  4) 그래도 안 되면 (소리가 있으면) 영상만이라도
    다 만든 뒤에만 out 이름으로 바꾸므로 중간에 끊겨도 깨진 out 파일이 생기지 않음.
    progress(처리한 초, 전체 초 또는 None)로 진행 상황 전달, note(list)에 쓴 방법을 남김
    → (성공 여부, 오류 내용, 마지막 명령)"""
    part = os.path.splitext(out)[0] + ".part.mp4"
    total = probe_media(vid)[1] if progress else None

    def build(tolerant, video_args, with_audio):
        tin = TOLERANT_IN if tolerant else []
        args = tin + ["-i", vid]
        if with_audio:
            args += tin + ["-i", wav, "-map", "0:v:0", "-map", "1:a:0"]
        # (+faststart는 큰 파일을 한 번 더 통째로 다시 쓰므로 사용하지 않음)
        args += video_args
        if with_audio:
            args += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        return args + [part]

    tries = [("", False, ["-c:v", "copy"], bool(wav)),
             ("깨진 부분 건너뛰고 합침", True, ["-c:v", "copy"], bool(wav))]
    if reencode:
        tries.append(("영상을 다시 인코딩해서 합침", True, REPAIR_VIDEO, bool(wav)))
    if wav:
        tries.append(("소리 파일이 깨져서 영상만 살림", True, ["-c:v", "copy"], False))
        if reencode:
            tries.append(("소리 파일이 깨져서 영상만 다시 인코딩", True, REPAIR_VIDEO, False))
    errs, args = [], []
    for how, tolerant, vargs, audio in tries:
        args = build(tolerant, vargs, audio)
        ok, err = run_ffmpeg(args, part, progress, total, stop)
        ok = _finish_part(ok, part, out)
        if ok:
            if note is not None and how:
                note.append(how)
            return True, "", [imageio_ffmpeg.get_ffmpeg_exe()] + args
        errs.append(f"[{how or '기본'}] {err.strip()}")
        if stop is not None and stop.is_set():
            break
    return False, "\n".join(errs), [imageio_ffmpeg.get_ffmpeg_exe()] + args


def repair_media(src, out, progress=None, reencode_first=False, note=None, stop=None):
    """열리지 않거나 손상된 영상을 다시 만듦 → (성공 여부, 오류 내용)
    1) 깨진 부분을 건너뛰며 그대로 복사  2) 영상·소리를 새로 인코딩 (손상된 프레임까지 정리)
    reencode_first: 재생 중 오류가 나는 파일은 복사로는 안 고쳐지므로 바로 2)"""
    part = os.path.splitext(out)[0] + ".part" + os.path.splitext(out)[1]
    total = probe_media(src)[1]
    copy = ("깨진 부분 건너뛰고 다시 저장", ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"])
    enc = ("다시 인코딩", ["-map", "0:v:0?", "-map", "0:a:0?"] + REPAIR_VIDEO + ["-c:a", "aac", "-b:a", "192k"])
    errs = []
    for how, margs in ([enc] if reencode_first else [copy, enc]):
        ok, err = run_ffmpeg(TOLERANT_IN + ["-i", src] + margs + ["-movflags", "+faststart", part],
                             part, progress, total, stop)
        if _finish_part(ok, part, out):
            if note is not None:
                note.append(how)
            return True, ""
        errs.append(f"[{how}] {err.strip()}")
        if stop is not None and stop.is_set():
            break
    return False, "\n".join(errs)


def audio_to_m4a(wav, out, progress=None, stop=None):
    """소리 파일만 남은 경우 → .m4a"""
    fix_wav_header(wav)
    part = os.path.splitext(out)[0] + ".part.m4a"
    ok, err = run_ffmpeg(TOLERANT_IN + ["-i", wav, "-c:a", "aac", "-b:a", "192k", part], part,
                         progress, probe_media(wav)[1], stop)
    return _finish_part(ok, part, out), err


def deep_check(path, stop=None, progress=None):
    """영상을 끝까지 읽어(디코딩) 손상된 곳이 있는지 → (오류 개수, 첫 오류 문구)"""
    total = probe_media(path)[1]
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-nostats", "-progress", "pipe:1",
           "-i", path, "-map", "0:v:0?", "-map", "0:a:0?", "-f", "null", "-"]
    lines = []
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             errors="replace", creationflags=NO_WINDOW | BELOW_NORMAL)
        t = threading.Thread(target=lambda: lines.extend(p.stderr.read().splitlines()), daemon=True)
        t.start()
        for line in p.stdout:
            if stop is not None and stop.is_set():
                p.kill()
                break
            if progress and line.startswith("out_time_us="):
                v = line.strip()[12:]
                if v.isdigit():
                    progress(int(v) / 1e6, total)
        p.wait()
        t.join(5)
    except Exception as ex:
        return 1, repr(ex)
    lines = [x for x in lines if x.strip()]
    return len(lines), (lines[0][:100] if lines else "")


# ---- 녹화 파일 이름
# 직접 녹화: 'capture_날짜_시각.tmp.mkv' + '.tmp.wav' → 'capture_날짜_시각.mp4'
# 자동 녹화: '제 1 강_제목(녹화중).mkv' + '(녹화중).wav' → 합치기 끝 '(원본).mp4' → 인코딩 끝 '제 1 강_제목.mp4'
REC_TAG, RAW_TAG = "(녹화중)", "(원본)"
NAME_SPACE = (("keep", "공백 그대로"), ("remove", "공백 제거"), ("under", "공백을 '_'로 채우기"))
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def rec_paths(base, auto=False):
    """녹화 base(확장자 없음) → (영상 임시 파일, 소리 임시 파일, 합친 MP4)"""
    if auto:
        return base + REC_TAG + ".mkv", base + REC_TAG + ".wav", base + RAW_TAG + ".mp4"
    return base + ".tmp.mkv", base + ".tmp.wav", base + ".mp4"


def lecture_stem(no, title, space="keep", mins=None):
    """강의 → 파일 이름 ('제 1 강_강의 제목', 꼬리표·확장자 없음)
    파일 이름에 쓸 수 없는 글자는 항상 빼고, 공백은 설정대로 (그대로 / 제거 / '_'), mins를 주면 '_48분'을 붙임"""
    title = " ".join(_BAD_NAME_CHARS.sub(" ", title or "").split())[:80].strip(" .")
    name = f"제 {no} 강" + (f"_{title}" if title else "") + (f"_{mins}분" if mins else "")
    if space == "remove":
        name = name.replace(" ", "")
    elif space == "under":
        name = name.replace(" ", "_")
    return name


def scan_lecture_files(out_dir, no, extra=()):
    """저장 폴더에서 이 강의(제 N 강)의 MP4를 찾음 (이름 형식이 바뀌었어도 '제 N 강'으로 시작하면 찾음)
    → (녹화 원본 [(경로, 정보)], 인코딩한 파일 [(경로, 정보)])  - 최근 파일이 앞. 다른 스레드에서 호출"""
    pat = re.compile(rf"^제[ _]*{int(no)}[ _]*강")
    cands = {}
    try:
        names = os.listdir(out_dir)
    except OSError:
        names = []
    for n in names:
        low = n.lower()
        if pat.match(n) and low.endswith(".mp4") and not low.endswith(".part.mp4"):
            p = os.path.normpath(os.path.join(out_dir, n))
            cands[os.path.normcase(p)] = p
    for p in extra:
        if p and os.path.isfile(p):
            cands[os.path.normcase(os.path.normpath(p))] = os.path.normpath(p)
    raw, enc = [], []
    for p in cands.values():
        info = media_info(p)
        name = os.path.basename(p)
        if name.endswith(RAW_TAG + ".mp4"):
            raw.append((p, info))
        elif is_encoded(info) or re.search(r"_small\d*\.mp4$", name, re.I):
            enc.append((p, info))
        else:
            raw.append((p, info))  # 예전 이름('제 1 강.mp4')의 녹화 원본

    def newest(lst):
        return sorted(lst, key=lambda x: os.path.getmtime(x[0]) if os.path.exists(x[0]) else 0, reverse=True)
    return newest(raw), newest(enc)


def unique_base(out_dir, name, auto=False):
    """out_dir/name (확장자 없음). 같은 이름의 녹화·임시 파일이 있으면 'name (2)', 'name (3)'..."""
    def taken(b):
        vid, wav, out = rec_paths(b, auto)
        return any(os.path.exists(p) for p in (vid, wav, out, os.path.splitext(out)[0] + ".part.mp4", b + ".mp4"))
    base = os.path.join(out_dir, name)
    n = 2
    while taken(base):
        base = os.path.join(out_dir, f"{name} ({n})")
        n += 1
    return base


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
        self.out_device = None  # 소리 듣기 출력 장치 (None = Windows 기본)
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
                    device=self.out_device, samplerate=self.sr, channels=self.ch, dtype="int16",
                    callback=self._out_cb)
                self.out.start()
                if self.error.startswith("모니터링"):
                    self.error = ""
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

    def set_out_device(self, dev):
        """소리 듣기 출력 장치 바꾸기 (듣는 중이면 새 장치로 다시 열기)"""
        self.out_device = dev
        if self.monitor:
            self.set_monitor(False)
            self.set_monitor(True)

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
        self.out_part = None        # 지금 저장 중인 임시 파일 (복구 탭이 건드리지 않도록)
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
        self.btn_save = ttk.Button(b, text="✂  잘라서 저장", style="Big.TButton", command=self.save)
        self.btn_save.pack(side="left")
        # 자르면서 '용량 줄이기' 탭의 설정(화질/해상도/프레임)으로 한 번에 인코딩
        self.var_compress = tk.BooleanVar(value=True)
        ttk.Checkbutton(b, text="용량 줄이기 설정으로 저장", variable=self.var_compress).pack(side="left", padx=(8, 0))
        self.btn_send = ttk.Button(b, text="인코딩 탭으로 보내기", command=self._send)
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
        # 다 만든 뒤에만 out 이름으로 바꿈 (중간에 끊기거나 실패해도 깨진 _trim 파일이 남지 않음)
        part = os.path.splitext(out)[0] + ".part" + ext
        self.out_part = part
        cmd += ["-movflags", "+faststart", part]
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, creationflags=flags)
        self.btn_save.config(state="disabled")
        self.lbl_status.config(text="저장 중...")
        threading.Thread(target=self._wait_save, args=(self.proc, length, out, part), daemon=True).start()

    def _wait_save(self, p, length, out, part):
        def ui(fn, *args):  # 창이 이미 닫혔으면 무시
            try:
                self.after(0, fn, *args)
            except (RuntimeError, tk.TclError):
                pass
        err = []
        t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
        t.start()
        for line in p.stdout:
            if line.startswith("out_time_us=") and line.strip()[12:].isdigit():
                pct = min(100, int(line.strip()[12:]) / 1000 / length * 100)
                ui(self.prog.config, {"value": pct})
        ok = p.wait() == 0 and os.path.exists(part) and probe_media(part)[0]
        t.join(5)
        try:
            if ok:
                os.replace(part, out)
            elif os.path.exists(part):
                os.remove(part)
        except OSError as ex:
            ok = False
            err.append(str(ex))
        self.out_part = None
        ui(self._save_done, ok, out, "".join(err))

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
    """'복구' 탭: 녹화·자르기·인코딩 중에 끊기거나 실패해서 생긴 파일 문제를 찾아서 고침
    - 녹화 합치기 안 됨: 영상(.tmp.mkv / (녹화중).mkv)과 소리(.wav)가 따로 남음 → MP4로 합침
    - 소리 파일만 남음 → .m4a로
    - 만들다 끊긴 파일(.part.mp4): 합치기·자르기·인코딩 중에 꺼짐 → 살릴 수 있으면 살리고, 아니면 정리
    - 열리지 않는 영상 / (정밀 검사) 재생 중 오류가 나는 영상 → 다시 만듦"""
    COLS = (("file", "파일", 280), ("kind", "문제", 170), ("time", "시각", 130), ("len", "길이", 70),
            ("size", "크기", 80), ("state", "상태", 280))
    KINDS = {"merge": "녹화 합치기 안 됨", "audio": "소리 파일만 남음", "part": "만들다 끊긴 파일",
             "broken": "열리지 않는 영상", "damaged": "재생 중 오류 (손상)"}
    BUSY_SECS = 30  # 최근 이 시간 안에 바뀐 파일은 다른 작업이 쓰는 중일 수 있어 건드리지 않음
    VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".m4a")

    def __init__(self, master, get_dir, get_active, on_idle):
        super().__init__(master)
        self.get_dir = get_dir        # 기본 폴더 (캡쳐 저장 폴더)
        self.get_active = get_active  # 지금 녹화·변환·자르기·인코딩 중인 파일 (normcase 경로 집합)
        self.on_idle = on_idle        # 복구 작업이 모두 끝났을 때 호출
        self.busy = False
        self.scan_id = 0
        self.items = {}               # 표 행 → {kind, src, vid, wav, out, base}
        self.stop_flag = threading.Event()
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
        self.btn_refresh = ttk.Button(top, text="문제 찾기", command=self.refresh)
        self.btn_refresh.pack(side="left", padx=4)
        self.btn_deep = ttk.Button(top, text="정밀 검사", command=self.deep_scan)
        self.btn_deep.pack(side="left")
        ttk.Button(top, text="폴더 열기", command=self.open_dir).pack(side="left", padx=4)

        ttk.Label(self, foreground="#555", justify="left", text=(
            "녹화·자르기·인코딩 중에 프로그램이 꺼지거나 실패해서 생긴 문제를 찾아서 고쳐요.\n"
            "· 녹화 합치기 안 됨: 영상(.tmp.mkv / (녹화중).mkv)과 소리(.wav)가 따로 남음 → 하나의 MP4로 합침 "
            "(소리 파일 머리말이 깨졌으면 고치고, 안 되면 깨진 부분을 건너뛰거나 다시 인코딩)\n"
            "· 만들다 끊긴 파일(.part.mp4): 살릴 수 있으면 살리고, 살릴 수 없거나 완성된 파일이 이미 있으면 휴지통으로\n"
            "· 열리지 않는 영상 · 재생 중 오류(정밀 검사: 영상을 끝까지 읽어 확인) → '_복구' 파일로 다시 만듦")
        ).pack(fill="x", padx=8, pady=(0, 6))

        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(box, columns=[c[0] for c in self.COLS], show="headings", height=14)
        for key, title, w in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, anchor="w" if key in ("file", "state", "kind") else "center",
                             stretch=key in ("file", "state"))
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.recover_selected())
        TreeTip(self.tree, ("file", "state"))

        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=8)
        self.btn_sel = ttk.Button(bot, text="선택 항목 복구", style="Big.TButton", command=self.recover_selected)
        self.btn_sel.pack(side="left")
        self.btn_all = ttk.Button(bot, text="전체 복구", style="Big.TButton", command=self.recover_all)
        self.btn_all.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(bot, text="중지", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")
        self.var_del = tk.BooleanVar(value=True)
        self.chk_del = ttk.Checkbutton(
            bot, variable=self.var_del,
            text="복구에 성공하면 남은 임시 파일·손상된 원본은 휴지통으로 (복구한 파일이 원래 이름을 이어받아요)")
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

    def _set(self, iid, col, text):
        if self.tree.exists(iid):
            self.tree.set(iid, col, text)

    def _set_buttons(self, state):
        for w in (self.btn_sel, self.btn_all, self.btn_refresh, self.btn_deep, self.btn_dir, self.chk_del):
            w.config(state=state)
        self.btn_stop.config(state="normal" if state == "disabled" else "disabled")

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

    # ---- 문제 찾기
    def _in_use(self, path, active):
        """다른 작업이 지금 쓰고 있는 파일인지 (작업 중인 파일 목록 + 최근에 바뀐 파일)"""
        if os.path.normcase(os.path.normpath(path)) in active:
            return True
        try:
            return time.time() - os.path.getmtime(path) < self.BUSY_SECS
        except OSError:
            return True

    @staticmethod
    def _when(path, base=""):
        m = re.search(r"(\d{8})_(\d{6})$", base)
        if m:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        try:
            return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            return ""

    def _add(self, item, size, state="복구 대기"):
        src = item["src"]
        if self.tree.exists(src):
            return
        self.items[src] = item
        self.tree.insert("", "end", iid=src, values=(
            os.path.basename(src), self.KINDS[item["kind"]], self._when(src, item.get("base", "")),
            "확인 중..." if item["kind"] != "damaged" else fmt_secs(item.get("dur") or 0),
            fmt_size(size), state))

    def refresh(self):
        if self.busy:
            return
        self.scan_id += 1
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.prog.config(value=0)
        d = self.var_dir.get().strip()
        if not os.path.isdir(d):
            self.lbl_status.config(text="폴더를 찾을 수 없어요.")
            return
        self.lbl_status.config(text="문제를 찾는 중...")
        self.btn_refresh.config(state="disabled")
        threading.Thread(target=self._scan, args=(self.scan_id, d, self.get_active()), daemon=True).start()

    def _scan(self, scan_id, d, active):
        """폴더의 파일을 보고 문제 목록을 만듦 (영상을 여는 확인이 있어 별도 스레드)"""
        try:
            names = sorted(os.listdir(d), reverse=True)  # 최근 녹화가 위로
        except OSError:
            names = []
        found, skipped, used = [], 0, set()

        def full(n):
            return os.path.join(d, n)
        # 1) 합치지 못한 녹화 (영상 + 소리)
        for n in names:
            if n.lower().endswith(".tmp.mkv"):
                base, auto = full(n[:-len(".tmp.mkv")]), False
            elif n.endswith(REC_TAG + ".mkv"):
                base, auto = full(n[:-len(REC_TAG + ".mkv")]), True
            else:
                continue
            vid, wav, mp4 = rec_paths(base, auto)
            used.update({os.path.normcase(vid), os.path.normcase(wav)})
            if self._in_use(vid, active) or (os.path.exists(wav) and self._in_use(wav, active)):
                skipped += 1
                continue
            size = os.path.getsize(vid) + (os.path.getsize(wav) if os.path.exists(wav) else 0)
            found.append(({"kind": "merge", "src": vid, "vid": vid, "wav": wav if os.path.exists(wav) else None,
                           "out": mp4, "base": base}, size))
        # 2) 소리 파일만 남음
        for n in names:
            low = n.lower()
            if not (low.endswith(".tmp.wav") or n.endswith(REC_TAG + ".wav")):
                continue
            p = full(n)
            if os.path.normcase(p) in used:
                continue
            if self._in_use(p, active):
                skipped += 1
                continue
            base = p[:-len(".tmp.wav")] if low.endswith(".tmp.wav") else p[:-len(REC_TAG + ".wav")]
            found.append(({"kind": "audio", "src": p, "wav": p, "out": base + "(소리).m4a", "base": base},
                          os.path.getsize(p)))
        # 3) 만들다 끊긴 파일 (.part.mp4 / .part.m4a)
        for n in names:
            low = n.lower()
            if not (low.endswith(".part.mp4") or low.endswith(".part.m4a")):
                continue
            p = full(n)
            if self._in_use(p, active):
                skipped += 1
                continue
            ext = os.path.splitext(n)[1]
            final = p[:-len(".part" + ext)] + ext
            found.append(({"kind": "part", "src": p, "out": final}, os.path.getsize(p)))
        # 4) 열리지 않는 영상
        videos = [full(n) for n in names
                  if n.lower().endswith(self.VIDEO_EXT) and ".part." not in n.lower()
                  and os.path.normcase(full(n)) not in used and not n.lower().endswith(".tmp.mkv")
                  and not n.endswith(REC_TAG + ".mkv")]
        for k, p in enumerate(videos):
            if scan_id != self.scan_id:
                return
            self._ui(self.lbl_status.config, {"text": f"문제를 찾는 중... 영상 확인 ({k + 1}/{len(videos)})"})
            if self._in_use(p, active):
                continue
            info = media_info(p)
            if not info["ok"] or not info["dur"]:
                found.append(({"kind": "broken", "src": p, "out": p}, os.path.getsize(p)))
        self._ui(self._scan_done, scan_id, found, skipped)
        # 길이 표시 (같은 파일이면 답도 같으므로 다른 작업이 시작돼도 끝까지 채움)
        for item, _ in found:
            src = item.get("vid") or item["src"]
            dur = probe_media(src)[1]
            self._ui(self._set, item["src"], "len", fmt_secs(dur) if dur else "알 수 없음")

    def _scan_done(self, scan_id, found, skipped):
        if scan_id != self.scan_id:
            return
        self.btn_refresh.config(state="normal")
        for item, size in found:
            self._add(item, size)
        text = f"고칠 수 있는 문제 {len(found)}개" if found else "문제가 있는 파일을 찾지 못했어요."
        if skipped:
            text += f"  ·  지금 쓰고 있는 파일 {skipped}개는 건너뜀 (작업이 끝난 뒤 다시 '문제 찾기')"
        if not found:
            text += "  재생할 때 깨지는 영상은 '정밀 검사'로 찾을 수 있어요."
        self.lbl_status.config(text=text)

    def deep_scan(self):
        """정밀 검사: 폴더의 영상을 끝까지 읽어 재생 중 오류(손상)가 있는 파일을 찾음 (시간이 걸림)"""
        if self.busy:
            return
        d = self.var_dir.get().strip()
        if not os.path.isdir(d):
            self.lbl_status.config(text="폴더를 찾을 수 없어요.")
            return
        sel = [i for i in self.tree.selection() if self.items.get(i, {}).get("kind") in ("broken", "damaged")]
        active = self.get_active()
        files = sel or [os.path.join(d, n) for n in sorted(os.listdir(d))
                        if n.lower().endswith(self.VIDEO_EXT) and ".part." not in n.lower()
                        and not n.lower().endswith(".tmp.mkv") and not n.endswith(REC_TAG + ".mkv")
                        and not self.tree.exists(os.path.join(d, n))]
        files = [f for f in files if not self._in_use(f, active)]
        if not files:
            self.lbl_status.config(text="검사할 영상이 없어요.")
            return
        if not messagebox.askyesno("정밀 검사", f"영상 {len(files)}개를 끝까지 읽어서 손상된 곳이 있는지 확인할까요?\n"
                                              "(영상 길이에 따라 오래 걸릴 수 있어요. '중지'로 멈출 수 있어요)",
                                   parent=self):
            return
        self.busy = True
        self.stop_flag.clear()
        self.scan_id += 1
        self._set_buttons("disabled")
        threading.Thread(target=self._deep_run, args=(files,), daemon=True).start()

    def _deep_run(self, files):
        n, bad = len(files), 0
        for i, p in enumerate(files):
            if self.stop_flag.is_set():
                break
            name = os.path.basename(p)

            def progress(sec, total, i=i, name=name):
                pct = min(100.0, sec / total * 100) if total else 0
                self._ui(self.prog.config, {"value": (i + pct / 100) / n * 100})
                self._ui(self.lbl_status.config, {"text": f"정밀 검사 중 ({i + 1}/{n}): {name}  {pct:.0f}%"})
            errors, first = deep_check(p, self.stop_flag, progress)
            if self.stop_flag.is_set():
                break
            if errors:
                bad += 1
                info = media_info(p)
                item = {"kind": "damaged" if info["ok"] else "broken", "src": p, "out": p, "dur": info["dur"]}
                self._ui(self._deep_found, item, os.path.getsize(p), f"오류 {errors}곳: {first}")
            elif self.tree.exists(p):
                self._ui(self._set, p, "state", "정밀 검사 - 이상 없음")
        self._ui(self._deep_done, n, bad)

    def _deep_found(self, item, size, state):
        if self.tree.exists(item["src"]):
            self.items[item["src"]] = item
            self.tree.set(item["src"], "kind", self.KINDS[item["kind"]])
            self.tree.set(item["src"], "state", state)
        else:
            self._add(item, size, state)

    def _deep_done(self, n, bad):
        self.busy = False
        self._set_buttons("normal")
        self.prog.config(value=0 if self.stop_flag.is_set() else 100)
        self.lbl_status.config(text=("정밀 검사를 멈췄어요." if self.stop_flag.is_set() else f"정밀 검사 끝 - {n}개 중 ")
                               + (f"손상된 영상 {bad}개 (목록에 추가)" if bad else "손상된 영상 없음"))
        self.on_idle()

    # ---- 복구
    def recover_selected(self):
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("복구", "목록에서 복구할 항목을 골라 주세요.", parent=self)
            return
        self._start(sel)

    def recover_all(self):
        self._start(list(self.tree.get_children()))

    def stop(self):
        self.stop_flag.set()
        self.lbl_status.config(text="멈추는 중...")

    def _start(self, iids):
        if self.busy or not iids:
            return
        active = self.get_active()
        iids = [i for i in iids if i in self.items]
        busy_now = [i for i in iids if self._in_use(i, active)]
        if busy_now:
            for i in busy_now:
                self._set(i, "state", "지금 쓰고 있는 파일이라 건너뜀")
            iids = [i for i in iids if i not in busy_now]
        if not iids:
            return
        self.busy = True
        self.stop_flag.clear()
        self.scan_id += 1  # 진행 중인 확인 작업은 멈춤
        self._set_buttons("disabled")
        threading.Thread(target=self._run, args=(iids, self.var_del.get()), daemon=True).start()

    @staticmethod
    def _free_name(path, tag="_복구"):
        stem, ext = os.path.splitext(path)
        out, k = stem + tag + ext, 2
        while os.path.exists(out):
            out, k = f"{stem}{tag}{k}{ext}", k + 1
        return out

    @staticmethod
    def _trash(*paths):
        """휴지통으로 (안 되면 그대로 둠) → 모두 보냈으면 True"""
        ok = True
        for p in paths:
            if p and os.path.exists(p) and not to_recycle_bin(p):
                ok = False
        return ok

    def _run(self, iids, cleanup):
        done = 0
        n = len(iids)
        for i, iid in enumerate(iids):
            if self.stop_flag.is_set():
                break
            item = self.items[iid]
            name = os.path.basename(item["src"])
            self._ui(self._set, iid, "state", "복구 중...")
            self._ui(self.lbl_status.config, {"text": f"복구 중 ({i + 1}/{n}): {name}"})

            def progress(sec, total, iid=iid, i=i):
                if total:
                    pct = min(100.0, sec / total * 100)
                    self._ui(self._set, iid, "state", f"복구 중... {pct:.0f}%")
                    self._ui(self.prog.config, {"value": (i + pct / 100) / n * 100})
                else:
                    self._ui(self._set, iid, "state", f"복구 중... {fmt_secs(sec)}")
            try:
                ok, state = getattr(self, "_fix_" + item["kind"])(item, progress, cleanup)
            except Exception as ex:
                ok, state = False, f"실패: {ex}"
            if self.stop_flag.is_set() and not ok:
                state = "중지됨"
            done += bool(ok)
            self._ui(self._set, iid, "state", state)
        self._ui(self._finish, done, n)

    def _fix_merge(self, item, progress, cleanup):
        vid, wav, out, base = item["vid"], item["wav"], item["out"], item["base"]
        if not os.path.exists(vid):
            return False, "영상 파일이 없어요"
        fixed = bool(wav) and fix_wav_header(wav)
        if os.path.exists(out) and probe_media(out)[0]:  # 정상 MP4는 덮어쓰지 않음
            out = self._free_name(out)
        note = ["소리 파일 머리말 고침"] if fixed else []
        ok, err, cmd = mux_to_mp4(vid, wav, out, progress, reencode=True, note=note, stop=self.stop_flag)
        if not ok:
            write_mux_log(base, cmd, err)
            return False, f"실패 (원인: {os.path.basename(base)}.log)"
        if cleanup:
            self._trash(vid, wav)
            try:
                os.remove(base + ".log")
            except OSError:
                pass
        return True, f"완료 → {os.path.basename(out)}" + (f" ({', '.join(note)})" if note else "")

    def _fix_audio(self, item, progress, cleanup):
        out = item["out"] if not os.path.exists(item["out"]) else self._free_name(item["out"], "")
        ok, err = audio_to_m4a(item["wav"], out, progress, self.stop_flag)
        if not ok:
            return False, f"실패: {' '.join(err.split())[:80]}"
        if cleanup:
            self._trash(item["wav"])
        return True, f"완료 → {os.path.basename(out)}"

    def _fix_part(self, item, progress, cleanup):
        part, final = item["src"], item["out"]
        if os.path.exists(final) and probe_media(final)[0]:  # 완성된 파일이 이미 있음 → 남은 조각일 뿐
            if cleanup and self._trash(part):
                return True, f"완성된 파일({os.path.basename(final)})이 있어서 휴지통으로 정리했어요"
            return True, f"완성된 파일({os.path.basename(final)})이 있어요 - 이 파일은 지워도 돼요"
        out = final if not os.path.exists(final) else self._free_name(final)
        note = []
        ok, _ = repair_media(part, out, progress, note=note, stop=self.stop_flag)
        if ok:
            if cleanup:
                self._trash(part)
            return True, f"살렸어요 → {os.path.basename(out)} ({', '.join(note)})"
        if self.stop_flag.is_set():
            return False, "중지됨"
        if cleanup and self._trash(part):
            return False, "끝까지 만들기 전에 끊겨서 살릴 수 없어요 → 휴지통으로 (원본으로 다시 작업해 주세요)"
        return False, "끝까지 만들기 전에 끊겨서 살릴 수 없어요 (원본으로 다시 작업해 주세요)"

    def _fix_broken(self, item, progress, cleanup, reencode_first=False):
        src = item["src"]
        out = os.path.splitext(self._free_name(src))[0] + ".mp4"
        note = []
        ok, err = repair_media(src, out, progress, reencode_first=reencode_first, note=note, stop=self.stop_flag)
        if not ok:
            if self.stop_flag.is_set():
                return False, "중지됨"
            msg = "영상 정보가 없어서 살릴 수 없어요" if not probe_media(src)[0] else \
                " ".join(err.split())[-80:] or "알 수 없는 오류"
            return False, f"실패: {msg}"
        state = f"완료 → {os.path.basename(out)} ({', '.join(note)})"
        if cleanup:  # 손상된 원본은 휴지통으로, 복구한 파일이 원래 이름을 이어받음 (확장자는 .mp4)
            target = os.path.splitext(src)[0] + ".mp4"
            if self._trash(src) and not os.path.exists(target):
                os.replace(out, target)
                state = f"완료 → {os.path.basename(target)} ({', '.join(note)}, 손상된 원본은 휴지통으로)"
        return True, state

    def _fix_damaged(self, item, progress, cleanup):
        return self._fix_broken(item, progress, cleanup, reencode_first=True)

    def _finish(self, done, n):
        self.busy = False
        self._set_buttons("normal")
        self.prog.config(value=100 if done == n else self.prog["value"])
        stopped = self.stop_flag.is_set()
        self.lbl_status.config(text=("복구를 멈췄어요: " if stopped else "복구 끝: ") + f"{done}개 성공"
                               + (f", {n - done}개 실패·남음" if done < n else "")
                               + "  ·  다시 확인하려면 '문제 찾기'")
        self.on_idle()

    def close(self):
        """프로그램 종료 시 호출. 복구 중이면 멈춤"""
        if self.busy:
            self.stop_flag.set()
        return True


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
        self.current_part = None  # 지금 만들고 있는 .part.mp4
        self.removed = set()    # 인코딩 중 목록에서 뺀 항목 (건너뜀)
        # 자동 녹화에서 보낸 파일 → 설정을 돌려주는 함수 () -> (설정, 끝나면 원본 삭제 여부, 만들 파일 경로)
        # (그 파일을 시작할 때 불러서, 작업 중에 바꾼 설정은 다음 파일부터 적용)
        self.auto_items = {}
        self.on_auto_done = None      # 자동 항목이 끝날 때 (경로, 결과 ok/fail/stopped/missing, 만든 파일, 문구, 속도)
        self.on_auto_progress = None  # 자동 항목 진행 (경로, 진행률 %, 남은 초 또는 None)
        self.paused = threading.Event()  # 일시중지 중 (다음 파일도 시작하지 않음)
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

        o = ttk.LabelFrame(self, text="설정", style="Section.TLabelframe", padding=(10, 4, 10, 8))
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
        self.btn_start = ttk.Button(bot, text="▶  용량 줄이기 시작", style="Big.TButton", command=self.start)
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

    def _main(self, fn):
        """작업 스레드에서 메인 스레드 함수를 실행하고 결과를 기다림"""
        box, ev = [], threading.Event()

        def run():
            try:
                box.append(fn())
            finally:
                ev.set()
        self._ui(run)
        ev.wait(30)
        return box[0] if box else None

    def _auto_progress(self, src, pct, left):
        if src in self.auto_items and self.on_auto_progress:
            self.on_auto_progress(src, pct, left)

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

    def enqueue(self, items):
        """자동 녹화가 보낸 파일 [(경로, 설정 함수)]을 줄이기 (인코딩 중이면 지금 작업이 끝난 뒤 이어서)"""
        paths = []
        for p, getter in items:
            p = os.path.normpath(p)
            if not os.path.isfile(p):
                continue
            self.auto_items[p] = getter
            self.removed.discard(p)
            if self.tree.exists(p) and not (self.busy and p == self.current):
                self.tree.set(p, "state", "대기")  # 전에 중지·실패한 항목도 다시
            paths.append(p)
        self.add_files(paths)
        if not self.busy:
            self.start(only_waiting=True)

    def drop_auto(self):
        """자동 녹화가 보낸 것 중 아직 시작하지 않은 파일을 목록에서 뺌 (작업 중지) → 뺀 경로(normcase) 집합"""
        dropped = set()
        for p in list(self.auto_items):
            if self.busy and p == self.current:
                continue
            self.auto_items.pop(p, None)
            self.removed.add(p)
            dropped.add(os.path.normcase(p))
            if self.tree.exists(p):
                self.tree.delete(p)
        return dropped

    def _probe(self, paths):
        for p in paths:
            info = media_info(p)
            self.info[p] = info
            self._ui(self._set, p, "len", fmt_secs(info["dur"]) if info["dur"] else "알 수 없음")
            if not info["ok"] and p not in self.auto_items:  # 자동 항목은 인코딩하면서 실패로 알림
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
    def start(self, only_waiting=False):
        if self.busy:
            return
        if only_waiting:  # 자동: 자동 녹화가 보낸 것 중 아직 시작 안 한 항목만 (실패한 항목은 다시 안 함)
            todo = [i for i in self.tree.get_children()
                    if i in self.auto_items and self.tree.set(i, "state") == "대기"]
            if not todo:
                return
        else:
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
            while self.paused.is_set() and not self.stop_flag.is_set():
                time.sleep(0.3)
            if self.stop_flag.is_set():
                break
            if src in self.removed:
                continue
            if not os.path.isfile(src):
                self._ui(self._set, src, "state", "파일이 없어요")
                if src in self.auto_items:
                    self._ui(self._auto_done, src, "missing")
                failed += 1
                continue
            self.current = src
            info = self.info.get(src) or media_info(src)
            self.info[src] = info
            getter = self.auto_items.get(src)
            job = self._main(getter) if getter else None
            item_opts, delete_src, want = job or (opts, False, None)
            # 자동 녹화: '제 1 강_제목(원본).mp4' → '제 1 강_제목.mp4' (그 이름이 이미 있으면 '_small')
            out = want if (want and os.path.normcase(want) != os.path.normcase(src)
                           and not os.path.exists(want)) else self._out_path(src)
            part = os.path.splitext(out)[0] + ".part.mp4"
            self.current_part = part
            cmd = ([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-progress", "pipe:1",
                    "-i", src, "-map", "0:v:0", "-map", "0:a?"]
                   + compress_args(*item_opts, info) + ["-movflags", "+faststart", part])
            name = os.path.basename(src)
            self._ui(self.lbl_status.config, {"text": f"줄이는 중 ({i + 1}/{n}): {name}"})
            self._ui(self._set, src, "state", "인코딩 중...")
            self._ui(self._auto_progress, src, 0.0, None)
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
                            self._ui(self._auto_progress, src, pct, left)
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
                if src in self.auto_items:
                    self._ui(self._auto_done, src, "stopped")
                break
            if ok:
                done += 1
                a, b = os.path.getsize(src), os.path.getsize(out)
                before, after = before + a, after + b
                self._ui(self._set, src, "out", f"{fmt_size(b)} ({b / a * 100:.0f}%)")
                state = f"완료 → {os.path.basename(out)}"
                if delete_src:  # 원본은 휴지통으로
                    if to_recycle_bin(src):
                        # 원하던 이름이 원본과 같았거나 비어 있으면 그 이름을 이어받음
                        target = want or src
                        if os.path.normcase(target) != os.path.normcase(out) and not os.path.exists(target):
                            os.replace(out, target)
                            out = target
                        state = f"완료 → {os.path.basename(out)} (원본은 휴지통으로)"
                    else:
                        state += " (원본 삭제 실패)"
                self._ui(self._set, src, "state", state)
                if src in self.auto_items:
                    speed = (time.time() - t0) / info["dur"] if info["dur"] else None
                    self._ui(self._auto_done, src, "ok", out, state, speed)
            else:
                failed += 1
                msg = " ".join("".join(err).split())[:80] or "알 수 없는 오류"
                self._ui(self._set, src, "state", f"실패: {msg}")
                if src in self.auto_items:
                    self._ui(self._auto_done, src, "fail", None, msg)
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
        if not self.stop_flag.is_set():  # 인코딩 중에 자동 녹화가 보낸 파일이 있으면 이어서
            self.after(500, self.start, True)

    def _auto_done(self, src, result, out=None, msg="", speed=None):
        if self.auto_items.pop(src, None) is None:
            return
        if self.on_auto_done:
            self.on_auto_done(src, result, out, msg, speed)

    def pause(self, on):
        """용량 줄이기 일시중지/다시 시작 (지금 인코딩 중인 ffmpeg도 멈춤)"""
        if on:
            self.paused.set()
            suspend_process(self.proc, True)
            if self.busy:
                self._prev_status = self.lbl_status.cget("text")
                self.lbl_status.config(text="일시중지 - 상태 체크가 끝나면 이어서 줄여요")
        else:
            suspend_process(self.proc, False)
            self.paused.clear()
            if self.busy and getattr(self, "_prev_status", None):
                self.lbl_status.config(text=self._prev_status)

    def stop(self):
        if not self.busy:
            return
        self.pause(False)
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
class TreeTip:
    """표 칸에 마우스를 올리면 그 칸의 글자 전체를 툴팁으로 보여줌 (잘려 보이는 긴 상태·제목용)"""

    def __init__(self, tree, cols):
        self.tree, self.cols = tree, cols
        self.tip = None
        self.cell = None
        self.job = None
        tree.bind("<Motion>", self._move, add="+")
        tree.bind("<Leave>", lambda e: self._hide(), add="+")
        tree.bind("<ButtonPress>", lambda e: self._hide(), add="+")

    def _move(self, ev):
        row, col = self.tree.identify_row(ev.y), self.tree.identify_column(ev.x)
        cell = (row, col)
        if cell == self.cell:
            return
        self._hide()
        self.cell = cell
        if not row or not col:
            return
        key = self.tree["columns"][int(col[1:]) - 1]
        if key in self.cols:
            text = str(self.tree.set(row, key))
            if text:
                self.job = self.tree.after(350, self._show, text, ev.x_root, ev.y_root)

    def _show(self, text, x, y):
        self.job = None
        self.tip = t = tk.Toplevel(self.tree)
        t.wm_overrideredirect(True)
        t.attributes("-topmost", True)
        tk.Label(t, text=text, background="#ffffe1", foreground="#222", relief="solid", borderwidth=1,
                 padx=6, pady=3, justify="left", wraplength=520).pack()
        t.update_idletasks()
        x = min(x + 12, t.winfo_screenwidth() - t.winfo_reqwidth() - 4)
        t.geometry(f"+{max(0, x)}+{y + 18}")

    def _hide(self):
        if self.job:
            self.tree.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None
        self.cell = None


class NoAuto:
    """직접 녹화 모드: 자동 녹화가 없음 (App이 자동 녹화 상태를 물어볼 때 쓰는 빈 객체)"""
    running = False
    runner = None
    stopped = False

    def save(self):
        pass

    def on_saved(self, path, ok=True):
        pass

    def stop(self):
        pass

    def _encoded(self, *args, **kw):
        pass


# ---- 자동 녹화 강의 표: 작업·상태
TASKS = ("녹화", "인코딩", "녹화 및 인코딩", "건너뛰기")
DEFAULT_TASK = "녹화 및 인코딩"
# 상태 변경 콤보박스 (녹화 중·인코딩 중은 작업이 진행되면 저절로 바뀜)
STATE_CHOICES = (("녹화 대기", "rec_wait"), ("녹화 중", "rec"), ("녹화 완료", "rec_done"),
                 ("인코딩 대기", "enc_wait"), ("인코딩 중", "enc"), ("인코딩 완료", "enc_done"),
                 ("건너뛰기", "skip"))
STATE_FIXED = {
    "rec_wait": "녹화 대기", "rec_done": "녹화 완료", "enc_wait": "인코딩 대기", "enc_done": "인코딩 완료",
    "skip": "사용자가 해당 강의의 작업을 진행하지 않도록 상태를 변경했습니다.",
    "missing": "파일이 존재하지 않습니다.",
    "rec_stopped": "사용자가 녹화 도중 작업을 중지했습니다.",
    "enc_stopped": "사용자가 인코딩 도중 작업을 중지했습니다.",
}
ALL_STATES = set(STATE_FIXED) | {"rec", "enc", "rec_fail", "enc_fail"}
REC_NEED = ("rec_wait", "rec_stopped", "rec_fail", "missing")    # 작업에 '녹화'가 있으면 녹화할 상태
ENC_NEED = ("rec_done", "enc_wait", "enc_stopped", "enc_fail")   # 작업에 '인코딩'이 있으면 인코딩할 상태
# 녹화 뒤 인코딩 진행 방식 (작업 도중 바꾸면 다음 파일부터 적용)
FLOWS = (("each", "녹화 완료 후 바로 인코딩"), ("all", "모든 녹화가 완료된 후 인코딩"), ("rec", "녹화만 진행"))
LECTURE_OVERHEAD = 30  # 강의마다 창 열기·전체화면·저장에 드는 시간 (초, 남은 시간 계산용)


def has_rec(task):
    return task in ("녹화", "녹화 및 인코딩")


def has_enc(task):
    return task in ("인코딩", "녹화 및 인코딩")


def fmt_hm(secs):
    secs = max(0, int(secs))
    h, m = secs // 3600, (secs % 3600 + 59) // 60
    if m == 60:
        h, m = h + 1, 0
    return f"{h}시간 {m}분" if h else f"{m}분"


def fmt_clock(t, now):
    days = (t.date() - now.date()).days
    day = "" if days <= 0 else "내일 " if days == 1 else f"{t:%m/%d} "
    return f"{day}{t:%H:%M}"


class AutoPanel(ttk.Frame):
    """온라인 강의 연속 자동 녹화 화면: 위 안내 줄 / 왼쪽 미리보기·장치·파일 이름·인코딩 설정 / 오른쪽 강의 표·작업"""

    COLS = (("no", "일련번호", 64), ("title", "강의 제목", 200), ("len", "길이", 84),
            ("task", "작업", 100), ("state", "상태", 300))

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.runner = None     # 녹화하는 AutoRecorder (녹화가 끝나면 None, 인코딩은 계속될 수 있음)
        self.session = False   # '작업 시작'부터 녹화·인코딩이 모두 끝날 때까지
        self.rows = {}         # 표 행 → {idx, no, title, mins, st, note, task, len, raw, enc, pct, left}
        self.run_rows = []     # 이번 실행에서 녹화할 표 행 (AutoRecorder 순번 → 행)
        self.muxed = set()     # MP4 합치기가 끝난 파일 (normcase)
        self.completed = set() # 끝까지 녹화를 마친 표 행 (합치기도 끝나야 확인 → 인코딩)
        self.enc_sent = {}     # 인코딩으로 보낸 표 행 → 원본 경로(normcase)
        self.verifying = 0     # 파일 확인 중인 개수
        self.cur_row = None    # 지금 녹화 중인 표 행
        self.stopped = False   # 사용자가 '중지'를 눌렀는지 (그러면 컴퓨터를 끄지 않음)
        self.counts = {}       # 이번 작업 결과 (녹화 완료/인코딩 완료/실패 개수)
        self.renaming = set()  # 파일 이름을 바꾸는 중인 표 행
        self._build()
        self._load_rows()
        self.after(1500, self._startup_check)
        self.after(1000, self._tick)

    @property
    def running(self):
        """App이 묻는 '자동 작업 중' (녹화 또는 인코딩)"""
        return self.session

    # ------------------------------------------------------------ 화면
    def _build(self):
        cfg = self.app.cfg
        # 위: 순서 안내
        top = ttk.Frame(self, padding=(10, 10, 10, 0))
        top.pack(fill="x")
        self.btn_edge = ttk.Button(top, text="① 자동녹화용 Edge 열기", command=self.app.open_edge)
        self.btn_edge.pack(side="left")
        ttk.Button(top, text="직접 녹화", command=self.app.open_direct).pack(side="right")
        for text in ("②  로그인 › 나의 강의실 › 강좌를 눌러 강의 목록 펼치기",
                     "③  LESSON 추가", "④  작업 시작"):
            ttk.Label(top, text="→", style="Hint.TLabel").pack(side="left", padx=8)
            ttk.Label(top, text=text).pack(side="left")

        # 맨 아래: 상태 줄 (왼쪽 자동 녹화 진행, 오른쪽 녹화 시간·장치 상태는 App이 채움)
        self.statusbar = ttk.Frame(self, padding=(10, 4, 10, 6))
        self.statusbar.pack(side="bottom", fill="x")
        ttk.Separator(self).pack(side="bottom", fill="x")
        self.lbl = ttk.Label(self.statusbar, text="")
        self.lbl.pack(side="left")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        # 오른쪽: 강의 표 + 작업
        right = ttk.Frame(body)
        right.pack(side="right", fill="y", padx=(0, 10), pady=10)
        lf = ttk.LabelFrame(right, text="강의 목록", style="Section.TLabelframe", padding=(8, 6, 8, 8))
        lf.pack(fill="both", expand=True)
        tf = ttk.Frame(lf)
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in self.COLS], show="headings", selectmode="extended")
        for key, text, w in self.COLS:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=w, anchor="w" if key in ("title", "state") else "center",
                             stretch=key in ("title", "state"))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Button-3>", self._popup)
        TreeTip(self.tree, ("title", "state"))  # 잘려 보이는 상태·제목 전체를 툴팁으로

        # 오른쪽 클릭: 작업 변경 / 상태 변경 / 파일 열기 / 삭제
        self.menu = tk.Menu(self, tearoff=0)
        for t in TASKS:
            self.menu.add_command(label=f"작업: {t}", command=lambda t=t: self.set_task(t))
        self.menu.add_separator()
        sm = tk.Menu(self.menu, tearoff=0)
        for name, code in STATE_CHOICES:
            sm.add_command(label=name, command=lambda n=name: self.set_state(n),
                           state="disabled" if code in ("rec", "enc") else "normal")
        self.menu.add_cascade(label="상태 변경", menu=sm)
        self.menu.add_separator()
        self.menu.add_command(label="녹화 파일 열기", command=lambda: self.open_file(False))
        self.menu.add_command(label="폴더에서 보기", command=lambda: self.open_file(True))
        self.menu.add_command(label="파일 이름만 지금 형식으로 바꾸기", command=lambda: self.rename_files(True))
        self.menu.add_separator()
        self.menu.add_command(label="표에서 삭제", command=self.remove_selected)

        rb = ttk.Frame(lf)
        rb.pack(fill="x", pady=(8, 0))
        self.btn_add = ttk.Button(rb, text="+ LESSON 추가", command=self.add_dialog)
        self.btn_add.pack(side="left")
        self.cb_state = ttk.Combobox(rb, values=[n for n, _ in STATE_CHOICES], state="readonly", width=11)
        self.cb_state.set("상태 변경")
        self.cb_state.bind("<<ComboboxSelected>>", self._on_state_combo)
        self.cb_state.pack(side="left", padx=(6, 0))
        self.btn_check = ttk.Button(rb, text="상태 체크", command=self.check_files)
        self.btn_check.pack(side="left", padx=(6, 0))
        self.btn_del = ttk.Button(rb, text="선택 삭제", command=self.remove_selected)
        self.btn_del.pack(side="left", padx=(6, 0))
        self.btn_clear = ttk.Button(rb, text="모두 지우기", command=self.clear_rows)
        self.btn_clear.pack(side="left", padx=(6, 0))
        ttk.Label(rb, text="오른쪽 클릭: 작업 변경", style="Hint.TLabel").pack(side="right")

        # 작업: 진행 방식 · 원본 제거 · 컴퓨터 종료 · 남은 시간 · 시작/중지
        run = ttk.LabelFrame(right, text="작업", style="Section.TLabelframe", padding=(8, 6, 8, 8))
        run.pack(fill="x", pady=(10, 0))
        self.lbl_total = ttk.Label(run, text="", foreground="#1a5fb4")
        self.lbl_total.pack(anchor="w")
        r1 = ttk.Frame(run)
        r1.pack(fill="x", pady=(6, 0))
        flow = cfg.get("auto_flow")
        if flow not in dict(FLOWS):  # 예전 설정 (자동으로 줄이기 + 시점)
            flow = ("rec" if not cfg.get("auto_enc") else
                    "each" if cfg.get("auto_enc_when") == "강의마다 저장되면 바로" else "all")
        self.var_flow = tk.StringVar(value=flow)
        for code, text in FLOWS:
            ttk.Radiobutton(r1, text=text, value=code, variable=self.var_flow,
                            command=self._on_option).pack(side="left", padx=(0, 12))
        r2 = ttk.Frame(run)
        r2.pack(fill="x", pady=(4, 0))
        self.var_edel = tk.BooleanVar(value=bool(cfg.get("auto_enc_delete")))
        ttk.Checkbutton(r2, text="인코딩 완료 후 원본파일 제거 (휴지통으로)", variable=self.var_edel,
                        command=self._on_option).pack(side="left")
        # 매번 꺼진 상태로 시작 (켜 둔 채 잊고 다음에 쓰다가 컴퓨터가 꺼지지 않도록 저장하지 않음)
        self.var_shutdown = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="작업 완료 후 컴퓨터 종료", variable=self.var_shutdown,
                        command=self._on_option).pack(side="left", padx=(16, 0))
        ttk.Label(run, text="작업 중에 바꾸면 다음 파일부터 적용돼요", style="Hint.TLabel").pack(anchor="w", pady=(2, 0))
        r3 = ttk.Frame(run)
        r3.pack(fill="x", pady=(8, 0))
        self.btn_start = ttk.Button(r3, text="▶  작업 시작", style="Big.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(r3, text="■  중지", style="Big.TButton", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 0))
        ttk.Label(r3, text="초").pack(side="right")
        self.var_gap = tk.StringVar(value=str(cfg.get("auto_gap", "5")))
        ttk.Spinbox(r3, from_=0, to=600, textvariable=self.var_gap, width=4, justify="center"
                    ).pack(side="right", padx=4)
        ttk.Label(r3, text="강의 사이 쉬는 시간").pack(side="right")

        # 왼쪽: 미리보기와 설정 탭은 App이 채움 (설정 탭 중 '파일 이름 · 인코딩'은 build_encode)
        self.left = ttk.Frame(body)
        self.left.pack(side="left", fill="both", expand=True)

    def build_encode(self, parent):
        """파일 이름 형식 + 인코딩(용량 줄이기) 설정"""
        cfg = self.app.cfg
        nf = ttk.LabelFrame(parent, text="파일 이름", style="Section.TLabelframe", padding=(10, 4, 10, 8))
        nf.pack(fill="x", pady=(8, 0))
        n0 = ttk.Frame(nf)
        n0.pack(fill="x", pady=2)
        sp = cfg.get("auto_name_space")
        self.var_space = tk.StringVar(value=sp if sp in dict(NAME_SPACE) else "keep")
        for code, text in NAME_SPACE:
            ttk.Radiobutton(n0, text=text, value=code, variable=self.var_space,
                            command=self._name_preview).pack(side="left", padx=(0, 10))
        self.var_namelen = tk.BooleanVar(value=bool(cfg.get("auto_name_len")))
        ttk.Checkbutton(n0, text="파일 이름에 영상 길이 넣기 (분 단위)", variable=self.var_namelen,
                        command=self._name_preview).pack(side="left", padx=(6, 0))
        self.btn_rename = ttk.Button(n0, text="표의 파일 이름 일괄 변경", command=self.rename_files)
        # 긴 안내·예시는 여러 줄로 (한 줄이면 창 가로 크기가 너무 커짐)
        ttk.Label(nf, text="이미 녹화·인코딩한 파일도 이름만 바꿀 수 있어요. 작업 중에는 끝난 강의만 바꿔요.\n"
                           "선택한 강의만: 표에서 오른쪽 클릭 › 파일 이름만 지금 형식으로 바꾸기",
                  style="Hint.TLabel", justify="left", wraplength=560).pack(side="bottom", anchor="w", pady=(2, 0))
        self.btn_rename.pack(side="right")
        self.lbl_name = ttk.Label(nf, text="", style="Hint.TLabel", justify="left", wraplength=560)
        self.lbl_name.pack(anchor="w", pady=(2, 0))

        o = ttk.LabelFrame(parent, text="인코딩 (용량 줄이기)", style="Section.TLabelframe", padding=(10, 4, 10, 8))
        o.pack(fill="x", pady=(8, 0))
        r1 = ttk.Frame(o)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="화질").pack(side="left", padx=(0, 6))
        self.var_eq = tk.StringVar(value=cfg.get("auto_enc_quality"))
        if self.var_eq.get() not in dict(ENC_QUALITY):
            self.var_eq.set(ENC_QUALITY[1][0])
        for name, _ in ENC_QUALITY:
            ttk.Radiobutton(r1, text=name, value=name, variable=self.var_eq,
                            command=self._update_total).pack(side="left", padx=(0, 10))
        ttk.Label(r1, text="해상도").pack(side="left", padx=(10, 4))
        self.cb_eres = ttk.Combobox(r1, values=ENC_RES, state="readonly", width=7)
        self.cb_eres.set(cfg.get("auto_enc_res") if cfg.get("auto_enc_res") in ENC_RES else "원본")
        self.cb_eres.pack(side="left")
        ttk.Label(r1, text="프레임").pack(side="left", padx=(12, 4))
        self.cb_efps = ttk.Combobox(r1, values=ENC_FPS, state="readonly", width=7)
        self.cb_efps.set(cfg.get("auto_enc_fps") if cfg.get("auto_enc_fps") in ENC_FPS else "30fps")
        self.cb_efps.pack(side="left")
        self.var_egpu = tk.BooleanVar(value=bool(cfg.get("auto_enc_gpu")))
        ttk.Checkbutton(r1, text="GPU로 빠르게 (NVIDIA)", variable=self.var_egpu,
                        command=self._update_total).pack(side="left", padx=(14, 0))
        ttk.Label(o, text="'녹화 완료 후 바로 인코딩'은 녹화 중에 인코딩해요 (CPU면 녹화가 끊길 수 있어요)",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 0))
        self._name_preview()

    def _on_option(self):
        """작업 설정을 바꿈 → 저장하고 (작업 중이면) 다음 파일부터 적용"""
        self.save()
        save_config(self.app.cfg)
        self._pump()
        self._update_total()

    def _name_preview(self):
        if not hasattr(self, "lbl_name"):
            return
        kids = self.tree.get_children()
        r = self.rows[kids[0]] if kids else {"no": 1, "title": "강의 제목", "mins": 48}
        mins = None
        if self.var_namelen.get():
            exp = self._expected(r.get("len", ""), r)[0] if kids else 48 * 60
            mins = max(1, round(exp / 60)) if exp else None
        stem = lecture_stem(r["no"], r["title"], self.var_space.get(), mins)
        self.lbl_name.config(text=f"녹화 중:    {stem}{REC_TAG}.mkv / .wav\n"
                                  f"합친 뒤:    {stem}{RAW_TAG}.mp4\n"
                                  f"인코딩 뒤: {stem}.mp4")
        self.save()

    def enc_options(self):
        """(화질, 해상도, 프레임, GPU) - GPU를 골랐어도 쓸 수 없으면 CPU로"""
        gpu = bool(self.var_egpu.get()) and self.app.encode.gpu_ok
        return self.var_eq.get(), self.cb_eres.get(), self.cb_efps.get(), gpu

    def flow(self):
        return self.var_flow.get()

    def _gap(self):
        try:
            return max(0, int(self.var_gap.get()))
        except ValueError:
            return 5

    # ------------------------------------------------------------ 표 행
    def _state_text(self, r):
        st, note = r["st"], r.get("note") or ""
        if st in ("rec", "enc"):
            word = "녹화 중" if st == "rec" else "인코딩 중"
            if r.get("pct") is not None:
                left = r.get("left")
                tail = f", {int(left) // 60}분 {int(left) % 60}초 남음" if left is not None else ", 남은 시간 계산 중"
                return f"{word}(진행률: {r['pct']:.1f}%{tail})"
            return f"{word}({note})" if note else word
        if st == "rec_fail":
            return f"녹화 실패: {note}"
        if st == "enc_fail":
            return f"인코딩 실패: {note}"
        text = STATE_FIXED[st]
        return f"{text} ({note})" if note else text

    def _values(self, r):
        return r["no"], r.get("title", ""), r.get("len", ""), r["task"], self._state_text(r)

    def _upd(self, iid, st=None, persist=True, **kw):
        """행 바꾸기 (st: 상태 코드, kw: note/task/raw/enc/len/pct/left) → 표 다시 그림"""
        if not iid or not self.tree.exists(iid):
            return
        r = self.rows[iid]
        if st is not None and st != r["st"]:
            r["pct"] = r["left"] = None
            r["note"] = ""
        if st is not None:
            r["st"] = st
        r.update(kw)
        self.tree.item(iid, values=self._values(r))
        if persist:
            self._persist()

    def _active_row(self, iid):
        """지금 녹화·인코딩 중이거나 인코딩으로 보낸 행 (상태를 바꾸면 안 됨)"""
        r = self.rows.get(iid)
        return bool(r and (r["st"] in ("rec", "enc") or iid in self.enc_sent
                           or (self.runner and iid == self.cur_row)))

    def needs_rec(self, iid):
        r = self.rows[iid]
        return has_rec(r["task"]) and r["st"] in REC_NEED

    def needs_enc(self, iid):
        r = self.rows[iid]
        return has_enc(r["task"]) and r["st"] in ENC_NEED

    def _stem(self, iid):
        """이 강의의 파일 이름 (지금 설정한 형식)"""
        r = self.rows[iid]
        mins = None
        if self.var_namelen.get():
            exp = self._row_expect(iid)[0]
            mins = max(1, round(exp / 60)) if exp else None
        return lecture_stem(r["no"], r.get("title", ""), self.var_space.get(), mins)

    # -- 메인 스레드로 넘기기
    def _ui(self, fn, *args):
        try:
            self.after(0, fn, *args)
        except (RuntimeError, tk.TclError):
            pass

    def _call(self, fn, *args):
        """다른 스레드에서 메인 스레드 함수를 실행하고 결과를 기다림"""
        box, ev = [], threading.Event()

        def run():
            try:
                box.append(fn(*args))
            finally:
                ev.set()
        self._ui(run)
        ev.wait(30)
        return box[0] if box else None

    # -- 강의 표 (설정 파일에 저장해 두고 다음 실행 때 다시 보여줌)
    @staticmethod
    def _old_state(text):
        """예전 버전의 상태 문구 → 상태 코드"""
        text = text or ""
        if "직접 표시" in text:
            return "skip"
        if text.startswith(("인코딩 완료", "용량 줄이기 완료")):
            return "enc_done"
        if text.startswith(("인코딩", "용량 줄이기")):
            return "enc_wait"
        if text.startswith("완료"):
            return "rec_done"
        return "rec_wait"

    def _load_rows(self):
        try:
            rows = json.loads(self.app.cfg.get("auto_list") or "[]")
        except ValueError:
            rows = []  # 예전 형식(입력한 글자 그대로)은 버림
        for r in rows if isinstance(rows, list) else []:
            if not (isinstance(r, dict) and "no" in r):
                continue
            st = r.get("st") if r.get("st") in ALL_STATES else self._old_state(r.get("state"))
            # 녹화·인코딩 중에 프로그램이 꺼졌음
            st = {"rec": "rec_stopped", "enc": "enc_stopped"}.get(st, st)
            task = r.get("task") if r.get("task") in TASKS else ("건너뛰기" if st == "skip" else DEFAULT_TASK)
            self._insert(r, st, task=task, length=r.get("len"), note=r.get("note", ""),
                         raw=r.get("raw") or r.get("file") or "", enc=r.get("enc", ""))
        self._update_total()

    def _save_rows(self):
        rows = []
        for iid in self.tree.get_children():
            r = self.rows[iid]
            rows.append({k: r.get(k) for k in ("idx", "no", "title", "mins", "st", "note", "task", "len", "raw", "enc")})
        self.app.cfg["auto_list"] = json.dumps(rows, ensure_ascii=False)

    def _persist(self):
        """표를 설정 파일에 바로 저장 (프로그램이 갑자기 꺼져도 다음에 그대로 복원)"""
        self._save_rows()
        try:
            save_config(self.app.cfg)
        except OSError:
            pass

    def _insert(self, lec, st="rec_wait", task=DEFAULT_TASK, length=None, note="", raw="", enc=""):
        if length is None:
            length = f"{lec['mins']}분" if lec.get("mins") is not None else ""
        r = {k: lec.get(k) for k in ("idx", "no", "title", "mins")}
        r.update(st=st, task=task, len=length or "", note=note or "", raw=raw or "", enc=enc or "",
                 pct=None, left=None)
        # 항상 일련번호 오름차순 (들어갈 자리를 찾아 끼워 넣음)
        kids = self.tree.get_children()
        pos = next((k for k, i in enumerate(kids) if self.rows[i]["no"] > r["no"]), "end")
        iid = self.tree.insert("", pos, values=self._values(r))
        self.rows[iid] = r
        return iid

    def _startup_check(self):
        """녹화·인코딩했다고 되어 있는 강의의 파일이 정말 있고 끝까지 녹화됐는지"""
        rows = [i for i in self.tree.get_children()
                if self.rows[i]["st"] in ("rec_done", "enc_wait", "enc_done", "enc_stopped", "enc_fail", "missing")]
        if rows and not self.session:
            self.check_files(rows, "녹화한 강의 확인")

    # ------------------------------------------------------------ 남은 시간
    def _enc_speed(self):
        """인코딩에 걸리는 시간 / 영상 길이 (지난 인코딩에서 잰 값, 없으면 대략)"""
        gpu = bool(self.var_egpu.get()) and self.app.encode.gpu_ok
        try:
            v = float(self.app.cfg.get("auto_enc_speed_gpu" if gpu else "auto_enc_speed_cpu") or 0)
        except ValueError:
            v = 0
        return v if v > 0 else (0.15 if gpu else 0.5)

    def _learn_speed(self, speed):
        if not speed or speed <= 0:
            return
        key = "auto_enc_speed_gpu" if (self.var_egpu.get() and self.app.encode.gpu_ok) else "auto_enc_speed_cpu"
        try:
            old = float(self.app.cfg.get(key) or 0)
        except ValueError:
            old = 0
        self.app.cfg[key] = f"{(old + speed) / 2 if old > 0 else speed:.3f}"

    def _estimate(self):
        """→ (녹화할 강의 수, 인코딩할 강의 수, 남은 초)"""
        flow = self.flow()
        gap = self._gap()
        speed = self._enc_speed()
        rec_n = enc_n = 0
        rec_t = enc_t = last_enc = 0.0
        for iid in self.tree.get_children():
            r = self.rows[iid]
            d = self._row_expect(iid)[0] or 0
            will_enc = False
            if r["st"] == "rec":
                rec_n += 1
                rec_t += (r["left"] if r.get("left") is not None else d + LECTURE_OVERHEAD) + 10
                will_enc = has_enc(r["task"])
            elif self.needs_rec(iid) and not (self.session and iid not in self.run_rows):
                rec_n += 1
                rec_t += d + LECTURE_OVERHEAD + gap
                will_enc = has_enc(r["task"])
            if flow == "rec":
                continue
            if r["st"] == "enc":
                enc_n += 1
                enc_t += r["left"] if r.get("left") is not None else d * speed
            elif will_enc or self.needs_enc(iid):
                enc_n += 1
                enc_t += d * speed
                if will_enc:
                    last_enc = d * speed
        if flow == "each":  # 녹화하면서 앞 강의를 인코딩 → 더 오래 걸리는 쪽 + 마지막 강의 인코딩
            total = max(rec_t + last_enc, enc_t)
        elif flow == "all":
            total = rec_t + enc_t
        else:
            total = rec_t
        return rec_n, enc_n, total

    def _update_total(self):
        if not hasattr(self, "var_egpu"):
            return  # build_encode 전
        rec_n, enc_n, total = self._estimate()
        if not rec_n and not enc_n:
            text = "남은 작업이 없어요" if self.tree.get_children() else ""
        else:
            parts = []
            if rec_n:
                parts.append(f"녹화 {rec_n}개")
            if enc_n:
                parts.append(f"인코딩 {enc_n}개")
            now = datetime.now()
            end = fmt_clock(now + timedelta(seconds=total), now)
            if self.session:
                text = " · ".join(parts) + f"  |  남은 시간 약 {fmt_hm(total)}  |  종료 예정 {end}"
            else:
                text = " · ".join(parts) + f"  |  예상 소요 약 {fmt_hm(total)}  |  지금 시작하면 {end} 종료 예정"
        if self.lbl_total.cget("text") != text:
            self.lbl_total.config(text=text)

    def _tick(self):
        """1초마다: 남은 시간 표시, (작업 중이면) 인코딩 보내기·작업 끝 확인"""
        try:
            if self.session:
                keep_awake(True)
                self._pump()
                self._check_done()
            self._update_total()
        finally:
            self.after(1000, self._tick)

    # ------------------------------------------------------------ 상태·작업 바꾸기
    def remove_selected(self):
        for iid in self.tree.selection():
            if self._active_row(iid):
                self.lbl.config(text="지금 녹화·인코딩 중인 강의는 지울 수 없어요.")
                continue
            self.tree.delete(iid)
            self.rows.pop(iid, None)
        self._update_total()
        self._persist()

    def _popup(self, ev):
        """표 오른쪽 클릭 → 작업·상태 변경 메뉴 (누른 행이 선택에 없으면 그 행만 선택)"""
        iid = self.tree.identify_row(ev.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.menu.tk_popup(ev.x_root, ev.y_root)

    def _on_state_combo(self, ev=None):
        choice = self.cb_state.get()
        self.cb_state.set("상태 변경")
        self.cb_state.selection_clear()
        self.tree.focus_set()
        self.set_state(choice)

    def _sel(self):
        sel = list(self.tree.selection())
        if not sel:
            self.lbl.config(text="표에서 강의를 먼저 선택해 주세요.")
        return sel

    def set_state(self, choice):
        """선택한 강의의 상태를 직접 바꿈 (작업도 그 상태에 맞게: 예) 인코딩 대기 → 작업에 인코딩 포함)"""
        code = dict(STATE_CHOICES).get(choice)
        if code in ("rec", "enc"):
            self.lbl.config(text=f"'{choice}'은(는) 작업이 진행되면 저절로 바뀌어요.")
            return
        if not code:
            return
        sel = self._sel()
        n, busy, confirm = 0, 0, []
        for iid in sel:
            if self._active_row(iid):
                busy += 1
                continue
            task = self.rows[iid]["task"]
            if code == "skip":
                task = "건너뛰기"
            elif code == "rec_wait" and not has_rec(task):
                task = DEFAULT_TASK
            elif code == "enc_wait" and not has_enc(task):
                task = DEFAULT_TASK if task == "녹화" else "인코딩"
            elif task == "건너뛰기":
                task = DEFAULT_TASK
            self._upd(iid, code, persist=False, task=task)
            n += 1
            if code == "rec_wait":
                self._requeue(iid)
            if code in ("rec_done", "enc_wait", "enc_done"):
                confirm.append(iid)
        self._persist()
        self._update_total()
        msg = f"{n}개를 '{choice}'(으)로 바꿨어요." if n else ""
        if busy:
            msg += f" 지금 작업 중인 {busy}개는 그대로 뒀어요."
        if n and code == "rec_wait":
            msg += " 지금 녹화가 끝나면 이어서 녹화해요." if self.runner else " '작업 시작'을 누르면 녹화해요."
        if n and code == "enc_wait":
            msg += " '작업 시작'을 누르면 인코딩해요." if not self.session else ""
        if msg:
            self.lbl.config(text=msg.strip())
        if confirm:
            self._confirm_files(confirm, code)
        self._pump()

    def _confirm_files(self, iids, code):
        """상태를 직접 '녹화 완료/인코딩 대기/인코딩 완료'로 바꾼 강의: 그 파일이 정말 있는지만 확인"""
        out_dir = self.app.var_dir.get()
        jobs = [(iid, self.rows[iid]["no"], self.rows[iid].get("raw"), self.rows[iid].get("enc")) for iid in iids]

        def work():
            res = []
            for iid, no, raw, enc in jobs:
                raws, encs = scan_lecture_files(out_dir, no, (raw, enc))
                res.append((iid, raws[0][0] if raws else "", encs[0][0] if encs else ""))
            self._ui(done, res)

        def done(res):
            missing = 0
            for iid, raw, enc in res:
                if not self.tree.exists(iid) or self._active_row(iid) or self.rows[iid]["st"] != code:
                    continue
                need = enc if code == "enc_done" else raw
                if need:
                    self._upd(iid, persist=False, raw=raw, enc=enc)
                else:
                    missing += 1
                    self._upd(iid, "missing", persist=False, raw=raw, enc=enc)
            self._persist()
            if missing:
                self.lbl.config(text=f"파일이 없는 강의 {missing}개는 '파일이 존재하지 않습니다.'로 표시했어요.")
        threading.Thread(target=work, daemon=True).start()

    def set_task(self, task):
        """선택한 강의의 작업 (녹화 / 인코딩 / 녹화 및 인코딩 / 건너뛰기)"""
        sel = self._sel()
        n, busy, recheck = 0, 0, []
        for iid in sel:
            r = self.rows[iid]
            if task == "건너뛰기" and self._active_row(iid):
                busy += 1
                continue
            if task == "건너뛰기":
                self._upd(iid, "skip", persist=False, task=task)
            elif r["st"] == "skip":  # 건너뛰기를 풀면 파일을 보고 상태를 다시 정함
                self._upd(iid, "rec_wait", persist=False, task=task)
                recheck.append(iid)
            else:
                st = r["st"]
                if not self._active_row(iid):
                    if st == "rec_done" and has_enc(task):
                        st = "enc_wait"
                    elif st == "enc_wait" and not has_enc(task):
                        st = "rec_done"
                self._upd(iid, st, persist=False, task=task)
            n += 1
            if self.needs_rec(iid):
                self._requeue(iid)
        self._persist()
        self._update_total()
        msg = f"{n}개의 작업을 '{task}'(으)로 바꿨어요." if n else ""
        if busy:
            msg += f" 지금 작업 중인 {busy}개는 건너뛸 수 없어요."
        if msg:
            self.lbl.config(text=msg.strip())
        if recheck and not self.session:
            self.check_files(recheck, msg.strip())
        self._pump()

    def _requeue(self, iid):
        """녹화 중이면 이 강의를 지금 하는 것들이 끝난 뒤 이어서 녹화"""
        if self.runner and iid != self.cur_row and iid not in self._pending():
            self.runner.lectures.append(dict(self.rows[iid]))
            self.run_rows.append(iid)

    def _pending(self):
        """이번 자동 녹화에서 아직 차례가 오지 않은 행"""
        pos = self.run_rows.index(self.cur_row) if self.cur_row in self.run_rows else -1
        return set(self.run_rows[pos + 1:])

    # ------------------------------------------------------------ 파일 확인
    @staticmethod
    def _expected(length, lec):
        """표의 '길이'로 기대하는 영상 길이(초)와 허용 오차 → (초, 오차) 또는 (None, 0)"""
        m = re.fullmatch(r"(\d+)분 ([\d.]+)초", length or "")
        if m:  # 녹화할 때 확인한 실제 영상 길이 (정확)
            return int(m.group(1)) * 60 + float(m.group(2)), 3.0
        m = re.fullmatch(r"(\d+)분", length or "")
        mins = int(m.group(1)) if m else lec.get("mins")
        if mins:  # 강의 목록의 '분' (반올림돼 있을 수 있음)
            return mins * 60, 60.0
        return None, 0

    MIN_SECS = 300      # 기대 길이를 모를 때: 이보다 짧으면 비정상 (5분)
    MIN_KBPS = 100      # 이보다 용량이 작으면 (거의 빈 화면) 비정상 (정상 강의 파일은 300~1000kbps)

    @classmethod
    def _judge(cls, path, info, exp, tol):
        """녹화 파일이 정상인지 → None(정상) 또는 문제 문구 (다른 스레드에서 호출해도 됨)"""
        if not info["ok"]:
            return "파일 깨짐"
        dur = info["dur"] or 0
        if exp and dur < exp - tol:
            return f"덜 녹화됨 {fmt_secs(dur)} / {fmt_secs(exp)}"
        if not exp and dur < cls.MIN_SECS:
            return f"너무 짧음 {fmt_secs(dur)}"
        try:
            kbps = os.path.getsize(path) * 8 / 1000 / max(dur, 1)
        except OSError:
            return "파일 없음"
        if kbps < cls.MIN_KBPS:
            return f"용량 비정상 {os.path.getsize(path) / 1e6:.1f}MB"
        return None

    def _row_expect(self, iid):
        return self._expected(self.rows[iid].get("len"), self.rows[iid])

    def _verify(self, iid, path, ok, bad):
        """만든 파일을 확인 → 정상이면 ok(), 아니면 bad(문제 문구)"""
        exp, tol = self._row_expect(iid)
        self.verifying += 1

        def work():
            problem = self._judge(path, media_info(path), exp, tol) if os.path.isfile(path) else "파일 없음"
            self._ui(done, problem)

        def done(problem):
            self.verifying -= 1
            if self.tree.exists(iid):
                bad(problem) if problem else ok()
            self._check_done()
        threading.Thread(target=work, daemon=True).start()

    def check_files(self, iids=None, note=""):
        """상태 체크: 강의마다 녹화 원본·인코딩한 파일이 있는지, 열리는지, 끝까지 녹화됐는지 확인해서 상태를 고침
        iids를 주면 그 강의만 (LESSON 추가 때). 인코딩 중이면 잠시 멈추고 먼저 확인"""
        out_dir = self.app.var_dir.get()
        jobs = []
        for iid in (self.tree.get_children() if iids is None else iids):
            if not self.tree.exists(iid):
                continue
            r = self.rows[iid]
            if self._active_row(iid) or r["st"] == "skip" or iid in self.renaming:
                continue  # 지금 작업 중 / 건너뛰기로 한 강의 / 이름 바꾸는 중은 그대로
            exp, tol = self._row_expect(iid)
            jobs.append((iid, r["no"], r.get("raw"), r.get("enc"), exp, tol))
        if not jobs:
            self.lbl.config(text=note or "확인할 강의가 없어요.")
            return
        enc = self.app.encode
        paused = enc.busy and not enc.paused.is_set()
        if paused:
            enc.pause(True)
        self.btn_check.config(state="disabled")
        self.lbl.config(text=f"상태 체크 중... (0/{len(jobs)})" + ("  ·  인코딩은 잠시 멈춤" if paused else ""))

        def work():
            res = []
            for k, (iid, no, raw, encp, exp, tol) in enumerate(jobs):
                raws, encs = scan_lecture_files(out_dir, no, (raw, encp))
                rp, rprob = (raws[0][0], self._judge(raws[0][0], raws[0][1], exp, tol)) if raws else ("", None)
                ep, eprob = (encs[0][0], self._judge(encs[0][0], encs[0][1], exp, tol)) if encs else ("", None)
                res.append((iid, rp, rprob, ep, eprob))
                self._ui(self.lbl.config, {"text": f"상태 체크 중... ({k + 1}/{len(jobs)})"})
            self._ui(self._check_files_done, res, paused, note)
        threading.Thread(target=work, daemon=True).start()

    def _check_files_done(self, res, paused, note=""):
        if paused:
            self.app.encode.pause(False)
        self.btn_check.config(state="normal")
        good = bad = missing = 0
        bad_rows = []
        for iid, raw, rprob, enc, eprob in res:
            if not self.tree.exists(iid) or self._active_row(iid) or self.rows[iid]["st"] == "skip":
                continue
            st = self.rows[iid]["st"]
            kw = {"raw": raw, "enc": enc}
            if enc and not eprob:  # 인코딩까지 끝난 강의
                good += 1
                self._upd(iid, "enc_done", persist=False, **kw)
            elif raw and not rprob:  # 녹화는 정상
                good += 1
                if st in ("enc_wait", "enc_stopped", "enc_fail"):
                    self._upd(iid, persist=False, **kw)
                else:
                    self._upd(iid, "enc_wait" if st == "enc_done" and has_enc(self.rows[iid]["task"]) else "rec_done",
                              persist=False, **kw)
            elif raw or enc:  # 파일이 이상함 → 다시 녹화
                problem = rprob or eprob
                if st in ("rec_wait", "rec_stopped", "rec_fail"):
                    self._upd(iid, persist=False, **kw)  # 원래 녹화할 강의 (중지·실패 표시는 그대로)
                    continue
                bad += 1
                bad_rows.append(iid)
                self._upd(iid, "rec_wait", persist=False, **kw)
                self._upd(iid, persist=False, note=f"파일 이상: {problem}")
                self._requeue(iid)
            else:  # 파일이 없음
                if st in ("rec_wait", "rec_stopped", "rec_fail", "missing"):
                    self._upd(iid, persist=False, **kw)
                    continue
                missing += 1
                bad_rows.append(iid)
                self._upd(iid, "missing", persist=False, **kw)
        self._update_total()
        self._persist()
        self._name_preview()
        if bad_rows:
            self.tree.selection_set(bad_rows)
            self.tree.see(bad_rows[0])
        msg = (note + "  ·  " if note else "") + f"상태 체크 - 파일이 정상인 강의 {good}개"
        if bad:
            msg += f", 파일이 이상해서 다시 녹화할 강의 {bad}개"
        if missing:
            msg += f", 파일이 없는 강의 {missing}개"
        if bad_rows:
            msg += " (선택해 둠)"
        if paused:
            msg += " · 인코딩 다시 시작"
        self.lbl.config(text=msg)

    def open_file(self, folder):
        sel = self.tree.selection()
        r = self.rows.get(sel[0]) if sel else None
        path = next((p for p in ((r.get("enc"), r.get("raw")) if r else ()) if p and os.path.isfile(p)), None)
        if not path:
            messagebox.showinfo("녹화 파일", "이 강의의 녹화 파일이 없어요.")
            return
        if folder:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)

    def clear_rows(self):
        if self.session or not self.rows:
            return
        if messagebox.askyesno("모두 지우기", "표의 강의를 모두 지울까요?\n(녹화한 파일은 지우지 않아요)"):
            self.tree.delete(*self.tree.get_children())
            self.rows.clear()
            self._update_total()
            self._persist()

    # ------------------------------------------------------------ 파일 이름 일괄 변경
    RENAME_OK_IN_WORK = ("rec_done", "enc_done", "skip", "missing")  # 작업 중에도 이름을 바꿀 수 있는 상태

    def _renamable(self, iid):
        """파일 이름만 바꿔도 되는 강의: 작업 중이 아니면 모두, 작업 중이면 녹화·인코딩이 끝난(또는 건너뛰는) 강의만"""
        if iid in self.renaming or self._active_row(iid):
            return False
        if not self.session:
            return True
        return self.rows[iid]["st"] in self.RENAME_OK_IN_WORK and not (self.runner and iid in self._pending())

    def rename_files(self, selected=False):
        """녹화 원본·인코딩한 파일의 이름만 지금 형식으로 바꿈 (영상은 그대로)
        selected: 표에서 고른 강의만. 이미 녹화·인코딩이 끝난 강의는 작업 중에도 바꿀 수 있음"""
        cand = list(self.tree.selection()) if selected else list(self.tree.get_children())
        if not cand:
            self.lbl.config(text="표에서 강의를 먼저 선택해 주세요." if selected else "표에 강의가 없어요.")
            return
        rows = [i for i in cand if self._renamable(i)]
        held = len(cand) - len(rows)
        if not rows:
            self.lbl.config(text="지금 녹화·인코딩 중이거나 차례를 기다리는 강의는 작업이 끝난 뒤에 바꿀 수 있어요.")
            return
        what = "선택한 강의" if selected else "표의 강의"
        if not messagebox.askyesno(
                "파일 이름 변경",
                f"{what} {len(rows)}개의 녹화 원본·인코딩한 파일 이름을 지금 형식으로 바꿀까요?\n"
                "(파일 이름만 바뀌고 영상은 그대로예요)\n\n"
                f"예) {self._stem(rows[0])}{RAW_TAG}.mp4  /  {self._stem(rows[0])}.mp4\n\n"
                "같은 이름의 다른 파일이 이미 있으면 그 파일은 바꾸지 않아요."
                + (f"\n지금 작업 중이거나 차례를 기다리는 {held}개는 빼고 바꿔요." if held else "")):
            return
        out_dir = self.app.var_dir.get()
        space, with_len = self.var_space.get(), bool(self.var_namelen.get())
        jobs = [(iid, self.rows[iid]["no"], self.rows[iid].get("title", ""), self.rows[iid].get("raw"),
                 self.rows[iid].get("enc")) for iid in rows]
        self.renaming.update(rows)
        self.btn_rename.config(state="disabled")
        if not self.session:
            self.btn_start.config(state="disabled")
        self.lbl.config(text=f"파일 이름 바꾸는 중... (0/{len(jobs)})")

        def target(p, info, no, title, tag):
            dur = info.get("dur") if info else None
            mins = max(1, round(dur / 60)) if (with_len and dur) else None
            return os.path.join(os.path.dirname(p), lecture_stem(no, title, space, mins) + tag + ".mp4")

        def work():
            res, n_ok, n_same, n_fail = [], 0, 0, 0
            for k, (iid, no, title, raw, enc) in enumerate(jobs):
                raws, encs = scan_lecture_files(out_dir, no, (raw, enc))
                new = {"raw": raws[0][0] if raws else "", "enc": encs[0][0] if encs else ""}
                for kind, lst, tag in (("raw", raws, RAW_TAG), ("enc", encs, "")):
                    if not lst:
                        continue
                    p, info = lst[0]
                    t = target(p, info, no, title, tag)
                    if os.path.normcase(t) == os.path.normcase(p):
                        if t != p:  # 대소문자만 다름
                            try:
                                os.rename(p, t)
                            except OSError:
                                pass
                        new[kind] = t
                        n_same += 1
                    elif os.path.exists(t):
                        n_fail += 1
                    else:
                        try:
                            os.rename(p, t)
                            new[kind] = t
                            n_ok += 1
                        except OSError:
                            n_fail += 1
                res.append((iid, new))
                self._ui(self.lbl.config, {"text": f"파일 이름 바꾸는 중... ({k + 1}/{len(jobs)})"})
            self._ui(done, res, n_ok, n_same, n_fail)

        def done(res, n_ok, n_same, n_fail):
            for iid, new in res:
                self.renaming.discard(iid)
                if self.tree.exists(iid):
                    self._upd(iid, persist=False, **new)
            self._persist()
            self.btn_rename.config(state="normal")
            if not self.session:
                self.btn_start.config(state="normal")
            msg = f"파일 이름 변경 - {n_ok}개 바꿈"
            if n_same:
                msg += f", 이미 같은 이름 {n_same}개"
            if n_fail:
                msg += f", 같은 이름의 파일이 있거나 사용 중이라 못 바꾼 파일 {n_fail}개"
            if held:
                msg += f", 작업 중이라 뺀 강의 {held}개"
            self.lbl.config(text=msg)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ LESSON 추가
    def add_dialog(self):
        """LESSON 입력 창 → Edge 강의 목록에서 찾아 표에 추가"""
        import lecture_auto
        if self.session:
            return
        d = tk.Toplevel(self)
        d.title("LESSON 추가")
        d.transient(self.winfo_toplevel())
        d.resizable(False, False)
        f = ttk.Frame(d, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="녹화할 LESSON을 입력하세요 (여러 줄 가능)\n"
                          "예: 제 1 강  /  제 1 강 ~ 제 3 강  /  1~3  /  1, 2, 3").pack(anchor="w")
        txt = tk.Text(f, width=44, height=12, undo=True, wrap="none")
        txt.pack(fill="both", expand=True, pady=8)
        msg = ttk.Label(f, text="", foreground="#b00", wraplength=360, justify="left")
        msg.pack(anchor="w")
        bf = ttk.Frame(f)
        bf.pack(fill="x", pady=(8, 0))
        ttk.Button(bf, text="취소", command=d.destroy).pack(side="right")
        btn = ttk.Button(bf, text="추가", command=lambda: ok())
        btn.pack(side="right", padx=6)

        def ok():
            lines = txt.get("1.0", "end").splitlines()
            if not any(x.strip() for x in lines):
                msg.config(text="LESSON을 입력해 주세요.")
                return
            btn.config(state="disabled")
            msg.config(text="Edge에서 강의 목록 읽는 중...", foreground="#555")

            def work():
                try:
                    lecs = lecture_auto.read_lectures()
                    self._ui(done, lecture_auto.match_lines(lines, lecs), None)
                except Exception as e:
                    self._ui(done, None, str(e))
            threading.Thread(target=work, daemon=True).start()

        def done(pairs, err):
            if not d.winfo_exists():
                return
            btn.config(state="normal")
            if err:
                msg.config(text=f"강의 목록을 읽지 못했어요: {err}", foreground="#b00")
                return
            have = {self.rows[i]["no"]: i for i in self.tree.get_children()}
            added, dup, bad = 0, 0, []
            check = []  # 녹화·인코딩 됐는지 확인할 강의 (새로 넣은 것 + 이미 표에 있던 것)
            for text, lec in pairs:
                if not lec:
                    why = "Edge 목록에 없음" if lecture_auto.lesson_numbers(text) else "LESSON 형식이 아님"
                    bad.append((text, why))
                elif lec["no"] in have:
                    if have[lec["no"]] not in check:
                        dup += 1
                        check.append(have[lec["no"]])
                else:
                    have[lec["no"]] = iid = self._insert(lec)
                    check.append(iid)
                    added += 1
            self._update_total()
            self._persist()
            self._name_preview()
            note = f"{added}개 추가" + (f", 이미 표에 있는 {dup}개는 상태만 확인" if dup else "")
            self.lbl.config(text=note)
            if check and not self.session:
                self.check_files(check, note)
            if not bad:
                d.destroy()
                return
            txt.delete("1.0", "end")
            txt.insert("1.0", "\n".join(t for t, _ in bad))
            msg.config(foreground="#b00", text=note + "\n추가하지 못한 줄 (고쳐서 다시 '추가', 또는 취소):\n"
                       + "\n".join(f"  · {t}  ←  {w}" for t, w in bad[:6])
                       + (f"\n  · ... 외 {len(bad) - 6}줄" if len(bad) > 6 else ""))

        txt.bind("<Control-Return>", lambda e: (ok(), "break")[1])
        d.update_idletasks()
        top = self.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - d.winfo_reqwidth()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - d.winfo_reqheight()) // 3
        d.geometry(f"+{max(0, x)}+{max(0, y)}")
        d.grab_set()
        txt.focus_set()

    def save(self):
        if not hasattr(self, "var_eq"):
            return  # build_encode 전
        self._save_rows()
        self.app.cfg["auto_gap"] = self.var_gap.get().strip() or "5"
        self.app.cfg.update(
            auto_flow=self.var_flow.get(), auto_enc_quality=self.var_eq.get(),
            auto_enc_res=self.cb_eres.get(), auto_enc_fps=self.cb_efps.get(), auto_enc_gpu=self.var_egpu.get(),
            auto_enc_delete=self.var_edel.get(), auto_name_space=self.var_space.get(),
            auto_name_len=self.var_namelen.get())

    # ------------------------------------------------------------ 실행
    def start(self):
        import lecture_auto
        if self.session:
            return
        if self.renaming:
            messagebox.showwarning("작업 시작", "파일 이름을 바꾸는 중이에요. 끝난 뒤에 시작해 주세요.")
            return
        if self.app.recording or self.app.busy:
            messagebox.showwarning("작업 시작", "녹화나 MP4 변환이 끝난 뒤에 시작해 주세요.")
            return
        kids = self.tree.get_children()
        rec_rows = [i for i in kids if self.needs_rec(i)]
        enc_rows = [i for i in kids if self.needs_enc(i)] if self.flow() != "rec" else []
        if not rec_rows and not enc_rows:
            text = "할 작업이 없어요. 'LESSON 추가'로 강의를 추가하거나 표의 '작업'·'상태'를 확인해 주세요."
            if self.flow() == "rec" and any(self.needs_enc(i) for i in kids):
                text += "\n('녹화만 진행'을 골라서 인코딩은 하지 않아요)"
            messagebox.showwarning("작업 시작", text)
            return
        if rec_rows and self.app.devices_lent:
            messagebox.showwarning("작업 시작", "직접 녹화 창에서 캡쳐 장치를 쓰고 있어요. 직접 녹화 창을 닫은 뒤에 시작해 주세요.")
            return
        if rec_rows and (not self.app.video or self.app.video.get_frame() is None):
            messagebox.showwarning("작업 시작", "캡쳐 장치의 영상 신호가 아직 들어오지 않았어요.")
            return
        self.save()
        save_config(self.app.cfg)
        if not rec_rows:  # 인코딩만 → 캡쳐 장치·Edge 없이도 됨
            self._begin([], enc_rows)
            return
        self.btn_start.config(state="disabled")
        self.lbl.config(text="Edge에서 강의 목록 확인 중...")

        def work():  # 그 사이 목록이 바뀌었을 수 있으므로 시작할 때 다시 확인
            try:
                lecs = lecture_auto.read_lectures()
                self._ui(self._go, rec_rows, enc_rows, lecs)
            except Exception as e:
                self._ui(self._not_started, f"강의 목록을 읽지 못했어요: {e}", enc_rows)
        threading.Thread(target=work, daemon=True).start()

    def _not_started(self, msg, enc_rows=()):
        self.btn_start.config(state="normal")
        self.lbl.config(text=msg)
        if enc_rows and messagebox.askyesno("작업 시작", f"{msg}\n\n녹화는 빼고 인코딩만 할까요?"):
            self._begin([], list(enc_rows))

    def _go(self, rec_rows, enc_rows, lecs):
        by_no = {}
        for lec in lecs:
            by_no.setdefault(lec["no"], lec)
        items, run_rows = [], []
        for iid in rec_rows:
            if not self.tree.exists(iid):
                continue
            lec = by_no.get(self.rows[iid]["no"])
            if lec:
                self.rows[iid].update({k: lec.get(k) for k in ("idx", "no", "title", "mins")})
                items.append(lec)
                run_rows.append(iid)
            else:
                self._upd(iid, "rec_fail", persist=False, note="Edge 목록에 없음")
        if not items and not enc_rows:
            self._not_started("Edge 목록에서 찾은 강의가 없어요. 강의 목록을 펼쳐 두었는지 확인해 주세요.")
            return
        self.run_rows = run_rows
        self._begin(items, enc_rows)

    def _begin(self, items, enc_rows):
        import lecture_auto
        self.session = True
        self.stopped = False
        self.muxed, self.completed = set(), set()
        self.enc_sent = {}
        self.cur_row = None
        self.counts = {"rec": 0, "enc": 0, "fail": 0}
        if not items:
            self.run_rows = []
        for iid in self.run_rows:
            self._upd(iid, "rec_wait", persist=False)
        for iid in enc_rows:
            if self.tree.exists(iid):
                self._upd(iid, "enc_wait", persist=False)
        for w in (self.btn_start, self.btn_add, self.btn_clear, self.btn_edge):
            w.config(state="disabled")
        self.btn_stop.config(state="normal")
        keep_awake(True)
        if items:
            hooks = {
                "start_rec": lambda name: self._call(self._start_rec),
                "stop_rec": lambda: self._call(self._stop_rec),
                "is_busy": lambda: self.app.busy,
                "skip": lambda i: self._call(self._skip, i),
                "status": lambda i, text: self._ui(self._set, i, text),
                "progress": lambda i, t, dur: self._ui(self._progress, i, t, dur),
                "length": lambda i, sec: self._ui(self._set_len, i, sec),
                "log": lambda text: self._ui(self.lbl.config, {"text": text}),
                "finished": lambda msg: self._ui(self._finished, msg),
            }
            self.runner = lecture_auto.AutoRecorder(items, hooks, gap=self._gap())
            self.lbl.config(text="작업 중... (Edge 강의 창과 마우스는 건드리지 마세요)")
            self.runner.start()
        else:
            self.lbl.config(text="인코딩 중...")
        self._persist()
        self._pump()
        self._update_total()

    # -- 녹화 (AutoRecorder가 부름)
    def _row(self, i):
        return self.run_rows[i] if 0 <= i < len(self.run_rows) else None

    def _skip(self, i):
        """i번째 강의를 시작하기 직전: 그 사이 표에서 지웠거나 작업·상태를 바꿨으면 건너뜀"""
        iid = self._row(i)
        return not iid or not self.tree.exists(iid) or not self.needs_rec(iid)

    def _start_rec(self):
        """녹화 시작 (메인 스레드). 파일 이름은 지금 설정한 형식으로
        이 강의를 전에 녹화하다 끊긴(또는 이상한) 파일이 있으면 휴지통으로 보내고 처음부터 다시 녹화"""
        iid = self.cur_row
        if not iid or not self.tree.exists(iid):
            return False
        old = self.rows[iid].get("raw")
        if old and os.path.isfile(old) and to_recycle_bin(old):
            self.lbl.config(text=f"전에 녹화하던 파일은 휴지통으로 보냈어요: {os.path.basename(old)}")
        out_dir = self.app.var_dir.get()
        os.makedirs(out_dir, exist_ok=True)
        base = unique_base(out_dir, self._stem(iid), auto=True)
        ok = self.app.start_recording(True, auto_base=base)
        if ok:
            # 갑자기 꺼져도 다음에 이 파일을 끊긴 파일로 알아보도록 바로 저장
            self._upd(iid, raw=os.path.normpath(rec_paths(base, True)[2]))
        return ok

    def _stop_rec(self):
        if self.app.recording:
            self.app.stop_recording()

    def _set_len(self, i, sec):
        """실제 영상 길이 (예: 48분 15.7초)"""
        self._upd(self._row(i), len=f"{int(sec // 60)}분 {sec % 60:.1f}초")

    def _progress(self, i, t, dur):
        iid = self._row(i)
        if iid and dur:
            self._upd(iid, "rec", persist=False, note="", pct=min(100.0, t / dur * 100), left=max(0.0, dur - t))

    def _set(self, i, text):
        """AutoRecorder가 보낸 진행 문구 → 상태"""
        iid = self._row(i)
        self.cur_row = iid
        if not iid or not self.tree.exists(iid):
            return
        r = self.rows[iid]
        if text == "완료":  # 끝까지 녹화함 → MP4 합치기가 끝나면 파일 확인
            self.completed.add(iid)
            self._upd(iid, "rec", note="파일 합치는 중", pct=None, left=None)
            self._dispatch()
        elif text.startswith("중지됨"):
            self._upd(iid, "rec_stopped")
        elif text.startswith("실패"):
            self.counts["fail"] = self.counts.get("fail", 0) + 1
            self._upd(iid, "rec_fail", note=text.split(":", 1)[-1].strip())
        else:
            note = "파일 합치는 중" if text == "저장 중" else text if text == "준비 중" else f"준비 중: {text}"
            self._upd(iid, "rec", persist=r["st"] != "rec", note=note, pct=None, left=None)
            if text.startswith(("강의 여는", "녹화 시작")):
                self.tree.see(iid)

    def on_saved(self, path, ok=True):
        """자동 녹화 강의 하나가 MP4로 합쳐짐 (끝까지 녹화했는지는 아직 모름). ok=False면 합치기 실패"""
        key = os.path.normcase(os.path.normpath(path)) if path else None
        if not ok:
            for iid in list(self.completed):
                if os.path.normcase(self.rows[iid].get("raw") or "") == key:
                    self.completed.discard(iid)
                    self.counts["fail"] = self.counts.get("fail", 0) + 1
                    self._upd(iid, "rec_fail", note="파일 합치기 실패 (직접 녹화 창의 복구 탭에서 다시 합칠 수 있어요)")
            return
        self.muxed.add(key)
        self._dispatch()

    def _dispatch(self):
        """끝까지 녹화를 마치고 합치기도 끝난 강의 → 파일 확인 → 정상이면 녹화 완료 (작업에 따라 인코딩 대기)
        (중지·종료·오류로 중간에 끊긴 파일은 '녹화 완료'가 되지 않음)"""
        for iid in list(self.completed):
            p = self.rows[iid].get("raw") if iid in self.rows else None
            key = os.path.normcase(os.path.normpath(p)) if p else None
            if key not in self.muxed:
                continue  # 아직 합치는 중
            self.completed.discard(iid)
            self.muxed.discard(key)
            self._verify(iid, p, lambda iid=iid: self._recorded(iid),
                         lambda problem, iid=iid: self._record_bad(iid, problem))

    def _recorded(self, iid):
        self.counts["rec"] = self.counts.get("rec", 0) + 1
        self._upd(iid, "enc_wait" if has_enc(self.rows[iid]["task"]) else "rec_done")
        self._pump()

    def _record_bad(self, iid, problem):
        self._upd(iid, "rec_wait", note=f"녹화 파일 이상: {problem}")
        self._requeue(iid)
        self.lbl.config(text=f"{self.rows[iid]['no']}강 파일이 이상해요 ({problem}) → 다시 녹화해요")

    def _finished(self, msg):
        """AutoRecorder가 끝남 (녹화 끝) → '모든 녹화 후 인코딩'이면 이제 인코딩"""
        self.runner = None
        self.cur_row = None
        self.lbl.config(text=msg)
        self._pump()
        self._check_done()

    # -- 인코딩
    def _pump(self):
        """인코딩할 차례가 된 강의(인코딩 대기)를 용량 줄이기로 보냄 (진행 방식에 따라)"""
        if not self.session or self.stopped or self.flow() == "rec":
            return
        if self.flow() == "all" and self.runner is not None:
            return  # 녹화가 모두 끝난 뒤에
        items = []
        for iid in self.tree.get_children():
            r = self.rows[iid]
            if iid in self.enc_sent or iid in self.renaming or r["st"] != "enc_wait" or not has_enc(r["task"]):
                continue
            src = r.get("raw")
            if not src or not os.path.isfile(src):
                self._upd(iid, "missing", persist=False)
                continue
            self.enc_sent[iid] = os.path.normcase(os.path.normpath(src))
            items.append((src, lambda iid=iid, src=src: self._enc_job(iid, src)))
        if items:
            self._persist()
            self.app.encode.enqueue(items)

    def _enc_job(self, iid, src):
        """인코딩을 시작할 때 부름 → (설정, 끝나면 원본 제거, 만들 파일) - 작업 중에 바꾼 설정도 반영"""
        d, name = os.path.split(src)
        if name.endswith(RAW_TAG + ".mp4"):
            out = os.path.join(d, name[:-len(RAW_TAG + ".mp4")] + ".mp4")
        else:  # 예전 이름 ('제 1 강.mp4')
            out = os.path.join(d, self._stem(iid) + ".mp4") if self.tree.exists(iid) else None
        return self.enc_options(), bool(self.var_edel.get()), out

    def _enc_row(self, src):
        key = os.path.normcase(os.path.normpath(src))
        return next((i for i, k in self.enc_sent.items() if k == key), None)

    def encode_progress(self, src, pct, left):
        """인코딩 진행률 → 표에 표시"""
        iid = self._enc_row(src)
        if iid:
            first = self.rows[iid]["st"] != "enc"
            self._upd(iid, "enc", persist=first, note="준비 중" if pct <= 0 else "",
                      pct=pct if pct > 0 else None, left=left)

    def _encoded(self, src, result, out=None, msg="", speed=None):
        """인코딩이 끝남 (ok / fail / stopped / missing) → 표에 표시"""
        iid = self._enc_row(src)
        if not iid:
            return
        self.enc_sent.pop(iid, None)
        if not self.tree.exists(iid):
            return
        if result == "ok":
            self.counts["enc"] = self.counts.get("enc", 0) + 1
            self._learn_speed(speed)
            raw = src if os.path.isfile(src) else ""
            self._upd(iid, "enc_done", raw=raw, enc=os.path.normpath(out) if out else "")
            if out:  # 인코딩한 파일도 길이·용량 확인
                self._verify(iid, out, lambda: None,
                             lambda problem, iid=iid: self._upd(iid, "enc_fail", note=f"인코딩한 파일 이상 ({problem})"))
        elif result == "stopped":
            self._upd(iid, "enc_stopped")
        elif result == "missing":
            self._upd(iid, "missing")
        else:
            self.counts["fail"] = self.counts.get("fail", 0) + 1
            self._upd(iid, "enc_fail", note=msg)
        self._pump()
        self._check_done()

    # -- 중지 / 끝
    def stop(self):
        if not self.session:
            return
        if not messagebox.askyesno(
                "작업 중지", "작업을 멈출까요?\n"
                           "지금 녹화 중인 강의는 다음에 시작하면 처음부터 다시 녹화하고,\n"
                           "인코딩 중인 파일은 만들던 파일을 지워요."):
            return
        self.stopped = True
        if self.runner:
            self.runner.stop()
        dropped = self.app.encode.drop_auto()  # 아직 시작 안 한 인코딩은 '인코딩 대기'로 남김
        for iid, key in list(self.enc_sent.items()):
            if key in dropped:
                self.enc_sent.pop(iid, None)
        self.app.encode.stop()
        self.btn_stop.config(state="disabled")
        self.lbl.config(text="멈추는 중...")
        self._check_done()

    def _check_done(self):
        """녹화·합치기·파일 확인·인코딩이 모두 끝났으면 작업 끝"""
        if not self.session or self.runner or self.app.busy or self.verifying or self.enc_sent or self.completed:
            return
        if not self.stopped:
            self._pump()
            if self.enc_sent:
                return
        self._end_session()

    def _end_session(self):
        self.session = False
        self.run_rows = []
        for w in (self.btn_start, self.btn_add, self.btn_del, self.btn_clear, self.btn_edge):
            w.config(state="normal")
        self.btn_stop.config(state="disabled")
        c = self.counts
        msg = f"작업 {'중지' if self.stopped else '끝'} - 녹화 완료 {c.get('rec', 0)}개, 인코딩 완료 {c.get('enc', 0)}개" \
              + (f", 실패 {c['fail']}개" if c.get("fail") else "")
        self.lbl.config(text=msg)
        self._update_total()
        self._persist()
        if self.var_shutdown.get() and not self.stopped:
            self.lbl.config(text=msg + "  ·  모든 작업이 끝나면 컴퓨터를 꺼요")
            self.after(3000, self._shutdown_when_idle)
        elif not (self.app.recording or self.app.sched or self.app.busy or self.app.encode.busy):
            keep_awake(False)

    def _shutdown_when_idle(self):
        """MP4 변환·인코딩까지 모두 끝나면 1분 뒤 컴퓨터 종료 (그 사이 취소 가능)"""
        if not self.var_shutdown.get() or self.session:
            return
        if self.app.busy or self.app.encode.busy or self.app.recording:
            self.after(3000, self._shutdown_when_idle)
            return
        d = tk.Toplevel(self)
        d.title("컴퓨터 종료")
        d.transient(self.winfo_toplevel())
        d.resizable(False, False)
        d.attributes("-topmost", True)
        f = ttk.Frame(d, padding=20)
        f.pack()
        lbl = ttk.Label(f, text="", font=("", 11), justify="center")
        lbl.pack(pady=(0, 14))
        left = [60]

        def cancel():
            left[0] = None
            self.var_shutdown.set(False)
            d.destroy()
            self.lbl.config(text="컴퓨터 종료를 취소했어요.")
            keep_awake(False)

        ttk.Button(f, text="취소", style="Big.TButton", command=cancel).pack()
        d.protocol("WM_DELETE_WINDOW", cancel)

        def count():
            if left[0] is None or not d.winfo_exists():
                return
            if left[0] <= 0:
                d.destroy()
                self.app.shutdown_pc()
                return
            lbl.config(text=f"모든 작업이 끝났어요.\n{left[0]}초 뒤에 컴퓨터를 꺼요.")
            left[0] -= 1
            self.after(1000, count)
        count()
        d.update_idletasks()
        top = self.winfo_toplevel()
        d.geometry(f"+{top.winfo_rootx() + (top.winfo_width() - d.winfo_reqwidth()) // 2}"
                   f"+{top.winfo_rooty() + (top.winfo_height() - d.winfo_reqheight()) // 3}")


class App:
    def __init__(self, root, mode="direct", cfg=None, peer=None, on_closed=None):
        self.root = root
        self.mode = mode  # "auto": 자동 녹화 창 (처음 열림), "direct": 직접 녹화 창 (탭 4개)
        self.peer = peer            # 직접 녹화 창: 이 창을 연 자동 녹화 창
        self.on_closed = on_closed  # 직접 녹화 창이 닫힐 때 알림
        self.direct = None          # 자동 녹화 창: 열려 있는 직접 녹화 창
        self.devices_lent = False   # 자동 녹화 창: 캡쳐 장치를 직접 녹화 창에 넘겨줌
        self.alive = True
        self.closing = False
        root.title(APP_NAME if mode == "auto" else f"{APP_NAME} - 직접 녹화")
        try:
            root.iconbitmap(resource_path("capturedeck.ico"))
        except tk.TclError:
            pass
        self.cfg = load_config() if cfg is None else cfg
        self.video = None
        self.audio = None
        self.recording = False
        self.rec_start = 0.0
        self.rec_base = None
        self.rec_paths = None      # 지금 녹화의 (영상 임시 파일, 소리 임시 파일, 합칠 MP4)
        self.busy = False
        self.close_pending = False  # MP4 변환 중 창을 닫으면 변환 후 종료
        self.last_audio_try = 0.0
        self.sched = None          # 예약된 (시작, 종료) datetime
        self.sched_rec = False     # 지금 녹화가 예약으로 시작된 것인지
        self.sched_armed = False   # '지금부터' 예약: '녹화 시작'을 기다리는 중

        self.video_names = []
        self.audio_devs = []
        self._build_ui()
        self._restore_window()
        self.refresh_devices(auto=True)
        self.tick()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- UI
    def _build_ui(self):
        auto = self.mode == "auto"
        self._style()
        if auto:
            # 자동 녹화: 탭 없이 한 화면. 복구/자르기/용량 줄이기 패널은 화면에 넣지 않고 기능만 씀
            self.nb = None
            self.auto = AutoPanel(self.root, self)
            self.auto.pack(fill="both", expand=True)
            r = self.cap = self.auto.left
            parent = ttk.Frame(self.root)  # pack하지 않음 (안 보이는 패널들의 부모)
        else:
            self.nb = ttk.Notebook(self.root)
            self.nb.pack(fill="both", expand=True)
            self.auto = NoAuto()
            r = self.cap = ttk.Frame(self.nb)
            parent = self.nb
        self.recover = RecoverPanel(parent, get_dir=lambda: self.var_dir.get(), get_active=self.all_active_files,
                                    on_idle=self._maybe_close)
        self.trim = TrimPanel(parent, get_dir=lambda: self.var_dir.get())
        self.encode = EncodePanel(parent, get_dir=lambda: self.var_dir.get(), cfg=self.cfg,
                                  on_busy=self._on_encode_busy)
        if not auto:  # 탭 순서: 녹화 / 자르기 / 인코딩 / 복구
            self.nb.add(r, text="  녹화  ")
            self.nb.add(self.trim, text="  자르기  ")
            self.nb.add(self.encode, text="  인코딩  ")
            self.nb.add(self.recover, text="  복구  ")
            self.nb.bind("<<NotebookTabChanged>>", self._on_tab)
        self.encode.on_auto_done = self.auto._encoded
        if auto:
            self.encode.on_auto_progress = self.auto.encode_progress
        self.trim.get_compress = self.encode.options
        self.trim.send_to_encode = self._send_to_encode
        self.trim.var_compress.set(bool(self.cfg.get("trim_compress", True)))
        self.root.bind("<space>", self._on_space)

        # 미리보기: 아래 조작부(높이 고정)를 뺀 나머지 공간을 모두 차지
        self.preview = FitView(r)
        self.preview.pack(fill="both", expand=True, padx=10, pady=(10, 8))
        self.prev_next = 0.0  # 다음 미리보기를 그릴 시각 (그리는 데 걸린 시간에 맞춰 조절)

        if auto:
            # 자동 녹화: 설정은 탭 두 개로 접어 두고 미리보기를 크게
            snb = ttk.Notebook(r)
            snb.pack(fill="x", padx=10, pady=(0, 10))
            dev = ttk.Frame(snb, padding=(10, 8))
            snb.add(dev, text="  캡쳐 장치  ")
            save = ttk.Frame(snb, padding=(10, 8))
            snb.add(save, text="  저장 · 파일 이름 · 인코딩  ")
            self._build_devices(dev)
            fo = ttk.Frame(save)
            fo.pack(fill="x")
            ttk.Label(fo, text="저장 폴더").pack(side="left", padx=(0, 8))
            self._build_folder(fo)
            self.auto.build_encode(save)
            self._build_schedule(ttk.Frame(r))  # 자동 녹화에서는 예약을 쓰지 않음 (화면에 넣지 않음)
        else:
            sec = ttk.Frame(r)
            sec.pack(fill="x", padx=10, pady=(0, 8))
            sec.columnconfigure(1, weight=1)
            dev = ttk.LabelFrame(sec, text="캡쳐 장치", style="Section.TLabelframe", padding=(10, 4, 10, 8))
            dev.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 10))
            self._build_devices(dev)
            sch = ttk.LabelFrame(sec, text="예약 녹화", style="Section.TLabelframe", padding=(10, 4, 10, 8))
            sch.grid(row=0, column=1, sticky="nsew")
            self._build_schedule(sch)
            fo = ttk.LabelFrame(sec, text="저장 폴더", style="Section.TLabelframe", padding=(10, 4, 10, 8))
            fo.grid(row=1, column=1, sticky="nsew", pady=(8, 0))
            self._build_folder(fo)

        b = ttk.Frame(r)
        self.btn_rec = ttk.Button(b, text="●  녹화 시작", style="Big.TButton", command=self.toggle_rec)
        self.btn_rec.pack(side="left")
        self.var_trim_after = tk.BooleanVar(value=self.cfg.get("trim_after", True))
        ttk.Checkbutton(b, text="녹화 종료 후 바로 편집", variable=self.var_trim_after,
                        command=self.on_trim_after).pack(side="right")
        if auto:  # 녹화 시간·장치 상태는 아래 상태 줄 오른쪽에
            bar = self.auto.statusbar
            self.lbl_status = ttk.Label(bar, text="", style="Hint.TLabel")
            self.lbl_status.pack(side="right")
            self.lbl_time = ttk.Label(bar, text="00:00:00", font=("Consolas", 11))
            self.lbl_time.pack(side="right", padx=(0, 14))
        else:
            ttk.Separator(r).pack(fill="x")
            b.pack(fill="x", padx=10, pady=8)
            self.lbl_time = ttk.Label(b, text="00:00:00", font=("Consolas", 16))
            self.lbl_time.pack(side="left", padx=14)
            self.lbl_status = ttk.Label(b, text="", style="Hint.TLabel")
            self.lbl_status.pack(side="left")

    def _style(self):
        """버튼·구역 제목·안내 글자 모양"""
        st = ttk.Style(self.root)
        try:
            fam = tkfont.nametofont("TkDefaultFont").actual("family")
        except tk.TclError:
            fam = ""
        st.configure("Big.TButton", font=(fam, 10, "bold"), padding=(14, 5))
        st.configure("Section.TLabelframe.Label", font=(fam, 9, "bold"), foreground="#1a5fb4")
        st.configure("Hint.TLabel", foreground="#666")
        st.configure("Treeview", rowheight=24)

    def _build_devices(self, f):
        """캡쳐 장치: 비디오/오디오 장치, 해상도/FPS, 새로고침, 소리 듣기, 오디오 레벨"""
        g = ttk.Frame(f)
        g.pack(fill="x")
        ttk.Label(g, text="비디오 장치").grid(row=0, column=0, sticky="w")
        self.cb_video = ttk.Combobox(g, state="readonly", width=34)
        self.cb_video.grid(row=0, column=1, padx=(8, 0), pady=3, sticky="w")
        ttk.Label(g, text="오디오 장치").grid(row=1, column=0, sticky="w")
        self.cb_audio = ttk.Combobox(g, state="readonly", width=34)
        self.cb_audio.grid(row=1, column=1, padx=(8, 0), pady=3, sticky="w")
        ttk.Label(g, text="해상도").grid(row=0, column=2, padx=(16, 0), sticky="w")
        self.cb_res = ttk.Combobox(g, values=RESOLUTIONS, width=10, state="readonly")
        self.cb_res.set(self.cfg["resolution"])
        self.cb_res.grid(row=0, column=3, padx=(8, 0), sticky="w")
        ttk.Label(g, text="FPS").grid(row=1, column=2, padx=(16, 0), sticky="w")
        self.cb_fps = ttk.Combobox(g, values=FPS_LIST, width=10, state="readonly")
        self.cb_fps.set(self.cfg["fps"])
        self.cb_fps.grid(row=1, column=3, padx=(8, 0), sticky="w")
        ttk.Button(g, text="장치 새로고침", command=lambda: self.refresh_devices(auto=False)
                   ).grid(row=0, column=4, padx=(16, 0), sticky="w")
        self.var_mon = tk.BooleanVar(value=self.cfg.get("monitor", False))
        ttk.Checkbutton(g, text="소리 듣기", variable=self.var_mon,
                        command=self.on_monitor).grid(row=1, column=4, padx=(16, 0), sticky="w")
        ttk.Label(g, text="오디오 레벨").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.level_bar = ttk.Progressbar(g, maximum=100)
        self.level_bar.grid(row=2, column=1, padx=(8, 0), pady=(4, 0), sticky="ew")
        ttk.Label(g, text="듣는 장치").grid(row=2, column=2, padx=(16, 0), pady=(4, 0), sticky="w")
        self.out_devs = list_output_devices()
        self.cb_out = ttk.Combobox(g, state="readonly", width=28, values=[n for _, n in self.out_devs])
        saved = self.cfg.get("monitor_out", "")
        if saved not in self.cb_out["values"]:
            saved = DEFAULT_OUT
            if "TO USB" in default_output_name().upper():  # 기본 출력이 캡쳐 보드로 가면 다른 장치(스피커/모니터)를
                saved = next((n for i, n in self.out_devs if i is not None and "TO USB" not in n.upper()), saved)
        self.cb_out.set(saved)
        self.cb_out.grid(row=2, column=3, columnspan=2, padx=(8, 0), pady=(4, 0), sticky="ew")
        self.cb_out.bind("<<ComboboxSelected>>", lambda e: self.on_monitor())
        for cb in (self.cb_video, self.cb_audio, self.cb_res, self.cb_fps):
            cb.bind("<<ComboboxSelected>>", lambda e: self.on_settings_changed())

    def _build_folder(self, f):
        """저장 폴더 입력 + 폴더 선택/열기"""
        self.var_dir = tk.StringVar(value=self.cfg["out_dir"])
        ttk.Button(f, text="열기", width=6, command=self.open_dir).pack(side="right")
        ttk.Button(f, text="선택...", width=7, command=self.choose_dir).pack(side="right", padx=(6, 6))
        ttk.Entry(f, textvariable=self.var_dir).pack(side="left", fill="x", expand=True)

    def _build_schedule(self, s):
        """예약 녹화: 시간 지정 / 지금부터 몇 시간"""
        self.var_s_mode = tk.StringVar(value=self.cfg["sched_mode"])
        # 방식 1: 몇 시 몇 분부터 몇 시 몇 분까지
        r1 = ttk.Frame(s)
        r1.grid(row=0, column=0, sticky="w", pady=2)
        self.rb_range = ttk.Radiobutton(r1, text="시간 지정", value="range", variable=self.var_s_mode, width=9)
        self.rb_range.pack(side="left")
        self.var_s_start = tk.StringVar(value=self._clock_or(self.cfg["sched_start"], "09:00:00"))
        self.var_s_end = tk.StringVar(value=self._clock_or(self.cfg["sched_end"], "10:00:00"))
        self.ent_s_start = ttk.Entry(r1, textvariable=self.var_s_start, width=9, justify="center")
        self.ent_s_start.pack(side="left", padx=(6, 4))
        ttk.Label(r1, text="부터").pack(side="left")
        self.ent_s_end = ttk.Entry(r1, textvariable=self.var_s_end, width=9, justify="center")
        self.ent_s_end.pack(side="left", padx=(8, 4))
        ttk.Label(r1, text="까지").pack(side="left")
        ttk.Label(r1, text="(24시간, 예: 14:30:00)", style="Hint.TLabel").pack(side="left", padx=(8, 0))
        # 방식 2: 지금부터 몇 시간 몇 분 몇 초간
        r2 = ttk.Frame(s)
        r2.grid(row=1, column=0, sticky="w", pady=2)
        self.rb_dur = ttk.Radiobutton(r2, text="지금부터", value="duration", variable=self.var_s_mode, width=9)
        self.rb_dur.pack(side="left")
        dh, dm, ds = self._initial_duration()
        self.var_s_h, self.var_s_m, self.var_s_s = (tk.StringVar(value=str(v)) for v in (dh, dm, ds))
        self.dur_entries = []
        for i, (var, unit) in enumerate(((self.var_s_h, "시간"), (self.var_s_m, "분"), (self.var_s_s, "초간"))):
            ent = ttk.Entry(r2, textvariable=var, width=4, justify="center")
            ent.pack(side="left", padx=(6 if i == 0 else 8, 4))
            ttk.Label(r2, text=unit).pack(side="left")
            self.dur_entries.append(ent)
        # 지금부터 녹화하면 몇 시 몇 분 몇 초에 끝나는지 실시간 표시
        self.lbl_s_until = ttk.Label(r2, text="", foreground="#1a5fb4")
        self.lbl_s_until.pack(side="left", padx=(10, 0))
        self.btn_sched = ttk.Button(s, text="예약", command=self.toggle_schedule)
        self.btn_sched.grid(row=0, column=1, padx=(16, 0), sticky="ew")
        self.btn_sched_end = ttk.Button(s, text="종료 시간 변경", command=self.change_sched_end, state="disabled")
        self.btn_sched_end.grid(row=1, column=1, padx=(16, 0), sticky="ew")
        for e in (self.ent_s_end, *self.dur_entries):  # 예약 중 Enter로도 종료 시간 변경
            e.bind("<Return>", lambda ev: self.change_sched_end() if self.sched else None)
        self.lbl_sched = ttk.Label(s, text="예약 없음", style="Hint.TLabel")
        self.lbl_sched.grid(row=0, column=2, rowspan=2, sticky="w", padx=(16, 0))
        self.sched_inputs = (self.rb_range, self.rb_dur, self.ent_s_start, self.ent_s_end, *self.dur_entries)

    # ---- 장치
    def refresh_devices(self, auto):
        if self.devices_lent:
            if not auto:
                messagebox.showinfo("캡쳐 장치", "직접 녹화 창에서 캡쳐 장치를 쓰고 있어요. 직접 녹화 창을 닫으면 다시 연결돼요.")
            return
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
        if self.devices_lent:
            messagebox.showinfo("캡쳐 장치", "직접 녹화 창에서 캡쳐 장치를 쓰고 있어요. 직접 녹화 창을 닫은 뒤에 바꿔 주세요.")
            for cb, key in ((self.cb_video, "video"), (self.cb_audio, "audio"), (self.cb_res, "resolution"),
                            (self.cb_fps, "fps")):
                cb.set(self.cfg[key])
            return
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
        self.audio.out_device = self._out_device()
        if self.audio.start():
            self.audio.set_monitor(self.var_mon.get())

    def _out_device(self):
        name = self.cb_out.get()
        return next((i for i, n in self.out_devs if n == name), None)

    def on_monitor(self):
        self.cfg["monitor"] = self.var_mon.get()
        self.cfg["monitor_out"] = self.cb_out.get()
        save_config(self.cfg)
        if self.audio:
            self.audio.set_out_device(self._out_device())
            self.audio.set_monitor(self.var_mon.get())
        # 듣는 장치가 캡쳐 보드로 들어가는 출력(예: HDMI)이면 소리가 녹화에 다시 섞이고 스피커로는 안 나옴
        out = self.cb_out.get()
        if self.var_mon.get() and out == DEFAULT_OUT:
            out = default_output_name()
        if self.var_mon.get() and out and "TO USB" in out.upper():
            messagebox.showwarning(
                "소리 듣기", f"듣는 장치 '{out}'은(는) 캡쳐 보드로 들어가는 출력 같아요.\n"
                           "이 장치로 내보내면 스피커로는 안 들리고, 녹화에 소리가 겹쳐 들어갈 수 있어요.\n"
                           "'듣는 장치'에서 스피커나 헤드폰을 골라 주세요.")

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

    def open_edge(self):
        """자동녹화 전용 프로필로 Edge 실행 (이미 떠 있으면 그 창을 그대로 사용)"""
        import edge_cdp
        self.auto.btn_edge.config(state="disabled")

        def work():
            try:
                already = edge_cdp.is_running()
                if not already:
                    edge_cdp.launch()
                msg = ("자동녹화용 Edge가 이미 열려 있어요." if already
                       else "자동녹화용 Edge를 열었어요.")
                self.root.after(0, lambda: (self.lbl_status.config(text=msg), self.auto.lbl.config(text=msg)))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: messagebox.showerror("Edge 열기", err))
            finally:
                self.root.after(0, lambda: self.auto.btn_edge.config(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    # ---- 녹화
    def toggle_rec(self):
        if self.auto.running:  # 자동 녹화 중에는 자동 녹화를 멈추는 버튼으로
            self.auto.stop()
            return
        if self.busy:
            return
        if not self.recording:
            if self.start_recording() and self.sched_armed:
                self._begin_armed()
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

    def _armed_text(self):
        total = self._get_duration()
        if total is None:
            return "녹화 대기 - 녹화할 시간을 확인해 주세요"
        return (f"녹화 대기 - '녹화 시작'을 누르면 {total // 3600}시간 {total % 3600 // 60}분 {total % 60}초 "
                f"녹화하고 멈춰요")

    def _begin_armed(self):
        """'지금부터' 예약 대기 중에 '녹화 시작'을 누름 → 지금부터 지정한 시간 뒤에 멈추는 예약 녹화로 전환"""
        self.sched_armed = False
        total = self._get_duration()
        if total is None:
            self.cancel_schedule()
            self.lbl_sched.config(text="녹화할 시간이 잘못돼서 일반 녹화로 시작했어요")
            return
        now = datetime.now()
        self.sched = (now, now + timedelta(seconds=total))
        self.sched_rec = True
        self._save_duration(total)
        save_config(self.cfg)
        self.btn_sched_end.config(state="normal")
        keep_awake(True)
        self._check_schedule()

    def toggle_schedule(self):
        if self.sched or self.sched_armed:
            self.cancel_schedule()
            return
        if self.auto.running:
            messagebox.showwarning("예약", "자동 녹화 중에는 예약할 수 없어요.")
            return
        mode = self.var_s_mode.get()
        now = datetime.now()
        if mode == "duration":
            total = self._get_duration()
            if total is None:
                messagebox.showwarning("예약", "녹화할 시간을 시간/분/초 숫자로 입력해 주세요. (예: 0시간 30분 15초)")
                return
            self.cfg["sched_mode"] = mode
            self._save_duration(total)
            if not self.recording:
                # 바로 시작하지 않고 대기 → '녹화 시작'을 누르면 그때부터 지정한 시간만큼 녹화
                save_config(self.cfg)
                self.sched_mode = mode
                self.sched_armed = True
                self.btn_sched.config(text="예약 취소")
                for w in self.sched_inputs:
                    w.config(state="disabled")
                for w in self.dur_entries:  # 시간은 녹화 시작 전까지 계속 바꿀 수 있음
                    w.config(state="normal")
                self.lbl_sched.config(text=self._armed_text())
                return
            # 이미 녹화 중이면 지금부터 지정한 시간 뒤에 멈춤
            self.sched = (now, now + timedelta(seconds=total))
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
        self.sched_armed = False
        self.btn_sched.config(text="예약")
        self.btn_sched_end.config(state="disabled")
        self.lbl_sched.config(text="예약 없음")
        for w in self.sched_inputs:
            w.config(state="normal")
        if not self.recording and not self.busy:  # MP4 변환 중이면 끝날 때까지 절전 방지 유지
            keep_awake(False)

    def _check_schedule(self):
        if self.sched_armed:  # '녹화 시작' 대기 중 (입력한 시간이 바뀌면 안내도 바뀜)
            text = self._armed_text()
            if self.lbl_sched.cget("text") != text:
                self.lbl_sched.config(text=text)
            return
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

    def start_recording(self, quiet=False, auto_base=None):
        if not self.video or self.video.get_frame() is None:
            if not quiet:
                messagebox.showwarning("녹화 불가", "영상 신호가 아직 들어오지 않았어요.")
            return False
        out_dir = self.var_dir.get()
        os.makedirs(out_dir, exist_ok=True)
        self.cfg["out_dir"] = out_dir
        save_config(self.cfg)
        if auto_base:  # 자동 녹화: '제 1 강_제목(녹화중).mkv/.wav' → '제 1 강_제목(원본).mp4'
            self.rec_base = auto_base
        else:  # 파일 이름 끝의 날짜_시각은 '복구' 탭에서 녹화 시각으로 씀
            self.rec_base = os.path.join(out_dir, "capture_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.rec_paths = rec_paths(self.rec_base, bool(auto_base))
        self.has_audio = bool(self.audio and self.audio.running)
        if self.has_audio:
            self.audio.start_rec(self.rec_paths[1])
        self.video.start_rec(self.rec_paths[0])
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
        threading.Thread(target=self._mux, args=(self.rec_base, self.rec_paths, self.has_audio), daemon=True).start()

    def _mux(self, base, paths, has_audio, sync=False):
        vid, wav, out = paths
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
               else f"변환 실패 - '복구' 탭에서 다시 시도할 수 있어요 (원인: {os.path.basename(base)}.log)")
        if not sync:
            self.root.after(0, self._mux_done, msg, out, ok)
        return ok

    def _mux_done(self, msg, path=None, ok=True):
        out = path if ok else None
        self.busy = False
        self.lbl_status.config(text=msg)
        if not self.recording and not self.sched and not self.auto.running:
            keep_awake(False)
        if self.close_pending:  # 변환 중에 창을 닫았으면 이제 종료
            self._maybe_close()
            return
        if path and self.auto.running:  # 자동 녹화 강의 저장됨(또는 실패) → 확인 후 (작업에 따라) 인코딩
            self.auto.on_saved(path, ok)
        if out and self.var_trim_after.get() and not self.recording and not self.auto.running \
                and self.trim.load(out):
            if self.nb:
                self.nb.select(self.trim)

    def on_trim_after(self):
        self.cfg["trim_after"] = self.var_trim_after.get()
        save_config(self.cfg)

    # ---- 주기 업데이트
    def tick(self):
        if not self.alive:
            return
        # 미리보기 (캡쳐 탭이 보일 때만)
        # 미리보기가 크면 그리는 데 시간이 걸리므로, 그 시간에 맞춰 그리는 횟수를 줄임
        # (화면 그리기가 녹화를 방해하지 않도록 녹화 중에는 더 여유를 둠)
        now = time.perf_counter()
        if self.video and (self.nb is None or self.nb.select() == str(self.cap)) and now >= self.prev_next:
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
            if not self.recording and not self.devices_lent and self.cb_audio.get() \
                    and time.time() - self.last_audio_try > 2:
                if self.audio:
                    self.audio.stop()
                self.start_audio()
        # 상태 표시
        if self.devices_lent and not self.busy:
            self.lbl_status.config(text="직접 녹화 창에서 캡쳐 장치를 쓰는 중")
        elif not self.busy:
            v = self.video.status if self.video else "비디오 장치 없음"
            a = "오디오 정상" if (self.audio and self.audio.running) else "오디오 연결 안 됨"
            if self.audio and self.audio.error and (not self.audio.running or self.audio.error.startswith("모니터링")):
                a += f" ({self.audio.error[:40]})"
            self.lbl_status.config(text=f"{v}  |  {a}")
        self._check_schedule()
        self._update_until_label()
        if self.recording:
            t = int(time.time() - self.rec_start)
            self.lbl_time.config(text=f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}")
        self.root.after(33, self.tick)

    # ---- 직접 녹화 창
    def open_direct(self):
        """자동 녹화 창 오른쪽 위 '직접 녹화' → 직접 녹화 창 (캡쳐 장치는 그 창으로 넘겨줌)"""
        if self.direct and self.direct.alive:
            top = self.direct.root
            top.deiconify()
            top.lift()
            top.focus_force()
            return
        if self.recording or self.busy or self.auto.runner:
            messagebox.showinfo("직접 녹화", "자동 녹화 중에는 열 수 없어요. 녹화가 끝난 뒤에 열어 주세요.\n"
                                          "(인코딩만 하는 중이면 열 수 있어요)")
            return
        self.save_all()  # 지금 장치·폴더 설정을 직접 녹화 창이 이어받음
        self._release_devices()
        top = tk.Toplevel(self.root)
        self.direct = App(top, "direct", self.cfg, peer=self, on_closed=self._direct_closed)
        self.auto.lbl.config(text="직접 녹화 창을 열었어요. 캡쳐 장치는 직접 녹화 창이 써요 (닫으면 이 창으로 돌아와요).")

    def _release_devices(self):
        self.devices_lent = True
        if self.video:
            self.video.stop()
            self.video.join(timeout=3)  # 장치를 완전히 놓은 뒤에 직접 녹화 창이 열도록
            self.video = None
        if self.audio:
            self.audio.stop()
            self.audio = None
        self.level_bar["value"] = 0

    def _direct_closed(self):
        """직접 녹화 창이 닫힘 → 그 창에서 바꾼 장치·폴더 설정으로 캡쳐 장치를 다시 엶"""
        self.direct = None
        self.devices_lent = False
        if self.closing or not self.alive:
            return
        cfg = self.cfg
        self.var_dir.set(cfg["out_dir"])
        self.cb_res.set(cfg["resolution"])
        self.cb_fps.set(cfg["fps"])
        self.var_mon.set(bool(cfg.get("monitor")))
        if cfg.get("monitor_out") in self.cb_out["values"]:
            self.cb_out.set(cfg["monitor_out"])
        self.refresh_devices(auto=True)
        self.auto.lbl.config(text="직접 녹화 창을 닫았어요. 캡쳐 장치를 다시 연결했어요.")

    def active_files(self):
        """이 창에서 지금 녹화·변환·자르기·인코딩 중인 파일 (normcase)"""
        files = []
        if self.rec_paths and (self.recording or self.busy):
            vid, wav, out = self.rec_paths
            files += [vid, wav, out, os.path.splitext(out)[0] + ".part.mp4"]
        if self.encode.busy:
            files += [self.encode.current, self.encode.current_part]
        if self.trim.proc:
            files.append(self.trim.out_part)
        return {os.path.normcase(os.path.normpath(f)) for f in files if f}

    def all_active_files(self):
        """두 창(자동·직접 녹화)에서 지금 쓰고 있는 파일 → 복구가 건드리지 않음"""
        files = self.active_files()
        for other in (self.peer, self.direct):
            if other and other.alive:
                files |= other.active_files()
        return files

    def _on_tab(self, ev=None):
        self.trim.pause()
        if self.nb.select() == str(self.recover):
            self.recover.shown()

    def _send_to_encode(self, path):
        self.encode.add_files([path])
        if self.nb:
            self.nb.select(self.encode)

    def _on_encode_busy(self, busy):
        """인코딩 중에는 절전 방지 (끝나면 녹화/예약/변환 중이 아닐 때만 해제)"""
        if busy:
            keep_awake(True)
        elif not (self.recording or self.sched or self.busy or self.auto.running):
            keep_awake(False)

    def _maybe_close(self):
        """변환/복구 중에 창을 닫았던 경우, 모두 끝났으면 종료"""
        if self.close_pending and not self.busy and not self.recover.busy:
            self.on_close()

    def on_close(self):
        if self.direct and self.direct.alive:  # 자동 녹화 창을 닫으면 직접 녹화 창부터
            self.closing = True
            self.direct.on_close()
            if self.direct and self.direct.alive:  # 직접 녹화 창에서 취소함 (또는 변환이 끝나길 기다림)
                self.closing = False
                return
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
        if self.auto.runner:
            if not self.recording and not messagebox.askyesno("종료", "자동 녹화 중이에요. 멈추고 종료할까요?"):
                return
            self.auto.runner.stop()
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
            self._mux(self.rec_base, self.rec_paths, self.has_audio, sync=True)
        if self.video:
            self.video.stop()
            self.video.join(timeout=3)  # 다른 창이 바로 장치를 열 수 있도록 완전히 놓을 때까지
        if self.audio:
            self.audio.stop()
        self.save_all()
        self.alive = False
        self.root.destroy()
        if self.on_closed:
            self.on_closed()

    def _on_space(self, ev):
        """자르기 탭에서 스페이스바 = 재생/일시정지 (입력칸에 글자 입력 중일 때는 제외)"""
        if self.nb and self.nb.select() == str(self.trim) \
                and not isinstance(ev.widget, (tk.Entry, ttk.Entry, tk.Text)):
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
        pre = "auto_" if self.mode == "auto" else ""
        m = re.fullmatch(r"(?:(\d+)x(\d+))?\+(-?\d+)\+(-?\d+)", self.cfg.get(pre + "geometry", ""))
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
        if self.cfg.get(pre + "zoomed"):
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass
        if self.nb:
            try:
                self.nb.select(int(self.cfg.get("tab", 0)))
            except (ValueError, tk.TclError):
                pass

    def _window_geometry(self):
        """저장할 창 크기·위치. 최대화 상태면 최대화 여부만 바꾸고 원래 크기는 그대로 둠"""
        pre = "auto_" if self.mode == "auto" else ""
        if self.root.state() == "zoomed":
            return {pre + "zoomed": True}
        return {pre + "zoomed": False, pre + "geometry": self.root.wm_geometry()}

    def shutdown_pc(self):
        """설정을 저장하고 컴퓨터 종료"""
        try:
            self.save_all()
        except Exception:
            pass
        if sys.platform == "win32":
            subprocess.Popen(["shutdown", "/s", "/t", "5"], creationflags=0x08000000)
        self.on_close()

    def save_all(self):
        """현재 화면의 모든 설정을 settings.ini에 저장"""
        self.cfg.update(
            video=self.cb_video.get(), audio=self.cb_audio.get(),
            resolution=self.cb_res.get(), fps=self.cb_fps.get(),
            out_dir=self.var_dir.get(), monitor=self.var_mon.get(), monitor_out=self.cb_out.get(),
            sched_mode=self.var_s_mode.get(), sched_start=self.var_s_start.get().strip(),
            sched_end=self.var_s_end.get().strip(),
            sched_duration=":".join(v.get().strip() or "0" for v in (self.var_s_h, self.var_s_m, self.var_s_s)),
            trim_after=self.var_trim_after.get(), trim_compress=self.trim.var_compress.get(),
            **self._window_geometry())
        if self.nb:
            self.cfg["tab"] = str(self.nb.index("current"))
        self.encode.save_options()
        self.auto.save()
        save_config(self.cfg)


def setup_error_log():
    """오류를 error.log에 남김 (창 프로그램이라 화면에 안 보이므로). 갑자기 꺼지는 경우도 기록"""
    import faulthandler
    import traceback
    path = APP_DIR / "error.log"
    try:
        f = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 시작\n")
    faulthandler.enable(f)

    def write(kind, et, ev, tb):
        f.write(f"--- {time.strftime('%H:%M:%S')} {kind}\n" + "".join(traceback.format_exception(et, ev, tb)))
    sys.excepthook = lambda et, ev, tb: write("오류", et, ev, tb)
    threading.excepthook = lambda a: write(f"스레드 오류 ({a.thread.name if a.thread else ''})",
                                           a.exc_type, a.exc_value, a.exc_traceback)
    tk.Tk.report_callback_exception = lambda self, et, ev, tb: write("화면 오류", et, ev, tb)
    import atexit
    atexit.register(lambda: f.write(f"===== {time.strftime('%H:%M:%S')} 종료\n"))


if __name__ == "__main__":
    setup_error_log()
    threading.Thread(target=video_encoder_args, daemon=True).start()  # 인코더 확인을 미리 해 둠
    root = tk.Tk()
    App(root, "auto", load_config())  # 자동 녹화 창이 바로 열림 (직접 녹화는 오른쪽 위 버튼)
    root.mainloop()
