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
    """``XGPAD_SHARED`` is naturally aligned, with the packed 16- and 8-byte reports inside it."""
    assert xgpad.INPUT_REPORT.size == 16 and xgpad.OUTPUT_REPORT.size == 8
    offsets = {name: getattr(xgpad.Shared, name).offset for name, _ in xgpad.Shared._fields_}
    assert offsets == {
        "magic": 0,
        "version": 4,
        "inputCount": 8,
        "inputRing": 12,
        "inputQpc": 272,
        "outputSeq": 280,
        "output": 284,
        "outputQpc": 296,
        "driverStatus": 304,
        "deliveredSeq": 308,
        "deliveredQpc": 312,
        "readsCompleted": 320,
        "outputsReceived": 324,
        "workerWakeups": 328,
        "readRequests": 332,
        "inputOverruns": 336,
    }
    assert ctypes.sizeof(xgpad.Shared) == 344


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
    pad._rumble, pad._index, pad._thread = (0, 0), 15, None
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
        last = bytes(pad.shared.inputRing[(pad.shared.inputCount - 1) % xgpad.INPUT_RING_SIZE].data)
        assert _unpack(last)["buttons"] == xgpad.BTN_A and _unpack(last)["lt"] == xgpad.trigger_10bit(17)

        def drive(report: bytes) -> None:  # what the driver does: seqlock write, then the output event
            rumbled.clear()
            pad.shared.outputSeq += 1
            ctypes.memmove(ctypes.addressof(pad.shared.output), report, len(report))
            pad.shared.outputSeq += 1
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
