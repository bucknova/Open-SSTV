# SPDX-License-Identifier: GPL-3.0-or-later
"""Radio control layer for sstv-app.

Provides an abstract ``Rig`` Protocol with two backends in v1: a no-op
``ManualRig`` (for users on VOX or hand-keyed PTT) and ``RigctldClient``
(a TCP client for Hamlib's ``rigctld`` daemon, which gives us PTT, frequency,
mode, and S-meter access for any of the hundreds of radios Hamlib supports).
Future backends (CAT-direct, flrig XML-RPC, USB-HID PTT) plug in by
implementing the ``Rig`` Protocol — application code never needs to change.
"""
