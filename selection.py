# -*- coding: utf-8 -*-
"""
In-process Windows Win32 Selection Reader, In-Place Replacer, and SAPI TTS Speaker
for OmniTranslate (顺译 · 多源翻译).
Uses only Python standard library `ctypes` (zero child processes, compatible with
WinShun Job Object ActiveProcessLimit = 1 without requiring 'processes' permission).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12      # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_C = 0x43
VK_V = 0x56

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Setup Win32 signatures for 64-bit pointer safety
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.GetAsyncKeyState.argtypes = [ ctypes.c_int ]
user32.GetAsyncKeyState.restype = wintypes.SHORT

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    ]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def get_clipboard_text() -> str | None:
    """Reads CF_UNICODETEXT from Windows clipboard."""
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return None
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return None
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        time.sleep(0.01)
    return None


def set_clipboard_text(text: str) -> bool:
    """Writes CF_UNICODETEXT to Windows clipboard."""
    data = (text + "\0").encode("utf-16-le")
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not h_mem:
                    return False
                ptr = kernel32.GlobalLock(h_mem)
                if not ptr:
                    kernel32.GlobalFree(h_mem)
                    return False
                ctypes.memmove(ptr, data, len(data))
                kernel32.GlobalUnlock(h_mem)
                if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                    kernel32.GlobalFree(h_mem)
                    return False
                return True
            finally:
                user32.CloseClipboard()
        time.sleep(0.01)
    return False


def _make_key_input(vk: int, key_up: bool = False) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = 0
    inp.u.ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = 0
    return inp


def _send_inputs(inputs: list[INPUT]) -> bool:
    if not inputs:
        return False
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    return sent == len(inputs)


def _wait_modifiers_released(timeout_ms: int = 220) -> None:
    """
    Waits briefly for the user to physically release Alt/Shift/Win after pressing a global hotkey
    so synthetic Ctrl+C / Ctrl+V is not interpreted as Ctrl+Alt+C by the target window.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        held = (
            (user32.GetAsyncKeyState(VK_MENU) & 0x8000)
            or (user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
            or (user32.GetAsyncKeyState(VK_LWIN) & 0x8000)
            or (user32.GetAsyncKeyState(VK_RWIN) & 0x8000)
        )
        if not held:
            return
        time.sleep(0.01)


def _send_ctrl_shortcut(vk_letter: int) -> bool:
    """
    Waits for physical modifiers to release, presses Ctrl BEFORE releasing any residual Alt
    (preventing Win32 menu-bar activation), and sends clean Ctrl + <vk_letter>.
    """
    _wait_modifiers_released(220)
    events: list[INPUT] = [
        _make_key_input(VK_CONTROL, key_up=False),
        _make_key_input(VK_MENU, key_up=True),
        _make_key_input(VK_SHIFT, key_up=True),
        _make_key_input(VK_LWIN, key_up=True),
        _make_key_input(VK_RWIN, key_up=True),
        _make_key_input(vk_letter, key_up=False),
        _make_key_input(vk_letter, key_up=True),
        _make_key_input(VK_CONTROL, key_up=True),
    ]
    return _send_inputs(events)


def read_selection(timeout_ms: int = 450) -> str | None:
    """
    Ported from KittyTools SelectionReader.ReadViaClipboardAsync:
    1. Snapshots current clipboard text
    2. Records GetClipboardSequenceNumber()
    3. Sends clean Ctrl+C
    4. Polls sequence number up to timeout_ms
    5. Reads copied selection and restores previous clipboard text
    """
    backup = get_clipboard_text()
    before_seq = user32.GetClipboardSequenceNumber()
    try:
        if not _send_ctrl_shortcut(VK_C):
            return None
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        changed = False
        while time.monotonic() < deadline:
            if user32.GetClipboardSequenceNumber() != before_seq:
                changed = True
                break
            time.sleep(0.012)
        if not changed:
            return None
        copied = get_clipboard_text()
        if not copied or not copied.strip():
            return None
        return copied
    finally:
        if backup is not None:
            # Small delay so target app finishes writing clipboard before restore
            time.sleep(0.015)
            set_clipboard_text(backup)


def replace_selection(translated_text: str) -> bool:
    """
    Preserves clipboard backup, writes translated_text, sends Ctrl+V, and restores backup
    after target application finishes processing paste.
    """
    if not translated_text:
        return False
    backup = get_clipboard_text()
    try:
        if not set_clipboard_text(translated_text):
            return False
        time.sleep(0.02)
        ok = _send_ctrl_shortcut(VK_V)
        time.sleep(0.24)
        return ok
    finally:
        if backup is not None:
            set_clipboard_text(backup)


# ---------------------------------------------------------------------------
# In-process Windows SAPI.SpVoice TTS via COM IDispatch (ole32 + oleaut32)
# ---------------------------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class VARIANT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [
            ("llVal", ctypes.c_longlong),
            ("lVal", wintypes.LONG),
            ("bstrVal", wintypes.LPWSTR),
            ("pdispVal", ctypes.c_void_p),
        ]

    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("u", _U),
    ]


