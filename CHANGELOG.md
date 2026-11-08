# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `--gamepad xgpad`: each controller becomes an Xbox Series X|S controller through the xgpad
  driver from the Sunshine fork, without ViGEmBus. Capture is the Share button, and the game's
  rumble reaches the controller as with `vigem`. Needs the driver installed and an elevated
  receiver.
- `GET /_input`: each controller's live state in one small document to poll: connection,
  battery, virtual pad and the latest buttons, sticks and triggers. The Sunshine plugin's live
  view reads it.
- `POST /_shutdown` stops the receiver from its own machine, helpers and virtual pads included,
  for a receiver running in the background where Ctrl-C cannot reach it.

### Fixed
- `stadiacast-provision provision` failed on Windows with "The operation was canceled by the
  user": the controller's Wi-Fi scan drops the Bluetooth link. The controller's public key is
  now read before the scan, and a dropped link is reconnected to finish with the scan results
  the controller already holds, finding the controller by its Fast Pair data since its
  Bluetooth address changes.

## [1.0.1] - 2026-11-07

Fixes from the first run on a real Windows PC: the headset stream no longer pauses while the
computer is silent, a helper that cannot start is reported with the reason, and a lab run keeps
its certificate out of `certs/`.

### Fixed
- On Windows the headset stream stopped whenever the computer fell silent and resumed with the
  next sound, because WASAPI feeds a loopback only while something plays: the capture now
  renders silence on the speaker while it runs, and the stream is continuous as on the other
  platforms.
- A helper process that ends (the DNS sink, the SNTP server) is logged as `helper_exited` with
  the last lines it wrote, instead of passing for running; a helper whose port is taken says
  who usually holds it. On Windows that is the Windows Time service on UDP 123, which has to
  be stopped for the SNTP server to start.
- `--self-signed` wrote its lab pair to `certs/`, where every later run found it and served it
  in place of the built-in certificate, which the controller refuses. The lab pair now stays in
  the run directory unless `--cert`/`--key` name a place for it.

## [1.0.0] - 2026-11-06

Nothing to configure before the first run, sound on the headset, a virtual gamepad on Windows
and ready-made executables.

### Added
- A built-in certificate the controller accepts, so there is nothing to obtain: the receiver
  serves it when none is configured. Your own still wins with `--cert`/`--key` or a pair under
  `certs/`.
- Headset audio: `--audio-source` plays a test tone, a file, what the computer plays
  (`system`) or one capture device on a headset plugged into the controller.
  `--audio-bitrate`, `--audio-application`, `--audio-volume` and `--audio-follow-volume` tune
  it; `/_audio` starts, stops and adjusts the stream while the receiver runs, and `/_status`
  reports it under `playback`.
- A virtual gamepad on Windows: `--gamepad vigem` turns each controller into an Xbox 360
  controller for the games on the PC, through the ViGEmBus driver, with the game's rumble
  sent back to the controller; `/_status` lists the pads under `gamepads`.
- Standalone executables for Windows, macOS (Apple silicon) and Linux attached to every
  release: `stadiacast` and `stadiacast-provision`, Python included.
- The battery level, whether a headset is plugged in and the controller's hardware
  description, in `/_status` and in the log.

### Changed
- A port 80 or 8008 held by another program no longer stops the receiver; 443 is the only
  port the controller needs.
- Whatever is missing on the computer (ffmpeg, an audio device, the `audio` or `gamepad`
  extra, the ViGEmBus driver, a Windows that cannot host a pad) stops the receiver with one
  line that says what to install, instead of a traceback.

## [0.1.0] - 2026-10-29

First public version of Stadiacast: a LAN-side stand-in for the Stadia cloud endpoints that
keeps the Stadia Controller's Wi-Fi mode working with your own PC. Everything below was
confirmed against real controllers running the last Wi-Fi firmware, `ota.282115`.

### Added
- The receiver (`stadiacast`): the HTTPS boot endpoints (`generate_204`, `stadiaconfig`,
  Omaha, Clearcut), the cloudcast discovery RPCs, the GMsg push channel, protobuf WebRTC
  signaling, STUN/ICE, DTLS-SRTP, WireHeader2 framing and the display RPC state machine up to
  READY, with every button, stick and trigger decoded into a JSON Lines event log.
- The game session: `StartGame`, the proxied `InitializationRequest`, a second ICE/DTLS
  endpoint, input on its `0x66` channel and rumble (`POST /_rumble`) kept alive across the
  controller's haptics watchdog.
- Several controllers at once: one session per controller (own UDP ports, DTLS certificates,
  GMsg queue and display link), labelled `c0`, `c1`, ... in the log and in `/_status`;
  `?controller=` selects the target of `/_rumble` and `/_start_game`.
- `--preset live`: the flag set proven end to end, including the Clearcut TLS block that keeps
  the controller from powering off after its first analytics upload.
- Helper processes spawned by the receiver: a redirecting DNS sink and an SNTP server.
- `stadiacast-provision`: Wi-Fi provisioning over Bluetooth LE for a controller in setup mode.
- `stadiacast-show-input`: a per-controller summary of the input recorded in a run.
- `tools/start_receiver.sh` / `tools/stop_receiver.sh`: rig settings in `.env`, a fresh run
  directory per start, an optional packet capture.
- The protobuf schemas with their provenance (`src/stadiacast/schemas/`) and a small proto2
  compiler that keeps the hand-written `.proto` files and their descriptors in sync.
- A fake controller (`tests/fake_controller.py`) that reproduces the firmware's network
  behaviour, and a test suite that drives the whole path over loopback without hardware.
- Documentation: setup guide, protocol notes, architecture overview, contributing guide,
  security policy, code of conduct.
- MIT License, with a `NOTICE` for the three protobuf schemas reproduced from the firmware.

[Unreleased]: https://github.com/jkfran/stadiacast/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/jkfran/stadiacast/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/jkfran/stadiacast/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/jkfran/stadiacast/releases/tag/v0.1.0
