"""The voice listener's decisions (03 §5.7), driven through fakes.

Nothing here registers a hotkey, opens a microphone, plays a sound or reaches a real
backend: every part the listener touches is injected. `win32.py` is imported only to
check that it imports and that its pure struct helpers are right."""

from __future__ import annotations

import array
import io
import math
import queue
import struct
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path

import httpx
import pytest

from loom import cli
from loom.voice import listener as L

# ---------------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------------


class FakeHotkeys:
    def __init__(self, taken: bool = False):
        self.taken = taken
        self.registered: L.Hotkey | None = None
        self.events: queue.Queue | None = None
        self.down = True
        self.armed: list[bool] = []

    def register(self, hotkey, events):
        if self.taken:
            raise L.HotkeyTaken(hotkey.text)
        self.registered = hotkey
        self.events = events

    def is_down(self):
        return self.down

    def arm_cancel(self, on):
        self.armed.append(on)

    def close(self):
        pass


class FakeAudio:
    def __init__(self, devices=("Mic A", "Mic B"), fail_start: str | None = None):
        self._devices = list(devices)
        self.fail_start = fail_start
        self.started: list[int | None] = []
        self.chunks: deque[bytes] = deque()
        self.tail = b""
        self.open = False

    def devices(self):
        return list(self._devices)

    def start(self, index):
        if self.fail_start:
            raise L.AudioError(self.fail_start)
        self.started.append(index)
        self.open = True

    def read(self):
        return self.chunks.popleft() if self.chunks else b""

    def stop(self):
        self.open = False
        tail, self.tail = self.tail, b""
        return tail


class FakeBackend:
    def __init__(self, values=None):
        self.values = values if values is not None else {}
        self.reachable = True
        self.reject: tuple[int, str] | None = None
        self.uploads: list[tuple[str, bytes, str]] = []
        self.statuses: list[dict] = []
        self.result = {"audio_id": "a1", "text": "hello", "inbox_id": 1}
        self.settings_calls = 0

    def _gate(self):
        if not self.reachable:
            raise L.Unreachable("connection refused")

    def settings(self):
        self.settings_calls += 1
        self._gate()
        return dict(self.values)

    def desk_id(self):
        self._gate()
        return "DESK"

    def upload(self, conversation_id, wav, action):
        self._gate()
        self.uploads.append((conversation_id, wav, action))
        if self.reject:
            raise L.Rejected(*self.reject)
        return dict(self.result)

    def status(self, payload):
        self._gate()
        self.statuses.append(payload)


class FakeCues:
    def __init__(self):
        self.played: list[str] = []

    def play(self, name):
        self.played.append(name)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def pcm(ms: int, amplitude: int = 3000, freq: float = 220.0) -> bytes:
    n = L.SAMPLE_RATE * ms // 1000
    samples = array.array("h", (int(amplitude * math.sin(2 * math.pi * freq * i / L.SAMPLE_RATE)) for i in range(n)))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def make(tmp_path: Path, **config) -> tuple[L.Listener, FakeHotkeys, FakeAudio, FakeBackend, FakeCues, FakeClock]:
    hot, audio, backend, cues, clock = FakeHotkeys(), FakeAudio(), FakeBackend(), FakeCues(), FakeClock()
    listener = L.Listener(L.Config(**config), hotkeys=hot, audio=audio, backend=backend, cues=cues,
                          clock=clock, spool=L.Spool(tmp_path / "voice-spool"))
    return listener, hot, audio, backend, cues, clock


def ready(tmp_path: Path, **config):
    parts = make(tmp_path, **config)
    assert parts[0].setup() is None
    return parts


# ---------------------------------------------------------------------------------
# hotkey strings
# ---------------------------------------------------------------------------------
def test_the_default_hotkey_parses_to_register_hotkey_arguments():
    hk = L.parse_hotkey("ctrl+alt+space")
    assert hk.text == "ctrl+alt+space"
    assert hk.vk == 0x20
    assert hk.mod_flags == L.MOD_CONTROL | L.MOD_ALT | L.MOD_NOREPEAT


