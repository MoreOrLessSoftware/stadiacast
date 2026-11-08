"""A virtual Xbox Series X|S controller per controller, through the xgpad driver (Windows).

``--gamepad xgpad`` drives the xgpad UMDF2 HID minidriver from the Sunshine fork
(``drivers/xgpad``), in place of ViGEmBus. Each pad is a software device (``SwDeviceCreate``)
that the driver presents as an Xbox Series controller over Bluetooth LE (``045E:0B13``), with the
inbox ``xinputhid`` filter on top, so XInput, Windows.Gaming.Input and GameInput all see a real
Series pad, Share button included. The receiver and the driver exchange HID reports through a
named shared memory section and two events per pad; the contract is ``drivers/xgpad/xgpad_shared.h``
(``XGPAD_SHARED_VERSION`` 3), mirrored here.

The report conversions at the top are pure Python so the tests check the mapping on every
platform; :class:`XgpadPad` needs Windows, the driver package and an elevated receiver (creating
a software device and ``Global\\`` objects both need administrator rights).
"""

from __future__ import annotations

import ctypes
import struct
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stadiacast.errors import SetupError

# ---- the shared contract (drivers/xgpad/xgpad_shared.h) ----------------------------------
SHARED_MAGIC = 0x44415047  # 'GPAD'
SHARED_VERSION = 3
INPUT_RING_SIZE = 16
STATUS_DRIVER_STARTED = 0x1

SECTION_NAME_FMT = "Global\\SunshineXgpad_{}"
INPUT_EVENT_NAME_FMT = "Global\\SunshineXgpad_{}_in"
OUTPUT_EVENT_NAME_FMT = "Global\\SunshineXgpad_{}_out"
TOKEN_MAX = 40  # characters, terminator included
TOKEN_PROPKEY = (uuid.UUID("6c1f8d2e-3b7a-4e59-9a41-58d2c7e0b913"), 2)

SWD_ENUMERATOR = "{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&02045e_PID&0b13_REV&0509"
HARDWARE_ID = "SunshineXgpad\\{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&02045e_PID&0b13"
#: Our own instance names, so a Sunshine running next to us (``Pad0``..``Pad15``) never collides.
#: The driver reads the digits as its fallback pad index, which the token makes unused.
INSTANCE_FMT = "Stadiacast{}"
MAX_PADS = 16
OBJECT_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;LS)"

# Input report 0x01 payload: lx ly rx ry lt rt (u16), hat (u8), buttons (u16), share (u8).
INPUT_REPORT = struct.Struct("<6HBHB")
# Output report 0x03 payload: enable, LT, RT, left, right (0..100), duration, start delay, loops.
OUTPUT_REPORT = struct.Struct("<8B")

BTN_A, BTN_B, BTN_X, BTN_Y = 0x0001, 0x0002, 0x0008, 0x0010
BTN_LB, BTN_RB = 0x0040, 0x0080
BTN_VIEW, BTN_MENU, BTN_GUIDE = 0x0400, 0x0800, 0x1000
BTN_LS, BTN_RS = 0x2000, 0x4000

MOTOR_RIGHT, MOTOR_LEFT = 0x01, 0x02

#: XInput ``wButtons`` bit -> the Series pad's HID button bit. The D-pad (bits 0..3) is the hat.
XINPUT_TO_XGPAD: dict[int, int] = {
    4: BTN_MENU,  # START: Stadia's "≡"
    5: BTN_VIEW,  # BACK: Stadia's "⋯"
    6: BTN_LS,
    7: BTN_RS,
    8: BTN_LB,
    9: BTN_RB,
    10: BTN_GUIDE,  # the Stadia button
    12: BTN_A,
    13: BTN_B,
    14: BTN_X,
    15: BTN_Y,
}


def hat_from_buttons(w: int) -> int:
    """XInput D-pad bits -> the HID hat: 0 released, 1 = N clockwise to 8 = NW.

    Opposing directions resolve to up and left, as Sunshine does.
    """
    up = bool(w & 0x1)
    down = not up and bool(w & 0x2)
    left = bool(w & 0x4)
    right = not left and bool(w & 0x8)
    if up:
        return 2 if right else 8 if left else 1
    if down:
        return 4 if right else 6 if left else 5
    if right:
        return 3
    if left:
        return 7
    return 0


