"""SAI — Sistema de Automação e Copiloto de Infraestrutura.

Internal infrastructure copilot: continuous discovery/inventory of the
environment, conversational diagnostics over Claude (Anthropic API) with
tool use, and governed remediation actions that require human approval
before execution (except a small pre-approved low-risk allowlist).

See SPEC.md at the project root for the full technical specification —
this package implements it section by section.
"""

__version__ = "1.0.0"
