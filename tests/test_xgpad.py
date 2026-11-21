"""The xgpad backend's side of the driver contract (drivers/xgpad/xgpad_shared.h), on every platform."""

from __future__ import annotations

import ctypes
import sys

import pytest

from stadiacast import xgpad
from stadiacast.gamepad import Button, GamepadState


def _fields(**kw: int) -> dict[str, int]:
    base = {"wButtons": 0, "bLeftTrigger": 0, "bRightTrigger": 0, "sThumbLX": 0, "sThumbLY": 0, "sThumbRX": 0}
    return base | {"sThumbRY": 0} | kw


def _unpack(report: bytes) -> dict[str, int]:
    names = ("lx", "ly", "rx", "ry", "lt", "rt", "hat", "buttons", "share")
    return dict(zip(names, xgpad.INPUT_REPORT.unpack(report), strict=True))


def test_the_shared_section_matches_the_c_layout() -> None:
    """``XGPAD_SHARED`` v4 is naturally aligned around packed 66-byte ring entries (offsets from the C compiler)."""
    assert xgpad.SHARED_VERSION == 4
    assert xgpad.INPUT_REPORT.size == 16 and xgpad.OUTPUT_REPORT.size == 8
    assert ctypes.sizeof(xgpad.Report) == 2 + xgpad.MAX_REPORT
    offsets = {name: getattr(xgpad.Shared, name).offset for name, _ in xgpad.Shared._fields_}
    assert offsets == {
        "magic": 0,
        "version": 4,
        "profile": 8,
        "inputCount": 12,
        "inputRing": 16,
        "inputQpc": 1072,
        "outputCount": 1080,
        "outputRing": 1084,
        "outputQpc": 2144,
        "driverStatus": 2152,
        "deliveredSeq": 2156,
        "deliveredQpc": 2160,
        "readsCompleted": 2168,
        "outputsReceived": 2172,
        "workerWakeups": 2176,
        "readRequests": 2180,
        "inputOverruns": 2184,
        "inputRejected": 2188,
        "featureRequests": 2192,
    }
    assert ctypes.sizeof(xgpad.Shared) == 2200


def _publish_output(shared: xgpad.Shared, report_id: int, payload: bytes) -> None:
    """What the driver does with an output report: fill the next ring slot, then publish it."""
    n = shared.outputCount + 1
    slot = shared.outputRing[(n - 1) % xgpad.OUTPUT_RING_SIZE]
    slot.id, slot.length = report_id, len(payload)
    ctypes.memmove(ctypes.addressof(slot.data), payload, len(payload))
    shared.outputCount = n


def _rumble(left: int, right: int) -> bytes:
    return xgpad.OUTPUT_REPORT.pack(xgpad.MOTOR_LEFT | xgpad.MOTOR_RIGHT, 0, 0, left, right, 0xFF, 0, 0)


def test_output_reports_are_read_from_the_ring_in_order() -> None:
    shared = xgpad.Shared()
    assert xgpad.read_output_reports(shared, 0) == ([], 0, 0)
    _publish_output(shared, xgpad.OUTPUT_REPORT_ID, _rumble(10, 20))
    _publish_output(shared, 0x02, b"\x01" * 47)  # a DualSense report: read, then ignored by the caller
    reports, consumed, lost = xgpad.read_output_reports(shared, 0)
    assert reports == [(xgpad.OUTPUT_REPORT_ID, _rumble(10, 20)), (0x02, b"\x01" * 47)]
    assert (consumed, lost) == (2, 0)
    assert xgpad.read_output_reports(shared, consumed) == ([], 2, 0)


def test_output_reports_the_ring_lapped_are_counted_lost() -> None:
    shared = xgpad.Shared()
    for i in range(xgpad.OUTPUT_RING_SIZE + 3):
        _publish_output(shared, xgpad.OUTPUT_REPORT_ID, _rumble(i, i))
    reports, consumed, lost = xgpad.read_output_reports(shared, 0)
    assert lost == 3 and consumed == xgpad.OUTPUT_RING_SIZE + 3
    assert [r[1] for r in reports] == [_rumble(i, i) for i in range(3, xgpad.OUTPUT_RING_SIZE + 3)]
    # a reset section (count below what was consumed) starts over from its current count
    assert xgpad.read_output_reports(xgpad.Shared(), 5) == ([], 0, 0)


def test_a_slot_overwritten_while_copied_is_counted_lost() -> None:
    class Racing:
        """The driver publishes a full ring's worth more reports right after the count is read."""

        def __init__(self) -> None:
            self.shared = xgpad.Shared()
            _publish_output(self.shared, xgpad.OUTPUT_REPORT_ID, _rumble(1, 1))
            self.reads = 0

        def __getattr__(self, name: str) -> object:
            if name == "outputCount":
                self.reads += 1
                return self.shared.outputCount + (xgpad.OUTPUT_RING_SIZE if self.reads > 1 else 0)
            return getattr(self.shared, name)

    assert xgpad.read_output_reports(Racing(), 0) == ([], 1, 1)  # type: ignore[arg-type]


