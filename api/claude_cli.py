"""The one place that builds a Claude Code CLI command line.

Every prompt this project sends embeds untrusted text (news headlines, articles, filings, Wikipedia), so the CLI
must never be able to act on it:
- `--tools ""` removes every built-in tool (Bash, PowerShell, file read/write, web fetch/search, agents);
- `--strict-mcp-config` loads no MCP servers at all, because without it the user's own servers (e.g. email,
  cloud drive, a command-running agent) stay available even with no built-in tools.
The model only reads the prompt and writes text back. A test fails if any other module assembles a `claude -p`
command line itself.
"""
from __future__ import annotations

import os

LOCKDOWN_FLAGS = ("--tools", "", "--strict-mcp-config")
# The pipeline runs on the Claude subscription login (docs/PHASE2_PROMPT.md: "No ANTHROPIC_API_KEY needed or
# wanted"). An API key in the environment makes the CLI use it instead, so a stale or unrelated key in the user's
# environment broke every scheduled call on 2026-11-09 ("Invalid API key · Fix external API key").
SCRUBBED_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def build_argv(binary: str, prompt: str) -> list[str]:
    """`claude -p <prompt>` with JSON output and nothing the model could act with."""
    return [binary, "-p", prompt, "--output-format", "json", *LOCKDOWN_FLAGS]


def child_env() -> dict[str, str]:
    """The environment for a CLI child process: everything except API-key credentials, so the CLI always
    authenticates with the subscription login."""
    drop = {v.upper() for v in SCRUBBED_ENV_VARS}
    return {k: v for k, v in os.environ.items() if k.upper() not in drop}
