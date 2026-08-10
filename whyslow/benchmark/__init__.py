"""whyslow agent-evaluation benchmark (RL-compatible environment).

This package builds a reproducible PostgreSQL incident, exposes it to an
external agent (Claude Code, Codex, a human), and deterministically evaluates
the resulting system state. It evaluates agents; it does not train a model and
it makes no LLM API calls. See ``whyslow/benchmark/README.md``.
"""