def axis_x(value: int) -> int:
    """Signed X (positive = right) -> the unsigned HID axis, 32768 at rest."""
    return max(0, min(65535, value + 32768))


def axis_y(value: int) -> int:
    """Signed Y (positive = up) -> the HID axis, increasing downwards; 0 maps to 32767 so XInput reads 0."""
    return max(0, min(65535, 32767 - value))


def trigger_10bit(value: int) -> int:
    """XInput's 0..255 trigger -> the Series pad's 0..1023."""
    return (max(0, min(255, value)) * 1023 + 127) // 255


def input_report(fields: dict[str, int]) -> bytes:
    """The 16-byte input report for one set of pad fields (XInput's, plus ``share``)."""
    w = fields["wButtons"]
    buttons = 0
    for bit, mask in XINPUT_TO_XGPAD.items():
        if w & (1 << bit):
            buttons |= mask
    return INPUT_REPORT.pack(
        axis_x(fields["sThumbLX"]),
        axis_y(fields["sThumbLY"]),
        axis_x(fields["sThumbRX"]),
        axis_y(fields["sThumbRY"]),
        trigger_10bit(fields["bLeftTrigger"]),
        trigger_10bit(fields["bRightTrigger"]),
        hat_from_buttons(w),
        buttons,
        1 if fields.get("share") else 0,
    )


NEUTRAL_REPORT = input_report(
    {"wButtons": 0, "bLeftTrigger": 0, "bRightTrigger": 0, "sThumbLX": 0, "sThumbLY": 0, "sThumbRX": 0, "sThumbRY": 0}
)


def motor_level(percent: int) -> int:
    """A 0..100 HID motor magnitude -> the controller's 0..65535."""
    return min(100, percent) * 65535 // 100


def apply_output_report(report: bytes, previous: tuple[int, int]) -> tuple[tuple[int, int], float]:
    """One output report -> the new (left, right) grip motor levels and how long they last.

    A motor whose enable bit is clear keeps its previous level. The duration is in seconds, 0 for
    "until changed" (``0xFF``, what XInput and GameInput send, or 0). The impulse triggers are
    dropped: the Stadia controller has no trigger motors.
    """
    enable, _lt, _rt, left, right, duration, delay, loops = OUTPUT_REPORT.unpack(report)
    new_left = motor_level(left) if enable & MOTOR_LEFT else previous[0]
    new_right = motor_level(right) if enable & MOTOR_RIGHT else previous[1]
    seconds = 0.0 if duration in (0, 0xFF) else (delay + duration) * (loops + 1) * 0.01
    return (new_left, new_right), seconds


def driver_installed() -> bool:
    """Whether an INF matching the xgpad hardware ID sits in the driver store (``%WINDIR%\\INF\\oem*.inf``)."""
    import os

    inf_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "INF"
    needle = b"sunshinexgpad\\"
    for inf in inf_dir.glob("oem*.inf"):
        try:
            data = inf.read_bytes()
        except OSError:
            continue
        text = data.decode("utf-16", "ignore") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("latin-1")
        if needle.decode() in text.lower():
            return True
    return False


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined,unused-ignore]
    except Exception:
        return False


def check_available() -> None:
    """A :class:`SetupError` saying what is missing for ``--gamepad xgpad``, or nothing."""
    if sys.platform != "win32":
        raise SetupError("--gamepad xgpad is Windows only: it drives the xgpad virtual Xbox Series controller driver")
    if not driver_installed():
        raise SetupError(
            "--gamepad xgpad needs the xgpad driver installed: the Sunshine installer's optional "
            "'Xbox Series Controller Driver' component, or drivers/xgpad/install-test.ps1 for a test build"
        )
    if not is_elevated():
        raise SetupError("--gamepad xgpad needs an elevated receiver: creating a virtual controller takes admin rights")


