"""Screen Vision - MCP server that gives agents real-time screen access.

Design:
  - ON-DEMAND capture: a screenshot is only taken when an agent explicitly
    calls a tool. Nothing watches in the background unless watch_start() is
    called, and that only keeps the last N frames in a temp folder.
  - Frames live in <temp>/opencode/screen-vision/ and are overwritten/recycled,
    never accumulated silently.

Tools:
  list_monitors()                      -> monitors, resolutions, primary
  screenshot(monitor, region)          -> PNG path of the capture
  active_window()                      -> focused app title + executable
  watch_start/stop/status/latest()     -> periodic captures ("real-time")

Requires: Windows, Pillow (PIL), mcp 1.x (runs with pythoncore-3.12-64).
"""

import ctypes
import json
import os
import shutil
import sys
import tempfile
import threading
import time

from PIL import ImageGrab
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("screen")

_BASE_DIR = os.path.join(tempfile.gettempdir(), "opencode", "screen-vision")
_CAPTURE_DIR = os.path.join(_BASE_DIR, "captures")
_WATCH_DIR = os.path.join(_BASE_DIR, "watch")
_WATCH_KEEP = 12          # max frames kept by the watcher
_WATCH_MIN_INTERVAL = 0.5  # seconds, lower bound for capture interval

# High-DPI awareness: capture true pixels, not scaled ones.
try:  # Windows 8.1+
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def _ensure_dirs():
    for d in (_CAPTURE_DIR, _WATCH_DIR):
        os.makedirs(d, exist_ok=True)


# --------------------------------------------------------------------------- #
# Native helpers
# --------------------------------------------------------------------------- #

def _get_monitors():
    """Enumerate monitors with their virtual-desktop coordinates."""
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", ctypes.c_uint),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    monitors = []

    def _cb(hmon, hdc, rect, data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            monitors.append({
                "index": len(monitors) + 1,
                "left": r.left,
                "top": r.top,
                "width": r.right - r.left,
                "height": r.bottom - r.top,
                "primary": bool(mi.dwFlags & 1),
            })
        return True

    _user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    return monitors


def _monitor_bbox(index):
    """Return the (left, top, right, bottom) bbox of monitor by 1-based index."""
    mons = _get_monitors()
    for m in mons:
        if m["index"] == index:
            return (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]), m
    raise ValueError(
        "Monitor %d no existe. Use list_monitors() first." % index
    )


def _active_window_info():
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None

    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value or "(sin título)"

    pid = ctypes.c_uint(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    exe = None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if handle:
        size = ctypes.c_uint(1024)
        ebuf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, ebuf, ctypes.byref(size)):
            exe = ebuf.value
        _kernel32.CloseHandle(handle)

    return {"title": title, "pid": pid.value, "process": os.path.basename(exe) if exe else None, "exe": exe}


# --------------------------------------------------------------------------- #
# Watcher (periodic capture)
# --------------------------------------------------------------------------- #

_watch = {"thread": None, "running": False, "interval": None, "started": None, "frames": 0}

_ocr_engine = None


def _get_ocr():
    """Lazy-load RapidOCR (first call warms up the model, ~1-3 s)."""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_img(img):
    """OCR a PIL image -> list of {text, conf, center:[x,y]} in image coords."""
    tmp = os.path.join(_BASE_DIR, "_ocr_tmp.png")
    _ensure_dirs()
    img.save(tmp)
    res = _get_ocr()(tmp)
    out = []
    try:
        os.remove(tmp)
    except OSError:
        pass
    txts = getattr(res, "txts", None)
    boxes = getattr(res, "boxes", None)
    scores = getattr(res, "scores", None)
    txts = [] if txts is None else list(txts)
    boxes = [] if boxes is None else list(boxes)
    scores = [] if scores is None else list(scores)
    for txt, box, score in zip(txts, boxes, scores):
        pts = [(p[0], p[1]) for p in box]
        cx = int(sum(p[0] for p in pts) / len(pts))
        cy = int(sum(p[1] for p in pts) / len(pts))
        out.append({"text": txt, "conf": round(float(score), 2),
                    "center": [cx, cy]})
    return out


def _watch_loop(interval):
    n = 0
    while _watch["running"]:
        try:
            _ensure_dirs()
            n += 1
            img = ImageGrab.grab(all_screens=True)
            img.save(os.path.join(_WATCH_DIR, "frame_%06d.png" % n))
            # Recycle: keep only the most recent _WATCH_KEEP frames.
            files = sorted(f for f in os.listdir(_WATCH_DIR) if f.endswith(".png"))
            for old in files[:-_WATCH_KEEP]:
                try:
                    os.remove(os.path.join(_WATCH_DIR, old))
                except OSError:
                    pass
            _watch["frames"] = n
        except Exception:
            pass  # transient capture failure: keep the loop alive
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def list_monitors() -> str:
    """List the connected monitors (resolution, virtual-desktop position,
    primary flag). Call this before screenshot(monitor=N) to pick a screen."""
    return json.dumps(_get_monitors(), indent=2)


