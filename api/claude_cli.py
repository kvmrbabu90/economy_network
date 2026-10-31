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

LOCKDOWN_FLAGS = ("--tools", "", "--strict-mcp-config")


def build_argv(binary: str, prompt: str) -> list[str]:
    """`claude -p <prompt>` with JSON output and nothing the model could act with."""
    return [binary, "-p", prompt, "--output-format", "json", *LOCKDOWN_FLAGS]
