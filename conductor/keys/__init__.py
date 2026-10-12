"""Conductor keys module — the API-key registry, absorbed (Track D3).

Read-only, masked-first surface over the workspace's `Stack/models/keys/`. The
registry JSON stores only non-secret metadata (masked suffixes, SHA-256 hashes,
locations, labels, dates) — never plaintext. This module never retrieves secrets.

v1 (D3) wraps the existing `apikey_registry` CLI as a subprocess (strangler pattern,
same as `context/` over `context.py`): `list` (masked inventory) and `audit` (findings).
The write paths (`scan`, `rotate`) stay in the CLI; the 1Password (`op`) secret-retrieval
backend is a separate, later milestone — `op` is not required for masked reads.
"""
