"""A virtual gamepad per controller, for the games on the receiver's PC (Windows).

``--gamepad vigem`` creates one Xbox 360 pad per controller through ``vgamepad`` (the ``gamepad``
extra, over the ViGEmBus driver); ``--gamepad xgpad`` creates one Xbox Series X|S pad through the
xgpad driver instead (:mod:`stadiacast.xgpad`, no ViGEmBus, Capture becomes Share). Either way
every decoded input report reaches the pad as XInput fields, and the receiver forwards the
game's rumble back to the controller through the game session, which the flag
switches on once the link is READY (rumble has no other path). The reports come from the
display link and, once a game runs, from the game session; both read into the same
:class:`~stadiacast.gamepad.GamepadState`. Two controllers are XInput players 1 and 2, each fed
only by its own reports. A pad that cannot be created shows its error in ``/_status`` and is
tried again on the controller's next report after it reboots.

:class:`PadBackend` is the small surface the receiver drives, so the tests run the same code
against a fake pad on every platform.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from stadiacast.errors import SetupError
from stadiacast.gamepad import XINPUT_LEFT_DEADZONE, XINPUT_RIGHT_DEADZONE, Button, GamepadState

if TYPE_CHECKING:
    from stadiacast.config import Config
    from stadiacast.receiver import Receiver

#: XInput ``wButtons`` bit -> ``vgamepad.XUSB_BUTTON`` name, 1:1 with the mask. Bits 12..15 carry
#: A/B/X/Y; bit 11 is unused by XInput (Stadia's Capture has no equivalent), bit 10 is the guide.
BUTTON_BITS: dict[int, str] = {
    0: "XUSB_GAMEPAD_DPAD_UP",
    1: "XUSB_GAMEPAD_DPAD_DOWN",
    2: "XUSB_GAMEPAD_DPAD_LEFT",
    3: "XUSB_GAMEPAD_DPAD_RIGHT",
    4: "XUSB_GAMEPAD_START",
    5: "XUSB_GAMEPAD_BACK",
    6: "XUSB_GAMEPAD_LEFT_THUMB",
    7: "XUSB_GAMEPAD_RIGHT_THUMB",
    8: "XUSB_GAMEPAD_LEFT_SHOULDER",
    9: "XUSB_GAMEPAD_RIGHT_SHOULDER",
    10: "XUSB_GAMEPAD_GUIDE",
    12: "XUSB_GAMEPAD_A",
    13: "XUSB_GAMEPAD_B",
    14: "XUSB_GAMEPAD_X",
    15: "XUSB_GAMEPAD_Y",
}

#: XInput motor levels are 0..255; the controller's take 0..65535.
MOTOR_SCALE = 257

BACKENDS = ("vigem", "xgpad")


class PadBackend(Protocol):
    """What the receiver drives: one virtual pad.

    ``apply`` gets XInput's ``XINPUT_GAMEPAD`` fields plus ``share`` (Stadia's Capture, which only
    a pad with a Share button uses). ``on_rumble`` registers a callback the backend calls, from any
    thread, with the game's motor levels already in the controller's 0..65535 range.
    """

    slot: int | None

    def apply(self, xinput: dict[str, int]) -> None: ...

    def reset(self) -> None: ...

    def on_rumble(self, callback: Callable[[int, int], None]) -> None: ...

    def close(self) -> None: ...


def occupied_xinput_slots() -> set[int]:
    """Which of XInput's players 0..3 have a pad right now (Windows; empty elsewhere)."""
    if sys.platform != "win32":
        return set()
    import ctypes

    class _Gamepad(ctypes.Structure):
        _fields_ = [
            ("wButtons", ctypes.c_ushort),
            ("bLeftTrigger", ctypes.c_ubyte),
            ("bRightTrigger", ctypes.c_ubyte),
            ("sThumbLX", ctypes.c_short),
            ("sThumbLY", ctypes.c_short),
            ("sThumbRX", ctypes.c_short),
            ("sThumbRY", ctypes.c_short),
        ]

    class _State(ctypes.Structure):
        _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _Gamepad)]

    for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            lib = ctypes.WinDLL(name)
        except OSError:
            continue
        lib.XInputGetState.argtypes = [ctypes.c_ulong, ctypes.POINTER(_State)]
        lib.XInputGetState.restype = ctypes.c_ulong
        state = _State()
        return {i for i in range(4) if lib.XInputGetState(i, ctypes.byref(state)) == 0}
    return set()


