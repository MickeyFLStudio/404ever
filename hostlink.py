"""Talks to ts404_host.dll inside another program (FL Studio).

inject(thread_id): loads the DLL into the thread's process with a WH_GETMESSAGE hook
                   (the DLL pins itself and the hook is removed again right away)
find_host(pid):    the DLL's "TS404Host" message-only window in that process, or 0
send(host, text):  one text command over WM_COPYDATA; the reply is the DLL's return value
                   (0 = refused / unknown, 1 = done, 1000+n = ping with n panels)
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))   # next to the exe's files when frozen
DLL = os.path.join(HERE, "ts404_host.dll")
HWND_MESSAGE = wintypes.HWND(-3)
WH_GETMESSAGE = 3
WM_NULL, WM_COPYDATA = 0x0000, 0x004A

for f in ("FindWindowExW", "SetWindowsHookExW"):
    getattr(user32, f).restype = wintypes.HANDLE
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HMODULE, wintypes.DWORD]
user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel32.LoadLibraryW.restype = wintypes.HMODULE
kernel32.LoadLibraryW.argtypes = [wintypes.LPCWSTR]
kernel32.GetProcAddress.restype = ctypes.c_void_p
kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]


_dll = None


def _module():
    global _dll
    if _dll is None:
        h = kernel32.LoadLibraryW(DLL)
        if not h:
            raise OSError("cannot load " + DLL)
        proc = kernel32.GetProcAddress(h, b"ts404_hook")
        if not proc:
            raise OSError("ts404_hook not exported by " + DLL)
        _dll = (h, proc)
    return _dll


def find_host(pid):
    h = 0
    while True:
        h = user32.FindWindowExW(HWND_MESSAGE, h, "TS404HostWnd", None) or 0
        if not h:
            return 0
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid:
            return h


def inject(thread_id, pid, timeout=5.0, pump=None):
    """Load the DLL into the thread's process and wait for its host window. Returns the window or 0.
    pump: called while waiting (needed when hooking the calling thread itself, which must keep pumping)."""
    host = find_host(pid)
    if host:
        return host
    hmod, proc = _module()
    hook = user32.SetWindowsHookExW(WH_GETMESSAGE, proc, hmod, thread_id)
    if not hook:
        raise OSError("SetWindowsHookEx failed: %d" % kernel32.GetLastError())
    try:
        end = time.time() + timeout
        while time.time() < end:
            user32.PostThreadMessageW(thread_id, WM_NULL, 0, 0)   # make sure the hook runs at least once
            if pump:
                pump()
            time.sleep(0.05)
            host = find_host(pid)
            if host:
                return host
        return 0
    finally:
        user32.UnhookWindowsHookEx(hook)   # the DLL pinned itself; it stays loaded


def send(host, text, timeout_ms=2000):
    data = text.encode("utf-8")
    buf = ctypes.create_string_buffer(data, len(data))
    cds = COPYDATASTRUCT(0, len(data), ctypes.cast(buf, ctypes.c_void_p))
    result = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(host, WM_COPYDATA, 0, ctypes.addressof(cds),
                                    0x0002 | 0x0008, timeout_ms, ctypes.byref(result))   # ABORTIFHUNG | NOTIMEOUTIFNOTHUNG
    return result.value if ok else None
