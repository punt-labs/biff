"""Peer integrations — optional enrichment from other Punt Labs tools.

Each submodule follows the integration standard (L0-L3):
- L0: Sentinel file check (presence)
- L1: Binary discovery (shutil.which, lazy + cached)
- No library imports of peer packages
- Graceful degradation when peer is absent
"""