@pytest.mark.parametrize("text,canonical,vk", [
    ("Alt + Ctrl + Space", "ctrl+alt+space", 0x20),
    ("control+shift+a", "ctrl+shift+a", 0x41),
    ("win+9", "win+9", 0x39),
    ("ctrl+f13", "ctrl+f13", 0x7C),
    ("f9", "f9", 0x78),
    ("pause", "pause", 0x13),
    ("ctrl+numpad0", "ctrl+numpad0", 0x60),
    ("shift+pgdn", "shift+pagedown", 0x22),
    ("super+return", "win+enter", 0x0D),
])
def test_hotkey_strings_are_canonicalised(text, canonical, vk):
    hk = L.parse_hotkey(text)
    assert (hk.text, hk.vk) == (canonical, vk)


@pytest.mark.parametrize("text,needle", [
    ("", "no hotkey"),
    ("ctrl+", "empty part"),
    ("ctrl+alt", "no key"),
    ("ctrl+a+b", "more than one key"),
    ("ctrl+ctrl+a", "twice"),
    ("ctrl+esc", "Esc"),
    ("a", "needs a modifier"),
    ("space", "needs a modifier"),
    ("ctrl+bogus", "unknown key"),
    ("ctrl+f25", "unknown key"),
])
def test_invalid_hotkey_strings_say_why(text, needle):
    with pytest.raises(ValueError) as err:
        L.parse_hotkey(text)
    assert needle in str(err.value)


# ---------------------------------------------------------------------------------
# audio helpers
# ---------------------------------------------------------------------------------
def test_wav_bytes_is_pcm16_mono_16k():
    data = L.wav_bytes(pcm(100))
    assert data[:4] == b"RIFF" and data[8:16] == b"WAVEfmt "
    fmt, channels, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", data[20:36])
    assert (fmt, channels, rate, byte_rate, align, bits) == (1, 1, 16000, 32000, 2, 16)
    assert len(data) == 44 + 3200
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnframes() == 1600


def test_peak_rms_separates_speech_from_room_noise():
    assert L.peak_rms(b"") == 0
    assert L.peak_rms(pcm(200, amplitude=0)) == 0
    assert L.peak_rms(pcm(200, amplitude=60)) < L.SILENCE_RMS
    loud = L.peak_rms(pcm(200, amplitude=3000))
    assert abs(loud - 3000 / math.sqrt(2)) < 50
    # One loud frame in a quiet clip is enough: the gate looks for any speech at all.
    assert L.peak_rms(pcm(500, amplitude=40) + pcm(60, amplitude=3000)) > L.SILENCE_RMS


@pytest.mark.parametrize("name", sorted(L.CUE_NOTES))
def test_cues_are_short_valid_wavs(name):
    with wave.open(io.BytesIO(L.cue_wav(name))) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert 0 < w.getnframes() / w.getframerate() < 0.5


# ---------------------------------------------------------------------------------
# start-up
# ---------------------------------------------------------------------------------
def test_an_invalid_hotkey_reports_and_exits_without_registering(tmp_path):
    listener, hot, _, backend, _, _ = make(tmp_path, hotkey="ctrl+alt")
    assert listener.setup() == L.EXIT_OK
    assert hot.registered is None
    assert backend.statuses[-1]["state"] == "error"
    assert "no key" in backend.statuses[-1]["error"]


def test_a_taken_hotkey_reports_the_combo_and_exits_for_a_retry(tmp_path):
    listener, hot, _, backend, _, _ = make(tmp_path, hotkey="alt+ctrl+space")
    hot.taken = True
    assert listener.setup() == L.EXIT_HOTKEY_TAKEN
    assert backend.statuses[-1]["state"] == "error"
    assert backend.statuses[-1]["error"] == "hotkey ctrl+alt+space is taken by another application"


