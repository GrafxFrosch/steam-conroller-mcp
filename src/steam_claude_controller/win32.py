"""Thin ctypes layer over Win32: window lookup, focus, capture and SendInput.

Synthetic input always goes to the *foreground* window, so every input path in this
package must go through `ensure_foreground` first (see controller.Controller._guard).
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes as wt
from dataclasses import dataclass

from PIL import Image, ImageGrab

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Physical pixels everywhere, so window rects match captured images on scaled displays.
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except (AttributeError, OSError):
    pass

HWND = ctypes.c_void_p
ULONG_PTR = ctypes.c_size_t

user32.GetForegroundWindow.restype = HWND
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
for _fn in ("IsWindow", "IsWindowVisible", "IsIconic", "SetForegroundWindow", "BringWindowToTop"):
    getattr(user32, _fn).argtypes = [HWND]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [HWND]
user32.GetWindowTextW.argtypes = [HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wt.POINT)]
user32.GetDC.argtypes = [HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [HWND, wt.HDC]
user32.PrintWindow.argtypes = [HWND, wt.HDC, wt.UINT]
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


# --------------------------------------------------------------------------- windows

@dataclass
class Window:
    hwnd: int
    pid: int
    title: str
    exe: str
    width: int
    height: int


def _process_exe(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Client area in screen coordinates: (left, top, right, bottom)."""
    rc = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, pt.x + rc.right, pt.y + rc.bottom


def list_windows() -> list[Window]:
    found: list[Window] = []
    exe_cache: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wt.BOOL, HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in exe_cache:
            exe_cache[pid.value] = _process_exe(pid.value)
        l, t, r, b = client_rect(hwnd)
        if r - l > 0 and b - t > 0:
            found.append(Window(hwnd, pid.value, title, exe_cache[pid.value], r - l, b - t))
        return True

    user32.EnumWindows(_cb, 0)
    return found


def window_alive(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def is_foreground(hwnd: int) -> bool:
    return (user32.GetForegroundWindow() or 0) == hwnd


def ensure_foreground(hwnd: int, timeout: float = 1.5) -> bool:
    """Bring hwnd to the foreground. Windows restricts this, so use the usual tricks."""
    if is_foreground(hwnd):
        return True
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    me = kernel32.GetCurrentThreadId()
    others = {user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None),
              user32.GetWindowThreadProcessId(hwnd, None)} - {me, 0}
    attached = [tid for tid in others if user32.AttachThreadInput(me, tid, True)]
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for tid in attached:
            user32.AttachThreadInput(me, tid, False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_foreground(hwnd):
            return True
        time.sleep(0.05)
        # Fallback: a synthetic Alt tap lifts the foreground lock for the next call.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    return is_foreground(hwnd)


# --------------------------------------------------------------------------- capture

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def _print_window(hwnd: int, w: int, h: int) -> Image.Image | None:
    hdc_win = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT: grabs the DWM surface even when occluded.
        if not user32.PrintWindow(hwnd, hdc_mem, 3):
            return None
        gdi32.SelectObject(hdc_mem, old)
        bmi = BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0):
            return None
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)


def capture(hwnd: int, allow_focus: bool = True) -> tuple[Image.Image, str]:
    """Capture the client area. Returns (image, method)."""
    l, t, r, b = client_rect(hwnd)
    img = _print_window(hwnd, r - l, b - t)
    if img is not None and img.convert("L").getextrema()[1] > 8:
        return img, "printwindow"
    # Some D3D games hand PrintWindow a black frame; read the screen instead.
    if allow_focus:
        ensure_foreground(hwnd)
        time.sleep(0.05)
        l, t, r, b = client_rect(hwnd)
    return ImageGrab.grab(bbox=(l, t, r, b), all_screens=True), "screen"


# --------------------------------------------------------------------------- input

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]

KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL = 0x1, 0x800
_BUTTON_FLAGS = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
                 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}

VK: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pgup": 0x21, "pageup": 0x21,
    "pgdn": 0x22, "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "ins": 0x2D, "insert": 0x2D, "del": 0x2E, "delete": 0x2E,
    "lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4, "ralt": 0xA5,
    "numlock": 0x90, "scrolllock": 0x91, "`": 0xC0, "grave": 0xC0, "-": 0xBD, "=": 0xBB,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    **{f"kp{d}": 0x60 + d for d in range(10)},
}


def vk_of(name: str) -> int:
    key = name.strip().lower()
    if key not in VK:
        raise ValueError(f"unknown key {name!r}; known: {', '.join(sorted(VK))}")
    return VK[key]


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise OSError(f"SendInput blocked (error {ctypes.get_last_error()}); "
                      "is the game running elevated?")


def key_event(vk: int, up: bool) -> None:
    # Send VK *and* scancode: message-based games read the VK, raw-input/DirectInput
    # games read the scancode.
    scan = user32.MapVirtualKeyW(vk, 4)  # MAPVK_VK_TO_VSC_EX
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED_VKS or (scan >> 8) == 0xE0:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=1)
    inp.u.ki = KEYBDINPUT(vk, scan & 0xFF, flags, 0, 0)
    _send(inp)


def type_unicode(text: str) -> None:
    for ch in text:
        down, up = INPUT(type=1), INPUT(type=1)
        down.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, 0)
        up.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
        _send(down, up)
        time.sleep(0.005)


def mouse_move_relative(dx: int, dy: int) -> None:
    inp = INPUT(type=0)
    inp.u.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0)
    _send(inp)


def mouse_button(button: str, up: bool) -> None:
    flags = _BUTTON_FLAGS[button][1 if up else 0]
    inp = INPUT(type=0)
    inp.u.mi = MOUSEINPUT(0, 0, 0, flags, 0, 0)
    _send(inp)


def mouse_wheel(notches: int) -> None:
    inp = INPUT(type=0)
    inp.u.mi = MOUSEINPUT(0, 0, ctypes.c_uint32(120 * notches).value, MOUSEEVENTF_WHEEL, 0, 0)
    _send(inp)


def set_cursor(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)