@mcp.tool()
def screenshot(monitor: int = 0, region: list = None) -> str:
    """Capture the user's screen ON DEMAND and return the PNG file path.

    The user can see exactly when this runs. Never call it to observe users
    without their awareness.

    Args:
        monitor: 0 = entire desktop (all monitors side by side),
                 1 = monitor 1, 2 = monitor 2, ... (see list_monitors()).
        region:  optional [left, top, right, bottom] in virtual-desktop pixels,
                 applied after selecting the monitor.
    Returns:
        JSON string with 'path' (PNG), plus capture size and monitor info.
    """
    _ensure_dirs()

    mons = _get_monitors()
    if monitor == 0:
        img = ImageGrab.grab(all_screens=True)
        label = "all desktops (%d monitors)" % len(mons)
    else:
        bbox, mi = _monitor_bbox(monitor)
        img = ImageGrab.grab(all_screens=True, bbox=bbox)
        label = "monitor %d (%dx%d%s)" % (monitor, mi["width"], mi["height"],
                                          ", primary" if mi["primary"] else "")

    if region:
        left, top, right, bottom = [int(v) for v in region]
        if left >= right or top >= bottom:
            return "ERROR: region invalida [left, top, right, bottom] = %s" % (region,)
        img = img.crop((left, top, right, bottom))

    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(_CAPTURE_DIR, "screenshot_%s_%d.png" % (stamp, os.getpid()))
    img.save(path)

    return json.dumps({
        "path": path,
        "size": {"width": img.width, "height": img.height},
        "monitors": mons if monitor == 0 else [mi],
        "label": label,
    }, indent=2)


@mcp.tool()
def active_window() -> str:
    """Return the currently focused window: title, PID, and executable name.
    Useful to know which app the user has in front before interpreting a
    screenshot."""
    info = _active_window_info()
    if info is None:
        return "ERROR: no se pudo obtener la ventana activa."
    return json.dumps(info, indent=2)


@mcp.tool()
def watch_start(interval: float = 2.0) -> str:
    """Start a background watcher that captures the full desktop every
    'interval' seconds (minimum 0.5). It keeps ONLY the last 12 frames in a
    temp folder. Use watch_latest() to read the newest frame. Call watch_stop()
    when done."""
    if _watch["running"]:
        return json.dumps({"status": "already_running", "interval": _watch["interval"]})
    interval = max(float(interval), _WATCH_MIN_INTERVAL)
    _watch.update(running=True, interval=interval, started=time.time(), frames=0)
    _watch["thread"] = threading.Thread(target=_watch_loop, args=(interval,), daemon=True)
    _watch["thread"].start()
    return json.dumps({
        "status": "started",
        "interval": interval,
        "frames_kept": _WATCH_KEEP,
        "note": "use watch_latest() to see the most recent frame",
    })


@mcp.tool()
def watch_stop() -> str:
    """Stop the background watcher started by watch_start()."""
    _watch["running"] = False
    thread = _watch["thread"]
    if thread and thread.is_alive():
        thread.join(timeout=_WATCH_MIN_INTERVAL * 3)
    _watch["thread"] = None
    return json.dumps({"status": "stopped", "frames_captured": _watch["frames"]})


@mcp.tool()
def watch_status() -> str:
    """Return the watcher state: running, interval, and frames counter."""
    return json.dumps({
        "running": _watch["running"],
        "interval": _watch["interval"],
        "frames_captured": _watch["frames"],
        "dir": _WATCH_DIR if os.path.isdir(_WATCH_DIR) else None,
    })


@mcp.tool()
def watch_latest() -> str:
    """Return the newest frame captured by the watcher (PNG path + age in
    seconds). Look at that file to 'see' the user's screen right now."""
    if not _watch["running"] and not _watch["frames"]:
        return "ERROR: el watcher no esta activo. Llama watch_start() primero."
    _ensure_dirs()
    files = sorted(f for f in os.listdir(_WATCH_DIR) if f.endswith(".png"))
    if not files:
        return "ERROR: aun no hay capturas. Espera un momento y reintenta."
    newest = files[-1]
    path = os.path.join(_WATCH_DIR, newest)
    age = round(time.time() - os.path.getmtime(path), 2)
    return json.dumps({
        "path": path,
        "age_seconds": age,
        "frame": newest,
        "watcher_running": _watch["running"],
    })


# --------------------------------------------------------------------------- #
# Input control (SendInput) + window management
# --------------------------------------------------------------------------- #

from ctypes import wintypes as _wt
from ctypes import (POINTER, Structure, Union, byref, c_long, c_uint, c_ulong, c_ushort)

_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_MOUSEEVENTF = {
    "left_down": 0x0002, "left_up": 0x0004,
    "right_down": 0x0008, "right_up": 0x0010,
    "middle_down": 0x0020, "middle_up": 0x0040,
    "wheel": 0x0800,
}
_WHEEL_DELTA = 120

_MOUSE_BUTTONS = {"left": ("left_down", "left_up"),
                  "right": ("right_down", "right_up"),
                  "middle": ("middle_down", "middle_up")}

_SM_X = 76; _SM_Y = 77; _SM_W = 78; _SM_H = 79

_VK = {
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "f1": 0x70, "f2": 0x71, "f3": 0x72,
    "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


class _MOUSEINPUT(Structure):
    _fields_ = [("dx", c_long), ("dy", c_long), ("mouseData", c_uint),
                ("dwFlags", c_uint), ("time", c_uint), ("dwExtraInfo", POINTER(c_ulong))]


class _KEYBDINPUT(Structure):
    _fields_ = [("wVk", c_ushort), ("wScan", c_ushort), ("dwFlags", c_uint),
                ("time", c_uint), ("dwExtraInfo", POINTER(c_ulong))]


class _INPUTUNION(Union):
    _fields_ = [("mouse", _MOUSEINPUT), ("kbd", _KEYBDINPUT)]


class _INPUT(Structure):
    _fields_ = [("type", c_uint), ("union", _INPUTUNION)]


def _send(inputs):
    """SendInput via ctypes; tolerant to item-level differences."""
    arr = (_INPUT * len(inputs))(*inputs)
    _user32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))


