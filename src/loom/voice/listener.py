"""The voice listener (03 §5.7): global hotkey → microphone → clip → the desk.

`loom listen` runs this as its own small process, normally started and supervised by
the backend (`supervise.py`). Every decision lives here, as a state machine over five
injected parts so it is tested with fakes and never touches real hardware in a test:

- `HotkeySource` — the registered hotkey: presses arrive as `("press",)` / `("cancel",)`
  events on a queue; `is_down()` says whether the main key is still held (hold mode
  polls it); `arm_cancel(bool)` registers Esc only while recording.
- `AudioSource` — `devices()`, `start(index | None)`, `read()` (PCM16 captured since the
  last read) and `stop()` (the rest).
- `Backend` — REST to the backend: settings, the desk id, the clip upload, the status.
- `Cues` — `play(name)` for start / stop / cancel / error tones.
- `Clock` — `monotonic()` and `sleep(s)`.

`win32.py` provides the real hotkey, audio and cues with ctypes and makes no decisions.

Flow: press → start cue → record → stop (release in hold mode, a second press in toggle
mode, `voice.max_seconds`, or Esc = cancel) → stop cue → shorter than `voice.min_ms` or
silent (no 30 ms frame above `SILENCE_RMS`) → dropped with the cancel cue → otherwise
the WAV is written to `APP_HOME/voice-spool/` first (persist before act) and the
uploader drains the spool oldest-first to `POST /conversations/{desk}/audio`. A clip
stays spooled only while the backend cannot be reached; any answer (ok, dropped,
failed, no model) settles it, since the backend has stored the audio by then.

Status (`POST /voice/status`) on start, on every state change and on every error.
States: starting | idle | recording | transcribing | error | unsupported | stopped.
"""

from __future__ import annotations

import array
import io
import logging
import math
import os
import queue
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # bytes, PCM16
BYTES_PER_MS = SAMPLE_RATE * SAMPLE_WIDTH // 1000  # 32
FRAME_MS = 30  # silence gate frame
SILENCE_RMS = 300  # of 32767 (about -40 dBFS): room noise stays under it, speech does not
TICK_S = 0.02  # hold-release polling and audio draining cadence
SETTINGS_RETRY_S = 60.0  # how long start-up waits for the backend before giving up
SPOOL_RETRY_S = 30.0  # while clips are spooled, how often the backend is tried again

# Exit codes (the supervisor restarts only on non-zero).
EXIT_OK = 0  # clean stop, unsupported platform, another listener, invalid hotkey
EXIT_HOTKEY_TAKEN = 3  # another application holds the combo; a later restart may win
EXIT_UNREACHABLE = 4  # the backend never answered at start-up (same code as the CLI)


# ---------------------------------------------------------------------------------
# hotkey strings
# ---------------------------------------------------------------------------------
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_ESCAPE = 0x1B

MODIFIERS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
MODIFIER_ALIASES = {"control": "ctrl", "super": "win", "meta": "win", "cmd": "win", "option": "alt"}
MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")

NAMED_KEYS: dict[str, int] = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "scrolllock": 0x91, "printscreen": 0x2C,
}
KEY_ALIASES = {"return": "enter", "del": "delete", "ins": "insert", "pgup": "pageup", "pgdn": "pagedown",
               "esc": "escape", "spacebar": "space"}
# Keys that type something: as a bare hotkey they would swallow ordinary typing.
TYPING_KEYS = {"space", "enter", "tab", "backspace"}


def _key_vk(key: str) -> int | None:
    if len(key) == 1 and "a" <= key <= "z":
        return ord(key.upper())
    if len(key) == 1 and "0" <= key <= "9":
        return ord(key)
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1
    if key.startswith("numpad") and key[6:].isdigit() and 0 <= int(key[6:]) <= 9:
        return 0x60 + int(key[6:])
    return NAMED_KEYS.get(key)


@dataclass(frozen=True)
class Hotkey:
    text: str  # canonical: "ctrl+alt+space"
    modifiers: tuple[str, ...]
    key: str
    vk: int
    mod_flags: int  # MOD_* | MOD_NOREPEAT, ready for RegisterHotKey


