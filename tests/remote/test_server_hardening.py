# SPDX-License-Identifier: GPL-3.0-or-later
"""remote.server — v0.6.14 security-audit hardening (section 1).

Real sockets on 127.0.0.1 with short timeouts, like test_server.py.  Covers
token redaction in logs (S-M1), short-token refusal + 401 backoff (S-M2),
request deadline + connection cap (S-M4), Host validation and the
Origin-or-Bearer POST rule (S-L1), the compose semaphore (S-L3),
Referrer-Policy (S-L4), the per-IP SSE cap (S-L5), the neutral Server
banner and the remote-TX cooldown (Info).
"""
from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.gallery.thumbnail_cache import ThumbnailCache
from open_sstv.remote import server as server_mod
from open_sstv.remote.control import ControlPlane
from open_sstv.remote.server import (
    MIN_TOKEN_LEN,
    RemoteServer,
    _AuthThrottle,
    _host_allowed,
    _redact,
    token_is_strong,
)
from open_sstv.remote.service import GalleryService

TOKEN = "hardening-token-0123456789"


def _service(tmp_path: Path, *, with_image: bool = True) -> GalleryService:
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    if with_image:
        Image.new("RGB", (32, 24), (20, 40, 60)).save(images / "2026-04-17_213512_scottie_s1.png")
    cfg = AppConfig(
        callsign="W0AEZ", images_save_dir=str(images),
        logbook_db_path=str(tmp_path / "logbook.db"),
    )
    return GalleryService(lambda: cfg, thumbnail_cache=ThumbnailCache(cache_dir=tmp_path / "t"))


