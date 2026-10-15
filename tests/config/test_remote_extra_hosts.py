"""v0.6.14 audit S-L1: ``remote_extra_hosts`` config coercion."""

from __future__ import annotations

from open_sstv.config.schema import AppConfig
from open_sstv.remote import normalise_hosts


def test_default_is_empty_list() -> None:
    assert AppConfig().remote_extra_hosts == []


def test_scalar_is_ignored() -> None:
    assert AppConfig(remote_extra_hosts="shack.ts.net").remote_extra_hosts == []


def test_entries_are_stripped_and_blanks_dropped() -> None:
    cfg = AppConfig(remote_extra_hosts=[" shack.ts.net ", "", 42])
    assert cfg.remote_extra_hosts == ["shack.ts.net", "42"]


def test_normalise_hosts_matches_server_form() -> None:
    assert normalise_hosts(["Shack.TS.net.", " ", "radio.lan"]) == frozenset(
        {"shack.ts.net", "radio.lan"}
    )
