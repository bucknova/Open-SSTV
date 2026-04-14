# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent configuration for sstv-app.

Stores user settings (audio device choices, sample rate, default TX mode,
rigctld host/port, callsign, image directories) as TOML in the
``platformdirs.user_config_dir("open_sstv")`` location. Read with stdlib
``tomllib``, written with ``tomli_w`` so we don't carry a Pydantic dep.
"""
