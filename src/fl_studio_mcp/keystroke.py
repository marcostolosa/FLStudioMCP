"""Small Win32 input helpers for FL Studio host-side UI operations.

The piano-roll bridge uses Ctrl+Alt+Y. Project Save As is implemented here
because FL's controller API exposes only ``FPT_SaveNew`` (which ignores the
requested path). These helpers use only :mod:`ctypes`; pywin32 is not required.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

log = logging.getLogger("fl_studio_mcp.keystroke")

HOTKEY_NAME = "Ctrl+Alt+Y (Win) / Cmd+Opt+Y (Mac)"


def request_file():
    from .file_bridge import REQUEST_FILE
    return REQUEST_FILE


def state_file():
    from .file_bridge import STATE_FILE
    return STATE_FILE


def clear_state() -> None:
    from .file_bridge import clear_state as _clear_state
    _clear_state()


def wait_for_state(deadline_sec: float = 3.0) -> dict | None:
    """Poll for the state file produced by the piano-roll pyscript."""
    from .file_bridge import wait_for_state as _wait_for_state
    return _wait_for_state(deadline_sec)


class _Win32Input:
    """Minimal SendInput wrapper, created lazily on Windows."""

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1

    def __init__(self):
        import ctypes
        import ctypes.wintypes as w

        self.ctypes = ctypes
        self.w = w
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", ctypes.POINTER(w.ULONG))]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                        ("dwFlags", w.DWORD), ("time", w.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(w.ULONG))]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]

        class _UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", w.DWORD), ("u", _UNION)]

        self.KEYBDINPUT = KEYBDINPUT
        self.INPUT = INPUT
        self.user32.SendInput.argtypes = (w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)

    def key(self, vk: int, *, up: bool = False, scan: int = 0, unicode: bool = False):
        item = self.INPUT()
        item.type = self.INPUT_KEYBOARD
        flags = (self.KEYEVENTF_KEYUP if up else 0) | (self.KEYEVENTF_UNICODE if unicode else 0)
        item.u.ki = self.KEYBDINPUT(vk, scan, flags, 0, None)
        return item

    def send(self, items) -> bool:
        seq = list(items)
        array = (self.INPUT * len(seq))(*seq)
        sent = self.user32.SendInput(len(seq), array, self.ctypes.sizeof(self.INPUT))
        return sent == len(seq)

    def combo(self, modifiers: list[int], key: int) -> bool:
        return self.send(
            [self.key(vk) for vk in modifiers] + [self.key(key), self.key(key, up=True)]
            + [self.key(vk, up=True) for vk in reversed(modifiers)]
        )

    def text(self, value: str) -> bool:
        units = value.encode("utf-16-le")
        items = []
        for offset in range(0, len(units), 2):
            unit = int.from_bytes(units[offset:offset + 2], "little")
            items.extend([self.key(0, scan=unit, unicode=True),
                          self.key(0, up=True, scan=unit, unicode=True)])
        return self.send(items)

    def find_fl_window(self) -> int:
        target = [0]
        callback_type = self.ctypes.WINFUNCTYPE(self.ctypes.c_int, self.w.HWND, self.w.LPARAM)

        def callback(hwnd, _):
            length = self.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return 1
            buf = self.ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buf, length + 1)
            if "FL Studio" in buf.value and self.user32.IsWindowVisible(hwnd):
                target[0] = hwnd
                return 0
            return 1

        self.user32.EnumWindows(callback_type(callback), 0)
        return target[0]


def send_hotkey_windows() -> bool:
    """Find FL Studio and send Ctrl+Alt+Y. Returns True on success."""
    if sys.platform != "win32":
        return False
    try:
        api = _Win32Input()
        hwnd = api.find_fl_window()
        if not hwnd:
            log.warning("FL Studio window not found for keystroke")
            return False
        api.user32.ShowWindow(hwnd, 9)
        api.user32.SetForegroundWindow(hwnd)
        time.sleep(0.08)
        return api.combo([0x11, 0x12], 0x59)  # Ctrl+Alt+Y
    except Exception as exc:
        log.warning("keystroke failed: %s", exc)
        return False


def save_project_as_windows(path: str, timeout_sec: float = 15.0) -> dict:
    """Save the active FL project to an explicit, previously-unused ``.flp`` path."""
    target = Path(path).expanduser()
    if target.suffix.lower() != ".flp":
        return {"ok": False, "error": "project_save_as path must end in .flp"}
    try:
        target = target.resolve(strict=False)
    except OSError as exc:
        return {"ok": False, "error": f"invalid project path: {exc}"}
    if not target.parent.is_dir():
        return {"ok": False, "error": f"parent directory does not exist: {target.parent}"}
    if target.exists():
        return {"ok": False, "error": f"target already exists; refusing to overwrite: {target}"}
    if sys.platform != "win32":
        return {"ok": False, "error": "project_save_as UI automation is available on Windows only"}

    try:
        api = _Win32Input()
        hwnd = api.find_fl_window()
        if not hwnd:
            return {"ok": False, "error": "FL Studio window not found"}
        fl_pid = api.w.DWORD()
        api.user32.GetWindowThreadProcessId(hwnd, api.ctypes.byref(fl_pid))

        api.user32.ShowWindow(hwnd, 9)
        api.user32.SetForegroundWindow(hwnd)
        time.sleep(0.08)
        if not api.combo([0x11, 0x10], 0x53):  # Ctrl+Shift+S
            return {"ok": False, "error": "failed to send Ctrl+Shift+S to FL Studio"}

        dialog = [0]
        deadline = time.monotonic() + min(max(timeout_sec, 1.0), 30.0)
        callback_type = api.ctypes.WINFUNCTYPE(api.ctypes.c_int, api.w.HWND, api.w.LPARAM)

        def find_dialog(candidate, _):
            if not api.user32.IsWindowVisible(candidate):
                return 1
            pid = api.w.DWORD()
            api.user32.GetWindowThreadProcessId(candidate, api.ctypes.byref(pid))
            if pid.value != fl_pid.value:
                return 1
            cls = api.ctypes.create_unicode_buffer(64)
            api.user32.GetClassNameW(candidate, cls, len(cls))
            if cls.value == "#32770":
                dialog[0] = candidate
                return 0
            return 1

        while time.monotonic() < deadline and not dialog[0]:
            api.user32.EnumWindows(callback_type(find_dialog), 0)
            if not dialog[0]:
                time.sleep(0.05)
        if not dialog[0]:
            return {"ok": False, "error": "FL Studio Save As dialog did not appear"}

        api.user32.SetForegroundWindow(dialog[0])
        time.sleep(0.05)
        if not api.combo([0x12], 0x4E):  # Alt+N -> File name / Nome
            return {"ok": False, "error": "failed to focus the Save As filename field"}
        if not api.combo([0x11], 0x41) or not api.text(str(target)):  # Ctrl+A, type path
            return {"ok": False, "error": "failed to enter the Save As path"}
        if not api.send([api.key(0x0D), api.key(0x0D, up=True)]):
            return {"ok": False, "error": "failed to submit the Save As dialog"}

        while time.monotonic() < deadline:
            if target.is_file() and target.stat().st_size > 0:
                return {"ok": True, "path": str(target), "bytes": target.stat().st_size,
                        "verified": True}
            time.sleep(0.05)
        return {"ok": False, "error": f"Save As completed without creating {target}"}
    except Exception as exc:
        log.warning("project Save As failed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