class DISPPARAMS(ctypes.Structure):
    _fields_ = [
        ("rgvarg", ctypes.POINTER(VARIANT)),
        ("rgdispidNamedArgs", ctypes.POINTER(wintypes.LONG)),
        ("cArgs", wintypes.UINT),
        ("cNamedArgs", wintypes.UINT),
    ]


ole32 = ctypes.windll.ole32
oleaut32 = ctypes.windll.oleaut32

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CLSIDFromProgID.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
ole32.CLSIDFromProgID.restype = ctypes.c_long
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(GUID),
    ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long

oleaut32.SysAllocString.argtypes = [wintypes.LPCWSTR]
oleaut32.SysAllocString.restype = ctypes.c_void_p
oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
oleaut32.SysFreeString.restype = None

IID_IDispatch = GUID(0x00020400, 0x0000, 0x0000, (ctypes.c_ubyte * 8)(0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46))
CLSCTX_INPROC_SERVER = 1
CLSCTX_LOCAL_SERVER = 4
VT_BSTR = 8
VT_I4 = 3
DISPATCH_METHOD = 1


_SPEAK_LOCK = threading.Lock()
_SPEAK_PENDING: list[str] = []
_SPEAK_ACTIVE = False


def _sapi_worker() -> None:
    global _SPEAK_ACTIVE
    hr = ole32.CoInitializeEx(None, 0)
    should_uninit = hr >= 0
    try:
        clsid = GUID()
        if ole32.CLSIDFromProgID("SAPI.SpVoice", ctypes.byref(clsid)) < 0:
            return
        p_disp = ctypes.c_void_p()
        if ole32.CoCreateInstance(
            ctypes.byref(clsid),
            None,
            CLSCTX_INPROC_SERVER | CLSCTX_LOCAL_SERVER,
            ctypes.byref(IID_IDispatch),
            ctypes.byref(p_disp),
        ) < 0 or not p_disp.value:
            return

        vtable = ctypes.cast(ctypes.cast(p_disp, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        ReleaseProto = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
        GetIDsOfNamesProto = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(GUID),
            ctypes.POINTER(wintypes.LPWSTR),
            wintypes.UINT,
            wintypes.LCID,
            ctypes.POINTER(wintypes.LONG),
        )
        InvokeProto = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            wintypes.LONG,
            ctypes.POINTER(GUID),
            wintypes.LCID,
            wintypes.WORD,
            ctypes.POINTER(DISPPARAMS),
            ctypes.POINTER(VARIANT),
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        release_fn = ReleaseProto(vtable[2])
        get_ids_fn = GetIDsOfNamesProto(vtable[5])
        invoke_fn = InvokeProto(vtable[6])

        try:
            iid_null = GUID()
            name = wintypes.LPWSTR("Speak")
            dispid = wintypes.LONG()
            if get_ids_fn(p_disp, ctypes.byref(iid_null), ctypes.byref(name), 1, 0, ctypes.byref(dispid)) < 0:
                return

            while True:
                with _SPEAK_LOCK:
                    if not _SPEAK_PENDING:
                        _SPEAK_ACTIVE = False
                        return
                    text = _SPEAK_PENDING.pop()
                    _SPEAK_PENDING.clear()

                bstr = oleaut32.SysAllocString(text[:1000])
                if not bstr:
                    continue
                try:
                    # SVSFPurgeBeforeSpeak (2) interrupts any previous utterance cleanly
                    args = (VARIANT * 2)()
                    args[0].vt = VT_I4
                    args[0].u.lVal = 2
                    args[1].vt = VT_BSTR
                    args[1].u.llVal = bstr

                    params = DISPPARAMS()
                    params.rgvarg = ctypes.cast(args, ctypes.POINTER(VARIANT))
                    params.rgdispidNamedArgs = None
                    params.cArgs = 2
                    params.cNamedArgs = 0

                    res = VARIANT()
                    invoke_fn(
                        p_disp,
                        dispid.value,
                        ctypes.byref(iid_null),
                        0,
                        DISPATCH_METHOD,
                        ctypes.byref(params),
                        ctypes.byref(res),
                        None,
                        None,
                    )
                finally:
                    oleaut32.SysFreeString(bstr)
        finally:
            release_fn(p_disp)
    except Exception:
        pass
    finally:
        with _SPEAK_LOCK:
            _SPEAK_ACTIVE = False
        if should_uninit:
            ole32.CoUninitialize()


def speak_async(text: str) -> None:
    """Speaks text asynchronously using a single serialized SAPI worker thread."""
    global _SPEAK_ACTIVE
    cleaned = (text or "").strip()
    if not cleaned:
        return
    with _SPEAK_LOCK:
        _SPEAK_PENDING.append(cleaned)
        if _SPEAK_ACTIVE:
            return
        _SPEAK_ACTIVE = True
    threading.Thread(target=_sapi_worker, daemon=True, name="omni-tts").start()