def test_setup_registers_and_reports_idle_with_devices(tmp_path):
    listener, hot, _, backend, _, _ = ready(tmp_path, mode="toggle", input_device="Mic B")
    assert hot.registered is not None and hot.registered.text == "ctrl+alt+space"
    status = backend.statuses[-1]
    assert status["state"] == "idle" and status["error"] is None
    assert status["devices"] == ["Mic A", "Mic B"] and status["device"] == "Mic B"
    assert status["mode"] == "toggle" and status["hotkey"] == "ctrl+alt+space"
    assert set(status) == {"enabled", "state", "hotkey", "mode", "error", "devices", "device", "updated_at"}


def test_no_microphone_and_a_missing_device_are_reported_not_fatal(tmp_path):
    listener, _, audio, backend, _, _ = make(tmp_path)
    audio._devices = []
    assert listener.setup() is None
    assert backend.statuses[-1]["state"] == "idle" and backend.statuses[-1]["error"] == "no microphone"

    listener, _, _, backend, _, _ = make(tmp_path, input_device="USB Mic")
    assert listener.setup() is None
    assert "'USB Mic' not found" in backend.statuses[-1]["error"]


# ---------------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------------
def test_hold_records_while_the_key_is_down_and_spools_on_release(tmp_path):
    listener, hot, audio, backend, cues, _ = ready(tmp_path)
    listener.press()
    assert listener.recording and audio.started == [None]  # default device
    assert cues.played == ["start"] and hot.armed == [True]
    assert backend.statuses[-1]["state"] == "recording"
    audio.chunks.extend([pcm(200), pcm(200)])
    listener.tick()
    listener.tick()
    assert listener.recording  # still held
    hot.down = False
    audio.tail = pcm(100)
    listener.tick()
    assert not listener.recording
    assert cues.played == ["start", "stop"] and hot.armed == [True, False]
    [clip] = listener.spool.pending()
    assert L.Spool.action_of(clip) == "send"
    with wave.open(io.BytesIO(clip.read_bytes())) as w:
        assert w.getnframes() == 16 * 500
    assert backend.statuses[-1]["state"] == "idle"


def test_toggle_starts_and_stops_on_presses_and_ignores_the_key_state(tmp_path):
    listener, hot, audio, _, _, _ = ready(tmp_path, mode="toggle", action="draft")
    listener.press()
    hot.down = False
    audio.chunks.append(pcm(400))
    listener.tick()
    assert listener.recording
    listener.press()
    assert not listener.recording
    [clip] = listener.spool.pending()
    assert L.Spool.action_of(clip) == "draft"


def test_a_second_press_in_hold_mode_is_ignored(tmp_path):
    listener, _, _, _, _, _ = ready(tmp_path)
    listener.press()
    listener.press()
    assert listener.recording


def test_esc_cancels_and_nothing_is_sent(tmp_path):
    listener, hot, audio, _, cues, _ = ready(tmp_path)
    listener.press()
    audio.chunks.append(pcm(800))
    listener.tick()
    listener.cancel()
    assert not listener.recording and not audio.open
    assert cues.played == ["start", "cancel"] and hot.armed == [True, False]
    assert listener.spool.pending() == []
    listener.cancel()  # Esc while idle: nothing
    assert cues.played == ["start", "cancel"]


def test_the_max_seconds_cap_stops_and_sends(tmp_path):
    listener, _, audio, _, cues, clock = ready(tmp_path, max_seconds=5)
    listener.press()
    audio.chunks.append(pcm(400))
    clock.t += 4.9
    listener.tick()
    assert listener.recording
    clock.t += 0.2
    listener.tick()
    assert not listener.recording and cues.played[-1] == "stop"
    assert len(listener.spool.pending()) == 1


def test_a_clip_shorter_than_min_ms_is_dropped(tmp_path):
    listener, hot, audio, _, cues, _ = ready(tmp_path, min_ms=300)
    listener.press()
    audio.tail = pcm(250)
    hot.down = False
    listener.tick()
    assert cues.played == ["start", "cancel"] and listener.spool.pending() == []


def test_an_empty_clip_is_dropped_even_with_min_ms_zero(tmp_path):
    listener, hot, _, _, cues, _ = ready(tmp_path, min_ms=0)
    listener.press()
    hot.down = False
    listener.tick()
    assert cues.played[-1] == "cancel" and listener.spool.pending() == []


