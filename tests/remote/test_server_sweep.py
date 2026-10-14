# SPDX-License-Identifier: GPL-3.0-or-later
"""remote.server / page — v0.6.14 sweep: tokenless self-lockout, ASCII tokens."""
from __future__ import annotations

import http.client
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.gallery.thumbnail_cache import ThumbnailCache
from open_sstv.remote import server as server_mod
from open_sstv.remote import token_is_strong as exported_token_is_strong
from open_sstv.remote.server import MIN_TOKEN_LEN, RemoteServer, token_is_strong
from open_sstv.remote.service import GalleryService

TOKEN = "sweep-token-0123456789abcdef"


def _service(tmp_path: Path) -> GalleryService:
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
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


def _get(srv: RemoteServer, path: str, headers: dict[str, str] | None = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


# --- Tokenless requests never lock the operator out -----------------------


class TestTokenlessRequests:
    @pytest.mark.parametrize(
        ("path", "headers"),
        [
            ("/api/gallery", {}),
            ("/api/gallery?token=", {}),
            ("/api/gallery", {"Authorization": "Bearer "}),
            ("/api/events", {}),
        ],
    )
    def test_tokenless_reloads_never_lock_out(
        self, server: RemoteServer, path: str, headers: dict[str, str]
    ) -> None:
        for _ in range(server_mod._AUTH_FREE_FAILURES * 3):
            assert _get(server, path, headers) == 401
        assert len(server._auth_throttle) == 0
        assert _get(server, "/api/gallery", {"Authorization": f"Bearer {TOKEN}"}) == 200

    def test_wrong_nonempty_token_still_locks_out(self, server: RemoteServer) -> None:
        for _ in range(server_mod._AUTH_FREE_FAILURES + 1):
            assert _get(server, "/api/gallery?token=wrong") == 401
        assert _get(server, "/api/gallery", {"Authorization": f"Bearer {TOKEN}"}) == 429

    def test_wrong_bearer_still_counts(self, server: RemoteServer) -> None:
        for _ in range(server_mod._AUTH_FREE_FAILURES + 1):
            assert _get(server, "/api/gallery", {"Authorization": "Bearer nope"}) == 401
        assert _get(server, "/api/gallery?token=" + TOKEN) == 429


class TestPageSkipsCallsWithoutToken:
    def test_page_guards_empty_token(self) -> None:
        from open_sstv.remote.page import render_page

        html = render_page()
        assert 'if (token === "")' in html
        assert "scan the QR" in html


# --- Tokens must be printable ASCII ----------------------------------------


class TestAsciiTokens:
    @pytest.mark.parametrize(
        "token",
        [
            "ü" * MIN_TOKEN_LEN,
            "a-long-enough-token-✓",
            "a-long-enough-token-€",
            "a-long-enough\ttoken-1",   # control char
            "a-long-enough-token-\x7f",
        ],
    )
    def test_non_ascii_or_control_is_not_strong(self, token: str) -> None:
        assert len(token) >= MIN_TOKEN_LEN
        assert not token_is_strong(token)

    def test_printable_ascii_ok(self) -> None:
        assert token_is_strong("Aa0!~-_.+/=? " + "x" * MIN_TOKEN_LEN)

    def test_package_export_is_the_same_function(self) -> None:
        assert exported_token_is_strong is token_is_strong

    def test_non_ascii_configured_token_replaced(self, tmp_path: Path, caplog) -> None:
        caplog.set_level(logging.WARNING, logger="open_sstv.remote.server")
        bad = "schlüssel-sehr-lang-genug"
        srv = RemoteServer(_service(tmp_path), port=0, token=bad)
        assert srv.token_rejected is True
        assert srv.token != bad
        assert token_is_strong(srv.token)
        assert bad not in caplog.text