def parse_hotkey(text: str) -> Hotkey:
    """`ctrl+alt+space` → a `Hotkey`. Raises ValueError with a message for the status
    line: unknown names, no key, two keys, a repeated modifier, Esc (it cancels a
    recording), or a key that types something without any modifier."""
    raw = str(text or "").strip().lower().replace(" ", "")
    if not raw:
        raise ValueError("no hotkey set")
    parts = raw.split("+")
    if any(not p for p in parts):
        raise ValueError(f"hotkey {text!r} has an empty part")
    mods: list[str] = []
    keys: list[str] = []
    for part in parts:
        name = MODIFIER_ALIASES.get(part, part)
        if name in MODIFIERS:
            if name in mods:
                raise ValueError(f"hotkey {text!r} names {name} twice")
            mods.append(name)
        else:
            keys.append(KEY_ALIASES.get(part, part))
    if not keys:
        raise ValueError(f"hotkey {text!r} has no key after the modifiers")
    if len(keys) > 1:
        raise ValueError(f"hotkey {text!r} names more than one key ({', '.join(keys)})")
    key = keys[0]
    if key == "escape":
        raise ValueError("Esc cancels a recording, so it cannot be the hotkey")
    vk = _key_vk(key)
    if vk is None:
        raise ValueError(f"hotkey {text!r}: unknown key {key!r} (use a-z, 0-9, f1-f24, numpad0-9 or a named key)")
    if not mods and (len(key) == 1 or key in TYPING_KEYS):
        raise ValueError(f"hotkey {text!r} needs a modifier: on its own {key!r} would stop typing it anywhere")
    ordered = tuple(m for m in MODIFIER_ORDER if m in mods)
    flags = MOD_NOREPEAT
    for m in ordered:
        flags |= MODIFIERS[m]
    return Hotkey("+".join((*ordered, key)), ordered, key, vk, flags)