def test_the_silence_gate_drops_a_quiet_clip_and_can_be_turned_off(tmp_path):
    listener, hot, audio, _, cues, _ = ready(tmp_path)
    listener.press()
    audio.tail = pcm(1000, amplitude=50)
    hot.down = False
    listener.tick()
    assert cues.played[-1] == "cancel" and listener.spool.pending() == []

    listener, hot, audio, _, cues, _ = ready(tmp_path / "b", silence_gate=False)
    listener.press()
    audio.tail = pcm(1000, amplitude=50)
    hot.down = False
    listener.tick()
    assert cues.played[-1] == "stop" and len(listener.spool.pending()) == 1


def test_the_chosen_device_is_opened_by_index(tmp_path):
    listener, _, audio, _, _, _ = ready(tmp_path, input_device="Mic B")
    listener.press()
    assert audio.started == [1]


def test_a_microphone_that_will_not_open_is_an_error_cue_and_status(tmp_path):
    listener, hot, audio, backend, cues, _ = ready(tmp_path)
    audio.fail_start = "no microphone"
    listener.press()
    assert not listener.recording and hot.armed == []
    assert cues.played == ["error"]
    assert backend.statuses[-1]["state"] == "idle"
    assert backend.statuses[-1]["error"] == "microphone: no microphone"


def test_a_read_failure_mid_clip_ends_the_recording(tmp_path):
    listener, _, audio, backend, cues, _ = ready(tmp_path)
    listener.press()

    def boom():
        raise L.AudioError("device unplugged")

    audio.read = boom  # type: ignore[method-assign]
    listener.tick()
    assert not listener.recording and cues.played[-1] == "error"
    assert "device unplugged" in backend.statuses[-1]["error"]
    assert listener.spool.pending() == []


def test_sounds_off_means_no_cues(tmp_path):
    listener, hot, audio, _, cues, _ = ready(tmp_path, sounds=False)
    listener.press()
    audio.tail = pcm(500)
    hot.down = False
    listener.tick()
    assert cues.played == [] and len(listener.spool.pending()) == 1


def test_a_finished_clip_wakes_the_uploader(tmp_path):
    listener, hot, audio, _, _, _ = ready(tmp_path)
    woken = []
    listener.on_submit = lambda: woken.append(True)
    listener.press()
    audio.tail = pcm(500)
    hot.down = False
    listener.tick()
    assert woken == [True]


# ---------------------------------------------------------------------------------
# upload and spool
# ---------------------------------------------------------------------------------
def _record(listener, hot, audio, ms=500):
    hot.down = True
    listener.press()
    audio.tail = pcm(ms)
    hot.down = False
    listener.tick()


def test_drain_uploads_to_the_desk_and_clears_the_spool(tmp_path):
    listener, hot, audio, backend, _, _ = ready(tmp_path, action="draft")
    _record(listener, hot, audio)
    assert listener.drain() == 1
    [(conv, wav, action)] = backend.uploads
    assert conv == "DESK" and action == "draft" and wav[:4] == b"RIFF"
    assert listener.spool.pending() == []
    states = [s["state"] for s in backend.statuses]
    assert "transcribing" in states and states[-1] == "idle"


def test_an_unreachable_backend_keeps_clips_and_drains_oldest_first_later(tmp_path):
    listener, hot, audio, backend, _, _ = ready(tmp_path)
    backend.reachable = False
    _record(listener, hot, audio, ms=400)
    _record(listener, hot, audio, ms=600)
    assert listener.drain() == 0
    assert len(listener.spool.pending()) == 2 and backend.uploads == []
    backend.reachable = True
    assert listener.drain() == 2
    lengths = [len(wav) for _, wav, _ in backend.uploads]
    assert lengths == [44 + 400 * 32, 44 + 600 * 32]
    assert listener.spool.pending() == []


def test_the_spool_survives_a_restart(tmp_path):
    listener, hot, audio, backend, _, _ = ready(tmp_path)
    backend.reachable = False
    _record(listener, hot, audio)
    second, _, _, backend2, _, _ = ready(tmp_path)  # same spool folder, new process
    assert second.drain() == 1 and len(backend2.uploads) == 1


