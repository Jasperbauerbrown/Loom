"""The listener's Windows layer (03 §5.7): ctypes over user32, winmm and kernel32, plus
`winsound`. No decisions live here — `listener.py` makes them all; this file only
registers, captures and plays. Imported lazily by `listener.main`, and only on Windows.

- `Hotkeys`: a thread that owns a message queue. `RegisterHotKey(NULL, …, MOD_NOREPEAT)`
  binds the combo to that thread, `GetMessageW` delivers `WM_HOTKEY`; Esc is registered
  and unregistered on the same thread (hotkeys are per-thread) when the listener posts
  `WM_APP_ARM`. `is_down()` reads `GetAsyncKeyState` for hold-mode release.
- `WaveIn`: waveIn at 16 kHz mono PCM16 into a ring of `RING` buffers of `BUFFER_MS`,
  polled for `WHDR_DONE` by `read()` (CALLBACK_NULL: no callback thread to reason about).
- `Cues`: the generated tones, played by `winsound.PlaySound(SND_MEMORY)` on a worker
  thread (SND_MEMORY cannot be combined with SND_ASYNC).
- `single_instance()`: the named mutex `Local\\loom-voice-listener`.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from collections import deque
from ctypes import wintypes
from typing import Any

from .listener import (
    BYTES_PER_MS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    MOD_NOREPEAT,
    VK_ESCAPE,
    AudioError,
    Hotkey,
    HotkeyTaken,
    cue_wav,
)

log = logging.getLogger(__name__)

MUTEX_NAME = "Local\\loom-voice-listener"
ERROR_ALREADY_EXISTS = 183

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP_ARM = 0x8000 + 1  # WM_APP + 1: wParam 1 = register Esc, 0 = unregister
PM_NOREMOVE = 0x0000
HOTKEY_ID = 1
CANCEL_ID = 2

WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001
MMSYSERR_NOERROR = 0
RING = 8
BUFFER_MS = 100

_user32: Any = None
_kernel32: Any = None
_winmm: Any = None


def _libs() -> tuple[Any, Any, Any]:
    """user32, kernel32, winmm with argtypes set (64-bit handles must not truncate)."""
    global _user32, _kernel32, _winmm
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        u.RegisterHotKey.restype = wintypes.BOOL
        u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        u.UnregisterHotKey.restype = wintypes.BOOL
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u.GetMessageW.restype = wintypes.BOOL
        u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        u.PeekMessageW.restype = wintypes.BOOL
        u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostThreadMessageW.restype = wintypes.BOOL
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.GetCurrentThreadId.argtypes = []
        k.GetCurrentThreadId.restype = wintypes.DWORD
        w = ctypes.WinDLL("winmm")
        w.waveInGetNumDevs.argtypes = []
        w.waveInGetNumDevs.restype = wintypes.UINT
        w.waveInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(WAVEINCAPSW), wintypes.UINT]
        w.waveInGetDevCapsW.restype = wintypes.UINT
        w.waveInOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
                                 ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        w.waveInOpen.restype = wintypes.UINT
        for name in ("waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer"):
            fn = getattr(w, name)
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(WAVEHDR), wintypes.UINT]
            fn.restype = wintypes.UINT
        for name in ("waveInStart", "waveInStop", "waveInReset", "waveInClose"):
            fn = getattr(w, name)
            fn.argtypes = [wintypes.HANDLE]
            fn.restype = wintypes.UINT
        _user32, _kernel32, _winmm = u, k, w
    return _user32, _kernel32, _winmm


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1  # 18 bytes in the SDK (mmeapi.h packs it)
    _fields_ = [
        ("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [
    ("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD), ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_size_t), ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(WAVEHDR)), ("reserved", ctypes.c_size_t),
]


class WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD), ("wReserved1", wintypes.WORD),
    ]


def pcm_format() -> WAVEFORMATEX:
    """16 kHz mono PCM16, the one format Loom records."""
    return WAVEFORMATEX(WAVE_FORMAT_PCM, 1, SAMPLE_RATE, SAMPLE_RATE * SAMPLE_WIDTH, SAMPLE_WIDTH, SAMPLE_WIDTH * 8, 0)


# ---------------------------------------------------------------------------------
# single instance
# ---------------------------------------------------------------------------------
def single_instance() -> int | None:
    """The mutex handle, or None when another listener already holds it."""
    _, k, _ = _libs()
    handle = k.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        k.CloseHandle(handle)
        return None
    return int(handle)


def release(handle: int | None) -> None:
    if handle:
        _libs()[1].CloseHandle(handle)


# ---------------------------------------------------------------------------------
# hotkeys
# ---------------------------------------------------------------------------------
class Hotkeys:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._vk = 0

    def register(self, hotkey: Hotkey, events: "queue.Queue[tuple[str, ...]]") -> None:
        ready = threading.Event()
        outcome: dict[str, Any] = {}
        self._vk = hotkey.vk

        def loop() -> None:
            u, k, _ = _libs()
            msg = wintypes.MSG()
            u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)  # creates this thread's queue
            self._tid = int(k.GetCurrentThreadId())
            ok = bool(u.RegisterHotKey(None, HOTKEY_ID, hotkey.mod_flags, hotkey.vk))
            outcome["ok"] = ok
            outcome["error"] = ctypes.get_last_error()
            ready.set()
            if not ok:
                return
            armed = False
            try:
                while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == WM_HOTKEY:
                        events.put(("press",) if msg.wParam == HOTKEY_ID else ("cancel",))
                    elif msg.message == WM_APP_ARM:
                        if msg.wParam and not armed:
                            armed = bool(u.RegisterHotKey(None, CANCEL_ID, MOD_NOREPEAT, VK_ESCAPE))
                        elif not msg.wParam and armed:
                            u.UnregisterHotKey(None, CANCEL_ID)
                            armed = False
            finally:
                if armed:
                    u.UnregisterHotKey(None, CANCEL_ID)
                u.UnregisterHotKey(None, HOTKEY_ID)

        self._thread = threading.Thread(target=loop, name="loom-voice-hotkey", daemon=True)
        self._thread.start()
        ready.wait(5)
        if not outcome.get("ok"):
            log.info("RegisterHotKey failed (error %s)", outcome.get("error"))
            raise HotkeyTaken(hotkey.text)

    def is_down(self) -> bool:
        return bool(_libs()[0].GetAsyncKeyState(self._vk) & 0x8000)

    def arm_cancel(self, on: bool) -> None:
        if self._tid:
            _libs()[0].PostThreadMessageW(self._tid, WM_APP_ARM, 1 if on else 0, 0)

    def close(self) -> None:
        if self._tid and self._thread is not None and self._thread.is_alive():
            _libs()[0].PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(timeout=2)


# ---------------------------------------------------------------------------------
# microphone
# ---------------------------------------------------------------------------------
class WaveIn:
    def __init__(self) -> None:
        self._handle: Any = None
        self._headers: list[WAVEHDR] = []
        self._buffers: list[Any] = []
        self._order: deque[int] = deque()

    def devices(self) -> list[str]:
        _, _, w = _libs()
        names = []
        for i in range(int(w.waveInGetNumDevs())):
            caps = WAVEINCAPSW()
            if w.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == MMSYSERR_NOERROR:
                names.append(caps.szPname)
        return names

    def start(self, index: int | None) -> None:
        _, _, w = _libs()
        if self._handle is not None:
            self.stop()
        handle = wintypes.HANDLE()
        fmt = pcm_format()
        device = WAVE_MAPPER if index is None else int(index)
        rc = w.waveInOpen(ctypes.byref(handle), device, ctypes.byref(fmt), 0, 0, CALLBACK_NULL)
        if rc != MMSYSERR_NOERROR:
            raise AudioError("no microphone" if rc == 2 else f"waveInOpen failed (MMSYSERR {rc})")  # 2 = BADDEVICEID
        self._handle = handle
        size = BYTES_PER_MS * BUFFER_MS
        self._buffers = [ctypes.create_string_buffer(size) for _ in range(RING)]
        self._headers = [WAVEHDR() for _ in range(RING)]
        self._order.clear()
        try:
            for i, (hdr, buf) in enumerate(zip(self._headers, self._buffers)):
                hdr.lpData = ctypes.addressof(buf)
                hdr.dwBufferLength = size
                self._check(w.waveInPrepareHeader(handle, ctypes.byref(hdr), ctypes.sizeof(hdr)), "prepare")
                self._check(w.waveInAddBuffer(handle, ctypes.byref(hdr), ctypes.sizeof(hdr)), "add buffer")
                self._order.append(i)
            self._check(w.waveInStart(handle), "start")
        except AudioError:
            self._close()
            raise

    @staticmethod
    def _check(rc: int, what: str) -> None:
        if rc != MMSYSERR_NOERROR:
            raise AudioError(f"waveIn {what} failed (MMSYSERR {rc})")

    def read(self) -> bytes:
        """Every finished buffer, in order, re-queued for more."""
        if self._handle is None:
            return b""
        _, _, w = _libs()
        out = bytearray()
        while self._order and self._headers[self._order[0]].dwFlags & WHDR_DONE:
            i = self._order.popleft()
            hdr = self._headers[i]
            out += self._buffers[i].raw[: hdr.dwBytesRecorded]
            hdr.dwFlags &= ~WHDR_DONE
            hdr.dwBytesRecorded = 0
            self._check(w.waveInAddBuffer(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)), "add buffer")
            self._order.append(i)
        return bytes(out)

    def stop(self) -> bytes:
        """Reset returns every buffer (the partial one too) marked done; collect them."""
        if self._handle is None:
            return b""
        _, _, w = _libs()
        w.waveInReset(self._handle)
        out = bytearray()
        for i in self._order:
            hdr = self._headers[i]
            if hdr.dwFlags & WHDR_DONE:
                out += self._buffers[i].raw[: hdr.dwBytesRecorded]
        self._close()
        return bytes(out)

    def _close(self) -> None:
        _, _, w = _libs()
        if self._handle is None:
            return
        w.waveInReset(self._handle)
        for hdr in self._headers:
            w.waveInUnprepareHeader(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
        w.waveInClose(self._handle)
        self._handle = None
        self._order.clear()


# ---------------------------------------------------------------------------------
# cues
# ---------------------------------------------------------------------------------
class Cues:
    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._sounds: dict[str, bytes] = {}
        self._thread = threading.Thread(target=self._worker, name="loom-voice-cues", daemon=True)
        self._thread.start()

    def play(self, name: str) -> None:
        self._queue.put(name)

    def _worker(self) -> None:
        import winsound

        while True:
            name = self._queue.get()
            if name is None:
                return
            data = self._sounds.get(name)
            if data is None:
                data = self._sounds[name] = cue_wav(name)
            try:
                winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            except RuntimeError as exc:
                log.debug("cue %s not played: %s", name, exc)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=2)