def wait_until_plugged_in(
    update: Callable[[], None], timeout: float = 10.0, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Send the first report once Windows has finished plugging the new pad in.

    ViGEm answers ``VIGEM_ERROR_TARGET_NOT_PLUGGED_IN`` to a report sent before the plug-in
    completes, which takes a moment after the target is added (seconds on a slow machine). A pad
    still unplugged at ``timeout`` means a Windows without the Xbox 360 controller driver the pad
    binds to (Windows Server): a ``SetupError`` that says so.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            update()
            return
        except RuntimeError as e:
            if "NOT_PLUGGED_IN" not in str(e):
                raise
            if time.monotonic() >= deadline:
                raise SetupError(
                    f"Windows did not plug the pad in within {timeout:g} s ({e}): a ViGEm pad binds to"
                    " Windows' own Xbox 360 controller driver, which desktop Windows 10 and 11 have"
                    " and Windows Server lacks"
                ) from e
            sleep(0.1)


class ViGEmPad:
    """One ViGEm Xbox 360 pad. Create it off the event loop: the wake-up dance sleeps."""

    def __init__(self, label: str, *, settle: bool = True) -> None:
        import vgamepad as vg  # Windows only; the import itself fails elsewhere

        self.label = label
        self._buttons = {bit: getattr(vg.XUSB_BUTTON, name) for bit, name in BUTTON_BITS.items()}
        before = occupied_xinput_slots()
        self.pad: Any = vg.VX360Gamepad()
        self.mask = 0
        self.slot: int | None = None
        wait_until_plugged_in(self.pad.update)
        if settle:
            self._settle(before)

    def _settle(self, before: set[int]) -> None:
        # XInput enumerates a fresh ViGEm target only after a short delay, and until a report
        # changes something the pad exposes uninitialised stick values (a constant phantom drift):
        # alternate a tiny deflection with a neutral report, then leave it neutral.
        time.sleep(1.0)
        for i in range(6):
            if i % 2:
                self.pad.left_joystick(200, 0)
            else:
                self.pad.reset()
            self.pad.update()
            time.sleep(0.25)
        self.pad.reset()
        self.pad.update()
        time.sleep(0.2)
        for _ in range(20):  # the XInput player this pad landed on: whatever appeared since `before`
            new = occupied_xinput_slots() - before
            if new:
                self.slot = min(new)
                return
            time.sleep(0.1)

    def apply(self, xinput: dict[str, int]) -> None:
        w = xinput["wButtons"]
        changed = w ^ self.mask
        if changed:
            for bit, button in self._buttons.items():
                if changed & (1 << bit):
                    if w & (1 << bit):
                        self.pad.press_button(button)
                    else:
                        self.pad.release_button(button)
            self.mask = w
        self.pad.left_joystick(xinput["sThumbLX"], xinput["sThumbLY"])
        self.pad.right_joystick(xinput["sThumbRX"], xinput["sThumbRY"])
        self.pad.left_trigger(xinput["bLeftTrigger"])
        self.pad.right_trigger(xinput["bRightTrigger"])
        self.pad.update()

    def reset(self) -> None:
        self.pad.reset()
        self.pad.update()
        self.mask = 0

    def on_rumble(self, callback: Callable[[int, int], None]) -> None:
        # ViGEm calls back on its own thread with (client, target, large_motor, small_motor, led, user),
        # the motors in XInput's 0..255.
        self.pad.register_notification(
            callback_function=lambda _c, _t, large, small, _led, _user: callback(
                min(65535, int(large) * MOTOR_SCALE), min(65535, int(small) * MOTOR_SCALE)
            )
        )

    def close(self) -> None:
        try:
            self.pad.unregister_notification()
        except Exception:  # not registered, or ViGEm already gone
            pass
        self.reset()
        self.pad = None  # vgamepad removes the target when the object goes away


class VirtualGamepads:
    """The receiver's gamepad output: one pad per controller label, created on the first report."""

    def __init__(
        self,
        cfg: Config,
        log: Callable[..., Any],
        rx: Receiver,
        backend_factory: Callable[[str], PadBackend],
        *,
        kind: str = "vigem",
    ) -> None:
        self.cfg = cfg
        self.log = log
        self.rx = rx
        self.kind = kind
        self._factory = backend_factory
        self._pads: dict[str, PadBackend] = {}
        self._creating: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, GamepadState] = {}
        self._frames: dict[str, int] = {}
        self._rumbles: dict[str, int] = {}
        self._failed: dict[str, str] = {}  # label -> why its pad could not be created, until release()
        self._dz = (XINPUT_LEFT_DEADZONE, XINPUT_RIGHT_DEADZONE) if cfg.gamepad_deadzone else (0, 0)

    @classmethod
    def create(cls, cfg: Config, log: Callable[..., Any], rx: Receiver) -> VirtualGamepads:
        """The backend ``--gamepad`` names, or a :class:`SetupError` saying what to install."""
        if cfg.gamepad not in BACKENDS:
            raise ValueError(f"unknown --gamepad {cfg.gamepad!r}; use off, {' or '.join(BACKENDS)}")
        if cfg.gamepad == "xgpad":
            from stadiacast.xgpad import XgpadPad, check_available

            check_available()
            return cls(cfg, log, rx, XgpadPad, kind="xgpad")
        if sys.platform != "win32":
            raise SetupError("--gamepad vigem is Windows only: it drives the ViGEmBus driver through vgamepad")
        try:
            import vgamepad  # noqa: F401
        except ImportError as e:
            raise SetupError("--gamepad vigem needs the gamepad extra: pip install 'stadiacast[gamepad]'") from e
        except RuntimeError as e:  # vgamepad connects to the bus on import; no driver, no import
            raise SetupError(
                f"--gamepad vigem needs the ViGEmBus driver installed (vgamepad said: {e}); "
                "get it from https://github.com/nefarius/ViGEmBus/releases"
            ) from e
        return cls(cfg, log, rx, ViGEmPad)

    def _fields(self, state: GamepadState) -> dict[str, int]:
        fields = state.to_xinput(*self._dz)
        fields["share"] = 1 if state.buttons & Button.CAPTURE else 0
        return fields

    # ---- input -----------------------------------------------------------------------
    def on_event(self, ev: dict[str, Any]) -> None:
        """One decoded input event from the receiver: apply it to that controller's pad.

        Both channels feed it: the display link, and the game session once a game runs (which
        ``--gamepad`` starts, so in a game this is where the input comes from). Only gamepad
        reports carry ``axes``; the game session's keyboard, mouse and feedback events do not.
        """
        label = str(ev.get("controller") or "")
        if not label or label in self._failed or "axes" not in ev:
            return
        state = GamepadState.from_event(ev)
        pad = self._pads.get(label)
        if pad is None:
            self._pending[label] = state  # applied as soon as the pad exists
            if label not in self._creating:
                self._creating[label] = asyncio.get_running_loop().create_task(self._create(label))
            return
        pad.apply(self._fields(state))
        self._frames[label] = self._frames.get(label, 0) + 1

    async def _create(self, label: str) -> None:
        try:
            pad = await asyncio.to_thread(self._factory, label)
        except Exception as e:  # the driver refused, or Windows never plugged the pad in: no pad until release()
            error = str(e) if isinstance(e, SetupError) else repr(e)  # our own wording stays plain text
            self._failed[label] = error
            self.log("gamepad_failed", controller=label, error=error)
            return
        finally:
            self._creating.pop(label, None)
        self._pads[label] = pad
        self._frames.setdefault(label, 0)
        self._rumbles.setdefault(label, 0)
        loop = asyncio.get_running_loop()

        def forward(large: int, small: int) -> None:  # from the driver's thread onto the loop
            loop.call_soon_threadsafe(self._rumble, label, large, small)

        pad.on_rumble(forward)
        self.log("gamepad_created", controller=label, kind=self.kind, slot=pad.slot, deadzone=self.cfg.gamepad_deadzone)
        state = self._pending.pop(label, None)
        if state is not None:
            pad.apply(self._fields(state))
            self._frames[label] += 1

    # ---- rumble ----------------------------------------------------------------------
    def _rumble(self, label: str, left: int, right: int) -> None:
        """The game's motor levels (0..65535), from the driver's thread via the loop, to the game session."""
        ctl = self.rx.controllers.get(label)
        if ctl is None:
            return
        self._rumbles[label] = self._rumbles.get(label, 0) + 1
        try:
            seq = ctl.game.set_rumble(left, right)
        except (RuntimeError, ValueError) as e:
            self.log("gamepad_rumble_failed", _quiet=True, controller=label, error=str(e))
            return
        self.log("gamepad_rumble", _quiet=True, controller=label, left=left, right=right, sent=seq is not None)

    # ---- lifecycle -------------------------------------------------------------------
    def release(self, label: str) -> None:
        """Let go of every button and stick: the controller rebooted or went away.

        A pad that could not be created is forgotten too, so the controller's next report tries
        again: rebooting the controller is how a user retries a pad ViGEm refused.
        """
        pad = self._pads.get(label)
        if pad is not None:
            pad.reset()
        self._pending.pop(label, None)
        self._failed.pop(label, None)

    def close(self) -> None:
        for task in self._creating.values():
            task.cancel()
        self._creating.clear()
        for label, pad in list(self._pads.items()):
            try:
                pad.close()
            except Exception as e:  # ViGEm may already be gone at shutdown
                self.log("gamepad_close_failed", controller=label, error=repr(e))
        self._pads.clear()

    def status(self) -> dict[str, Any]:
        """Per label: the pad's XInput ``slot`` and its counters, or the ``error`` its creation ended in."""
        out: dict[str, Any] = {
            label: {"slot": pad.slot, "frames": self._frames.get(label, 0), "rumbles": self._rumbles.get(label, 0)}
            for label, pad in self._pads.items()
        }
        out.update({label: {"error": error} for label, error in self._failed.items()})
        return out


__all__ = [
    "BACKENDS",
    "BUTTON_BITS",
    "MOTOR_SCALE",
    "PadBackend",
    "ViGEmPad",
    "VirtualGamepads",
    "occupied_xinput_slots",
]
