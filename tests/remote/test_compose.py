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


def _tagged_photo(orientation: int, size: tuple[int, int] = (160, 100)) -> bytes:
    """A JPEG with an EXIF Orientation tag over an asymmetric gradient.

    The pixels are identical for every ``orientation``; only the tag differs,
    so any output difference is attributable to orientation handling.
    """
    img = PIL.Image.new("RGB", size)
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = ((x * 7) % 256, (y * 11) % 256, 90)
    exif = img.getexif()
    exif[0x0112] = orientation  # 0x0112 = EXIF Orientation
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif, quality=95)
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

    def test_exif_orientation_is_applied(self, service: ComposeService) -> None:
        # Same pixels, different Orientation tag: if EXIF were ignored the two
        # renders would be identical.  Applying it makes the rotated one differ.
        tid = service.list_templates()[0]["id"]
        upright = service.render(_tagged_photo(1), tid, {}, "scottie_s1")
        rotated = service.render(_tagged_photo(6), tid, {}, "scottie_s1")
        assert upright is not None and rotated is not None
        assert upright.tobytes() != rotated.tobytes()

    def test_blank_rst_defaults(self, service: ComposeService) -> None:
        # A blank RST must not crash the renderer (QSOState default is 595).
        tid = service.list_templates()[0]["id"]
        img = service.render(_photo_bytes(), tid, {"rst": ""}, "martin_m1")
        assert img is not None


class TestStaging:
    def test_stage_and_resolve(self, service: ComposeService) -> None:
        tid = service.list_templates()[0]["id"]
        sid = service.stage(_photo_bytes(), tid, {"tocall": "K1ABC"}, "scottie_s1")
        assert sid is not None and service.is_staged_id(sid)
        img = service.staged_image(sid)
        assert img is not None

    def test_unknown_staged_id(self, service: ComposeService) -> None:
        assert service.staged_image("s-deadbeef") is None

    def test_bad_render_does_not_stage(self, service: ComposeService) -> None:
        assert service.stage(b"not an image", "x", {}, "scottie_s1") is None

    def test_staging_store_is_bounded(self, service: ComposeService) -> None:
        from open_sstv.remote.compose import _MAX_STAGED

        tid = service.list_templates()[0]["id"]
        ids = [service.stage(_photo_bytes(), tid, {}, "scottie_s1")
               for _ in range(_MAX_STAGED + 3)]
        # The oldest were evicted; only the last _MAX_STAGED survive.
        assert service.staged_image(ids[0]) is None
        assert service.staged_image(ids[-1]) is not None

    def test_gallery_id_is_not_staged(self, service: ComposeService) -> None:
        assert service.is_staged_id("ee5998afb64b94f6") is False