# ---- Windows plumbing (ctypes) ----------------------------------------------------------
class _InputReport(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("data", ctypes.c_ubyte * INPUT_REPORT.size)]


class _OutputReport(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("data", ctypes.c_ubyte * OUTPUT_REPORT.size)]


class Shared(ctypes.Structure):
    """``XGPAD_SHARED``: natural alignment (it sits outside the header's ``pack(1)`` block)."""

    _fields_ = [
        ("magic", ctypes.c_uint32),
        ("version", ctypes.c_uint32),
        ("inputCount", ctypes.c_long),
        ("inputRing", _InputReport * INPUT_RING_SIZE),
        ("inputQpc", ctypes.c_int64),
        ("outputSeq", ctypes.c_long),
        ("output", _OutputReport),
        ("outputQpc", ctypes.c_int64),
        ("driverStatus", ctypes.c_long),
        ("deliveredSeq", ctypes.c_long),
        ("deliveredQpc", ctypes.c_int64),
        ("readsCompleted", ctypes.c_long),
        ("outputsReceived", ctypes.c_long),
        ("workerWakeups", ctypes.c_long),
        ("readRequests", ctypes.c_long),
        ("inputOverruns", ctypes.c_long),
    ]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, u: uuid.UUID) -> _Guid:
        f = u.fields
        tail = u.bytes[8:]
        return cls(f[0], f[1], f[2], (ctypes.c_ubyte * 8)(*tail))


class _DevPropKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", ctypes.c_ulong)]


class _DevPropCompKey(ctypes.Structure):
    _fields_ = [("Key", _DevPropKey), ("Store", ctypes.c_int), ("LocaleName", ctypes.c_wchar_p)]


class _DevProperty(ctypes.Structure):
    _fields_ = [
        ("CompKey", _DevPropCompKey),
        ("Type", ctypes.c_ulong),
        ("BufferSize", ctypes.c_ulong),
        ("Buffer", ctypes.c_void_p),
    ]


class _SwDeviceCreateInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("pszInstanceId", ctypes.c_wchar_p),
        ("pszzHardwareIds", ctypes.c_void_p),
        ("pszzCompatibleIds", ctypes.c_void_p),
        ("pContainerId", ctypes.c_void_p),
        ("CapabilityFlags", ctypes.c_ulong),
        ("pszDeviceDescription", ctypes.c_wchar_p),
        ("pszDeviceLocation", ctypes.c_wchar_p),
        ("pSecurityDescriptor", ctypes.c_void_p),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_ulong),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


SW_CAPS = 0x1 | 0x2 | 0x8  # Removable | SilentInstall | DriverRequired
DEVPROP_TYPE_STRING = 0x12
WAIT_OBJECT_0, WAIT_TIMEOUT, INFINITE = 0, 0x102, 0xFFFFFFFF
ERROR_ALREADY_EXISTS_HR = -2147024713  # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS) = 0x800700B7
FILE_MAP_ALL_ACCESS, PAGE_READWRITE = 0xF001F, 0x04
#: PnP creation (normally well under a second) plus the driver picking up its shared objects
#: (it polls for them every 250 ms).
ATTACH_TIMEOUT = 13.0
CR_SUCCESS = 0
DN_STARTED = 0x8


