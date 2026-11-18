"""
Edge 자동 조작 (CaptureDeck 자동 녹화용)

Edge의 개발자 연결 기능(Chrome DevTools Protocol)으로 페이지를 직접 다룸.
최신 Edge는 평소 쓰는 프로필로는 외부 연결을 막으므로, 자동 녹화 전용 프로필로 Edge를 띄움
(처음 한 번 그 창에서 강의 사이트에 직접 로그인하면 이후에는 로그인 상태가 유지됨).
"""
import itertools
import json
import os
import socket
import subprocess
import time
import urllib.request

PORT = 9333
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CaptureDeck", "edge_profile")


def edge_path():
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(p):
                return p
    return None


def is_running():
    """자동 녹화용 Edge가 연결 가능한 상태로 떠 있는지"""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def launch(url="about:blank"):
    """자동 녹화용 Edge 실행 (이미 떠 있으면 그대로 사용)"""
    if is_running():
        return True
    exe = edge_path()
    if not exe:
        raise RuntimeError("Microsoft Edge를 찾을 수 없어요.")
    os.makedirs(PROFILE_DIR, exist_ok=True)
    subprocess.Popen([exe, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE_DIR}",
                      "--no-first-run", "--no-default-browser-check", "--start-maximized", url])
    for _ in range(100):
        if is_running():
            return True
        time.sleep(0.1)
    raise RuntimeError("Edge에 연결하지 못했어요.")


def targets():
    """열린 탭/팝업/프레임 목록"""
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
        return json.load(r)


def pages():
    return [t for t in targets() if t.get("type") == "page"]


def close_target(target_id):
    """탭/팝업 닫기"""
    b = browser_session()
    try:
        b.send("Target.closeTarget", {"targetId": target_id})
    finally:
        b.close()


def browser_session():
    """브라우저 전체에 연결 (창 위치·크기 조절용)"""
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5) as r:
        url = json.load(r)["webSocketDebuggerUrl"]
    return Session({"webSocketDebuggerUrl": url})


def window_bounds(target_id):
    b = browser_session()
    try:
        w = b.send("Browser.getWindowForTarget", {"targetId": target_id})
        return w["windowId"], w["bounds"]
    finally:
        b.close()


def move_window(target_id, left, top, width, height):
    """창을 지정한 위치·크기로 (최대화/전체화면 상태면 먼저 보통 상태로 되돌림)"""
    b = browser_session()
    try:
        wid = b.send("Browser.getWindowForTarget", {"targetId": target_id})["windowId"]
        b.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        b.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {
            "left": left, "top": top, "width": width, "height": height}})
    finally:
        b.close()


class Session:
    """탭(또는 팝업) 하나에 연결해 명령을 보냄"""

    def __init__(self, target):
        import websocket  # pip install websocket-client (Edge 열기만 할 때는 필요 없음)
        self.target = target
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30,
                                              suppress_origin=True)
        self.ids = itertools.count(1)
        self.events = []
        self.dialogs = []  # 자동으로 닫은 알림창(alert/confirm) 내용 (Page.enable 후에만 받음)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def send(self, method, params=None, timeout=30):
        i = next(self.ids)
        self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            self.ws.settimeout(max(0.1, end - time.time()))
            msg = json.loads(self.ws.recv())
            if msg.get("id") == i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            if msg.get("method") == "Page.javascriptDialogOpening":
                # 알림창이 떠 있으면 페이지가 멈추므로 바로 확인을 눌러 닫음 (응답은 기다리지 않음)
                self.dialogs.append(msg["params"].get("message", ""))
                self.ws.send(json.dumps({"id": next(self.ids), "method": "Page.handleJavaScriptDialog",
                                         "params": {"accept": True}}))
            elif "method" in msg:
                self.events = self.events[-200:] + [msg]
        raise TimeoutError(method)

    def eval(self, expr, user_gesture=False, await_promise=False):
        """페이지에서 자바스크립트 실행 → 결과 값"""
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "userGesture": user_gesture, "awaitPromise": await_promise})
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError((d.get("exception") or {}).get("description") or d.get("text"))
        return (r.get("result") or {}).get("value")

    def key(self, key=" ", code="Space", vk=32, text=" "):
        """키보드 입력 (마우스는 움직이지 않음 → 재생기 조작 막대가 나타나지 않음)"""
        base = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        self.send("Input.dispatchKeyEvent", dict(base, type="keyDown", text=text))
        self.send("Input.dispatchKeyEvent", dict(base, type="keyUp"))

    def bring_to_front(self):
        self.send("Page.bringToFront")

    def click(self, x, y):
        """페이지 좌표(x, y)를 실제 마우스처럼 클릭 (전체화면 버튼처럼 진짜 클릭이 필요한 곳용)"""
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})
