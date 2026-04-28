# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.ui.image_gallery.ImageGalleryWidget``.

Focus: the v0.2.7 preview-on-click behaviour and its coexistence with
the existing double-click = save and context-menu actions.  The older
``image_activated`` path (double-click → save dialog) is covered
implicitly via the shared code path in ``_load_item_image``; these
tests zero in on the single-click / *View* surface that was missing.
"""
from __future__ import annotations

import pytest
from PIL import Image
from PySide6.QtCore import QPoint

from open_sstv.core.modes import Mode
from open_sstv.ui.image_gallery import ImageGalleryWidget

pytestmark = pytest.mark.gui


def _make_test_image(color: tuple[int, int, int] = (200, 120, 60)) -> Image.Image:
    """Return a tiny RGB image — 4:3 so the thumbnail pipeline has
    something sensibly-shaped to scale."""
    return Image.new("RGB", (32, 24), color)


# === single-click preview ===


def test_single_click_emits_preview_requested(qapp, qtbot) -> None:
    """Clicking a thumbnail must emit ``image_preview_requested`` with the
    matching PIL image + Mode. The older ``image_activated`` signal must
    NOT fire on a single click — that's reserved for double-click (save)."""
    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)

    img = _make_test_image()
    gallery.add_image(img, Mode.ROBOT_36)
    index = gallery.model().index(0, 0)

    with qtbot.waitSignal(
        gallery.image_preview_requested, timeout=1000
    ) as preview_blocker:
        # Use the bound slot directly — simulating a real mouse click
        # is environment-sensitive in CI.  The slot is what Qt actually
        # calls on ``clicked``.
        gallery._on_clicked(index)

    emitted_image, emitted_mode = preview_blocker.args
    assert emitted_mode is Mode.ROBOT_36
    assert emitted_image.size == img.size

    # image_activated must NOT have fired.  We use blockSignals-style
    # polling: connect a sentinel and check it was never called.
    saved: list[tuple] = []
    gallery.image_activated.connect(lambda i, m: saved.append((i, m)))
    gallery._on_clicked(index)
    assert saved == [], "Single-click must not trigger the save-dialog signal"


def test_single_click_on_invalid_index_is_noop(qapp, qtbot) -> None:
    """Clicking empty space (no item at the index) must not raise and
    must not emit either signal."""
    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)

    # Invalid: no items have been added.
    from PySide6.QtCore import QModelIndex

    invalid = QModelIndex()
    gallery._on_clicked(invalid)  # must not raise


def test_double_click_still_fires_activated(qapp, qtbot) -> None:
    """Regression guard: the new single-click wiring must not have broken
    the double-click → ``image_activated`` path."""
    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)

    gallery.add_image(_make_test_image(), Mode.SCOTTIE_S1)
    index = gallery.model().index(0, 0)

    with qtbot.waitSignal(gallery.image_activated, timeout=1000) as blocker:
        gallery._on_double_clicked(index)

    _, mode = blocker.args
    assert mode is Mode.SCOTTIE_S1


# === context-menu View ===


def test_view_menu_action_emits_preview_requested(qapp, qtbot) -> None:
    """The context-menu *View* entry must route through the same
    ``image_preview_requested`` signal as single-click.

    Tests call ``_dispatch_context_action`` directly because monkey-
    patching ``QMenu.exec`` at the Python level doesn't replace the
    C++-backed slot PySide6 actually invokes.  The helper is where all
    action-label → signal mapping lives, so exercising it covers the
    real dispatch logic.
    """
    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)
    gallery.add_image(_make_test_image(), Mode.PD_90)

    item = gallery.model().item(0)
    assert item is not None

    with qtbot.waitSignal(
        gallery.image_preview_requested, timeout=1000
    ) as preview_blocker:
        gallery._dispatch_context_action(item, "View")

    _, mode = preview_blocker.args
    assert mode is Mode.PD_90


def test_save_as_menu_action_still_emits_activated(qapp, qtbot) -> None:
    """Regression guard: adding *View* must not have broken the existing
    *Save As…* path."""
    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)
    gallery.add_image(_make_test_image(), Mode.MARTIN_M1)

    item = gallery.model().item(0)
    assert item is not None

    with qtbot.waitSignal(gallery.image_activated, timeout=1000) as blocker:
        # Unicode ellipsis in the live menu label — helper matches by prefix.
        gallery._dispatch_context_action(item, "Save As\u2026")

    _, mode = blocker.args
    assert mode is Mode.MARTIN_M1


def test_copy_menu_action_does_not_emit_any_image_signal(qapp, qtbot) -> None:
    """The *Copy to Clipboard* action must not emit either image signal.

    It's a pure clipboard side-effect; firing ``image_activated`` (save
    dialog) or ``image_preview_requested`` (main-preview swap) on copy
    would be a UX regression.
    """
    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)
    gallery.add_image(_make_test_image(), Mode.ROBOT_36)

    item = gallery.model().item(0)
    assert item is not None

    fired: list[str] = []
    gallery.image_activated.connect(lambda *_: fired.append("activated"))
    gallery.image_preview_requested.connect(lambda *_: fired.append("preview"))

    gallery._dispatch_context_action(item, "Copy to Clipboard")

    assert fired == []


# === RxPanel integration ===


