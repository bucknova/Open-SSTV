# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.templates.tokens.resolve_tokens``.

Covers:

* Both token syntaxes (``%c``, ``{callsign}``) resolve identically.
* Unknown tokens pass through unchanged (forward-compat).
* ``%%`` renders as literal ``%`` and doesn't interact with other passes.
* Date / time / timestamp resolve against the supplied context clock,
  not the system clock (resolver stays pure and testable).
* Direction and callsign / mode fields render as given.
"""
from __future__ import annotations

import datetime

from open_sstv.templates.tokens import TokenContext, resolve_tokens


def _ctx(**overrides: object) -> TokenContext:
    """Return a TokenContext with sensible test defaults, overridable."""
    defaults: dict[str, object] = {
        "callsign": "W0AEZ",
        "mode": "Scottie-S1",
        "direction": "RX",
        "now_utc": datetime.datetime(
            2026, 4, 17, 21, 35, 12, tzinfo=datetime.timezone.utc
        ),
    }
    defaults.update(overrides)
    return TokenContext(**defaults)  # type: ignore[arg-type]


# === percent-style tokens ===


def test_percent_callsign() -> None:
    assert resolve_tokens("%c", _ctx()) == "W0AEZ"


def test_percent_mode() -> None:
    assert resolve_tokens("%m", _ctx()) == "Scottie-S1"


def test_percent_date_defaults_to_iso() -> None:
    assert resolve_tokens("%d", _ctx()) == "2026-04-17"


def test_percent_time_defaults_to_no_colons() -> None:
    assert resolve_tokens("%t", _ctx()) == "213512"


def test_percent_timestamp_is_unix_epoch() -> None:
    # Epoch for 2026-04-17 21:35:12 UTC
    expected = int(
        datetime.datetime(2026, 4, 17, 21, 35, 12, tzinfo=datetime.timezone.utc).timestamp()
    )
    assert resolve_tokens("%ts", _ctx()) == str(expected)


def test_percent_direction_rx() -> None:
    assert resolve_tokens("%rx_tx", _ctx(direction="RX")) == "RX"


def test_percent_direction_tx() -> None:
    assert resolve_tokens("%rx_tx", _ctx(direction="TX")) == "TX"


def test_percent_literal_percent() -> None:
    """`%%` must become a literal `%`, not consume the next character."""
    assert resolve_tokens("100%%", _ctx()) == "100%"
    assert resolve_tokens("%%c", _ctx()) == "%c", (
        "Escaped %% must NOT be re-resolved as the %c callsign token."
    )


# === named-style tokens ===


def test_named_callsign() -> None:
    assert resolve_tokens("{callsign}", _ctx()) == "W0AEZ"


def test_named_mode() -> None:
    assert resolve_tokens("{mode}", _ctx()) == "Scottie-S1"


def test_named_date() -> None:
    assert resolve_tokens("{date}", _ctx()) == "2026-04-17"


def test_named_time() -> None:
    assert resolve_tokens("{time}", _ctx()) == "213512"


def test_named_timestamp() -> None:
    expected = int(
        datetime.datetime(2026, 4, 17, 21, 35, 12, tzinfo=datetime.timezone.utc).timestamp()
    )
    assert resolve_tokens("{timestamp}", _ctx()) == str(expected)


def test_named_direction() -> None:
    assert resolve_tokens("{direction}", _ctx(direction="TX")) == "TX"


# === mixed / composite patterns ===


def test_typical_filename_pattern() -> None:
    """The default pattern ``%d_%t_%m`` resolves to a sortable filename."""
    assert resolve_tokens("%d_%t_%m", _ctx()) == "2026-04-17_213512_Scottie-S1"


def test_pattern_with_callsign() -> None:
    """Common preset: date_time_callsign_mode."""
    assert (
        resolve_tokens("%d_%t_%c_%m", _ctx())
        == "2026-04-17_213512_W0AEZ_Scottie-S1"
    )


def test_mixed_syntax_in_same_pattern() -> None:
    """Both syntaxes work in the same pattern — documented but not
    advertised."""
    result = resolve_tokens("{date}_%t_{callsign}_%m", _ctx())
    assert result == "2026-04-17_213512_W0AEZ_Scottie-S1"


# === unknown / unresolvable tokens ===


def test_unknown_percent_token_passes_through() -> None:
    """Older install must not mangle a pattern referencing a future token."""
    # %x isn't a defined token, must survive verbatim.
    assert resolve_tokens("prefix_%x_suffix", _ctx()) == "prefix_%x_suffix"


def test_unknown_named_token_passes_through() -> None:
    assert (
        resolve_tokens("prefix_{future_token}_suffix", _ctx())
        == "prefix_{future_token}_suffix"
    )


def test_bare_percent_is_not_a_token() -> None:
    """A lone ``%`` with no valid following letter passes through."""
    assert resolve_tokens("100% good", _ctx()) == "100% good"


# === empty / boundary cases ===


def test_empty_pattern_returns_empty() -> None:
    assert resolve_tokens("", _ctx()) == ""


def test_pattern_with_no_tokens_is_unchanged() -> None:
    assert resolve_tokens("just-literal-text", _ctx()) == "just-literal-text"


def test_empty_callsign_resolves_to_empty_string() -> None:
    """When callsign is blank (listening-only op who skipped first-launch),
    %c resolves to "" — the filename builder handles the resulting
    double-separator collapse."""
    assert resolve_tokens("%d_%c_%m", _ctx(callsign="")) == "2026-04-17__Scottie-S1"


# === custom format strings ===


def test_custom_date_format() -> None:
    """Callers can override date format per-resolution."""
    ctx = _ctx(date_format="%Y%m%d")
    assert resolve_tokens("%d", ctx) == "20260417"


def test_custom_time_format() -> None:
    ctx = _ctx(time_format="%H-%M-%S")
    assert resolve_tokens("%t", ctx) == "21-35-12"


# === context purity ===


def test_resolver_does_not_consult_system_clock() -> None:
    """Two resolutions of ``%ts`` with the same context return the same
    value — the resolver must use ``ctx.now_utc``, not ``time.time()``."""
    ctx = _ctx()
    first = resolve_tokens("%ts", ctx)
    second = resolve_tokens("%ts", ctx)
    assert first == second
