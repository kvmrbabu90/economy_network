"""LLM extraction (Part B): section text -> grounded CandidateEdges.

Two pluggable backends behind a uniform interface, selected by the
``EXTRACTOR`` environment variable:

    EXTRACTOR=claude-cli   (default)  -- shells out to `claude -p ... --output-format json`
                                          on the user's Max subscription. NO API key.
    EXTRACTOR=gemma                   -- POSTs to a local Ollama server.

Whichever backend is used, every candidate goes through the grounding gate
(:func:`verify_grounding`) before it is allowed into ``edges_raw.jsonl``
(CLAUDE.md invariant #4). A snippet that doesn't appear literally in the
filing's cached .txt -- or doesn't contain the named target -- is dropped.
The verify gate is deliberately dumb: fabricated quotes can't be substrings
of real text, so the gate catches them regardless of how plausible they sound.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

import requests
import yaml

from schema.models import CandidateEdge, EdgeType, Provenance
from pipeline.sections import SectionResult, load_filing_text


log = logging.getLogger("extract.llm")


# ---------------------------------------------------------------------------
# Prompt + parsing
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """\
You are extracting B2B relationships from a U.S. SEC 10-K filing.

SOURCE COMPANY: {company_name}
SECTION FOCUS: {section_name}

INSTRUCTIONS
- Extract ONLY relationships that the text below explicitly states.
- A `supplies` edge means {company_name} SELLS to the target (e.g. a customer-concentration disclosure like \"Sales to X accounted for 15% of our net sales\" means {company_name} supplies X).
- A `competes_with` edge means {company_name} competes against the named target.
- Do NOT invent competitors or customers that the text does not name.
- Do NOT include the source company itself as a target.
- Do NOT use the type \"customer_of\" -- always use \"supplies\" with the customer as the target.
- The snippet MUST be a verbatim substring of the text below that proves the relationship and that mentions the target's name.

OUTPUT FORMAT
Return a strict JSON array, with no surrounding prose, no markdown fences, no explanation.
Each item must be a JSON object with these exact keys:
  {{
    "target":     "<the OTHER party's name as written in the text>",
    "type":       "supplies" | "competes_with",
    "snippet":    "<a short verbatim substring of the text below that states this relationship>",
    "confidence": <number 0-1>
  }}
If there are NO extractable relationships, return [].

TEXT
<<<
{section_text}
>>>
"""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_array(s: str) -> Optional[list]:
    """Parse a JSON array from the model's text. Defensive on fences/prose."""
    if not s:
        return None
    s = s.strip()
    # Direct parse first.
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else None
    except json.JSONDecodeError:
        pass
    # Strip markdown fences and retry.
    m = _JSON_FENCE_RE.search(s)
    if m:
        try:
            v = json.loads(m.group(1))
            return v if isinstance(v, list) else None
        except json.JSONDecodeError:
            pass
    # Fall back to the substring between the first '[' and matching ']'.
    start = s.find("[")
    end = s.rfind("]")
    if start >= 0 and end > start:
        try:
            v = json.loads(s[start:end + 1])
            return v if isinstance(v, list) else None
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# Extractor backends
# ---------------------------------------------------------------------------

class Extractor(Protocol):
    """Common shape for LLM-extractor backends."""

    name: str

    def extract(self, prompt: str) -> tuple[Optional[list[dict]], dict[str, Any]]:
        """Return (parsed_items_or_None, meta_dict).

        meta carries duration_ms, model id, error info — used by the
        orchestrator for logging and provenance.extracted_by tagging.
        """
        ...


@dataclass
class ClaudeCLIExtractor:
    """Shells out to `claude -p ... --output-format json` on the Max plan.

    No Anthropic API key, no Anthropic SDK client -- per the Phase 2 prompt
    this MUST run through the local Claude Code CLI so it bills against the
    user's Max subscription.
    """

    name: str = "claude-cli"
    binary: Optional[str] = None  # resolved lazily
    extracted_by: str = "llm:claude-cli"

    def _resolve_binary(self) -> str:
        if self.binary and Path(self.binary).exists():
            return self.binary
        # Try the standard install location first (Windows native installer
        # drops the binary here), then fall back to whatever's on PATH.
        candidates = [
            os.environ.get("CLAUDE_CLI"),
            str(Path.home() / ".local" / "bin" / "claude.exe"),
            shutil.which("claude.exe"),
            shutil.which("claude"),
        ]
        for c in candidates:
            if c and Path(c).exists():
                self.binary = c
                return c
        raise RuntimeError(
            "Could not find the `claude` CLI. Install via "
            "`irm https://claude.ai/install.ps1 | iex` and run `claude login`, "
            "then either put it on PATH or set CLAUDE_CLI to its full path."
        )

    def extract(self, prompt: str) -> tuple[Optional[list[dict]], dict[str, Any]]:
        binary = self._resolve_binary()
        cmd = [binary, "-p", prompt, "--output-format", "json"]
        # IMPORTANT: stdin=DEVNULL avoids the "no stdin data received in 3s"
        # warning the CLI emits when run non-interactively.
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return None, {"error": "timeout", "extracted_by": self.extracted_by}
        if proc.returncode != 0:
            return None, {
                "error": f"exit={proc.returncode}",
                "stderr": (proc.stderr or "")[:500],
                "extracted_by": self.extracted_by,
            }
        envelope: dict[str, Any] = {}
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return None, {
                "error": f"envelope-json: {exc}",
                "stdout_head": (proc.stdout or "")[:500],
                "extracted_by": self.extracted_by,
            }
        if envelope.get("is_error"):
            return None, {
                "error": envelope.get("result", "is_error=true"),
                "extracted_by": self.extracted_by,
            }
        model_text = envelope.get("result", "")
        items = _extract_json_array(model_text)
        meta = {
            "duration_ms": envelope.get("duration_ms"),
            "model": envelope.get("model"),
            "extracted_by": self.extracted_by,
        }
        return items, meta


@dataclass
class GemmaExtractor:
    """Local Ollama fallback (Gemma 2 27B by default).

    Used only when EXTRACTOR=gemma. The prompt is the same; Ollama returns a
    JSON envelope with the model's text under `response`.
    """

    name: str = "gemma"
    model: str = "gemma2:27b"
    url: str = "http://localhost:11434/api/generate"
    extracted_by: str = "llm:gemma"

    def extract(self, prompt: str) -> tuple[Optional[list[dict]], dict[str, Any]]:
        try:
            resp = requests.post(
                self.url,
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=180,
            )
        except requests.RequestException as exc:
            return None, {"error": f"ollama-conn: {exc}", "extracted_by": self.extracted_by}
        if not resp.ok:
            return None, {
                "error": f"ollama-http-{resp.status_code}",
                "extracted_by": self.extracted_by,
            }
        body = resp.json()
        model_text = body.get("response", "")
        items = _extract_json_array(model_text)
        return items, {
            "duration_ms": body.get("total_duration", 0) // 1_000_000,
            "model": self.model,
            "extracted_by": self.extracted_by,
        }


def get_extractor() -> Extractor:
    name = (os.environ.get("EXTRACTOR") or "claude-cli").strip().lower()
    if name in {"claude", "claude-cli", "claude_cli"}:
        return ClaudeCLIExtractor()
    if name in {"gemma", "ollama"}:
        return GemmaExtractor()
    raise RuntimeError(
        f"Unknown EXTRACTOR={name!r}; expected one of: claude-cli, gemma"
    )


# ---------------------------------------------------------------------------
# Verify gate (invariant #4)
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Whitespace normalization used by BOTH sides of the substring check."""
    if not s:
        return ""
    # Replace common unicode whitespace + smart quotes with ascii equivalents.
    repl = {
        " ": " ", " ": " ", " ": " ", " ": " ",
        "–": "-", "—": "-",
        "‘": "'", "’": "'", "“": '"', "”": '"',
    }
    for k, v in repl.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s).strip()


def _target_in_snippet(target: str, snippet: str) -> bool:
    """Does the snippet mention the target (or a distinctive token of it)?"""
    if not target or not snippet:
        return False
    t = _normalize(target).lower()
    sn = _normalize(snippet).lower()
    if not t:
        return False
    if t in sn:
        return True
    # Fall back to the longest "distinctive" token (>=4 letters, not a stopword).
    stop = {
        "company", "companies", "corp", "corporation", "inc", "incorporated",
        "ltd", "limited", "plc", "holdings", "group", "the", "and",
    }
    tokens = [
        re.sub(r"[^a-z0-9\-]+", "", tk) for tk in t.split()
    ]
    distinctive = [tk for tk in tokens if len(tk) >= 4 and tk not in stop]
    return any(tk in sn for tk in distinctive)


def verify_grounding(snippet: str, target: str, full_text: str) -> bool:
    """Return True iff (a) the snippet is a substring of the filing's text and
    (b) the snippet mentions the target name (or a distinctive token of it)."""
    if not snippet or not full_text or not target:
        return False
    n_snip = _normalize(snippet)
    n_full = _normalize(full_text)
    if n_snip not in n_full:
        return False
    return _target_in_snippet(target, snippet)


# ---------------------------------------------------------------------------
# Segment gating (currently empty allowlist for Consumer Staples)
# ---------------------------------------------------------------------------

def load_conglomerates_allowlist(path: Path) -> set[str]:
    """Return the set of CIKs (canonical 'cik:...' ids) permitted to be decomposed."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = data.get("allowed_ciks", []) or []
    return {str(x).strip() for x in raw if x}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

CHECKPOINT_NAME = "extract_checkpoint.json"
# Append-only side-file: every verified LLM candidate gets written here as it
# is produced. The orchestrator reads this file on every run to assemble the
# final edges_raw.jsonl, so a re-run that skips all checkpointed pairs still
# emits the full graph (instead of just the rule edges). Delete this file
# alongside the checkpoint when you want a clean redo.
LLM_CANDIDATES_NAME = "_extract/llm_candidates.jsonl"


@dataclass
class LLMExtractionResult:
    candidates: list[CandidateEdge] = field(default_factory=list)
    counts_by_type: dict[str, int] = field(default_factory=dict)
    accepted: int = 0
    rejected: int = 0
    processed_pairs: int = 0
    skipped_pairs: int = 0


def _load_checkpoint(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {(d["cik"], d["section"]) for d in raw.get("done", []) if "cik" in d and "section" in d}
    except (json.JSONDecodeError, KeyError):
        log.warning("Could not parse checkpoint %s; starting fresh", path)
        return set()


def _save_checkpoint(path: Path, done: set[tuple[str, str]]) -> None:
    payload = {"done": [{"cik": c, "section": s} for c, s in sorted(done)]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _build_candidate(
    *,
    source_id: str,
    target_raw: str,
    edge_type: str,
    snippet: str,
    confidence: float,
    filing_accession: str,
    filing_url: str,
    extracted_by: str,
) -> CandidateEdge:
    if edge_type not in {"supplies", "competes_with"}:
        raise ValueError(f"unexpected edge type from LLM: {edge_type!r}")
    return CandidateEdge(
        source_id=source_id,
        target_raw=target_raw,
        type=EdgeType(edge_type),
        confidence=max(0.0, min(1.0, float(confidence))),
        provenance=Provenance(
            filing=filing_accession,
            url=filing_url,
            snippet=snippet,
            extracted_by=extracted_by,  # already in the widened Literal
        ),
        verified=True,
    )


def run_llm_extraction(
    *,
    companies: list[dict[str, Any]],
    section_results: list[SectionResult],
    data_root: Path,
    repo_root: Path,
    extractor: Optional[Extractor] = None,
    conglomerates_path: Optional[Path] = None,
) -> LLMExtractionResult:
    extractor = extractor or get_extractor()
    log.info("LLM extractor: %s", extractor.name)

    # Segment allowlist (empty for Consumer Staples by default; checked but
    # never acted upon unless a CIK is added to the yaml).
    cgl_path = conglomerates_path or (repo_root / "config" / "conglomerates.yaml")
    allowed_segments = load_conglomerates_allowlist(cgl_path)
    if allowed_segments:
        log.info("Conglomerate allowlist non-empty: %s", sorted(allowed_segments))
    else:
        log.info("Conglomerate allowlist empty -- minting zero Segment nodes this run")

    # Resolve company id -> filing accession/url + section text + cached .txt path
    sections_by_cik = {sr.cik: sr for sr in section_results}
    company_by_cik = {n["id"].split("cik:", 1)[1]: n for n in companies if n["id"].startswith("cik:")}

    ckpt_path = data_root / CHECKPOINT_NAME
    done = _load_checkpoint(ckpt_path)
    llm_side_path = data_root / LLM_CANDIDATES_NAME
    llm_side_path.parent.mkdir(parents=True, exist_ok=True)

    result = LLMExtractionResult()
    counts: dict[str, int] = {"supplies": 0, "competes_with": 0}

    # Carry forward candidates produced in earlier runs (Phase 2 prompt §B5:
    # the run is resumable). The orchestrator uses these when assembling
    # edges_raw.jsonl so a checkpoint-skip re-run still emits the full graph.
    if llm_side_path.exists():
        with llm_side_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ce = CandidateEdge.model_validate_json(line)
                except Exception as exc:
                    log.warning("Skipping malformed line in %s: %s", llm_side_path, exc)
                    continue
                result.candidates.append(ce)
                t = ce.type if isinstance(ce.type, str) else ce.type.value
                counts[t] = counts.get(t, 0) + 1
        log.info(
            "Loaded %d prior LLM candidates from %s", len(result.candidates), llm_side_path,
        )

    for cik_pad, node in company_by_cik.items():
        sr = sections_by_cik.get(cik_pad)
        if sr is None:
            log.info("No section result for %s; skipping LLM", cik_pad)
            continue
        filings = (node.get("metadata") or {}).get("filings") or []
        filing = filings[0] if filings else {}
        accession = filing.get("accession", "")
        filing_url = filing.get("url", "")
        local_path = filing.get("local_path", "")
        if not local_path:
            log.info("No filing for %s; skipping LLM", node.get("name"))
            continue
        htm = Path(local_path) if Path(local_path).is_absolute() else (repo_root / local_path)
        _, full_text = load_filing_text(htm)
        normalized_full = _normalize(full_text)

        for section_name, section_text in sr.sections.items():
            if not section_text:
                continue  # nothing to extract from
            key = (cik_pad, section_name)
            if key in done:
                result.skipped_pairs += 1
                continue
            prompt = PROMPT_TEMPLATE.format(
                company_name=node.get("name", node["id"]),
                section_name=section_name,
                section_text=section_text,
            )
            items, meta = _try_extract(extractor, prompt)
            if items is None:
                log.warning(
                    "Extractor failed for %s/%s (%s) -- not checkpointing, will retry next run",
                    node.get("name"), section_name, meta,
                )
                continue
            extracted_by = meta.get("extracted_by", "llm")
            for it in items:
                if not isinstance(it, dict):
                    result.rejected += 1
                    continue
                target = (it.get("target") or "").strip()
                edge_type = (it.get("type") or "").strip()
                snippet = (it.get("snippet") or "").strip()
                try:
                    confidence = float(it.get("confidence", 0.7))
                except (TypeError, ValueError):
                    confidence = 0.5
                if not target or not snippet or edge_type not in {"supplies", "competes_with"}:
                    result.rejected += 1
                    continue
                if target.strip().lower() == (node.get("name") or "").strip().lower():
                    # Source-as-target -- always wrong.
                    result.rejected += 1
                    continue
                # Verify against the FULL cached filing text, not just the
                # section chunk -- snippets may legitimately span a window.
                if not verify_grounding(snippet, target, normalized_full):
                    result.rejected += 1
                    log.debug(
                        "REJECTED %s -> %s (%s): not grounded in %s",
                        node.get("name"), target, edge_type, accession,
                    )
                    continue
                try:
                    ce = _build_candidate(
                        source_id=node["id"],
                        target_raw=target,
                        edge_type=edge_type,
                        snippet=snippet,
                        confidence=confidence,
                        filing_accession=accession,
                        filing_url=filing_url,
                        extracted_by=extracted_by,
                    )
                except Exception as exc:
                    result.rejected += 1
                    log.debug("Schema rejection: %s", exc)
                    continue
                result.candidates.append(ce)
                result.accepted += 1
                counts[edge_type] = counts.get(edge_type, 0) + 1
                # Persist immediately so a crash mid-run doesn't lose work.
                with llm_side_path.open("a", encoding="utf-8") as fh:
                    fh.write(ce.model_dump_json())
                    fh.write("\n")

            done.add(key)
            result.processed_pairs += 1
            _save_checkpoint(ckpt_path, done)

    result.counts_by_type = counts
    return result


def _try_extract(
    extractor: Extractor, prompt: str, retries: int = 1
) -> tuple[Optional[list[dict]], dict[str, Any]]:
    """Call the backend with one retry on malformed/empty output."""
    items, meta = extractor.extract(prompt)
    if items is not None:
        return items, meta
    for _ in range(retries):
        log.info("Extractor retry (%s)", meta.get("error"))
        items, meta = extractor.extract(prompt)
        if items is not None:
            return items, meta
    return None, meta
