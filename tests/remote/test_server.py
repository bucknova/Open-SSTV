# SPDX-License-Identifier: GPL-3.0-or-later
"""remote.server — embedded HTTP server endpoints, auth, and path-safety fence."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.gallery.thumbnail_cache import ThumbnailCache
from open_sstv.remote.control import ControlPlane
from open_sstv.remote.server import RemoteServer
from open_sstv.remote.service import GalleryService

TOKEN = "test-token-123"


def _img(path: Path, size: tuple[int, int] = (32, 24)) -> Path:
    Image.new("RGB", size, (20, 40, 60)).save(path)
    return path


@pytest.fixture
def server(tmp_path: Path) -> Iterator[RemoteServer]:
    images = tmp_path / "images"
    images.mkdir()
    _img(images / "2026-04-17_213512_scottie_s1.png")
    cfg = AppConfig(
        images_save_dir=str(images),
        logbook_db_path=str(tmp_path / "logbook.db"),
    )
    svc = GalleryService(lambda: cfg, thumbnail_cache=ThumbnailCache(cache_dir=tmp_path / "thumbs"))
    srv = RemoteServer(svc, host="127.0.0.1", port=0, token=TOKEN)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _get(
    srv: RemoteServer, path: str, *, token: str | None = None, header: bool = False
) -> tuple[int, bytes, str]:
    """GET ``path``; return (status, body, content-type).  Token via query or Bearer header."""
    url = f"http://127.0.0.1:{srv.port}{path}"
    headers = {}
    if token is not None and header:
        headers["Authorization"] = f"Bearer {token}"
    elif token is not None:
        url += ("&" if "?" in url else "?") + f"token={token}"
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 — localhost test
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def _ids(srv: RemoteServer) -> list[str]:
    status, body, _ = _get(srv, "/api/gallery", token=TOKEN)
    assert status == 200
    return [item["id"] for item in json.loads(body)]


def _post(
    srv: RemoteServer, path: str, body: dict, *, token: str | None = TOKEN,
    origin: str | None = None,
) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{srv.port}{path}"
    if token is not None:
        url += f"?token={token}"
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    req = urllib.request.Request(  # noqa: S310 — localhost test
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class _CtlSpy:
    def __init__(self) -> None:
        self.transmits: list[tuple[str, str]] = []
        self.unkeys: list[str] = []

    def transmit(self, image_id: str, mode: str) -> None:
        self.transmits.append((image_id, mode))

    def unkey(self, reason: str) -> None:
        self.unkeys.append(reason)


@pytest.fixture
def tx_server(tmp_path: Path) -> Iterator[tuple[RemoteServer, _CtlSpy]]:
    import time as _time

    images = tmp_path / "images"
    images.mkdir()
    _img(images / "2026-04-17_213512_scottie_s1.png")
    cfg = AppConfig(images_save_dir=str(images), logbook_db_path=str(tmp_path / "l.db"))
    svc = GalleryService(lambda: cfg, thumbnail_cache=ThumbnailCache(cache_dir=tmp_path / "t"))
    spy = _CtlSpy()
    control = ControlPlane(
        now=_time.monotonic, transmit=spy.transmit, unkey=spy.unkey, enabled=lambda: True
    )
    srv = RemoteServer(svc, host="127.0.0.1", port=0, token=TOKEN, control=control)
    srv.start()
    try:
        yield srv, spy
    finally:
        srv.stop()


class TestControlEndpoints:
    def test_lease_take_and_busy(self, tx_server: tuple[RemoteServer, _CtlSpy]) -> None:
        srv, _ = tx_server
        assert _post(srv, "/api/tx/lease", {"client": "A"})[0] == 200
        status, body = _post(srv, "/api/tx/lease", {"client": "B"})
        assert status == 409 and body["error"] == "busy"

    def test_endpoints_require_token(self, tx_server: tuple[RemoteServer, _CtlSpy]) -> None:
        srv, _ = tx_server
        assert _post(srv, "/api/tx/lease", {"client": "A"}, token=None)[0] == 401

    def test_cross_origin_refused(self, tx_server: tuple[RemoteServer, _CtlSpy]) -> None:
        srv, _ = tx_server
        status, body = _post(srv, "/api/tx/lease", {"client": "A"}, origin="http://evil.example")
        assert status == 403 and body["error"] == "cross-origin refused"

    def test_missing_client_id(self, tx_server: tuple[RemoteServer, _CtlSpy]) -> None:
        srv, _ = tx_server
        assert _post(srv, "/api/tx/lease", {})[0] == 400

    def test_request_needs_lease(self, tx_server: tuple[RemoteServer, _CtlSpy]) -> None:
        srv, _ = tx_server
        (iid,) = _ids(srv)
        status, body = _post(srv, "/api/tx/request",
                             {"client": "A", "image_id": iid, "mode": "scottie_s1"})
        assert status == 403 and body["error"] == "not_lease_holder"

    def test_full_flow_confirms_and_transmits(
        self, tx_server: tuple[RemoteServer, _CtlSpy]
    ) -> None:
        srv, spy = tx_server
        (iid,) = _ids(srv)
        assert _post(srv, "/api/tx/lease", {"client": "A"})[0] == 200
        status, body = _post(srv, "/api/tx/request",
                             {"client": "A", "image_id": iid, "mode": "scottie_s1"})
        assert status == 200 and body["token"]
        assert _post(srv, "/api/tx/confirm", {"client": "A", "token": body["token"]})[0] == 200
        assert spy.transmits == [(iid, "scottie_s1")]
        # abort unkeys.
        assert _post(srv, "/api/tx/abort", {"client": "A"})[0] == 200
        assert spy.unkeys == ["operator_abort"]

    def test_request_unknown_image(self, tx_server: tuple[RemoteServer, _CtlSpy]) -> None:
        srv, _ = tx_server
        _post(srv, "/api/tx/lease", {"client": "A"})
        status, _ = _post(srv, "/api/tx/request",
                          {"client": "A", "image_id": "deadbeef", "mode": "scottie_s1"})
        assert status == 404

    def test_request_unknown_mode(self, tx_server: tuple[RemoteServer, _CtlSpy]) -> None:
        srv, _ = tx_server
        (iid,) = _ids(srv)
        _post(srv, "/api/tx/lease", {"client": "A"})
        status, body = _post(srv, "/api/tx/request",
                            {"client": "A", "image_id": iid, "mode": "banana"})
        assert status == 400 and body["error"] == "unknown mode"

    def test_confirm_bad_token(self, tx_server: tuple[RemoteServer, _CtlSpy]) -> None:
        srv, spy = tx_server
        (iid,) = _ids(srv)
        _post(srv, "/api/tx/lease", {"client": "A"})
        _post(srv, "/api/tx/request", {"client": "A", "image_id": iid, "mode": "scottie_s1"})
        status, body = _post(srv, "/api/tx/confirm", {"client": "A", "token": "wrong"})
        assert status == 400 and body["error"] == "bad_token"
        assert spy.transmits == []


class TestControlGateDisabled:
    def test_request_denied_when_tx_disabled(self, tmp_path: Path) -> None:
        import time as _time

        images = tmp_path / "images"
        images.mkdir()
        _img(images / "2026-04-17_213512_scottie_s1.png")
        cfg = AppConfig(images_save_dir=str(images), logbook_db_path=str(tmp_path / "l.db"))
        svc = GalleryService(lambda: cfg, thumbnail_cache=ThumbnailCache(cache_dir=tmp_path / "t"))
        control = ControlPlane(
            now=_time.monotonic, transmit=lambda *a: None, unkey=lambda *a: None,
            enabled=lambda: False,  # TX gate OFF
        )
        srv = RemoteServer(svc, host="127.0.0.1", port=0, token=TOKEN, control=control)
        srv.start()
        try:
            (iid,) = _ids(srv)
            _post(srv, "/api/tx/lease", {"client": "A"})
            status, body = _post(srv, "/api/tx/request",
                                {"client": "A", "image_id": iid, "mode": "scottie_s1"})
            assert status == 403 and body["error"] == "tx_disabled"
        finally:
            srv.stop()


class TestPage:
    def test_root_serves_html_without_token(self, server: RemoteServer) -> None:
        status, body, ctype = _get(server, "/")
        assert status == 200
        assert ctype.startswith("text/html")
        assert b"Open-SSTV" in body

    def test_url_contains_token(self, server: RemoteServer) -> None:
        assert f"token={TOKEN}" in server.url

    def test_responses_carry_csp(self, server: RemoteServer) -> None:
        # CSP is defense-in-depth: connect-src 'self' blocks any injected
        # markup from exfiltrating the token to another origin.
        url = f"http://127.0.0.1:{server.port}/"
        with urllib.request.urlopen(url) as resp:  # noqa: S310 — localhost test
            csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'none'" in csp
        assert "connect-src 'self'" in csp

    def test_page_has_live_status_indicator(self, server: RemoteServer) -> None:
        _, body, _ = _get(server, "/")
        html = body.decode("utf-8")
        assert 'id="status"' in html and "statuspill" in html

    def test_page_has_logbook_view(self, server: RemoteServer) -> None:
        _, body, _ = _get(server, "/")
        html = body.decode("utf-8")
        assert 'data-view="logbook"' in html and 'id="logBody"' in html

    def test_gallery_card_escapes_server_fields(self, server: RemoteServer) -> None:
        # Regression guard: the gallery card must route filename/mode/
        # callsign through esc() (a malicious filename is XSS otherwise).
        _, body, _ = _get(server, "/")
        html = body.decode("utf-8")
        for expr in ("esc(item.name)", "esc(item.mode)", "esc(item.callsign)"):
            assert expr in html, expr


class TestAuth:
    def test_gallery_requires_token(self, server: RemoteServer) -> None:
        status, _, _ = _get(server, "/api/gallery")
        assert status == 401

    def test_gallery_rejects_wrong_token(self, server: RemoteServer) -> None:
        status, _, _ = _get(server, "/api/gallery", token="nope")
        assert status == 401

    def test_bearer_header_authenticates(self, server: RemoteServer) -> None:
        status, body, _ = _get(server, "/api/gallery", token=TOKEN, header=True)
        assert status == 200
        assert len(json.loads(body)) == 1


class TestImages:
    def test_gallery_lists_the_image(self, server: RemoteServer) -> None:
        status, body, ctype = _get(server, "/api/gallery", token=TOKEN)
        assert status == 200
        assert ctype == "application/json"
        items = json.loads(body)
        assert len(items) == 1
        assert items[0]["mode"] == "scottie_s1"

    def test_logbook_requires_token(self, server: RemoteServer) -> None:
        status, _, _ = _get(server, "/api/logbook")
        assert status == 401

    def test_logbook_empty_without_db(self, server: RemoteServer) -> None:
        # The fixture points at a logbook path that was never created.
        status, body, ctype = _get(server, "/api/logbook", token=TOKEN)
        assert status == 200
        assert ctype == "application/json"
        assert json.loads(body) == []

    def test_thumbnail_is_png(self, server: RemoteServer) -> None:
        (item_id,) = _ids(server)
        status, body, ctype = _get(server, f"/api/thumb/{item_id}", token=TOKEN)
        assert status == 200
        assert ctype == "image/png"
        assert body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_full_image_served(self, server: RemoteServer) -> None:
        (item_id,) = _ids(server)
        status, body, ctype = _get(server, f"/api/image/{item_id}", token=TOKEN)
        assert status == 200
        assert ctype == "image/png"
        assert body[:8] == b"\x89PNG\r\n\x1a\n"


class TestPathSafety:
    def test_unknown_id_is_404_not_a_file_read(self, server: RemoteServer) -> None:
        # An id the server never issued must not resolve to any file —
        # the whole point of the opaque-id fence.
        status, _, _ = _get(server, "/api/image/deadbeefdeadbeef", token=TOKEN)
        assert status == 404

    def test_traversal_style_id_is_404(self, server: RemoteServer) -> None:
        # Even a path-shaped id can only ever be looked up in the registry.
        status, _, _ = _get(server, "/api/image/..%2f..%2fetc%2fpasswd", token=TOKEN)
        assert status in (404, 400)

    def test_unknown_route_is_404(self, server: RemoteServer) -> None:
        status, _, _ = _get(server, "/api/nope", token=TOKEN)
        assert status == 404


class TestControlWiring:
    def test_server_exposes_a_control_plane(self, server: RemoteServer) -> None:
        assert isinstance(server.control, ControlPlane)

    def test_injected_control_is_used(self, tmp_path: Path) -> None:
        cp = ControlPlane(
            now=lambda: 0.0, transmit=lambda *a: None, unkey=lambda *a: None,
            enabled=lambda: True,
        )
        images = tmp_path / "images"
        images.mkdir()
        cfg = AppConfig(images_save_dir=str(images), logbook_db_path=str(tmp_path / "l.db"))
        svc = GalleryService(lambda: cfg, thumbnail_cache=ThumbnailCache(cache_dir=tmp_path / "t"))
        srv = RemoteServer(svc, host="127.0.0.1", port=0, token=TOKEN, control=cp)
        assert srv.control is cp

    def test_default_control_tx_gate_follows_getter(self, tmp_path: Path) -> None:
        images = tmp_path / "images"
        images.mkdir()
        cfg = AppConfig(images_save_dir=str(images), logbook_db_path=str(tmp_path / "l.db"))
        svc = GalleryService(lambda: cfg, thumbnail_cache=ThumbnailCache(cache_dir=tmp_path / "t"))
        srv = RemoteServer(svc, host="127.0.0.1", port=0, token=TOKEN, tx_enabled=lambda: True)
        assert srv.control.status()["tx_enabled"] is True


class TestLivePreview:
    def test_preview_204_when_no_decode(self, server: RemoteServer) -> None:
        status, body, _ = _get(server, "/api/rx/preview", token=TOKEN)
        assert status == 204
        assert body == b""

    def test_preview_serves_set_frame(self, server: RemoteServer) -> None:
        # Simulate the app pushing an in-progress frame (a PIL image); the
        # server encodes it to PNG on the request thread.
        server._service.set_live_image(Image.new("RGB", (16, 12), (0, 100, 0)))
        status, body, ctype = _get(server, "/api/rx/preview", token=TOKEN)
        assert status == 200
        assert ctype == "image/png"
        assert body[:8] == b"\x89PNG\r\n\x1a\n"

    def test_preview_requires_token(self, server: RemoteServer) -> None:
        status, _, _ = _get(server, "/api/rx/preview")
        assert status == 401


class TestEventStream:
    def _open_stream(self, server: RemoteServer, token: str) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", f"/api/events?token={token}")
        return conn.getresponse()

    def test_events_require_token(self, server: RemoteServer) -> None:
        resp = self._open_stream(server, "wrong")
        assert resp.status == 401

    def test_events_stream_published_event(self, server: RemoteServer) -> None:
        resp = self._open_stream(server, TOKEN)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        # Read the initial ": connected" comment — once seen, the client is
        # subscribed, so a publish now will reach this stream.
        first = resp.fp.readline()
        assert first.startswith(b":")
        server.hub.publish({"type": "rx.started", "mode": "scottie_s1"})
        # Read forward to the data line (skips the blank line after the comment).
        data_line = b""
        for _ in range(5):
            line = resp.fp.readline()
            if line.startswith(b"data: "):
                data_line = line
                break
        assert data_line, "no data frame received"
        payload = json.loads(data_line[len(b"data: "):].decode("utf-8"))
        assert payload == {"type": "rx.started", "mode": "scottie_s1"}
        resp.close()