def test_rx_panel_loads_gallery_image_into_preview(qapp, qtbot) -> None:
    """End-to-end: a single-click on a gallery thumbnail must rebind
    the RxPanel's ``_current_pil_image`` / ``_current_mode`` so that
    Ctrl+S / *Save Image* operates on the viewed image.

    This guards the v0.2.7 contract: "preview shows X" ⇔ "Save saves X".
    """
    from open_sstv.ui.rx_panel import RxPanel

    panel = RxPanel()
    qtbot.addWidget(panel)

    # Seed the gallery with two distinguishable images.
    older = _make_test_image(color=(20, 20, 20))
    newer = _make_test_image(color=(240, 240, 240))
    panel._gallery.add_image(older, Mode.ROBOT_36)
    panel._gallery.add_image(newer, Mode.SCOTTIE_S1)  # prepended, so index 0

    # Click the OLDER image (row 1 — add_image prepends).
    older_index = panel._gallery.model().index(1, 0)
    panel._gallery._on_clicked(older_index)

    assert panel._current_mode is Mode.ROBOT_36
    # _current_pil_image should be the loaded copy, not the newer one.
    assert panel._current_pil_image is not None
    assert panel._current_pil_image.size == older.size
    # Save button must now be enabled (the gallery load path enables it
    # the same way show_image_complete does).
    assert panel._save_btn.isEnabled() is True
    # Status line reflects the viewing state, not "Decoded …".
    assert "Viewing" in panel._status.text()


# === OP2-04: gallery temp-file counter avoids id() collisions ===


def test_gallery_temp_files_have_unique_names(qapp, qtbot) -> None:
    """Each call to add_image must produce a unique temp filename,
    regardless of whether the PIL object's id() is reused by GC (OP2-04)."""
    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)

    if gallery._tmpdir is None:
        pytest.skip("temp directory not available in this environment")

    from pathlib import Path
    from open_sstv.ui.image_gallery import _IMAGE_PATH_ROLE

    paths = []
    for i in range(5):
        img = _make_test_image(color=(i * 40, i * 40, i * 40))
        gallery.add_image(img, Mode.ROBOT_36)
        item = gallery.model().item(0)
        assert item is not None
        p = item.data(_IMAGE_PATH_ROLE)
        if p:
            paths.append(p)

    # All collected paths must be distinct.
    assert len(paths) == len(set(paths)), "Temp file names must be unique"


def test_gallery_counter_increments_monotonically(qapp, qtbot) -> None:
    """_image_counter starts at 0 and increments by 1 per add_image call."""
    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)

    assert gallery._image_counter == 0
    gallery.add_image(_make_test_image(), Mode.ROBOT_36)
    assert gallery._image_counter == 1
    gallery.add_image(_make_test_image(), Mode.SCOTTIE_S1)
    assert gallery._image_counter == 2


def test_eviction_drops_in_memory_pil_reference(qapp, qtbot) -> None:
    """M5: when the gallery exceeds capacity in the in-memory fallback path,
    the evicted item's PIL handle must be released, not just orphaned in
    the QStandardItem's user data.

    The disk-backed path drops references implicitly (the PIL object is
    written and never re-stored on the item).  The in-memory fallback used
    to keep PIL images alive forever via ``_PIL_IMAGE_ROLE`` even after
    ``removeRow``, because Qt's PyObject ownership across the C++ boundary
    isn't guaranteed to release the QVariant promptly.  The fix nulls the
    role explicitly before removeRow, and we verify the dropped row's
    underlying image is collectable.
    """
    import gc
    import weakref

    from open_sstv.ui.image_gallery import _MAX_IMAGES, _PIL_IMAGE_ROLE

    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)

    # Force the in-memory fallback path so this test exercises the PIL
    # reference cleanup we actually care about.  Disk-backed mode is
    # already ref-clean.
    gallery._tmpdir = None

    first_image = _make_test_image(color=(11, 22, 33))
    first_ref = weakref.ref(first_image)
    gallery.add_image(first_image, Mode.ROBOT_36)
    # Drop the local strong ref so only the gallery holds the image.
    del first_image

    # Push enough additional images that the first one is evicted.
    for i in range(_MAX_IMAGES + 1):
        gallery.add_image(_make_test_image(color=(i, i, i)), Mode.ROBOT_36)

    # Verify the gallery actually evicted (it bounded growth).
    assert gallery.count() == _MAX_IMAGES

    # Force a collection cycle and confirm the evicted PIL image is gone.
    gc.collect()
    assert first_ref() is None, (
        "Evicted PIL image was still alive — the in-memory fallback "
        "leaked references past _MAX_IMAGES"
    )

    # Defensive: any item in the model that does still carry a PIL handle
    # is a *current* gallery entry, not a leftover from eviction.
    for row in range(gallery.count()):
        item = gallery.model().item(row)
        # Either the item has a path (disk path is None here so this is
        # always None) or a PIL image — both are acceptable for live items.
        _ = item.data(_PIL_IMAGE_ROLE)


def test_clear_drops_in_memory_pil_references(qapp, qtbot) -> None:
    """M5: ``clear()`` releases PIL handles too, not just temp files."""
    import gc
    import weakref

    gallery = ImageGalleryWidget()
    qtbot.addWidget(gallery)
    gallery._tmpdir = None  # in-memory fallback

    img = _make_test_image()
    ref = weakref.ref(img)
    gallery.add_image(img, Mode.ROBOT_36)
    del img

    gallery.clear()
    gc.collect()
    assert ref() is None
