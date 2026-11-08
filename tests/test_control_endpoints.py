"""The receiver's own HTTP surface: /_status, unknown paths, canned and overridden RPC responses and the
clock pin."""

from __future__ import annotations

import asyncio
import datetime
import email.utils

from cryptography import x509

from tests.fake_controller import FakeController
from tests.helpers import make_receiver, no_log


def test_status_and_unknown_paths(tmp_path):
    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)
        try:
            st, _, body = await fc.request("GET", "whatever.local", "/_status", port=rx.http.http_ports[0], tls=False)
            assert st == 200
            assert b'"controllers"' in body
            st, _, _ = await fc.request(
                "POST", "cloudcast-pa.googleapis.com", "/v1/unknown/thing", b"\x08\x05", "application/x-protobuf"
            )
            assert st == 404
            rec = [r for r in rx.log.records if r["event"] == "http_request"][-1]
            assert rec["decoded"] == [{"f": 1, "varint": 5}]
            assert rec["body_file"].endswith(".bin")
            # an unknown v1: method still gets an empty 200
            st, _, body = await fc.request(
                "POST", "cloudcast-pa.googleapis.com", "/v1:Nope?key=K", b"\x08\x05", "application/x-protobuf"
            )
            assert (st, body) == (200, b"")
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_rpc_field_overrides_and_canned_bodies(tmp_path):
    async def run():
        rx = make_receiver(
            tmp_path,
            rpc_fields={
                "RegisterController": {"poll_interval_ms": 2500, "pending": {"value": True}},
                "DiscoverDisplay": {"display_id": "tv"},
            },
            rpc_responses={"ResolveDiscovery": b"\x08\x01"},
        )
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)
        try:
            boot = await fc.boot()
            assert boot["register_controller"][1]["poll_interval_ms"] == "2500"
            assert boot["register_controller"][1]["pending"] == {"value": True}
            await fc.start_udp()
            assert (await fc.discover()).display_id == "tv"
            assert await fc.resolve() == (200, b"\x08\x01")
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_clock_pin_auto_midpoint_of_cert_window(tmp_path):
    """--clock-pin auto: the controller's clock (Date header + GetCurrentTime
    replies) must sit inside the served certificate's validity window, so the
    expiry check never fails."""

    async def run():
        rx = make_receiver(tmp_path, clock_pin="auto")
        await rx.start()
        try:
            with open(rx.cfg.cert, "rb") as fh:
                cert = x509.load_pem_x509_certificate(fh.read())
            lo = cert.not_valid_before_utc.timestamp()
            hi = cert.not_valid_after_utc.timestamp()
            t = rx.now()
            assert lo <= t <= hi
            hdr = email.utils.formatdate(rx.now(), usegmt=True)
            assert lo <= email.utils.parsedate_to_datetime(hdr).timestamp() <= hi
            assert any(r["event"] == "clock_pinned" for r in rx.log.records)
        finally:
            await rx.stop()

    asyncio.run(run())


def test_clock_pin_fixed_date_freezes_the_controller_clock(tmp_path):
    """An explicit pin anchors the clock; it advances with uptime but stays at
    the pinned date (the cert window is judged against it forever)."""

    async def run():
        rx = make_receiver(tmp_path, clock_pin="2026-06-01T12:00:00Z")
        await rx.start()
        try:
            epoch = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.UTC).timestamp()
            t1, t2 = rx.now(), rx.now()
            assert epoch <= t1 < epoch + 30  # pinned date, small drift
            assert t2 >= t1  # time still flows forward within a session
            assert t2 < epoch + 3600  # ...but stays at the pinned date
        finally:
            await rx.stop()

    asyncio.run(run())


def test_input_shows_each_controllers_live_state(tmp_path):
    """``/_input`` is what the Sunshine plugin's panel polls: connection, battery, pad and the latest report."""
    import json

    from tests.helpers import connect_firmware_flow, wait_for

    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = await connect_firmware_flow(rx)
        port = rx.http.http_ports[0]
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            await fc.keepalive()
            st, _, body = await fc.request("GET", "localhost", "/_input", port=port, tls=False)
            doc = json.loads(body)
            assert st == 200 and doc["gamepad"] == "off"
            (entry,) = doc["controllers"].values()
            assert entry["display_state"] in ("ready", "input") and entry["input"] is None
            assert entry["heard_s"] < 5 and entry["gamepad"] is None

            assert fc.send_input_rpc(buttons=(1 << 12) | (1 << 11), lx=20000, ry=-300, lt=255)  # A + Capture
            await wait_for(lambda: rx._inputs)
            _, _, body = await fc.request("GET", "localhost", "/_input", port=port, tls=False)
            (entry,) = json.loads(body)["controllers"].values()
            got = entry["input"]
            assert got["held"] == ["capture", "a"] and got["buttons"] == (1 << 12) | (1 << 11)
            assert (got["lx"], got["ry"], got["l2"], got["r2"]) == (20000, -300, 255, 0)

            label = next(iter(rx.controllers))
            rx.controllers[label].reset_boot_state()  # a reboot lets go of everything
            _, _, body = await fc.request("GET", "localhost", "/_input", port=port, tls=False)
            assert json.loads(body)["controllers"][label]["input"] is None
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_shutdown_is_local_and_post_only(tmp_path):
    from stadiacast.http_server import HttpRequest

    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)
        port = rx.http.http_ports[0]
        try:
            st, _, _ = await fc.request("GET", "localhost", "/_shutdown", port=port, tls=False)
            assert st == 405 and not rx.stop_requested.is_set()
            remote = HttpRequest(
                "POST", "/_shutdown", "HTTP/1.1", [], b"", ("10.0.0.150", 5000), "t", None, None, path="/_shutdown"
            )
            assert rx._shutdown(remote).status == 403 and not rx.stop_requested.is_set()
            st, _, _ = await fc.request("POST", "localhost", "/_shutdown", b"", port=port, tls=False)
            assert st == 200 and rx.stop_requested.is_set()
            assert rx.log.counts["shutdown_requested"] == 1
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_the_cli_stops_on_a_shutdown_request(tmp_path, monkeypatch):
    """``amain`` returns when ``/_shutdown`` is posted: how a background receiver is stopped on Windows."""
    from stadiacast import cli
    from stadiacast.receiver import Receiver

    made: list[Receiver] = []

    class Recording(Receiver):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(cli, "Receiver", Recording)

    async def run():
        cfg = make_receiver(tmp_path).cfg
        cfg.log_dir = str(tmp_path / "run2")
        task = asyncio.ensure_future(cli.amain(cfg))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if made and made[0].started:
                break
        port = made[0].http.http_ports[0]
        fc = FakeController("127.0.0.1", 0, made[0].protos, log=no_log)
        try:
            st, _, _ = await fc.request("POST", "localhost", "/_shutdown", b"", port=port, tls=False)
        finally:
            fc.close()
        assert st == 200
        assert await asyncio.wait_for(task, 5) == 0

    asyncio.run(run())