def test_only_xbox_output_reports_carry_rumble() -> None:
    assert xgpad.xbox_output_payload(xgpad.OUTPUT_REPORT_ID, _rumble(5, 6) + b"\0") == _rumble(5, 6)
    assert xgpad.xbox_output_payload(0x02, _rumble(5, 6)) is None
    assert xgpad.xbox_output_payload(xgpad.OUTPUT_REPORT_ID, b"\x03" * 4) is None


def test_a_neutral_pad_reads_zero_in_xinput() -> None:
    r = _unpack(xgpad.NEUTRAL_REPORT)
    assert r == {
        "lx": 32768,
        "ly": 32767,
        "rx": 32768,
        "ry": 32767,
        "lt": 0,
        "rt": 0,
        "hat": 0,
        "buttons": 0,
        "share": 0,
    }


def test_every_stadia_button_lands_on_its_series_button() -> None:
    expected = {
        Button.A: xgpad.BTN_A,
        Button.B: xgpad.BTN_B,
        Button.X: xgpad.BTN_X,
        Button.Y: xgpad.BTN_Y,
        Button.L1: xgpad.BTN_LB,
        Button.R1: xgpad.BTN_RB,
        Button.L3: xgpad.BTN_LS,
        Button.R3: xgpad.BTN_RS,
        Button.MENU: xgpad.BTN_MENU,
        Button.OPTIONS: xgpad.BTN_VIEW,
        Button.STADIA: xgpad.BTN_GUIDE,
    }
    for stadia, series in expected.items():
        r = _unpack(xgpad.input_report(GamepadState(buttons=int(stadia)).to_xinput()))
        assert (r["buttons"], r["hat"], r["share"]) == (series, 0, 0), stadia
    r = _unpack(xgpad.input_report(GamepadState(buttons=int(Button.ASSISTANT)).to_xinput()))
    assert r["buttons"] == 0  # the Series pad has nothing for Assistant
    assert _unpack(xgpad.input_report(_fields(share=1)))["share"] == 1


@pytest.mark.parametrize(
    ("dpad", "hat"),
    [
        (0, 0),
        (Button.DPAD_UP, 1),
        (Button.DPAD_UP | Button.DPAD_RIGHT, 2),
        (Button.DPAD_RIGHT, 3),
        (Button.DPAD_DOWN | Button.DPAD_RIGHT, 4),
        (Button.DPAD_DOWN, 5),
        (Button.DPAD_DOWN | Button.DPAD_LEFT, 6),
        (Button.DPAD_LEFT, 7),
        (Button.DPAD_UP | Button.DPAD_LEFT, 8),
        (Button.DPAD_UP | Button.DPAD_DOWN, 1),  # opposites resolve to up / left
        (Button.DPAD_LEFT | Button.DPAD_RIGHT, 7),
    ],
)
def test_the_dpad_is_the_hat(dpad: int, hat: int) -> None:
    r = _unpack(xgpad.input_report(_fields(wButtons=int(dpad))))
    assert (r["hat"], r["buttons"]) == (hat, 0)


def test_sticks_and_triggers_scale_to_the_hid_ranges() -> None:
    r = _unpack(
        xgpad.input_report(
            _fields(
                sThumbLX=32767, sThumbLY=32767, sThumbRX=-32768, sThumbRY=-32768, bLeftTrigger=255, bRightTrigger=128
            )
        )
    )
    assert (r["lx"], r["ly"]) == (65535, 0)  # right, up (HID Y grows downwards)
    assert (r["rx"], r["ry"]) == (0, 65535)  # left, down
    assert (r["lt"], r["rt"]) == (1023, 514)


def test_output_reports_become_controller_rumble() -> None:
    def out(enable: int, left: int, right: int, duration: int = 0xFF, delay: int = 0, loops: int = 0) -> bytes:
        return xgpad.OUTPUT_REPORT.pack(enable, 50, 50, left, right, duration, delay, loops)

    both = xgpad.MOTOR_LEFT | xgpad.MOTOR_RIGHT
    assert xgpad.apply_output_report(out(both, 100, 50), (0, 0)) == ((65535, 32767), 0.0)
    assert xgpad.apply_output_report(out(xgpad.MOTOR_RIGHT, 100, 0), (1234, 999)) == ((1234, 0), 0.0)  # left kept
    assert xgpad.apply_output_report(out(0x0C, 100, 100), (7, 8)) == ((7, 8), 0.0)  # impulse triggers only: dropped
    assert xgpad.apply_output_report(out(both, 200, 0), (0, 0))[0] == (65535, 0)  # clamped
    (_, seconds) = xgpad.apply_output_report(out(both, 10, 10, duration=20, delay=5, loops=1), (0, 0))
    assert seconds == pytest.approx(0.5)  # (5 + 20) * 10 ms, twice
    assert xgpad.apply_output_report(out(both, 10, 10, duration=0), (0, 0))[1] == 0.0


