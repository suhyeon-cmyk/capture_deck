"""
영상-소리 싱크 테스트 도구

1) 테스트 영상 만들기
   python tools/sync_test.py make [분] [저장할 파일]
   - 매초 화면이 하얗게 번쩍(50ms)이는 순간 '삐' 소리(1kHz, 50ms)가 정확히 같이 나옴
   - 10초마다는 번쩍임/삐 소리를 길게(250ms) → 1초 이상 어긋나도 몇 초 차이인지 구분 가능
   - 화면 가운데에 재생 시각과 프레임 번호 표시

2) 녹화본 분석
   python tools/sync_test.py check <녹화한 파일>
   - 번쩍임과 삐 소리를 찾아 짝지어서 시간대별로 소리가 몇 ms 빠른지/늦은지 보여 줌
"""
import os
import subprocess
import sys

import numpy as np
import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
FPS = 60
SR = 48000
SHORT, LONG = 0.05, 0.25  # 번쩍임/삐 길이 (초)
# 매초 시작 부분 SHORT초, 10초마다는 LONG초 동안 켜짐
ON = f"lt(mod(t\\,1)\\,if(lt(mod(t\\,10)\\,1)\\,{LONG}\\,{SHORT}))"


def make(minutes=15.0, out="sync_test.mp4"):
    font = "C\\:/Windows/Fonts/consola.ttf"
    vf = ",".join([
        f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='{ON}'",
        f"drawtext=fontfile='{font}':text='%{{pts\\:hms}}':fontsize=140:fontcolor=0x00C8FF"
        ":x=(w-tw)/2:y=(h-th)/2-80",
        f"drawtext=fontfile='{font}':text='frame %{{n}}':fontsize=60:fontcolor=0x9A9A9A"
        ":x=(w-tw)/2:y=(h-th)/2+90",
        f"drawtext=fontfile='{font}':text='CaptureDeck sync test - fullscreen':fontsize=40"
        ":fontcolor=0x707070:x=(w-tw)/2:y=h-90",
    ])
    beep = f"if({ON.replace(chr(92), '')}, 0.5*sin(2*PI*1000*t), 0)"
    cmd = [FF, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c=0x202020:s=1920x1080:r={FPS}:d={minutes * 60}",
           "-f", "lavfi", "-i", f"aevalsrc=exprs='{beep}|{beep}':s={SR}:d={minutes * 60}",
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", str(FPS),
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", out]
    subprocess.run(cmd, check=True, creationflags=NO_WINDOW)
    print(f"만들었어요: {os.path.abspath(out)} ({minutes:g}분)")


# ---------------------------------------------------------------- 분석
def _flashes(path):
    """화면 가운데 밝기 (프레임 시각 기준 60fps로 맞춤) → 번쩍임 [(시작 초, 길이 초)]"""
    p = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:v:0",
                        # start_time=0: 영상 스트림이 늦게 시작하면 그만큼 첫 프레임으로 채워 시각을 맞춤
                        "-vf", f"fps={FPS}:start_time=0,crop=iw*0.5:ih*0.5,scale=8:8,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, creationflags=NO_WINDOW, check=True)
    lum = np.frombuffer(p.stdout, np.uint8).reshape(-1, 64).mean(1)
    on = lum > 150
    return _edges(on, FPS)


def _beeps(path):
    """1kHz 성분 크기 (1ms 단위) → 삐 소리 [(시작 초, 길이 초)]"""
    p = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:a:0",
                        # 소리 스트림이 늦게 시작하면 그만큼 무음으로 채워 시각을 맞춤
                        "-af", "aresample=async=1:first_pts=0",
                        "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True, creationflags=NO_WINDOW, check=True)
    x = np.frombuffer(p.stdout, np.int16).astype(np.float32)
    t = np.arange(len(x)) / SR
    win = np.ones(240) / 240  # 5ms
    i = np.convolve(x * np.cos(2 * np.pi * 1000 * t), win, "same")
    q = np.convolve(x * np.sin(2 * np.pi * 1000 * t), win, "same")
    mag = np.hypot(i, q)[: len(x) // 48 * 48].reshape(-1, 48).max(1)  # 1ms 단위
    peak = np.percentile(mag, 99.9)
    if peak <= 0:
        return []
    return _edges(mag > peak * 0.3, 1000)


def _edges(on, rate):
    """켜짐 구간 → [(시작 초, 길이 초)] (짧은 끊김은 이어 붙임)"""
    out = []
    idx = np.flatnonzero(np.diff(on.astype(np.int8)) == 1) + 1
    if on[0]:
        idx = np.r_[0, idx]
    for s in idx:
        e = s
        gap = int(rate * 0.02)
        while e < len(on) and on[e:e + gap + 1].any():
            e += 1
        if out and s / rate - (out[-1][0] + out[-1][1]) < 0.3:
            continue  # 같은 번쩍임/삐의 일부
        out.append((s / rate, (e - s) / rate))
    return out


def check(path):
    print(f"분석 중: {path}")
    fl, bp = _flashes(path), _beeps(path)
    print(f"번쩍임 {len(fl)}개, 삐 소리 {len(bp)}개 찾음")
    if len(fl) < 5 or len(bp) < 5:
        print("번쩍임이나 삐 소리를 충분히 찾지 못했어요. 전체 화면으로 재생했는지, 소리가 녹음됐는지 확인해 주세요.")
        return
    long_f = [s for s, d in fl if d > (SHORT + LONG) / 2]
    long_b = np.array([s for s, d in bp if d > (SHORT + LONG) / 2])
    # 10초마다 나오는 긴 신호끼리 짝지어 그 구간의 대략적인 차이(초 단위)를 구함
    # → 각 번쩍임은 가장 가까운 긴 신호의 차이를 기준으로 짝지음 (중간에 1초 넘게 바뀌어도 구분)
    anchors = []
    for f in long_f:
        if len(long_b):
            d = long_b[np.abs(long_b - f).argmin()] - f
            if abs(d) < 5:
                anchors.append((f, d))
    if not anchors:
        print("10초마다 나오는 긴 신호를 찾지 못해 1초 이상의 차이는 구분할 수 없어요.")
        anchors = [(0.0, 0.0)]
    at = np.array([a for a, _ in anchors])
    b = np.array([s for s, _ in bp])
    rows = []
    for f, _ in fl:
        coarse = anchors[int(np.abs(at - f).argmin())][1]
        j = np.abs(b - (f + coarse)).argmin()
        off = b[j] - f
        if abs(off - coarse) < 0.45:
            rows.append((f, off))
    if not rows:
        print("번쩍임과 삐 소리를 짝지을 수 없었어요.")
        return
    rows = np.array(rows)
    t, off = rows[:, 0], rows[:, 1] * 1000
    print(f"짝지은 신호 {len(rows)}개 (번쩍임 기준 {len(rows) / len(fl) * 100:.0f}%)\n")
    print("  + 이면 소리가 화면보다 늦게, - 이면 소리가 화면보다 빨리 나옴 (영상 1프레임 = 17ms)")
    print("  시간대        평균(ms)   최소    최대   개수")
    for m in range(int(t.max() // 60) + 1):
        sel = (t >= m * 60) & (t < (m + 1) * 60)
        if sel.any():
            o = off[sel]
            print(f"  {m:3d}:00~{m + 1:3d}:00  {np.median(o):+7.0f}  {o.min():+6.0f}  {o.max():+6.0f}  {sel.sum():4d}")
    # 갑자기 바뀐 지점
    jumps = []
    for i in range(5, len(off) - 5):
        a, c = np.median(off[i - 5:i]), np.median(off[i:i + 5])
        if abs(c - a) > 40:
            jumps.append((t[i], a, c))
    shown = -99
    for tt, a, c in jumps:
        if tt - shown > 10:
            print(f"  ! {int(tt // 60)}:{tt % 60:04.1f} 근처에서 차이가 {a:+.0f}ms → {c:+.0f}ms 로 바뀜")
            shown = tt
    first, last = np.median(off[:20]), np.median(off[-20:])
    print(f"\n처음 {first:+.0f}ms → 마지막 {last:+.0f}ms "
          f"(녹화 {t[-1] - t[0]:.0f}초 동안 {last - first:+.0f}ms 변함)")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "make":
        make(float(sys.argv[2]) if len(sys.argv) > 2 else 15.0,
             sys.argv[3] if len(sys.argv) > 3 else "sync_test.mp4")
    elif len(sys.argv) == 3 and sys.argv[1] == "check":
        check(sys.argv[2])
    else:
        print(__doc__)