# ---------------------------------------------------------------------------------
# audio helpers (pure)
# ---------------------------------------------------------------------------------
def wav_bytes(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """PCM16 mono → a complete WAV file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _samples(pcm: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()  # PCM in a WAV is little-endian
    return samples


def peak_rms(pcm: bytes, frame_ms: int = FRAME_MS, rate: int = SAMPLE_RATE) -> float:
    """The loudest frame's RMS (0..32767). A clip is speech when this clears the gate."""
    samples = _samples(pcm)
    step = max(1, rate * frame_ms // 1000)
    peak = 0.0
    for start in range(0, len(samples), step):
        frame = samples[start:start + step]
        if not frame:
            break
        rms = math.sqrt(sum(s * s for s in frame) / len(frame))
        peak = max(peak, rms)
    return peak


def tone_wav(freq: float, ms: int, *, volume: float = 0.25, rate: int = 22050) -> bytes:
    """A short sine tone with a 5 ms fade at both ends (no clicks), as a WAV."""
    count = rate * ms // 1000
    fade = max(1, rate * 5 // 1000)
    samples = array.array("h")
    for i in range(count):
        env = min(1.0, i / fade, (count - 1 - i) / fade)
        samples.append(int(32767 * volume * env * math.sin(2 * math.pi * freq * i / rate)))
    if sys.byteorder == "big":
        samples.byteswap()
    return wav_bytes(samples.tobytes(), rate)


# One tone (or a two-note figure) per cue: rising = start, falling = stop.
CUE_NOTES: dict[str, tuple[tuple[float, int], ...]] = {
    "start": ((660.0, 60), (880.0, 70)),
    "stop": ((880.0, 60), (660.0, 70)),
    "cancel": ((440.0, 90),),
    "error": ((220.0, 120), (220.0, 120)),
}


def cue_wav(name: str) -> bytes:
    pcm = b""
    for freq, ms in CUE_NOTES[name]:
        with wave.open(io.BytesIO(tone_wav(freq, ms)), "rb") as w:
            pcm += w.readframes(w.getnframes())
    return wav_bytes(pcm, 22050)


# ---------------------------------------------------------------------------------
# the injected parts
# ---------------------------------------------------------------------------------
class HotkeyTaken(Exception):
    """RegisterHotKey failed: another application owns the combo."""


class AudioError(Exception):
    """The microphone could not be opened or read."""


class Unreachable(Exception):
    """The backend could not be reached; nothing was delivered."""


class Rejected(Exception):
    """The backend answered with an error. `detail` is its message."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class HotkeySource(Protocol):
    def register(self, hotkey: Hotkey, events: "queue.Queue[tuple[str, ...]]") -> None: ...
    def is_down(self) -> bool: ...
    def arm_cancel(self, on: bool) -> None: ...
    def close(self) -> None: ...


class AudioSource(Protocol):
    def devices(self) -> list[str]: ...
    def start(self, index: int | None) -> None: ...
    def read(self) -> bytes: ...
    def stop(self) -> bytes: ...


class Backend(Protocol):
    def settings(self) -> dict[str, Any]: ...
    def desk_id(self) -> str: ...
    def upload(self, conversation_id: str, wav: bytes, action: str) -> dict[str, Any]: ...
    def status(self, payload: dict[str, Any]) -> None: ...


class Cues(Protocol):
    def play(self, name: str) -> None: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class SilentCues:
    def play(self, name: str) -> None:
        return None


# ---------------------------------------------------------------------------------
# the backend over HTTP
# ---------------------------------------------------------------------------------
class HttpBackend:
    """The four REST calls the listener makes. Connection failures are `Unreachable`
    (the clip stays spooled); any HTTP answer ≥ 400 is `Rejected`. A read timeout on
    an upload means the backend has the clip and is slow, so it is `Rejected` too:
    retrying it would post the same words twice."""

    def __init__(self, url: str, token: str = "", timeout: float = 300.0):
        import httpx

        self._httpx = httpx
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.url = url.rstrip("/")
        self.http = httpx.Client(base_url=self.url + "/api", headers=headers, timeout=httpx.Timeout(timeout, connect=5.0))

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        httpx = self._httpx
        try:
            response = self.http.request(method, path, **kw)
        except httpx.TimeoutException as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                raise Unreachable(f"cannot reach the backend at {self.url}: {exc}") from exc
            raise Rejected(0, f"the backend did not answer in time ({exc})") from exc
        except httpx.TransportError as exc:
            raise Unreachable(f"cannot reach the backend at {self.url}: {exc}") from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
                detail = str(payload.get("detail")) if isinstance(payload, dict) and "detail" in payload else response.text
            except ValueError:
                detail = response.text
            raise Rejected(response.status_code, detail.strip()[:400])
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def settings(self) -> dict[str, Any]:
        payload = self._request("GET", "/settings")
        return dict((payload or {}).get("values") or {})

    def desk_id(self) -> str:
        rows = self._request("GET", "/conversations", params={"kind": "desk"})
        if isinstance(rows, dict):  # tolerate an envelope
            rows = rows.get("conversations") or rows.get("items") or []
        for row in rows or []:
            if row.get("kind", "desk") == "desk":
                return str(row["id"])
        raise Rejected(404, "the backend has no desk conversation")

    def upload(self, conversation_id: str, wav: bytes, action: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/conversations/{conversation_id}/audio",
            params={"action": action, "source": "hotkey"},
            content=wav, headers={"Content-Type": "audio/wav"},
        ) or {}

    def status(self, payload: dict[str, Any]) -> None:
        self._request("POST", "/voice/status", json=payload, timeout=10.0)

    def close(self) -> None:
        self.http.close()


# ---------------------------------------------------------------------------------
# the spool
# ---------------------------------------------------------------------------------
class Spool:
    """Clips waiting for the backend, one WAV each, named so that sorting is age:
    `<epoch ms>-<seq>-<action>.wav`. Written atomically (tmp + replace)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, wav: bytes, action: str) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._seq += 1
            name = f"{int(time.time() * 1000):013d}-{self._seq:04d}-{action}.wav"
        path = self.folder / name
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(wav)
        os.replace(tmp, path)
        return path

    def pending(self) -> list[Path]:
        try:
            return sorted(p for p in self.folder.iterdir() if p.suffix == ".wav")
        except FileNotFoundError:
            return []

    @staticmethod
    def action_of(path: Path) -> str:
        action = path.stem.rsplit("-", 1)[-1]
        return action if action in ("send", "draft") else "send"


# ---------------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------------
@dataclass
class Config:
    hotkey: str = "ctrl+alt+space"
    mode: str = "hold"
    action: str = "send"
    input_device: str = ""
    max_seconds: int = 120
    min_ms: int = 300
    silence_gate: bool = True
    sounds: bool = True
    enabled: bool = True

    @classmethod
    def from_settings(cls, values: dict[str, Any]) -> "Config":
        d = cls()

        def get(key: str, default: Any) -> Any:
            v = values.get(f"voice.{key}")
            return default if v is None else v

        return cls(
            hotkey=str(get("hotkey", d.hotkey)),
            mode=str(get("mode", d.mode)) if get("mode", d.mode) in ("hold", "toggle") else d.mode,
            action=str(get("action", d.action)) if get("action", d.action) in ("send", "draft") else d.action,
            input_device=str(get("input_device", d.input_device) or ""),
            max_seconds=int(get("max_seconds", d.max_seconds)),
            min_ms=int(get("min_ms", d.min_ms)),
            silence_gate=bool(get("silence_gate", d.silence_gate)),
            sounds=bool(get("sounds", d.sounds)),
            enabled=bool(get("enabled", d.enabled)),
        )


class Listener:
    """The decisions. Drive it with `press()`, `cancel()` and `tick()` (what `run()`
    does from the hotkey queue every `TICK_S`), and `drain()` for the spool (what the
    uploader thread does). Thread-safety: `press`/`cancel`/`tick` run on one thread;
    `drain` on another; shared state is the status, guarded by a lock."""

    def __init__(self, config: Config, *, hotkeys: HotkeySource, audio: AudioSource, backend: Backend,
                 cues: Cues, clock: Clock, spool: Spool, on_submit: Callable[[], None] | None = None):
        self.config = config
        self.hotkeys = hotkeys
        self.audio = audio
        self.backend = backend
        self.cues = cues
        self.clock = clock
        self.spool = spool
        self.on_submit = on_submit or (lambda: None)
        self.hotkey: Hotkey | None = None
        self.devices: list[str] = []
        self.recording = False
        self._started_at = 0.0
        self._pcm: list[bytes] = []
        self._uploading = False
        self._state = "starting"
        self._error: str | None = None
        self._status_lock = threading.Lock()
        self._drain_lock = threading.Lock()
        self.last_contact_ok = False

    # -- status -----------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str | None:
        return self._error

    def payload(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "state": self._state,
            "hotkey": self.hotkey.text if self.hotkey else self.config.hotkey,
            "mode": self.config.mode,
            "error": self._error,
            "devices": list(self.devices),
            "device": self.config.input_device,
            "updated_at": _now_iso(),
        }

    def _derived_state(self) -> str:
        """While running, the state follows the flags; before or after (starting, error,
        unsupported, stopped) it stays what was set."""
        if self._state not in ("idle", "recording", "transcribing"):
            return self._state
        if self.recording:
            return "recording"
        return "transcribing" if self._uploading else "idle"

    def set_status(self, state: str | None = None, error: str | None | bool = False) -> None:
        """Set the state (None = derive it from the flags) and/or the error
        (`error=False` leaves it) and post. A failed post is logged, never raised:
        status is a report, not part of the work."""
        with self._status_lock:
            self._state = state if state is not None else self._derived_state()
            if error is not False:
                self._error = error  # type: ignore[assignment]
            payload = self.payload()
        try:
            self.backend.status(payload)
            self.last_contact_ok = True
        except (Unreachable, Rejected) as exc:
            self.last_contact_ok = False
            log.info("status not delivered: %s", exc)

    def _refresh(self, error: str | None | bool = False) -> None:
        self.set_status(None, error)

    def _cue(self, name: str) -> None:
        if self.config.sounds:
            try:
                self.cues.play(name)
            except Exception as exc:  # a cue never breaks a recording
                log.debug("cue %s failed: %s", name, exc)

    # -- start-up ---------------------------------------------------------------
    def setup(self) -> int | None:
        """Parse the hotkey, list devices, register. Returns an exit code when the
        listener cannot run (after reporting why), else None."""
        try:
            self.hotkey = parse_hotkey(self.config.hotkey)
        except ValueError as exc:
            self.set_status("error", str(exc))
            return EXIT_OK  # a restart cannot fix the string; a settings change restarts us
        try:
            self.devices = list(self.audio.devices())
        except AudioError as exc:
            self.devices = []
            log.info("device list unavailable: %s", exc)
        events: queue.Queue[tuple[str, ...]] = queue.Queue()
        self.events = events
        try:
            self.hotkeys.register(self.hotkey, events)
        except HotkeyTaken:
            self.set_status("error", f"hotkey {self.hotkey.text} is taken by another application")
            return EXIT_HOTKEY_TAKEN
        problems = []
        if not self.devices:
            problems.append("no microphone")
        elif self.config.input_device and self.config.input_device not in self.devices:
            problems.append(f"microphone {self.config.input_device!r} not found; using the system default")
        self.set_status("idle", "; ".join(problems) or None)
        return None

    def _device_index(self) -> int | None:
        name = self.config.input_device
        if name and name in self.devices:
            return self.devices.index(name)
        return None  # the system default (WAVE_MAPPER)

    # -- events -----------------------------------------------------------------
    def press(self) -> None:
        if not self.recording:
            self._begin()
        elif self.config.mode == "toggle":
            self._finish()
        # hold mode: a press while recording cannot happen (MOD_NOREPEAT); ignore it

    def cancel(self) -> None:
        if not self.recording:
            return
        self._end_capture()
        self._pcm = []
        self._cue("cancel")
        self._refresh()

    def tick(self) -> None:
        if not self.recording:
            return
        try:
            chunk = self.audio.read()
        except AudioError as exc:
            self._fail_capture(str(exc))
            return
        if chunk:
            self._pcm.append(chunk)
        if self.clock.monotonic() - self._started_at >= self.config.max_seconds:
            self._finish()
        elif self.config.mode == "hold" and not self.hotkeys.is_down():
            self._finish()

    # -- recording --------------------------------------------------------------
    def _begin(self) -> None:
        try:
            self.audio.start(self._device_index())
        except AudioError as exc:
            self._cue("error")
            self._refresh(f"microphone: {exc}")
            return
        self.recording = True
        self._pcm = []
        self._started_at = self.clock.monotonic()
        self.hotkeys.arm_cancel(True)
        self._cue("start")
        self._refresh()

    def _end_capture(self) -> bytes:
        self.recording = False
        self.hotkeys.arm_cancel(False)
        try:
            return self.audio.stop()
        except AudioError as exc:
            log.info("audio stop failed: %s", exc)
            return b""

    def _fail_capture(self, message: str) -> None:
        self._end_capture()
        self._pcm = []
        self._cue("error")
        self._refresh(f"microphone: {message}")

    def _finish(self) -> None:
        rest = self._end_capture()
        pcm = b"".join(self._pcm) + rest
        self._pcm = []
        ms = len(pcm) // BYTES_PER_MS
        if ms < max(1, self.config.min_ms):
            log.info("clip dropped: %d ms is shorter than %d ms", ms, self.config.min_ms)
            self._cue("cancel")
            self._refresh()
            return
        if self.config.silence_gate and peak_rms(pcm) < SILENCE_RMS:
            log.info("clip dropped: silent (peak RMS %.0f < %d)", peak_rms(pcm), SILENCE_RMS)
            self._cue("cancel")
            self._refresh()
            return
        self._cue("stop")
        self.spool.put(wav_bytes(pcm), self.config.action)  # persist before act
        self._refresh()
        self.on_submit()

    # -- upload -----------------------------------------------------------------
    def drain(self) -> int:
        """Upload spooled clips oldest-first until the spool is empty or the backend is
        unreachable. Returns how many were settled. Any answer settles a clip (the
        backend stored it before answering); only Unreachable keeps it."""
        settled = 0
        with self._drain_lock:
            pending = self.spool.pending()
            if not pending:
                return 0
            self._uploading = True
            self._refresh()
            error: str | None | bool = False
            try:
                try:
                    desk = self.backend.desk_id()
                except Unreachable as exc:
                    self.last_contact_ok = False
                    log.info("upload deferred: %s", exc)
                    return 0
                except Rejected as exc:
                    self.last_contact_ok = True
                    self._cue("error")
                    error = f"cannot find the desk: {exc.detail}"
                    return 0
                for path in pending:
                    try:
                        wav = path.read_bytes()
                    except OSError as exc:
                        log.warning("spooled clip %s unreadable: %s", path.name, exc)
                        _unlink(path)
                        continue
                    try:
                        result = self.backend.upload(desk, wav, Spool.action_of(path))
                    except Unreachable as exc:
                        self.last_contact_ok = False
                        log.info("upload deferred (%d spooled): %s", len(self.spool.pending()), exc)
                        break
                    except Rejected as exc:
                        self.last_contact_ok = True
                        _unlink(path)
                        settled += 1
                        self._cue("error")
                        error = f"voice clip not delivered: {exc.detail}"
                        continue
                    self.last_contact_ok = True
                    _unlink(path)
                    settled += 1
                    log.info("clip delivered%s", " (no speech found)" if (result or {}).get("dropped") else "")
                    if error is False:
                        error = None  # a delivered clip clears the last error
            finally:
                self._uploading = False
                self._refresh(error)
        return settled

    # -- the loop ---------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        """Hotkey events + ticks on this thread; uploads on a second thread woken by
        each new clip, by any successful contact, and every `SPOOL_RETRY_S` while clips
        wait."""
        wake = threading.Event()
        self.on_submit = wake.set
        wake.set()  # anything spooled by an earlier run goes first

        def uploader() -> None:
            while not stop.is_set():
                wake.wait(SPOOL_RETRY_S)
                wake.clear()
                if stop.is_set():
                    return
                self.drain()

        worker = threading.Thread(target=uploader, name="loom-voice-upload", daemon=True)
        worker.start()
        while not stop.is_set():
            try:
                event = self.events.get(timeout=TICK_S)
            except queue.Empty:
                event = None
            if event == ("press",):
                self.press()
            elif event == ("cancel",):
                self.cancel()
            self.tick()
        if self.recording:
            self._end_capture()
        wake.set()
        worker.join(timeout=2)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------------
# entrypoint (`loom listen`)
# ---------------------------------------------------------------------------------
def fetch_settings(backend: Backend, clock: Clock, wait_s: float = SETTINGS_RETRY_S) -> dict[str, Any] | None:
    """`GET /settings`, retried once a second: the supervisor starts the listener while
    the backend is still starting up. None after `wait_s` of silence."""
    deadline = clock.monotonic() + wait_s
    while True:
        try:
            return backend.settings()
        except (Unreachable, Rejected) as exc:
            if clock.monotonic() >= deadline:
                log.error("giving up: %s", exc)
                return None
            clock.sleep(1.0)


def report_once(backend: Backend, state: str, error: str | None, config: Config | None = None) -> None:
    payload = {
        "enabled": config.enabled if config else True, "state": state,
        "hotkey": config.hotkey if config else "", "mode": config.mode if config else "",
        "error": error, "devices": [], "device": config.input_device if config else "",
        "updated_at": _now_iso(),
    }
    try:
        backend.status(payload)
    except (Unreachable, Rejected) as exc:
        log.info("status not delivered: %s", exc)


def main(url: str, token: str = "", home: Path | None = None, *, platform: str | None = None,
         backend: Backend | None = None, clock: Clock | None = None) -> int:
    """`loom listen`. Returns the process exit code (see EXIT_*)."""
    from ..config import app_home

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    backend = backend or HttpBackend(url, token)
    clock = clock or SystemClock()
    platform = platform or sys.platform
    values = fetch_settings(backend, clock)
    if values is None:
        print(f"cannot reach the backend at {url}", file=sys.stderr)
        return EXIT_UNREACHABLE
    config = Config.from_settings(values)
    if platform != "win32":
        report_once(backend, "unsupported", "voice input needs Windows (global hotkey and microphone)", config)
        return EXIT_OK

    from . import win32  # lazy: ctypes.windll exists only on Windows

    mutex = win32.single_instance()
    if mutex is None:
        report_once(backend, "error", "another Loom listener owns the hotkey", config)
        return EXIT_OK
    hotkeys = win32.Hotkeys()
    cues = win32.Cues()
    listener = Listener(
        config, hotkeys=hotkeys, audio=win32.WaveIn(), backend=backend, cues=cues,
        clock=clock, spool=Spool(Path(home or app_home()) / "voice-spool"),
    )
    stop = threading.Event()
    try:
        code = listener.setup()
        if code is not None:
            return code
        listener.run(stop)
        return EXIT_OK
    except KeyboardInterrupt:
        stop.set()
        return EXIT_OK
    finally:
        try:
            hotkeys.close()
            cues.close()
        finally:
            if listener.state not in ("error",):
                listener.set_status("stopped")
            win32.release(mutex)