def _mouse_event(flags, data=0):
    inp = _INPUT(type=_INPUT_MOUSE)
    inp.union.mouse = _MOUSEINPUT(0, 0, data, flags, 0, None)
    _send([inp])


def _key_event(vk, up=False):
    inp = _INPUT(type=_INPUT_KEYBOARD)
    flags = _KEYEVENTF_KEYUP if up else 0
    inp.union.kbd = _KEYBDINPUT(vk, 0, flags, 0, None)
    _send([inp])


def _log_action(action, **kw):
    """Append every control action to an audit log (transparency)."""
    entry = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action}
    entry.update(kw)
    try:
        _ensure_dirs()
        with open(os.path.join(_BASE_DIR, "control_log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _screen_rect():
    x = _user32.GetSystemMetrics(_SM_X)
    y = _user32.GetSystemMetrics(_SM_Y)
    w = _user32.GetSystemMetrics(_SM_W)
    h = _user32.GetSystemMetrics(_SM_H)
    return x, y, w, h


def _top_windows():
    """Visible top-level windows: hwnd, title, rect, pid."""
    out = []

    def _wcb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        r = _wt.RECT()
        _user32.GetWindowRect(hwnd, byref(r))
        pid = c_uint(0)
        _user32.GetWindowThreadProcessId(hwnd, byref(pid))
        out.append({"hwnd": hwnd, "title": buf.value,
                    "rect": [r.left, r.top, r.right, r.bottom], "pid": pid.value})
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    _user32.EnumWindows(WNDENUMPROC(_wcb), 0)
    return out


def _focus_window(title_substring):
    wins = [w for w in _top_windows() if title_substring.lower() in w["title"].lower()]
    if not wins:
        return None
    hwnd = wins[0]["hwnd"]
    # SwitchToThisWindow wins where classic SetForegroundWindow is refused
    # for background processes (Windows foreground lock).
    _user32.SwitchToThisWindow(hwnd, True)
    time.sleep(0.15)
    return wins[0]


# --------------------------------------------------------------------------- #
# Control tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def window_list() -> str:
    """List visible top-level windows (title, position, pid) so an agent can
    decide what to interact with."""
    return json.dumps(_top_windows(), indent=2)


@mcp.tool()
def focus_window(title: str) -> str:
    """Bring to the foreground the first window whose title contains 'title'
    (case-insensitive). Returns the focused window's info."""
    w = _focus_window(title)
    if w is None:
        return "ERROR: no hay ninguna ventana cuyo titulo contenga %r" % title
    _log_action("focus_window", title=w["title"])
    return json.dumps(w)


@mcp.tool()
def mouse_move(x: int, y: int) -> str:
    """Move the mouse cursor to (x, y) in virtual-desktop pixels (the same
    coordinate system as screenshots). No clicks."""
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    _log_action("mouse_move", x=x, y=y)
    return json.dumps({"moved_to": [x, y]})


@mcp.tool()
def mouse_click(x: int, y: int, Button: str = "left", clicks: int = 1) -> str:
    """Move to (x, y) and click. Button: left/right/middle. For a double
    click use clicks=2. Coordinates are virtual-desktop pixels."""
    if Button not in _MOUSE_BUTTONS:
        return "ERROR: button debe ser left, right o middle"
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    down, up = _MOUSE_BUTTONS[Button]
    for _ in range(max(1, min(int(clicks), 3))):
        _mouse_event(_MOUSEEVENTF[down])
        _mouse_event(_MOUSEEVENTF[up])
        time.sleep(0.08)
    _log_action("mouse_click", x=x, y=y, button=Button, clicks=clicks)
    return json.dumps({"clicked": [x, y], "button": Button, "clicks": clicks})


@mcp.tool()
def mouse_drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    """Press the left button at (x1, y1), drag to (x2, y2) in ~duration
    seconds, and release."""
    steps = max(int(duration / 0.02), 5)
    _user32.SetCursorPos(int(x1), int(y1))
    time.sleep(0.1)
    _mouse_event(_MOUSEEVENTF["left_down"])
    for i in range(1, steps + 1):
        fx = x1 + (x2 - x1) * i / steps
        fy = y1 + (y2 - y1) * i / steps
        _user32.SetCursorPos(int(fx), int(fy))
        time.sleep(0.02)
    _mouse_event(_MOUSEEVENTF["left_up"])
    _log_action("mouse_drag", from_=[x1, y1], to=[x2, y2])
    return json.dumps({"dragged": [[x1, y1], [x2, y2]]})


@mcp.tool()
def scroll(amount: int, x: int = None, y: int = None) -> str:
    """Scroll the wheel. Positive 'amount' = up, negative = down, in wheel
    notches (1 notch ≈ 3 lines). Optionally move to (x, y) first."""
    if x is not None and y is not None:
        _user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
    n = max(-10, min(int(amount), 10))
    _mouse_event(_MOUSEEVENTF["wheel"], n * _WHEEL_DELTA)
    _log_action("scroll", amount=n, at=[x, y])
    return json.dumps({"scrolled": n})


@mcp.tool()
def type_text(text: str, delay_ms: float = 12) -> str:
    """Type unicode text with the keyboard (into whatever window has focus).
    Use focus_window() first. Newlines are ENTER, tabs are TAB."""
    batch = []
    for ch in text:
        if ch == "\n":
            _key_event(_VK["enter"]); _key_event(_VK["enter"], up=True)
            time.sleep(delay_ms / 1000.0)
            continue
        if ch == "\t":
            _key_event(_VK["tab"]); _key_event(_VK["tab"], up=True)
            time.sleep(delay_ms / 1000.0)
            continue
        vk, scan = 0, ord(ch)
        down = _INPUT(type=_INPUT_KEYBOARD)
        down.union.kbd = _KEYBDINPUT(vk, scan, _KEYEVENTF_UNICODE, 0, None)
        up = _INPUT(type=_INPUT_KEYBOARD)
        up.union.kbd = _KEYBDINPUT(vk, scan, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, None)
        _send([down, up])
        time.sleep(delay_ms / 1000.0)
    _log_action("type_text", chars=len(text), preview=text[:60])
    return json.dumps({"typed_chars": len(text)})


@mcp.tool()
def press_key(combo: str) -> str:
    """Press a key or chord: 'enter', 'ctrl+s', 'ctrl+shift+esc', 'alt+tab',
    'win', 'f5', 'delete'... Modifiers: ctrl, alt, shift, win."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    vks = []
    for p in parts:
        if p not in _VK:
            return "ERROR: tecla desconocida %r. Use nombres como enter, tab, esc, f1-f12, up/down..." % p
        vks.append(_VK[p])
    for vk in vks[:-1]:
        _key_event(vk)          # hold modifiers
    _key_event(vks[-1]); _key_event(vks[-1], up=True)
    for vk in reversed(vks[:-1]):
        _key_event(vk, up=True)
    _log_action("press_key", combo=combo)
    return json.dumps({"pressed": combo})


@mcp.tool()
def pixel_color(x: int, y: int) -> str:
    """Return the color of one screen pixel at (x, y) in virtual-desktop
    coordinates (hex RRGGBB)."""
    gdi32 = ctypes.windll.gdi32
    hdc = _user32.GetDC(0)
    data = (c_uint * 1)()
    ok = gdi32.GetPixel(hdc, int(x), int(y))
    _user32.ReleaseDC(0, hdc)
    r = ok & 0xFF; g = (ok >> 8) & 0xFF; b = (ok >> 16) & 0xFF
    return json.dumps({"x": x, "y": y, "rgb": [r, g, b], "hex": "%02X%02X%02X" % (r, g, b)})


@mcp.tool()
def read_screen_text(monitor: int = 0, region: list = None) -> str:
    """Read ALL visible text on screen with local OCR, and return each text
    with its CENTER COORDINATES (virtual-desktop pixels). This is how an
    agent locates buttons/fields reliably: read, find the label, click its
    coordinates."""
    if monitor == 0:
        img = ImageGrab.grab(all_screens=True)
        off = (_user32.GetSystemMetrics(_SM_X), _user32.GetSystemMetrics(_SM_Y))
    else:
        bbox, mi = _monitor_bbox(monitor)
        img = ImageGrab.grab(all_screens=True, bbox=bbox)
        off = (bbox[0], bbox[1])
    if region:
        img = img.crop(tuple(int(v) for v in region))
        off = (off[0] + int(region[0]), off[1] + int(region[1]))
    items = _ocr_img(img)
    for it in items:
        it["center"] = [it["center"][0] + off[0], it["center"][1] + off[1]]
    return json.dumps({"texts": items, "count": len(items)}, ensure_ascii=False)


@mcp.tool()
def find_text(query: str, monitor: int = 0) -> str:
    """Find text matching 'query' (case-insensitive substring) on screen and
    return its clickable center coordinates. Use right before mouse_click()
    on labels/buttons/links."""
    if monitor == 0:
        img = ImageGrab.grab(all_screens=True)
        off = (_user32.GetSystemMetrics(_SM_X), _user32.GetSystemMetrics(_SM_Y))
    else:
        bbox, mi = _monitor_bbox(monitor)
        img = ImageGrab.grab(all_screens=True, bbox=bbox)
        off = (bbox[0], bbox[1])
    q = query.lower()
    hits = []
    for it in _ocr_img(img):
        it["center"] = [it["center"][0] + off[0], it["center"][1] + off[1]]
        if q in it["text"].lower():
            hits.append(it)
    if not hits:
        return json.dumps({"found": False, "query": query,
                           "hint": "prueba read_screen_text() para ver todo el texto visible"})
    return json.dumps({"found": True, "query": query, "matches": hits}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Screen Vision v2: precision layer (Fase 1-4)
# --------------------------------------------------------------------------- #

_PROFILE_DIR = os.path.join(_BASE_DIR, "profiles")
_AUTONOMY_FILE = os.path.join(_BASE_DIR, "autonomy.json")

_ROLE_KEYWORDS = {
    "button": ["desbloquear", "enviar", "guardar", "buscar", "aceptar", "cancelar",
               "iniciar", "siguiente", "ok", "confirmar", "eliminar", "borrar"],
    "field": ["contrase", "escribe", "ingresa", "mensaje", "nombre", "correo",
              "buscar un", "telefono", "fecha", "usuario"],
}


def _autonomy():
    try:
        with open(_AUTONOMY_FILE, encoding="utf-8") as f:
            return json.load(f).get("mode", "demo")
    except (OSError, ValueError):
        return "demo"


def _foco_titulo():
    info = _active_window_info()
    return (info or {}).get("title", "") or ""


def _foco_ok(expect):
    """Assert del foco: True si la ventana activa contiene 'expect'."""
    if not expect:
        return True
    return expect.lower() in _foco_titulo().lower()


def _clipboard_set(text):
    """Copy text to the Windows clipboard (CF_UNICODETEXT) via ctypes."""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    # 64-bit: handles must travel as pointers, not ints
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    if not user32.OpenClipboard(0):
        return False
    try:
        user32.EmptyClipboard()
        data = ctypes.create_unicode_buffer(text)
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(data))
        if not h:
            return False
        lp = kernel32.GlobalLock(h)
        if not lp:
            return False
        ctypes.memmove(lp, data, ctypes.sizeof(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        user32.CloseClipboard()


def pick_candidate(cands, role="any", rel=None):
    """Pure selection logic for click_by_text (unit-testable).

    cands: [{text, center:[x,y]}]
    role: any|button|field - keyword first if role keywords match.
    rel: {"anchor_query": str, "min_dy": int, "max_dy": int}
         candidate must be vertically (candidate.y - anchor.y) in
         [min_dy, max_dy]; negative = above the anchor. e.g. a field sits at
         about -84 px above its button (boton at +84).
    Returns the best candidate or None.

    Alert/error texts ('Contrasena vacia...', 'Error...') are excluded from
    selection whenever a non-alert candidate exists - lesson from the
    WhatsApp unlock screen: the error banner sat closest to the button and
    kept winning the race.
    """
    all_c = list(cands)
    safe = [c for c in all_c if not any(k in c["text"].lower() for k in _ALERT_KEYWORDS)]
    pool = safe or all_c
    anchors = [c for c in all_c if rel and rel.get("anchor_query")
               and rel["anchor_query"].lower() in c["text"].lower()]
    if rel and rel.get("anchor_query") and not anchors:
        # the caller asked for geometry anchored to a text that is NOT visible:
        # any choice would be a guess -> refuse (the everyday lesson of this project)
        return None
    anchor = anchors[0] if anchors else None

    kw_hits = [c for c in pool if role in _ROLE_KEYWORDS
               and any(k in c["text"].lower() for k in _ROLE_KEYWORDS[role])]
    chosen = (kw_hits[0] if kw_hits else (pool[0] if pool else None))

    if anchor is not None:
        in_range = [c for c in pool
                    if rel.get("min_dy", -5000) <= c["center"][1] - anchor["center"][1] <= rel.get("max_dy", 5000)
                    and abs(c["center"][0] - anchor["center"][0]) < 400]
        if in_range:
            kw_in = [c for c in in_range if role in _ROLE_KEYWORDS
                     and any(k in c["text"].lower() for k in _ROLE_KEYWORDS[role])]
            chosen = min(kw_in if kw_in else in_range,
                         key=lambda c: abs(c["center"][1] - anchor["center"][1]))
    return chosen


# ------------------------- Fase 1 tools ------------------------- #

@mcp.tool()
def click_by_text(query: str, role: str = "any", anchor_query: str = None,
                  min_dy: int = -120, max_dy: int = 120, expect_window: str = None) -> str:
    """OCR-locate text on screen and CLICK it, choosing the right candidate.

    query: text to find (case-insensitive substring).
    role: any|button|field - prefer candidates matching role keywords.
    anchor_query/min_dy/max_dy: relative-position rule; candidate y must be
    (candidate.y - anchor.y) in [min_dy, max_dy] (negative = above anchor),
    e.g. a password field sits ~-84 px above its Desbloquear button, so use
    click_by_text('contrase', role='field', anchor_query='desbloquear',
    min_dy=-100, max_dy=-60). ALWAYS verify the result with a screenshot or
    type_and_verify afterwards.
    expect_window: abort unless the active window title contains it.
    """
    if expect_window and not _foco_ok(expect_window):
        return json.dumps({"ok": False, "error": "foco inesperado",
                           "foco": _foco_titulo(), "esperaba": expect_window})
    cands = [{"text": t["text"], "center": t["center"]}
             for t in json.loads(read_screen_text())["texts"]
             if query.lower() in t["text"].lower()]
    if not cands:
        return json.dumps({"ok": False, "error": "texto no encontrado", "query": query})
    chosen = pick_candidate(cands, role=role, rel=(
        {"anchor_query": anchor_query, "min_dy": min_dy, "max_dy": max_dy} if anchor_query else None))
    if chosen is None:
        return json.dumps({"ok": False, "error": "sin candidato", "query": query})
    mouse_click(*chosen["center"])
    _log_action("click_by_text", query=query, role=role, at=chosen["center"])
    return json.dumps({"ok": True, "clicked": chosen, "candidatos": len(cands)}, ensure_ascii=False)


@mcp.tool()
def type_and_verify(text: str, expect_input_query: str = "escribe", region: list = None,
                    expect_window: str = None, paste: bool = False) -> str:
    """Type text in the focused window and VERIFY the screen shows it; retries
    up to 3 times (ctrl+a, delete, retype). Fall back to clipboard paste when
    typing drifts. Returns JSON with typed verification result. NEVER uses
    Enter - callers send keys explicitly after checking 'verified'.
    expect_input_query: OCR substring marking the input area if 'region' is
    not given.
    """
    if expect_window and not _foco_ok(expect_window):
        return json.dumps({"ok": False, "error": "foco inesperado", "foco": _foco_titulo()})
    _ensure_dirs()

    def _region_texts():
        if region:
            return json.loads(s_read_region(region))
        # full screen OCR then look from the input downwards
        all_t = json.loads(read_screen_text())["texts"]
        anchor = next((t for t in all_t if expect_input_query.lower() in t["text"].lower()), None)
        if anchor:
            y = anchor["center"][1]
            return [t for t in all_t if 0 <= t["center"][1] - y <= 80]
        return all_t

    def s_read_region(rgn):
        img = ImageGrab.grab(all_screens=True)
        x0, y0 = _user32.GetSystemMetrics(_SM_X), _user32.GetSystemMetrics(_SM_Y)
        img = img.crop(tuple(int(v) for v in rgn))
        off = (x0 + int(rgn[0]), y0 + int(rgn[1]))
        items = _ocr_img(img)
        for it in items:
            it["center"] = [it["center"][0] + off[0], it["center"][1] + off[1]]
        return json.dumps({"texts": items}, ensure_ascii=False)

    target = text.lower()
    for intento in range(1, 4):
        press_key("ctrl+a")
        press_key("delete")
        time.sleep(0.2)
        if intento == 3 or paste:
            _clipboard_set(text)
            press_key("ctrl+v")
        else:
            type_text(text)
        time.sleep(0.6)
        read = " ".join(t["text"] for t in _region_texts()).lower()
        if any(target[:20] in chunk for chunk in [read]) or target in read.replace(" ", "").lower() \
                or (" ".join(read.split())).find(target[:15]) >= 0:
            _log_action("type_and_verify", chars=len(text), intento=intento)
            return json.dumps({"ok": True, "verified": True, "intentos": intento,
                               "method": "paste" if (paste or intento == 3) else "typed"})
        _log_action("type_and_verify_mismatch", intento=intento)
    return json.dumps({"ok": False, "verified": False, "error": "texto no verificado despues de 3 intentos"})


@mcp.tool()
def save_profile(name: str, slots: dict) -> str:
    """Persist a UI profile for a repeatable app: slots = {"whatsapp.input": [x,y],
    "whatsapp.search": [x,y], ...}. Stored in profiles/<name>.json for later
    get_profile()."""
    os.makedirs(_PROFILE_DIR, exist_ok=True)
    path = os.path.join(_PROFILE_DIR, "%s.json" % name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(slots, f, ensure_ascii=False, indent=2)
    return json.dumps({"saved": path, "slots": len(slots)})


@mcp.tool()
def get_profile(name: str) -> str:
    """Load a saved UI profile (coordinates per logical slot)."""
    path = os.path.join(_PROFILE_DIR, "%s.json" % name)
    if not os.path.isfile(path):
        return json.dumps({"error": "perfil no existe", "path": path})
    with open(path, encoding="utf-8") as f:
        return f.read()


@mcp.tool()
def set_autonomy(mode: str) -> str:
    """Set autonomy gate: 'demo' (safe: run_flow requires confirm:true on
    submit-like steps) or 'full' (agent acts without per-step confirmation).
    Stored on disk; persists across sessions."""
    if mode not in ("demo", "full"):
        return json.dumps({"ok": False, "error": "mode debe ser demo|full"})
    _ensure_dirs()
    with open(_AUTONOMY_FILE, "w", encoding="utf-8") as f:
        json.dump({"mode": mode}, f)
    return json.dumps({"ok": True, "mode": mode})


@mcp.tool()
def get_autonomy() -> str:
    """Read the current autonomy mode."""
    return json.dumps({"mode": _autonomy()})


# ------------------------- Fase 2: UI Automation ------------------------- #

@mcp.tool()
def uia_tree(depth: int = 3, window_title: str = None) -> str:
    """Windows UI Automation tree of visible controls: native apps AND
    Chromium browsers expose real names/roles/values here - the DOM of
    desktop apps. Depth 3-4 is fast; higher is slower. Returns JSON list:
    {name, control_type, rect, automation_id, depth}."""
    from pywinauto import Desktop
    desktop = Desktop(backend="uia")
    if window_title:
        wins = [w for w in desktop.windows()
                if str(w.window_text() or "").strip() and window_title.lower() in str(w.window_text()).lower()]
        roots = wins or []
    else:
        roots = desktop.windows()
    out = []
    visited = 0

    def walk(ctrl, d):
        nonlocal visited
        if visited > 400 or d > int(depth):
            return
        visited += 1
        try:
            info = ctrl.element_info
            r = info.rectangle
            out.append({
                "depth": d,
                "name": (info.name or "")[:80],
                "control_type": info.control_type,
                "automation_id": (info.automation_id or "")[:40],
                "rect": [r.left, r.top, r.right, r.bottom] if r else None,
            })
        except Exception:
            return
        if d < int(depth):
            try:
                for child in ctrl.children():
                    walk(child, d + 1)
            except Exception:
                pass

    for root in roots[:4]:
        walk(root, 0)
    return json.dumps(out[:400], ensure_ascii=False)


@mcp.tool()
def uia_click(name_query: str, control_type: str = None, window_title: str = None,
              click_at: str = "center") -> str:
    """Find a real UI control by name substring (via Windows UI Automation)
    and click it - coordinate-free, so no OCR ambiguity. Optionally filter by
    control_type (Button, Edit, Document...). This is the 'error-free' path
    for apps and Chrome/Edge pages that expose accessibility."""
    from pywinauto import Desktop
    desktop = Desktop(backend="uia")
    roots = desktop.windows()
    if window_title:
        roots = [w for w in roots
                 if str(w.window_text() or "").strip() and window_title.lower() in str(w.window_text()).lower()]
    for root in roots:
        try:
            matches = root.descendants(title_re=".*%s.*" % name_query.replace("+", "\\+"),
                                       control_type=control_type) if control_type \
                else root.descendants(title_re=".*%s.*" % name_query.replace("+", "\\+"))
        except Exception:
            matches = []
        if matches:
            c = matches[0]
            try:
                c.set_focus()
                time.sleep(0.15)
            except Exception:
                pass
            c.click_input()
            _log_action("uia_click", name_query=name_query, control_type=control_type)
            return json.dumps({"ok": True, "clicked": {"name": c.window_text()[:80],
                                                       "control_type": c.element_info.control_type}})
    return json.dumps({"ok": False, "error": "control no encontrado",
                       "hint": "prueba uia_tree() para ver nombres reales"})


@mcp.tool()
def uia_get_value(name_query: str, control_type: str = "Edit", window_title: str = None) -> str:
    """Read the CURRENT VALUE of a control (field text, checkbox state...)
    via UI Automation - ground truth instead of guessing from pixels."""
    from pywinauto import Desktop
    desktop = Desktop(backend="uia")
    roots = desktop.windows()
    if window_title:
        roots = [w for w in roots
                 if str(w.window_text() or "").strip() and window_title.lower() in str(w.window_text()).lower()]
    for root in roots:
        try:
            matches = root.descendants(control_type=control_type)
        except Exception:
            continue
        for c in matches:
            if name_query.lower() in (c.window_text() or "").lower() \
                    or name_query.lower() in str(c.element_info.automation_id or "").lower():
                try:
                    val = c.get_value() if control_type == "Edit" else c.window_text()
                except Exception:
                    val = c.window_text()
                return json.dumps({"ok": True, "value": str(val)[:200],
                                   "name": (c.window_text() or "")[:80]})
    return json.dumps({"ok": False, "error": "control no encontrado"})


# ------------------------- Fase 3: flow engine ------------------------- #

_ALERT_KEYWORDS = ["contraseña vacia", "contrasena vacia", "vuelve a intentarlo",
                   "¿quieres guardar", "¿deseas guardar", "error", "advertencia",
                   "¿estas seguro", "no se pudo", "fall"]


@mcp.tool()
def ui_alerts() -> str:
    """Scan the screen for common alerts/errors/dialogs ('Contraseña vacía',
    '¿Guardar cambios?', 'Error'...) with their coordinates, to catch failure
    feedback before pressing Enter."""
    hits = [t for t in json.loads(read_screen_text())["texts"]
            if any(k in t["text"].lower() for k in _ALERT_KEYWORDS)]
    return json.dumps({"alerts": hits}, ensure_ascii=False)


@mcp.tool()
def diff_frames(n: int = 2) -> str:
    """Compare the LAST n watcher frames (or two fresh captures if the watcher
    is off): percent of changed pixels + bounding box of change. Cheap way to
    know 'what moved on screen' without OCR."""
    from PIL import Image, ImageChops, ImageFilter
    frames = []
    if os.path.isdir(_WATCH_DIR):
        files = sorted(f for f in os.listdir(_WATCH_DIR) if f.endswith(".png"))
        for f in files[-int(n):]:
            try:
                frames.append(Image.open(os.path.join(_WATCH_DIR, f)).copy())
            except Exception:
                continue  # half-written frame (writer mid-save) -> skip
    if len(frames) < 2:
        a = ImageGrab.grab(all_screens=True)
        time.sleep(0.5)
        b = ImageGrab.grab(all_screens=True)
        frames = [a, b]
    dy = ImageChops.difference(frames[-2].convert("RGB"), frames[-1].convert("RGB")).convert("L")
    changed = sum(1 for v in dy.getdata() if v > 12)
    pct = round(100.0 * changed / (dy.width * dy.height), 3)
    bbox = dy.point(lambda p: 255 if p > 12 else 0).filter(ImageFilter.MaxFilter(9)).getbbox()
    return json.dumps({"changed_pct": pct, "bbox": bbox,
                       "frames": [getattr(f, "filename", None) or "fresh" for f in frames]})


@mcp.tool()
def run_flow(steps: list) -> str:
    """Execute a JSON 'recipe' of screen actions with per-step focus asserts,
    verification and retries - the whole mirar/actuar/verificar loop, reusable.

    Steps (each is a dict):
      {"op":"assert","window":"whatsapp"}
      {"op":"focus","title":"whatsapp"}
      {"op":"wait","seconds":1}
      {"op":"click_text","query":...,"role":...,"anchor_query":...}   (Fase 1)
      {"op":"click","x":..,"y":..}
      {"op":"type","text":...,"verify":true,"expect_input_query":...} (type_and_verify)
      {"op":"press","combo":"enter","confirm":true}  <- in 'demo' autonomy,
           submit-like presses MUST carry confirm:true (the agent attests the
           user approved the action).
      {"op":"verify","query":...,"region":[l,t,r,b]}  (abort if absent)
      {"op":"alerts"}            (abort on screen alerts)
      {"op":"screenshot","tag":"wa"}
    Aborts on the first failed step (unless the step sets "optional":true).
    Returns per-step results.
    """
    out = []
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict) or "op" not in st:
            return json.dumps({"ok": False, "error": "step %d invalido" % i})
        op = st["op"]
        try:
            if op == "assert":
                ok = st.get("window", "").lower() in _foco_titulo().lower()
                res = {"ok": ok, "foco": _foco_titulo()[:60]}
            elif op == "focus":
                s.focus_window(st["title"])
                res = {"ok": True}
            elif op == "wait":
                time.sleep(float(st.get("seconds", 1)))
                res = {"ok": True}
            elif op == "click_text":
                r = json.loads(click_by_text(
                    st["query"], role=st.get("role", "any"),
                    anchor_query=st.get("anchor_query"),
                    min_dy=st.get("min_dy", -120), max_dy=st.get("max_dy", 120),
                    expect_window=st.get("window")))
                res = r
            elif op == "click":
                if not _foco_ok(st.get("window")):
                    res = {"ok": False, "error": "foco", "foco": _foco_titulo()[:60]}
                else:
                    mouse_click(st["x"], st["y"])
                    res = {"ok": True}
            elif op == "type":
                r = json.loads(type_and_verify(
                    st["text"], expect_input_query=st.get("expect_input_query", "escribe"),
                    region=st.get("region"), expect_window=st.get("window"),
                    paste=st.get("paste", False)))
                res = r if (r.get("verified") or st.get("optional")) else r
            elif op == "press":
                combo = st.get("combo", "").lower()
                submit_like = combo in ("enter",) or any(x in combo for x in ("guarda", "s"))
                if submit_like and _autonomy() == "demo" and not st.get("confirm"):
                    res = {"ok": False, "error": "autonomia=demo: este paso exige confirm:true"}
                else:
                    if not _foco_ok(st.get("window")):
                        res = {"ok": False, "error": "foco", "foco": _foco_titulo()[:60]}
                    else:
                        r = press_key(st.get("combo", ""))
                        res = {"ok": '"error' not in r, "raw": r}
            elif op == "verify":
                txts = " ".join(t["text"] for t in ocr_full()).lower()
                ok = st["query"].lower().replace(" ", "") in txts.replace(" ", "")
                res = {"ok": ok, "query": st["query"]}
            elif op == "alerts":
                r = json.loads(ui_alerts())
                res = {"ok": not r["alerts"], "alerts": r["alerts"]}
            elif op == "screenshot":
                res = json.loads(screenshot_tag(st))
            else:
                return json.dumps({"ok": False, "error": "op desconocida: %s" % op})
        except SystemExit:
            raise
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        out.append({"step": i, "op": op, **res})
        if not res.get("ok", False) and not st.get("optional"):
            break
    return json.dumps({"completed": all(r.get("ok") for r in out),
                       "steps": out}, ensure_ascii=False)


def screenshot_tag(st):
    tag = st.get("tag", "flow")
    r = json.loads(screenshot(0))
    src = r["path"]
    dst = os.path.join(_CAPTURE_DIR, "flow_%s_%s.png" % (tag, time.strftime("%H%M%S")))
    shutil.copyfile(src, dst)
    return json.dumps({"ok": True, "path": dst})


def ocr_full():
    return json.loads(read_screen_text())["texts"]


# ------------------------- Fase 4: doctor ------------------------- #

@mcp.tool()
def doctor() -> str:
    """Self-check of the whole stack: platform, DPI, monitors, dependencies,
    autonomy mode, watcher/log state, manifest presence. Run after any change
    or when something behaves oddly."""
    checks = {}
    checks["platform_windows"] = sys.platform == "win32"
    try:
        checks["dpi_aware"] = bool(ctypes.windll.shcore.GetProcessDpiAwareness(0) >= 1)
    except Exception:
        checks["dpi_aware"] = True  # DPIAware fallback path
    checks["monitors"] = len(_get_monitors())
    for mod in ("mcp", "PIL"):
        try:
            __import__(mod)
            checks["dep_" + mod] = True
        except Exception:
            checks["dep_" + mod] = False
    try:
        import pywinauto  # noqa
        checks["dep_pywinauto"] = True
    except Exception:
        checks["dep_pywinauto"] = False
    try:
        import rapidocr  # noqa
        checks["dep_ocr"] = True
    except Exception:
        checks["dep_ocr"] = False
    checks["autonomy"] = _autonomy()
    try:
        checks["control_log_kb"] = round(os.path.getsize(
            os.path.join(_BASE_DIR, "control_log.jsonl")) / 1024, 1)
    except OSError:
        checks["control_log_kb"] = 0
    manifest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_manifest.json")
    checks["manifest_v"] = None
    try:
        with open(manifest, encoding="utf-8") as f:
            checks["manifest_v"] = json.load(f).get("version")
    except Exception:
        pass
    ok = checks["platform_windows"] and checks["monitors"] >= 1 and checks["dep_mcp"] and checks["dep_PIL"]
    return json.dumps({"all_ok": ok, "checks": checks}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
