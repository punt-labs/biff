"""Peer integrations — optional enrichment from other Punt Labs tools.

Each submodule follows the integration standard:
- L0: Sentinel file check (presence)
- L1: Binary discovery (shutil.which, lazy + cached)
- Graceful degradation when peer is absent
"""
