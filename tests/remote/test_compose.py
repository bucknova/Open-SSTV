# SPDX-License-Identifier: GPL-3.0-or-later
"""remote.compose — server-side render via the desktop compositor."""
from __future__ import annotations

import io

import PIL.Image
import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.remote.compose import ComposeService
from open_sstv.templates import manager as template_manager


def _photo_bytes(size: tuple[int, int] = (200, 150)) -> bytes:
    buf = io.BytesIO()
    PIL.Image.new("RGB", size, (40, 90, 140)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def service() -> ComposeService:
    cfg = AppConfig(callsign="W0AEZ", operator_name="Kevin", grid_square="DN70")
    return ComposeService(lambda: cfg, templates_dir=template_manager._bundled_templates_dir())


class TestListTemplates:
    def test_lists_the_bundled_templates(self, service: ComposeService) -> None:
        tpls = service.list_templates()
        assert len(tpls) == 8
        assert all(t["id"] and t["name"] and t["role"] for t in tpls)
        # ids are unique
        assert len({t["id"] for t in tpls}) == 8


class TestRender:
    def test_renders_to_the_mode_frame_size(self, service: ComposeService) -> None:
        from open_sstv.core.modes import MODE_TABLE, Mode

        tid = service.list_templates()[0]["id"]
        img = service.render(
            _photo_bytes(), tid,
            {"tocall": "K1ABC", "rst": "595", "name": "Sam", "note": "73!"},
            "scottie_s1",
        )
        assert img is not None
        spec = MODE_TABLE[Mode.SCOTTIE_S1]
        assert img.size == (spec.width, spec.display_height)

    def test_unknown_template_returns_none(self, service: ComposeService) -> None:
        assert service.render(_photo_bytes(), "deadbeef", {}, "scottie_s1") is None

    def test_unknown_mode_returns_none(self, service: ComposeService) -> None:
        tid = service.list_templates()[0]["id"]
        assert service.render(_photo_bytes(), tid, {}, "banana") is None

    def test_undecodable_photo_returns_none(self, service: ComposeService) -> None:
        tid = service.list_templates()[0]["id"]
        assert service.render(b"not an image", tid, {}, "scottie_s1") is None

    def test_empty_photo_returns_none(self, service: ComposeService) -> None:
        tid = service.list_templates()[0]["id"]
        assert service.render(b"", tid, {}, "scottie_s1") is None

    def test_oversized_photo_rejected(self, service: ComposeService) -> None:
        from open_sstv.remote.compose import MAX_PHOTO_BYTES

        tid = service.list_templates()[0]["id"]
        assert service.render(b"x" * (MAX_PHOTO_BYTES + 1), tid, {}, "scottie_s1") is None

    def test_blank_rst_defaults(self, service: ComposeService) -> None:
        # A blank RST must not crash the renderer (QSOState default is 595).
        tid = service.list_templates()[0]["id"]
        img = service.render(_photo_bytes(), tid, {"rst": ""}, "martin_m1")
        assert img is not None