def test_the_driver_check_reads_the_driver_store(tmp_path, monkeypatch) -> None:
    inf = tmp_path / "INF"
    inf.mkdir()
    monkeypatch.setenv("WINDIR", str(tmp_path))
    (inf / "oem3.inf").write_text("[Standard.NTamd64]\n%D% = X, USB\\VID_1234\n")
    assert not xgpad.driver_installed()
    body = "%DeviceDesc% = Xgpad, SunshineXgpad\\{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&02045e_PID&0b13\n"
    (inf / "oem19.inf").write_bytes(b"\xff\xfe" + body.encode("utf-16-le"))
    assert xgpad.driver_installed()


@pytest.mark.skipif(sys.platform != "win32", reason="the pad's Win32 side")
def test_the_pad_writes_the_ring_and_forwards_rumble_on_windows() -> None:
    """The input ring and the rumble thread against real Win32 events (no device: that needs elevation)."""
    import threading

    api = xgpad._get_api()
    pad = xgpad.XgpadPad.__new__(xgpad.XgpadPad)
    pad._api, pad._handles, pad._view, pad._device, pad._callback = api, [], None, None, None
    pad._rumble, pad._index, pad._thread, pad._output_consumed = (0, 0), 15, None, 0
    buffer = ctypes.create_string_buffer(ctypes.sizeof(xgpad.Shared))
    pad.shared = xgpad.Shared.from_buffer(buffer)
    pad._input_event, pad._output_event, pad._stop = (pad._event(None) for _ in range(3))
    got: list[tuple[int, int]] = []
    rumbled = threading.Event()
    pad.on_rumble(lambda left, right: (got.append((left, right)), rumbled.set()))
    pad._thread = threading.Thread(target=pad._feedback_loop, daemon=True)
    pad._thread.start()
    try:
        for i in range(xgpad.INPUT_RING_SIZE + 2):  # wraps around the ring
            pad.apply(_fields(wButtons=int(Button.A), bLeftTrigger=i))
        assert pad.shared.inputCount == xgpad.INPUT_RING_SIZE + 2
        slot = pad.shared.inputRing[(pad.shared.inputCount - 1) % xgpad.INPUT_RING_SIZE]
        assert (slot.id, slot.length) == (xgpad.INPUT_REPORT_ID, xgpad.INPUT_REPORT.size)
        last = bytes(slot.data[: slot.length])
        assert _unpack(last)["buttons"] == xgpad.BTN_A and _unpack(last)["lt"] == xgpad.trigger_10bit(17)

        def drive(report: bytes) -> None:  # what the driver does: append to the output ring, then the event
            rumbled.clear()
            _publish_output(pad.shared, xgpad.OUTPUT_REPORT_ID, report)
            api.SetEvent(pad._output_event)

        _publish_output(pad.shared, 0x02, b"\0" * 47)  # nothing for the grips: no callback
        api.SetEvent(pad._output_event)

        drive(xgpad.OUTPUT_REPORT.pack(xgpad.MOTOR_LEFT | xgpad.MOTOR_RIGHT, 0, 0, 100, 50, 0xFF, 0, 0))
        assert rumbled.wait(2) and got == [(65535, 32767)]
        drive(xgpad.OUTPUT_REPORT.pack(xgpad.MOTOR_LEFT | xgpad.MOTOR_RIGHT, 0, 0, 20, 20, 5, 0, 0))  # 50 ms
        assert rumbled.wait(2) and got[-1] == (13107, 13107)
        rumbled.clear()
        assert rumbled.wait(2) and got[-1] == (0, 0)  # a finite effect stops on its own
    finally:
        pad.close()
    assert pad._thread is None and pad._handles == []


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 module lookup")
def test_the_creation_callback_lives_in_a_module() -> None:
    """SwDeviceCreate pins the module holding its callback: a ctypes callback has none, which is
    the ERROR_MOD_NOT_FOUND (0x8007007E) seen live, so the backend passes a kernel32 function."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    lookup = kernel32.GetModuleHandleExW
    lookup.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    module = ctypes.c_void_p()
    from_address = 0x4 | 0x2  # GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | _UNCHANGED_REFCOUNT
    assert lookup(from_address, xgpad._get_api().callback_stub, ctypes.byref(module))
    trampoline = ctypes.WINFUNCTYPE(None)(lambda: None)  # type: ignore[attr-defined,unused-ignore]
    assert not lookup(from_address, ctypes.cast(trampoline, ctypes.c_void_p), ctypes.byref(module))
    assert ctypes.get_last_error() == 126
