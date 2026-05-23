"""Rule-extracted regulated_by edges from config/regulators.yaml.

This module owns Phase 2 Part A4 -- the deterministic, model-free portion of
extraction. For each Company node it computes:

    regulators(company) = _default
                        + sector[company.gics_sector]
                        + industry[resolved_gics_industry]

(de-duped by regulator id), and emits one CandidateEdge per regulator plus the
Regulator Node itself.

The GICS-level mismatch (Phase 2 gotcha #1) is reconciled by routing every
company's stored sub-industry through config/gics_subindustry_to_industry.yaml.
The loader raises KeyError on an unmapped sub-industry by design -- a silent
fallthrough would produce a company with zero industry-level regulators and
nobody would notice until Phase 5.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from schema.models import (
    CandidateEdge,
    EdgeType,
    Node,
    NodeType,
    Provenance,
)


log = logging.getLogger("extract.regulators")


# ---------------------------------------------------------------------------
# YAML loaders
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegulatorSpec:
    id: str
    name: str
    scope: str


@dataclass
class RegulatorsConfig:
    """In-memory view of config/regulators.yaml."""

    default: list[RegulatorSpec]
    by_sector: dict[str, list[RegulatorSpec]]
    by_industry: dict[str, list[RegulatorSpec]]
    source_path: Path
    source_url: str = (
        "https://github.com/kvmrbabu90/economy_network/blob/main/config/regulators.yaml"
    )


def _to_specs(raw: Iterable[dict]) -> list[RegulatorSpec]:
    specs: list[RegulatorSpec] = []
    for entry in raw or []:
        specs.append(
            RegulatorSpec(
                id=str(entry["id"]).strip(),
                name=str(entry["name"]).strip(),
                scope=str(entry.get("scope", "")).strip(),
            )
        )
    return specs


def load_regulators_config(path: Path) -> RegulatorsConfig:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RegulatorsConfig(
        default=_to_specs(data.get("_default") or []),
        by_sector={k: _to_specs(v) for k, v in (data.get("sector") or {}).items()},
        by_industry={k: _to_specs(v) for k, v in (data.get("industry") or {}).items()},
        source_path=path,
    )


def load_subindustry_map(path: Path) -> dict[str, str]:
    """Load the GICS Sub-Industry -> Industry rollup. Raises if file missing."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def resolve_gics_industry(
    sub_industry: Optional[str],
    sub_to_industry: dict[str, str],
    *,
    company_name: str = "<unknown>",
) -> str:
    """Resolve a stored GICS Sub-Industry to its GICS Industry. Fail loud on miss."""
    if not sub_industry:
        raise KeyError(
            f"company {company_name!r}: missing GICS sub-industry on node "
            "(populate `industry` field in companies.jsonl)"
        )
    if sub_industry not in sub_to_industry:
        raise KeyError(
            f"company {company_name!r}: GICS sub-industry {sub_industry!r} is not "
            "mapped in config/gics_subindustry_to_industry.yaml. Add it, then re-run."
        )
    return sub_to_industry[sub_industry]


# ---------------------------------------------------------------------------
# Per-company regulator computation
# ---------------------------------------------------------------------------

def regulators_for_company(
    *,
    gics_sector: Optional[str],
    gics_industry: str,
    cfg: RegulatorsConfig,
) -> list[RegulatorSpec]:
    """Apply the default+sector+industry stack, de-duped by regulator id."""
    pool: list[RegulatorSpec] = []
    pool.extend(cfg.default)
    if gics_sector and gics_sector in cfg.by_sector:
        pool.extend(cfg.by_sector[gics_sector])
    if gics_industry in cfg.by_industry:
        pool.extend(cfg.by_industry[gics_industry])
    seen: set[str] = set()
    deduped: list[RegulatorSpec] = []
    for spec in pool:
        if spec.id in seen:
            continue
        seen.add(spec.id)
        deduped.append(spec)
    return deduped


def regulator_node(spec: RegulatorSpec) -> Node:
    """Render a RegulatorSpec into a validated Regulator Node."""
    return Node(
        id=spec.id,
        type=NodeType.Regulator,
        name=spec.name,
        aliases=[spec.name],
        identifiers={},
        metadata={"scope": spec.scope, "extracted_by": "rule"},
    )


def regulated_by_candidate(
    *,
    source_id: str,
    spec: RegulatorSpec,
    gics_sector: Optional[str],
    gics_industry: str,
    source_path: Path,
) -> CandidateEdge:
    """Render a CandidateEdge: company --regulated_by--> regulator."""
    snippet = (
        f"regulators.yaml: sector={gics_sector!r} "
        f"industry={gics_industry!r} -> {spec.id} ({spec.name})"
    )
    return CandidateEdge(
        source_id=source_id,
        target_raw=spec.id,  # already a canonical "regulator:<slug>"
        type=EdgeType.regulated_by,
        confidence=1.0,
        provenance=Provenance(
            filing="",
            url="",
            snippet=snippet,
            extracted_by="rule",
        ),
        verified=True,  # rule-generated edges are verified by construction
    )


# ---------------------------------------------------------------------------
# Orchestration: companies.jsonl -> rule edges + regulator nodes
# ---------------------------------------------------------------------------

@dataclass
class RuleExtractResult:
    candidates: list[CandidateEdge]
    regulator_nodes: list[Node]
    per_company: dict[str, list[str]]  # source_id -> list of regulator ids
    resolved_industries: dict[str, str]  # source_id -> resolved GICS Industry


def extract_rule_edges(
    companies: list[dict[str, Any]],
    cfg: RegulatorsConfig,
    sub_to_industry: dict[str, str],
) -> RuleExtractResult:
    candidates: list[CandidateEdge] = []
    nodes_by_id: dict[str, Node] = {}
    per_company: dict[str, list[str]] = {}
    resolved_industries: dict[str, str] = {}

    for node in companies:
        cid = node["id"]
        name = node.get("name", "<unnamed>")
        sector = node.get("sector")
        sub_industry = node.get("industry")
        resolved = resolve_gics_industry(
            sub_industry, sub_to_industry, company_name=name
        )
        resolved_industries[cid] = resolved
        regs = regulators_for_company(
            gics_sector=sector, gics_industry=resolved, cfg=cfg
        )
        per_company[cid] = [r.id for r in regs]
        for spec in regs:
            if spec.id not in nodes_by_id:
                nodes_by_id[spec.id] = regulator_node(spec)
            candidates.append(
                regulated_by_candidate(
                    source_id=cid,
                    spec=spec,
                    gics_sector=sector,
                    gics_industry=resolved,
                    source_path=cfg.source_path,
                )
            )

    return RuleExtractResult(
        candidates=candidates,
        regulator_nodes=list(nodes_by_id.values()),
        per_company=per_company,
        resolved_industries=resolved_industries,
    )


def write_regulator_nodes_jsonl(nodes: Iterable[Node], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for node in nodes:
            fh.write(node.model_dump_json())
            fh.write("\n")
            n += 1
    return n


def append_candidates_jsonl(candidates: Iterable[CandidateEdge], out_path: Path) -> int:
    """Append one JSON line per candidate. Caller controls truncate vs append."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("a", encoding="utf-8") as fh:
        for ce in candidates:
            fh.write(ce.model_dump_json())
            fh.write("\n")
            n += 1
    return n