class _Api:
    """The Win32 functions this module calls, with handle-sized argument and return types."""

    def __init__(self) -> None:
        wd: Any = ctypes.WinDLL  # type: ignore[attr-defined,unused-ignore]
        k32 = wd("kernel32", use_last_error=True)
        adv = wd("advapi32", use_last_error=True)
        cfg = wd("cfgmgr32", use_last_error=True)
        h, p, b, d = ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong

        def fn(lib: Any, name: str, res: Any, *args: Any) -> Any:
            f = getattr(lib, name)
            f.restype, f.argtypes = res, list(args)
            return f

        self.CreateFileMappingW = fn(k32, "CreateFileMappingW", h, h, p, d, d, d, ctypes.c_wchar_p)
        self.MapViewOfFile = fn(k32, "MapViewOfFile", p, h, d, d, d, ctypes.c_size_t)
        self.UnmapViewOfFile = fn(k32, "UnmapViewOfFile", b, p)
        self.CreateEventW = fn(k32, "CreateEventW", h, p, b, b, ctypes.c_wchar_p)
        self.SetEvent = fn(k32, "SetEvent", b, h)
        self.CloseHandle = fn(k32, "CloseHandle", b, h)
        self.WaitForMultipleObjects = fn(k32, "WaitForMultipleObjects", d, d, ctypes.POINTER(h), b, d)
        self.QueryPerformanceCounter = fn(k32, "QueryPerformanceCounter", b, ctypes.POINTER(ctypes.c_int64))
        self.LocalFree = fn(k32, "LocalFree", p, p)
        self.ConvertSddl = fn(
            adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", b, ctypes.c_wchar_p, d, ctypes.POINTER(p), p
        )
        self.SwDeviceCreate = fn(
            cfg,
            "SwDeviceCreate",
            ctypes.c_long,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.POINTER(_SwDeviceCreateInfo),
            d,
            ctypes.POINTER(_DevProperty),
            p,  # the creation callback: see callback_stub
            p,
            ctypes.POINTER(h),
        )
        self.SwDeviceClose = fn(cfg, "SwDeviceClose", None, h)
        self.CM_Locate_DevNodeW = fn(cfg, "CM_Locate_DevNodeW", d, ctypes.POINTER(d), ctypes.c_wchar_p, d)
        self.CM_Get_DevNode_Status = fn(cfg, "CM_Get_DevNode_Status", d, ctypes.POINTER(d), ctypes.POINTER(d), d, d)
        # SwDeviceCreate pins the module that holds its callback and fails with ERROR_MOD_NOT_FOUND
        # (0x8007007E) when there is none, which is the case for every ctypes callback (libffi
        # trampolines live in plain allocated memory). So it gets a do-nothing function that lives in
        # kernel32 instead (GetTickCount takes no arguments, so the four it receives are ignored), and
        # readiness is read from the driver's own status flag in the shared section.
        self.callback_stub = ctypes.cast(k32.GetTickCount, ctypes.c_void_p).value


_api: _Api | None = None
_slots_lock = threading.Lock()
_slots_in_use: set[int] = set()


def _get_api() -> _Api:
    global _api
    if _api is None:
        _api = _Api()
    return _api