@pytest.fixture
def server(tmp_path: Path) -> Iterator[RemoteServer]:
    srv = RemoteServer(_service(tmp_path), host="127.0.0.1", port=0, token=TOKEN)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _request(
    srv: RemoteServer, method: str, path: str, *, host: str | None = None,
    headers: dict[str, str] | None = None, body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{srv.port}")
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def _bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _closed_within(sock: socket.socket, seconds: float, *, trickle: bool = False) -> bool:
    """True if the server closes *sock* within *seconds*."""
    end = time.monotonic() + seconds
    sock.settimeout(0.1)
    while time.monotonic() < end:
        try:
            if trickle:
                sock.send(b"X")
            data = sock.recv(1024)
            if data == b"":
                return True
        except TimeoutError:
            pass
        except OSError:
            return True
        time.sleep(0.1)
    return False


# --- S-M2: weak fixed tokens -------------------------------------------


class TestTokenStrength:
    def test_boundary(self) -> None:
        assert not token_is_strong("x" * (MIN_TOKEN_LEN - 1))
        assert token_is_strong("x" * MIN_TOKEN_LEN)

    def test_short_fixed_token_not_used(self, tmp_path: Path, caplog) -> None:
        caplog.set_level(logging.WARNING, logger="open_sstv.remote.server")
        srv = RemoteServer(_service(tmp_path), port=0, token="demo")
        assert srv.token_rejected is True
        assert srv.token != "demo"
        assert token_is_strong(srv.token)
        assert "shorter than" in caplog.text
        assert "demo" not in caplog.text  # the token is never logged

    def test_strong_fixed_token_kept(self, tmp_path: Path) -> None:
        srv = RemoteServer(_service(tmp_path), port=0, token=TOKEN)
        assert srv.token == TOKEN and srv.token_rejected is False

    def test_blank_token_generated(self, tmp_path: Path) -> None:
        srv = RemoteServer(_service(tmp_path), port=0, token="")
        assert token_is_strong(srv.token) and srv.token_rejected is False

    def test_short_token_does_not_authenticate(self, tmp_path: Path) -> None:
        srv = RemoteServer(_service(tmp_path), host="127.0.0.1", port=0, token="demo")
        srv.start()
        try:
            assert _request(srv, "GET", "/api/gallery?token=demo")[0] == 401
            assert _request(srv, "GET", f"/api/gallery?token={srv.token}")[0] == 200
        finally:
            srv.stop()


# --- S-M2: per-IP 401 backoff -------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class TestAuthThrottle:
    def test_free_failures_then_exponential_lockout(self) -> None:
        clock = _Clock()
        th = _AuthThrottle(clock, free_failures=3, window_s=60, max_lockout_s=300)
        for _ in range(3):
            th.failure("10.0.0.1")
            assert th.locked_for("10.0.0.1") == 0.0
        th.failure("10.0.0.1")
        assert th.locked_for("10.0.0.1") == pytest.approx(2.0)
        clock.t += 2.0
        th.failure("10.0.0.1")
        assert th.locked_for("10.0.0.1") == pytest.approx(4.0)

    def test_lockout_capped(self) -> None:
        clock = _Clock()
        th = _AuthThrottle(clock, free_failures=0, window_s=60, max_lockout_s=30)
        for _ in range(20):
            th.failure("10.0.0.1")
            clock.t += th.locked_for("10.0.0.1")
        th.failure("10.0.0.1")
        assert th.locked_for("10.0.0.1") <= 30.0

    def test_other_ips_unaffected(self) -> None:
        clock = _Clock()
        th = _AuthThrottle(clock, free_failures=0)
        th.failure("10.0.0.1")
        assert th.locked_for("10.0.0.1") > 0
        assert th.locked_for("10.0.0.2") == 0.0

    def test_forgotten_after_quiet_window(self) -> None:
        clock = _Clock()
        th = _AuthThrottle(clock, free_failures=0, window_s=60)
        th.failure("10.0.0.1")
        clock.t += 2.0 + 61.0
        assert th.locked_for("10.0.0.1") == 0.0
        assert len(th) == 0

    def test_success_clears(self) -> None:
        th = _AuthThrottle(_Clock(), free_failures=1)
        th.failure("10.0.0.1")
        th.success("10.0.0.1")
        assert len(th) == 0

    def test_memory_bounded(self) -> None:
        th = _AuthThrottle(_Clock(), max_tracked=50)
        for i in range(500):
            th.failure(f"10.0.{i // 256}.{i % 256}")
        assert len(th) == 50

    def test_ipv6_keyed_by_slash_64(self) -> None:
        th = _AuthThrottle(_Clock(), free_failures=0)
        th.failure("2001:db8::1")
        assert th.locked_for("2001:db8::ffff") > 0
        assert th.locked_for("2001:db8:0:1::1") == 0.0


class TestBackoffOverHttp:
    def test_lockout_returns_429_even_with_good_token(
        self, server: RemoteServer, monkeypatch
    ) -> None:
        for _ in range(server_mod._AUTH_FREE_FAILURES):
            assert _request(server, "GET", "/api/gallery?token=wrong")[0] == 401
        # This failure starts the lockout…
        assert _request(server, "GET", "/api/gallery?token=wrong")[0] == 401
        # …so now even the right token is refused, without being compared.
        status, headers, _ = _request(server, "GET", "/api/gallery", headers=_bearer())
        assert status == 429
        assert int(headers["retry-after"]) >= 1

    def test_good_token_resets_before_lockout(self, server: RemoteServer) -> None:
        for _ in range(server_mod._AUTH_FREE_FAILURES):
            assert _request(server, "GET", "/api/gallery?token=wrong")[0] == 401
        assert _request(server, "GET", "/api/gallery", headers=_bearer())[0] == 200
        assert _request(server, "GET", "/api/gallery?token=wrong")[0] == 401


# --- S-M1: token never reaches the log ---------------------------------


class TestLogRedaction:
    def test_redact_helper(self) -> None:
        line = '"GET /api/thumb/abc?token=s3cr3t-value&t=2 HTTP/1.1" 200 -'
        out = _redact(line)
        assert "s3cr3t-value" not in out and "token=<redacted>&t=2" in out

    def test_access_log_redacts_query_token(self, server: RemoteServer, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="open_sstv.remote.server")
        assert _request(server, "GET", f"/api/gallery?token={TOKEN}")[0] == 200
        assert _request(server, "GET", "/api/gallery?token=guess-guess")[0] == 401
        assert "/api/gallery" in caplog.text
        assert TOKEN not in caplog.text
        assert "guess-guess" not in caplog.text


# --- S-L1: Host validation + Origin-or-Bearer ---------------------------


class TestHostValidation:
    NAMES = frozenset({"localhost", "shack-mac", "shack-mac.local"})

    @pytest.mark.parametrize("host", [
        "127.0.0.1", "127.0.0.1:8730", "192.168.1.20:8730", "100.64.1.2",
        "[::1]:8730", "[fe80::1%en0]:8730", "[::1]", "localhost:8730",
        "LOCALHOST", "shack-mac:8730", "shack-mac.local:8730", "localhost.",
    ])
    def test_allowed(self, host: str) -> None:
        assert _host_allowed(host, self.NAMES)

    @pytest.mark.parametrize("host", [
        None, "", "evil.example", "evil.example:8730", "127.0.0.1.evil.example",
        "localhost:abc", "::1", "[::1", "[nothex]:80", "shack-mac.evil.example",
    ])
    def test_refused(self, host: str | None) -> None:
        assert not _host_allowed(host, self.NAMES)

    def test_rebinding_host_gets_421(self, server: RemoteServer) -> None:
        # Even with the token, a request naming a foreign host is refused —
        # this is what a DNS-rebinding page's requests look like.
        assert _request(server, "GET", "/", host="evil.example:80")[0] == 421
        status, _, _ = _request(
            server, "GET", "/api/gallery", host="evil.example", headers=_bearer()
        )
        assert status == 421

    def test_loopback_names_accepted(self, server: RemoteServer) -> None:
        for host in (f"localhost:{server.port}", f"[::1]:{server.port}", "192.168.1.5:8730"):
            assert _request(server, "GET", "/", host=host)[0] == 200

    def test_own_hostname_accepted(self, server: RemoteServer) -> None:
        base = socket.gethostname().lower().split(".")[0]
        assert _request(server, "GET", "/", host=f"{base}.local:{server.port}")[0] == 200

    def test_extra_hosts(self, tmp_path: Path) -> None:
        srv = RemoteServer(_service(tmp_path), host="127.0.0.1", port=0, token=TOKEN,
                           extra_hosts=["Shack.Tail1234.ts.net"])
        srv.start()
        try:
            assert _request(srv, "GET", "/", host="shack.tail1234.ts.net")[0] == 200
        finally:
            srv.stop()


@pytest.fixture
def tx_server(tmp_path: Path) -> Iterator[tuple[RemoteServer, list]]:
    keyed: list[tuple[str, str]] = []
    control = ControlPlane(
        now=time.monotonic, transmit=lambda i, m: keyed.append((i, m)),
        unkey=lambda r: None, enabled=lambda: True,
    )
    srv = RemoteServer(_service(tmp_path), host="127.0.0.1", port=0, token=TOKEN,
                       control=control)
    srv.start()
    try:
        yield srv, keyed
    finally:
        srv.stop()


class TestPostOriginOrBearer:
    BODY = json.dumps({"client": "A"}).encode()

    def test_query_token_without_origin_refused(self, tx_server) -> None:
        srv, _ = tx_server
        status, _, body = _request(
            srv, "POST", f"/api/tx/lease?token={TOKEN}",
            headers={"Content-Type": "application/json"}, body=self.BODY,
        )
        assert status == 403 and json.loads(body)["error"] == "cross-origin refused"

    def test_query_token_with_same_origin_allowed(self, tx_server) -> None:
        srv, _ = tx_server
        status, _, _ = _request(
            srv, "POST", f"/api/tx/lease?token={TOKEN}",
            headers={"Content-Type": "application/json",
                     "Origin": f"http://127.0.0.1:{srv.port}"},
            body=self.BODY,
        )
        assert status == 200

    def test_bearer_without_origin_allowed(self, tx_server) -> None:
        srv, _ = tx_server
        status, _, _ = _request(
            srv, "POST", "/api/tx/lease",
            headers={"Content-Type": "application/json", **_bearer()}, body=self.BODY,
        )
        assert status == 200


# --- S-M4: request deadline + connection cap ----------------------------


class TestSlowloris:
    def test_trickled_headers_dropped_at_deadline(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(server_mod, "_HEADER_DEADLINE_S", 1.0)
        monkeypatch.setattr(server_mod, "_REAPER_INTERVAL_S", 0.05)
        srv = RemoteServer(_service(tmp_path), host="127.0.0.1", port=0, token=TOKEN)
        srv.start()
        try:
            sock = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
            sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Slow: ")
            # One byte every 100 ms keeps the per-recv timeout (15 s) fresh
            # forever; only an overall deadline ends this.
            t0 = time.monotonic()
            assert _closed_within(sock, 4.0, trickle=True)
            assert time.monotonic() - t0 < 3.0
            sock.close()
        finally:
            srv.stop()

    def test_sse_stream_outlives_header_deadline(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(server_mod, "_HEADER_DEADLINE_S", 0.5)
        monkeypatch.setattr(server_mod, "_REAPER_INTERVAL_S", 0.05)
        srv = RemoteServer(_service(tmp_path), host="127.0.0.1", port=0, token=TOKEN)
        srv.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            conn.request("GET", f"/api/events?token={TOKEN}")
            resp = conn.getresponse()
            assert resp.status == 200
            time.sleep(1.2)  # well past the header deadline
            srv.hub.publish({"type": "rx.started", "mode": "scottie_s1"})
            for _ in range(8):
                line = resp.fp.readline()
                if line.startswith(b"data: ") and b"rx.started" in line:
                    break
            else:
                raise AssertionError("stream was cut by the header deadline")
            conn.close()
        finally:
            srv.stop()


class TestConnectionCap:
    def test_connections_over_cap_closed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(server_mod, "_MAX_CONNECTIONS", 3)
        srv = RemoteServer(_service(tmp_path), host="127.0.0.1", port=0, token=TOKEN)
        srv.start()
        idle: list[socket.socket] = []
        try:
            for _ in range(3):
                idle.append(socket.create_connection(("127.0.0.1", srv.port), timeout=2))
            time.sleep(0.3)
            extra = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
            assert _closed_within(extra, 2.0), "4th connection should be refused"
            extra.close()
            # The held slots are still fine, and freeing one admits a new client.
            idle.pop().close()
            time.sleep(0.3)
            assert _request(srv, "GET", "/")[0] == 200
        finally:
            for s in idle:
                s.close()
            srv.stop()


# --- S-L3: compose semaphore -------------------------------------------


class TestComposeThrottle:
    def test_third_concurrent_render_gets_503(self, tmp_path: Path) -> None:
        srv = RemoteServer(_service(tmp_path), host="127.0.0.1", port=0, token=TOKEN,
                           tx_enabled=lambda: True)
        release = threading.Event()
        entered = threading.Semaphore(0)

        def slow_render(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
            entered.release()
            release.wait(5)
            return None  # → 400 "could not render"

        srv._compose.render = slow_render  # type: ignore[method-assign]
        srv.start()
        buf = io.BytesIO()
        Image.new("RGB", (16, 16)).save(buf, "PNG")
        body = json.dumps({"photo": base64.b64encode(buf.getvalue()).decode(),
                           "template_id": "x", "mode": "scottie_s1"}).encode()
        hdrs = {"Content-Type": "application/json", **_bearer()}
        results: list[int] = []

        def post() -> None:
            results.append(_request(srv, "POST", "/api/compose/render",
                                    headers=hdrs, body=body)[0])

        workers = [threading.Thread(target=post) for _ in range(2)]
        try:
            for w in workers:
                w.start()
            assert entered.acquire(timeout=3) and entered.acquire(timeout=3)
            status, headers, _ = _request(srv, "POST", "/api/compose/render",
                                          headers=hdrs, body=body)
            assert status == 503 and "retry-after" in headers
        finally:
            release.set()
            for w in workers:
                w.join(5)
            srv.stop()
        assert results == [400, 400]


# --- S-L4 / Info: response headers -------------------------------------


class TestHeaders:
    def test_referrer_policy_and_banner(self, server: RemoteServer) -> None:
        for path, hdrs in (("/", None), ("/api/gallery", _bearer())):
            _, headers, _ = _request(server, "GET", path, headers=hdrs)
            assert headers["referrer-policy"] == "no-referrer"
            assert headers["server"] == "Open-SSTV"
            assert "python" not in headers["server"].lower()


class TestPageTokenHandling:
    """The page side of S-M1 / S-L4 and the lease-id Info finding."""

    @pytest.fixture
    def html(self) -> str:
        from open_sstv.remote.page import render_page

        return render_page()

    def test_fetches_use_bearer_header(self, html: str) -> None:
        assert '"Bearer " + token' in html
        # No fetch() puts the token in its URL any more.
        assert "fetch(q(" not in html

    def test_token_moved_out_of_url(self, html: str) -> None:
        assert 'sessionStorage.setItem("sstv_token"' in html
        assert "history.replaceState" in html

    def test_lease_id_uses_crypto(self, html: str) -> None:
        assert "crypto.randomUUID" in html and "crypto.getRandomValues" in html

    def test_cooldown_has_a_message(self, html: str) -> None:
        assert "cooldown:" in html


# --- S-L5: per-IP SSE cap -----------------------------------------------


class TestSsePerIpCap:
    def test_one_ip_cannot_take_every_slot(self, server: RemoteServer) -> None:
        conns: list[http.client.HTTPConnection] = []
        try:
            for _ in range(server_mod._MAX_SSE_PER_IP):
                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                conn.request("GET", f"/api/events?token={TOKEN}")
                assert conn.getresponse().status == 200
                conns.append(conn)
            status, _, _ = _request(server, "GET", f"/api/events?token={TOKEN}")
            assert status == 429
        finally:
            for c in conns:
                c.close()


# --- Info: remote-TX cooldown over HTTP ---------------------------------


class TestTxCooldownHttp:
    def test_second_request_inside_cooldown_is_429(self, tx_server) -> None:
        srv, keyed = tx_server
        ids = json.loads(_request(srv, "GET", "/api/gallery", headers=_bearer())[2])
        iid = ids[0]["id"]
        hdrs = {"Content-Type": "application/json", **_bearer()}

        def post(path: str, obj: dict) -> tuple[int, dict]:
            st, _, b = _request(srv, "POST", path, headers=hdrs, body=json.dumps(obj).encode())
            return st, json.loads(b or b"{}")

        assert post("/api/tx/lease", {"client": "A"})[0] == 200
        st, body = post("/api/tx/request", {"client": "A", "image_id": iid, "mode": "scottie_s1"})
        assert st == 200
        assert post("/api/tx/confirm", {"client": "A", "token": body["token"]})[0] == 200
        assert keyed == [(iid, "scottie_s1")]
        srv.control.on_tx_finished()
        st, body = post("/api/tx/request", {"client": "A", "image_id": iid, "mode": "scottie_s1"})
        assert st == 429 and body["error"] == "cooldown"
