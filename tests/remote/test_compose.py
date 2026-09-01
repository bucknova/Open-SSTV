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


class TestDecompressionCap:
    """A small upload declaring a huge canvas must be refused before decode.

    Pillow's MAX_IMAGE_PIXELS only *raises* above 2x the limit; between 1x
    and 2x it warns and decodes anyway. So a ~150 KB solid-colour PNG
    declaring 7000x7000 cleared the 32 MP cap and materialised hundreds of
    MB of pixels per request, with nothing bounding concurrency.
    """

    def test_oversized_canvas_rejected_without_decoding(self, tmp_path) -> None:
        import io
        from dataclasses import replace

        from PIL import Image

        from open_sstv.config.schema import AppConfig
        from open_sstv.remote.compose import ComposeService
        from open_sstv.security import MAX_IMAGE_PIXELS

        buf = io.BytesIO()
        Image.new("RGB", (7000, 7000), (3, 5, 7)).save(buf, "PNG", compress_level=9)
        payload = buf.getvalue()
        assert 7000 * 7000 > MAX_IMAGE_PIXELS
        assert len(payload) < 1_000_000, "payload should be small — that's the point"

        cfg = replace(AppConfig(), logbook_db_path=str(tmp_path / "no.db"))
        svc = ComposeService(lambda: cfg)
        # Use a REAL template id: _resolve() runs before the decode, so a
        # bogus id would make this pass without ever exercising the cap.
        templates = svc.list_templates()
        assert templates, "need at least one template for this test to mean anything"
        template_id = templates[0]["id"]

        # Sanity: a normal photo through the same call must succeed, so a
        # None below can only be the pixel cap.
        small = io.BytesIO()
        Image.new("RGB", (320, 256), (9, 9, 9)).save(small, "PNG")
        assert svc.render(small.getvalue(), template_id, {}, "scottie_s1") is not None

        assert svc.render(payload, template_id, {}, "scottie_s1") is None