def _winerror(what: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{what} failed (Windows error {code})")


class XgpadPad:
    """One virtual Xbox Series controller. Create it off the event loop: PnP takes a moment."""

    def __init__(self, label: str) -> None:
        from stadiacast.virtual_gamepad import occupied_xinput_slots

        self.label = label
        self.slot: int | None = None
        self._api = _get_api()
        self._handles: list[int] = []
        self._view: int | None = None
        self._device: int | None = None
        self._thread: threading.Thread | None = None
        self._callback: Callable[[int, int], None] | None = None
        self._rumble = (0, 0)
        self._index = self._take_index()
        self.token = f"{self._index}_{uuid.uuid4().hex[:16]}"
        try:
            self._create_objects()
            before = occupied_xinput_slots()
            self._create_device()
            self._wait_for_driver()
            self._stop = self._event(None)
            self._thread = threading.Thread(target=self._feedback_loop, name=f"xgpad-{label}", daemon=True)
            self._thread.start()
            self.reset()
            for _ in range(30):  # the XInput player the pad landed on: whatever appeared since `before`
                new = occupied_xinput_slots() - before
                if new:
                    self.slot = min(new)
                    break
                time.sleep(0.1)
        except BaseException:
            self.close()
            raise

    # ---- creation ---------------------------------------------------------------------
    @staticmethod
    def _take_index() -> int:
        with _slots_lock:
            for i in range(MAX_PADS):
                if i not in _slots_in_use:
                    _slots_in_use.add(i)
                    return i
        raise SetupError(f"--gamepad xgpad supports at most {MAX_PADS} pads")

    def _event(self, name: str | None, attrs: Any = None) -> int:
        h = self._api.CreateEventW(attrs, False, False, name)
        if not h:
            raise _winerror(f"CreateEventW({name})")
        self._handles.append(h)
        return int(h)

    def _create_objects(self) -> None:
        api = self._api
        sd = ctypes.c_void_p()
        if not api.ConvertSddl(OBJECT_SDDL, 1, ctypes.byref(sd), None):
            raise _winerror("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            sa = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), sd, 0)
            size = ctypes.sizeof(Shared)
            section = api.CreateFileMappingW(
                ctypes.c_void_p(-1), ctypes.byref(sa), PAGE_READWRITE, 0, size, SECTION_NAME_FMT.format(self.token)
            )
            if not section:
                raise _winerror("CreateFileMappingW")
            self._handles.append(section)
            self._input_event = self._event(INPUT_EVENT_NAME_FMT.format(self.token), ctypes.byref(sa))
            self._output_event = self._event(OUTPUT_EVENT_NAME_FMT.format(self.token), ctypes.byref(sa))
        finally:
            api.LocalFree(sd)
        view = api.MapViewOfFile(section, FILE_MAP_ALL_ACCESS, 0, 0, size)
        if not view:
            raise _winerror("MapViewOfFile")
        self._view = view
        self.shared = Shared.from_address(view)
        ctypes.memset(view, 0, size)
        self.shared.magic = SHARED_MAGIC
        self.shared.version = SHARED_VERSION  # the driver starts from a neutral state on its own

    def _create_device(self) -> None:
        api = self._api
        hardware_ids = ctypes.create_unicode_buffer(HARDWARE_ID + "\0")  # a multi-string: double terminator
        compatible_ids = ctypes.create_unicode_buffer("\0")
        info = _SwDeviceCreateInfo()
        info.cbSize = ctypes.sizeof(info)
        info.pszInstanceId = INSTANCE_FMT.format(self._index)
        info.pszzHardwareIds = ctypes.cast(hardware_ids, ctypes.c_void_p)
        info.pszzCompatibleIds = ctypes.cast(compatible_ids, ctypes.c_void_p)
        info.CapabilityFlags = SW_CAPS
        info.pszDeviceDescription = "Stadiacast Virtual Xbox Series Controller"

        token = ctypes.create_unicode_buffer(self.token)
        guid, pid = TOKEN_PROPKEY
        prop = _DevProperty()
        prop.CompKey.Key = _DevPropKey(_Guid.from_uuid(guid), pid)
        prop.CompKey.Store = 0  # DEVPROP_STORE_SYSTEM
        prop.Type = DEVPROP_TYPE_STRING
        prop.BufferSize = (len(self.token) + 1) * ctypes.sizeof(ctypes.c_wchar)
        prop.Buffer = ctypes.cast(token, ctypes.c_void_p)

        result = ERROR_ALREADY_EXISTS_HR
        for _ in range(20):  # a just-removed device with this instance ID may still be registered
            device = ctypes.c_void_p()
            result = api.SwDeviceCreate(
                SWD_ENUMERATOR,
                "HTREE\\ROOT\\0",
                ctypes.byref(info),
                1,
                ctypes.byref(prop),
                api.callback_stub,
                None,
                ctypes.byref(device),
            )
            if result >= 0:
                self._device = device.value
                break
            if result != ERROR_ALREADY_EXISTS_HR:
                break
            time.sleep(0.1)
        if result < 0:
            raise SetupError(
                f"Windows could not create the virtual Xbox Series controller (SwDeviceCreate failed with HRESULT"
                f" 0x{result & 0xFFFFFFFF:08X}); is the xgpad driver installed and the receiver elevated?"
            )
        self.instance_id = f"SWD\\{SWD_ENUMERATOR}\\{info.pszInstanceId}"

    def _device_status(self) -> str:
        """What PnP says about the device: missing, started, or its problem code."""
        api = self._api
        devinst, status, problem = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
        if api.CM_Locate_DevNodeW(ctypes.byref(devinst), self.instance_id, 0) != CR_SUCCESS:
            return "Windows never created the device"
        if api.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), devinst, 0) != CR_SUCCESS:
            return "Windows created the device but cannot report its state"
        if problem.value:
            return f"the device has problem code {problem.value} in Device Manager (driver did not load)"
        if status.value & DN_STARTED:
            return "the device started"
        return "the device exists but has not started"

    def _wait_for_driver(self) -> None:
        deadline = time.monotonic() + ATTACH_TIMEOUT
        while not self.shared.driverStatus & STATUS_DRIVER_STARTED:
            if time.monotonic() >= deadline:
                state = self._device_status()
                hint = (
                    "the installed driver probably does not speak shared memory"
                    f" v{SHARED_VERSION}; reinstall it from a matching Sunshine build"
                    if state == "the device started"
                    else "check the device in Device Manager"
                )
                raise SetupError(
                    f"the xgpad driver did not attach to the pad within {ATTACH_TIMEOUT:g} s ({state}): {hint}"
                )
            time.sleep(0.05)

    # ---- input ------------------------------------------------------------------------
    def _write(self, report: bytes) -> None:
        shared = self.shared
        count = shared.inputCount + 1  # single writer: this pad's caller
        ctypes.memmove(ctypes.addressof(shared.inputRing[(count - 1) % INPUT_RING_SIZE]), report, len(report))
        qpc = ctypes.c_int64()
        self._api.QueryPerformanceCounter(ctypes.byref(qpc))
        shared.inputQpc = qpc.value
        # Publish after the report: x64 keeps stores in order, so the driver never reads a half-written slot.
        shared.inputCount = count
        self._api.SetEvent(self._input_event)

    def apply(self, fields: dict[str, int]) -> None:
        self._write(input_report(fields))

    def reset(self) -> None:
        self._write(NEUTRAL_REPORT)

    # ---- rumble -----------------------------------------------------------------------
    def on_rumble(self, callback: Callable[[int, int], None]) -> None:
        self._callback = callback

    def _read_output(self) -> bytes:
        shared = self.shared
        while True:
            seq = shared.outputSeq
            data = bytes(shared.output.data)
            if not seq & 1 and seq == shared.outputSeq:
                return data

    def _emit(self, levels: tuple[int, int]) -> None:
        self._rumble = levels
        if self._callback is not None:
            self._callback(*levels)

    def _feedback_loop(self) -> None:
        handles = (ctypes.c_void_p * 2)(self._output_event, self._stop)
        stop_at: float | None = None  # when a finite-duration effect ends
        while True:
            timeout = INFINITE if stop_at is None else max(0, int((stop_at - time.monotonic()) * 1000))
            wait = self._api.WaitForMultipleObjects(2, handles, False, timeout)
            if wait == WAIT_OBJECT_0:
                levels, seconds = apply_output_report(self._read_output(), self._rumble)
                stop_at = time.monotonic() + seconds if seconds else None
                if levels != self._rumble:
                    self._emit(levels)
            elif wait == WAIT_TIMEOUT:
                stop_at = None
                if self._rumble != (0, 0):
                    self._emit((0, 0))
            else:  # the stop event, or a failed wait
                return

    # ---- teardown ---------------------------------------------------------------------
    def _close_device(self) -> None:
        if self._device:
            self._api.SwDeviceClose(self._device)
            self._device = None

    def close(self) -> None:
        if self._thread is not None:
            self._api.SetEvent(self._stop)
            self._thread.join(2)
            self._thread = None
        self._close_device()  # the device goes away with its handle
        if self._view:
            self._api.UnmapViewOfFile(self._view)
            self._view = None
        for h in self._handles:
            self._api.CloseHandle(h)
        self._handles.clear()
        with _slots_lock:
            _slots_in_use.discard(self._index)


__all__ = [
    "XINPUT_TO_XGPAD",
    "Shared",
    "XgpadPad",
    "apply_output_report",
    "check_available",
    "driver_installed",
    "hat_from_buttons",
    "input_report",
    "motor_level",
]