def test_a_rejected_clip_is_settled_with_an_error_cue_and_status(tmp_path):
    listener, hot, audio, backend, cues, _ = ready(tmp_path)
    backend.reject = (502, "voice message could not be transcribed: timeout")
    _record(listener, hot, audio)
    assert listener.drain() == 1
    assert listener.spool.pending() == []  # the backend stored it; Retry lives there
    assert cues.played[-1] == "error"
    assert backend.statuses[-1]["error"] == "voice clip not delivered: voice message could not be transcribed: timeout"
    backend.reject = None
    _record(listener, hot, audio)
    listener.drain()
    assert backend.statuses[-1]["error"] is None  # a delivered clip clears it


def test_a_dropped_transcript_is_not_an_error(tmp_path):
    listener, hot, audio, backend, cues, _ = ready(tmp_path)
    backend.result = {"audio_id": "a1", "text": "", "dropped": True}
    _record(listener, hot, audio)
    listener.drain()
    assert "error" not in cues.played and backend.statuses[-1]["error"] is None


def test_status_failures_never_break_recording(tmp_path):
    listener, hot, audio, backend, _, _ = ready(tmp_path)
    backend.reachable = False
    _record(listener, hot, audio)
    assert len(listener.spool.pending()) == 1 and not listener.last_contact_ok


# ---------------------------------------------------------------------------------
# settings and entrypoint
# ---------------------------------------------------------------------------------
def test_config_reads_the_voice_settings():
    c = L.Config.from_settings({
        "voice.enabled": True, "voice.hotkey": "ctrl+shift+v", "voice.mode": "toggle",
        "voice.action": "draft", "voice.input_device": "Mic B", "voice.max_seconds": 30,
        "voice.min_ms": 0, "voice.silence_gate": False, "voice.sounds": False, "general.port": 8500,
    })
    assert c == L.Config(hotkey="ctrl+shift+v", mode="toggle", action="draft", input_device="Mic B",
                         max_seconds=30, min_ms=0, silence_gate=False, sounds=False, enabled=True)
    assert L.Config.from_settings({}) == L.Config()


def test_fetch_settings_waits_for_a_starting_backend():
    backend, clock = FakeBackend({"voice.enabled": True}), FakeClock()
    calls = {"n": 0}
    real = backend.settings

    def flaky():
        calls["n"] += 1
        if calls["n"] < 4:
            raise L.Unreachable("not yet")
        return real()

    backend.settings = flaky  # type: ignore[method-assign]
    assert L.fetch_settings(backend, clock) == {"voice.enabled": True}
    assert calls["n"] == 4


def test_main_gives_up_on_a_backend_that_never_answers(capsys):
    backend, clock = FakeBackend(), FakeClock()
    backend.reachable = False
    assert L.main("http://127.0.0.1:9", backend=backend, clock=clock, platform="win32") == L.EXIT_UNREACHABLE
    assert "cannot reach the backend" in capsys.readouterr().err
    assert backend.settings_calls > 30


def test_main_reports_unsupported_off_windows_and_exits_zero(tmp_path):
    backend = FakeBackend({"voice.enabled": True, "voice.hotkey": "ctrl+alt+space"})
    assert L.main("http://x", home=tmp_path, backend=backend, clock=FakeClock(), platform="linux") == 0
    [status] = backend.statuses
    assert status["state"] == "unsupported" and "Windows" in status["error"]


