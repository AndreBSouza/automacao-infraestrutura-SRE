"""Connector layer — one module per external system (SPEC.md section 5).

Each connector exposes read tools (executed directly by the LLM
orchestrator) and write tools (always routed through the Approval Engine
before any real side effect occurs, unless the exact tool+scope pair is in
the low-risk allowlist — see sai/approval/allowlist.py).
"""