def test_run_drains_an_earlier_spool_and_handles_queued_presses(tmp_path):
    listener, hot, audio, backend, cues, _ = ready(tmp_path, mode="toggle")
    listener.spool.put(L.wav_bytes(pcm(500)), "send")  # left over from an earlier run
    stop = threading.Event()
    worker = threading.Thread(target=listener.run, args=(stop,), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not backend.uploads and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(backend.uploads) == 1
        hot.events.put(("press",))
        while not listener.recording and time.monotonic() < deadline:
            time.sleep(0.01)
        audio.tail = pcm(500)
        hot.events.put(("press",))
        while len(backend.uploads) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(backend.uploads) == 2
    finally:
        stop.set()
        worker.join(timeout=5)
    assert not worker.is_alive()


# ---------------------------------------------------------------------------------
# the HTTP backend (MockTransport; no network)
# ---------------------------------------------------------------------------------
def _http(handler) -> L.HttpBackend:
    backend = L.HttpBackend("http://loom.test", token="tok")
    backend.http.close()
    backend.http = httpx.Client(base_url="http://loom.test/api", transport=httpx.MockTransport(handler),
                                headers={"Authorization": "Bearer tok"})
    return backend


def test_http_backend_speaks_the_contract():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/settings":
            return httpx.Response(200, json={"values": {"voice.mode": "hold"}, "schema": [], "groups": {}})
        if path == "/api/conversations":
            return httpx.Response(200, json=[{"id": "D1", "kind": "desk"}])
        if path == "/api/conversations/D1/audio":
            return httpx.Response(202, json={"audio_id": "a", "text": "hi", "inbox_id": 3})
        if path == "/api/voice/status":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    backend = _http(handler)
    assert backend.settings() == {"voice.mode": "hold"}
    assert backend.desk_id() == "D1"
    assert backend.upload("D1", b"RIFF....", "draft")["text"] == "hi"
    backend.status({"state": "idle"})
    upload = seen[2]
    assert dict(upload.url.params) == {"action": "draft", "source": "hotkey"}
    assert upload.headers["content-type"] == "audio/wav" and upload.content == b"RIFF...."
    assert upload.headers["authorization"] == "Bearer tok"
    assert dict(seen[1].url.params) == {"kind": "desk"}
    assert seen[3].method == "POST"


def test_http_backend_failure_classes():
    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(L.Unreachable):
        _http(refused).upload("D1", b"x", "send")

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(L.Rejected) as err:  # the backend has the clip: never resend it
        _http(slow).upload("D1", b"x", "send")
    assert "did not answer in time" in err.value.detail

    def failed(request):
        return httpx.Response(502, json={"detail": "could not be transcribed", "audio_id": "a"})

    with pytest.raises(L.Rejected) as err:
        _http(failed).upload("D1", b"x", "send")
    assert (err.value.status, err.value.detail) == (502, "could not be transcribed")


# ---------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------
def test_listen_parses_and_keeps_the_global_url_and_token(monkeypatch, tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(["--url", "http://global:1", "--token", "g", "listen"])
    assert args.func is cli.cmd_listen and (args.url, args.token) == ("http://global:1", "g")
    args = parser.parse_args(["listen", "--url", "http://local:2", "--token", "l"])
    assert (args.url, args.token) == ("http://local:2", "l")

    got = {}
    monkeypatch.setattr(L, "main", lambda url, token, home: got.update(url=url, token=token, home=home) or 0)
    monkeypatch.setenv("LOOM_TOKEN", "from-env")
    assert cli.main(["--home", str(tmp_path), "listen", "--url", "http://h:3/"]) == 0
    assert got == {"url": "http://h:3", "token": "from-env", "home": tmp_path}


# ---------------------------------------------------------------------------------
# win32.py: imports, and its pure struct helpers (no Win32 call is made)
# ---------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="the ctypes layer is Windows-only")
def test_win32_imports_and_its_structs_have_the_sdk_layout():
    import ctypes

    from loom.voice import win32

    fmt = win32.pcm_format()
    assert (fmt.wFormatTag, fmt.nChannels, fmt.nSamplesPerSec, fmt.nAvgBytesPerSec, fmt.nBlockAlign, fmt.wBitsPerSample) == (1, 1, 16000, 32000, 2, 16)
    assert ctypes.sizeof(win32.WAVEFORMATEX) == 18
    assert ctypes.sizeof(win32.WAVEINCAPSW) == 80
    assert ctypes.sizeof(win32.WAVEHDR) == (48 if ctypes.sizeof(ctypes.c_void_p) == 8 else 32)
    assert win32.MUTEX_NAME == "Local\\loom-voice-listener"
    assert win32._user32 is None  # importing loaded no Win32 library
