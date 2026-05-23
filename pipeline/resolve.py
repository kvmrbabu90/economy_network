"""Phase 3 resolution: CandidateEdges -> validated Edges with canonical ids.

Pure code, no model calls. The job is string matching, an alias table, and
graph assembly. This is the phase where the "one node, many viewpoints" rule
becomes operational -- the Walmart fragments ("Walmart Inc." / "Walmart,
Inc." / "Wal-Mart Stores, Inc." / "Walmart Stores, Inc." / "Walmart") all
collapse to a single canonical CIK, and the single-node invariant is
asserted in code at the end of the run.

Resolution rules (per docs/PHASE3_PROMPT.md):

  1. regulated_by edges: target is already a canonical regulator: id; pass through.
  2. Exact normalized match against registry+alias table -> auto-match.
  3. Fuzzy match (rapidfuzz token_set_ratio):
       - exactly 1 candidate >= HIGH_BAR  -> auto-match
       - 2+ candidates >= HIGH_BAR OR best in 1..HIGH_BAR-1 LOW_BAR..HIGH_BAR-1
         band -> WRITE TO REVIEW QUEUE, drop from main graph
  4. No match -> mint a provisional slug: node. The edge is kept but its
     confidence is capped (so a strict cutoff naturally moves it to the
     below-threshold audit file, off the main graph).

Outputs:
    data/nodes.jsonl                  -- all canonical Nodes
    data/edges.jsonl                  -- validated Edges (above cutoff)
    data/aliases.jsonl                -- alias_normalized -> canonical_id
    data/review_queue.jsonl           -- ambiguous matches awaiting review
    data/edges_below_threshold.jsonl  -- audit-keep below the cutoff
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from rapidfuzz import fuzz, process

from schema.models import (
    CandidateEdge,
    Edge,
    EdgeType,
    Node,
    NodeType,
    Provenance,
)


log = logging.getLogger("resolve")


# ---------------------------------------------------------------------------
# Normalization (Part A2 -- the backbone)
# ---------------------------------------------------------------------------

# Suffix tokens stripped from BOTH ends of a name before comparison. These are
# generic legal / structural words that say nothing about which company is
# meant. Kept conservative -- we strip "company" but not "wine" or "media".
_SUFFIX_TOKENS = {
    "inc", "incorporated", "incorp",
    "corp", "corporation", "corporated",
    "company", "companies", "co", "cos",
    "ltd", "limited",
    "plc",
    "sa", "s.a", "s.a.",
    "gmbh", "ag", "kgaa",
    "nv", "n.v", "n.v.", "bv", "b.v",
    "lp", "llp", "llc",
    "holdings", "holding",
    "group",
    "the",
    "brands",
    "stores", "store",
    "enterprises", "enterprise",
}

# Single-pass character cleanup. Anything outside [a-z0-9\s\-] becomes space.
_NON_TOKEN_RE = re.compile(r"[^a-z0-9\s\-]+")
_DASH_RUN_RE = re.compile(r"[-_]+")
_WS_RE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Map any name string to a canonical normalized form.

    Used everywhere -- exact registry lookups, fuzzy match input, alias-table
    keys, slug minting. Must be deterministic and idempotent: normalize(x) ==
    normalize(normalize(x)).
    """
    if not name:
        return ""
    # NFKD decomposition + ASCII fold removes accents (Nestle vs Nestlé).
    s = unicodedata.normalize("NFKD", str(name))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = s.replace("&", " and ")
    # Collapse compound legal-suffix abbreviations like "S.A." / "N.V." /
    # "A.G." into bare tokens ("sa" / "nv" / "ag") BEFORE general punctuation
    # stripping. Without this, the period-to-space conversion below splits
    # "s.a." into two tokens "s" and "a", neither of which is in the suffix
    # filter list -- so "Nestlé S.A." would end up as "nestle s a" instead of
    # "nestle". A simple two-letter abbreviation cover is enough for our data
    # (Nestlé S.A., Danone S.A., N.V., A.G., ...).
    s = re.sub(r"\b([a-z])\.([a-z])\b\.?", r"\1\2", s)
    s = _NON_TOKEN_RE.sub(" ", s)
    s = _DASH_RUN_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    tokens = s.split()
    while tokens and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    while tokens and tokens[0] in _SUFFIX_TOKENS:
        tokens.pop(0)
    out = " ".join(tokens)
    # Hardcoded compound-name fixup: "Wal Mart" -> "Walmart". The dash strip
    # earlier turns "Wal-Mart" into "wal mart"; without this two-token form
    # would never match the single-token "Walmart". This is the ONLY name-
    # specific fixup -- everything else stays general.
    out = out.replace("wal mart", "walmart")
    return out


def to_slug(normalized: str) -> str:
    """Convert a normalized name (lowercase, spaces) to a kebab-case slug body.

    Output passes the schema's slug-id body regex (^[a-z0-9][a-z0-9\\-]*$).
    """
    s = re.sub(r"[\s_]+", "-", normalized).strip("-")
    s = re.sub(r"[^a-z0-9\-]", "", s)
    s = re.sub(r"-+", "-", s)
    return s


# ---------------------------------------------------------------------------
# Registry + alias table (Part A1)
# ---------------------------------------------------------------------------

@dataclass
class RegistryEntry:
    canonical_id: str
    normalized_name: str
    raw_name: str
    type: str


@dataclass
class Registry:
    entries: list[RegistryEntry] = field(default_factory=list)
    # normalized_alias -> canonical_id
    by_norm: dict[str, str] = field(default_factory=dict)
    nodes_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    aliases: list[dict[str, str]] = field(default_factory=list)
    # Sources that produced an alias collision (for diagnostics).
    collisions: list[tuple[str, str, str]] = field(default_factory=list)

    def add_node(self, node: dict[str, Any]) -> None:
        nid = node["id"]
        if nid in self.nodes_by_id:
            return
        self.nodes_by_id[nid] = node
        self.entries.append(
            RegistryEntry(
                canonical_id=nid,
                normalized_name=normalize(node.get("name", "")),
                raw_name=node.get("name", ""),
                type=node.get("type", "Company"),
            )
        )

    def add_alias(
        self, alias: str, canonical_id: str, source: str, *, strict: bool = False
    ) -> bool:
        """Record an alias->canonical mapping. Returns True if added/already present."""
        n = normalize(alias)
        if not n:
            return False
        existing = self.by_norm.get(n)
        if existing is not None and existing != canonical_id:
            # Cross-canonical collision -- the very thing the invariant check
            # is meant to surface. Record it but do not silently overwrite.
            self.collisions.append((n, existing, canonical_id))
            if strict:
                raise ValueError(
                    f"Alias collision: normalized {n!r} -> both {existing} and {canonical_id}"
                )
            return False
        self.by_norm[n] = canonical_id
        self.aliases.append(
            {
                "alias": alias,
                "alias_normalized": n,
                "canonical_id": canonical_id,
                "source": source,
            }
        )
        return True

    def lookup(self, name: str) -> Optional[str]:
        return self.by_norm.get(normalize(name))


def seed_registry(companies: list[dict], regulators: list[dict]) -> Registry:
    """Build a Registry from the canonical Phase 1 + Phase 2 nodes."""
    reg = Registry()
    for n in companies + regulators:
        reg.add_node(n)
    for n in companies + regulators:
        nid = n["id"]
        reg.add_alias(n.get("name", ""), nid, source="seed:name")
        for a in n.get("aliases", []) or []:
            reg.add_alias(a, nid, source="seed:alias")
        for t in n.get("tickers", []) or []:
            reg.add_alias(t, nid, source="seed:ticker")
        # Also try the legal-suffix-stripped form of the registered name --
        # e.g. "The Kraft Heinz Company" -> "kraft heinz". This is what
        # downstream extractor outputs will mostly look like.
        reg.add_alias(normalize(n.get("name", "")), nid, source="seed:normalized")
    return reg


# ---------------------------------------------------------------------------
# Resolution (Part B)
# ---------------------------------------------------------------------------

# Token-set ratio thresholds. ">= HIGH_BAR" means a confident match; the
# 75..HIGH_BAR band is the "ambiguous" zone where we queue rather than guess.
HIGH_BAR = 90
LOW_BAR = 75


@dataclass
class ResolutionDecision:
    target_id: str
    action: str  # "exact" | "fuzzy-auto" | "queued" | "minted-slug"
    confidence_cap: Optional[float] = None  # cap to apply to the edge's confidence
    suggestions: list[tuple[str, int]] = field(default_factory=list)  # (canonical_id, score)


@dataclass
class ReviewItem:
    target_raw: str
    target_normalized: str
    source_id: str
    source_filing: str
    edge_type: str
    suggestions: list[dict[str, Any]]  # [{canonical_id, name, score}]


# Provisional slug-target edges are capped so a strict edges.jsonl cutoff
# moves them off the main graph automatically (per Phase 3 prompt §C3 note).
PROVISIONAL_CONFIDENCE_CAP = 0.5


def _cap_for_target(canonical_id: str, registry: Registry) -> Optional[float]:
    """Return PROVISIONAL_CONFIDENCE_CAP iff the target is a provisional slug.

    Applied uniformly to every resolution path (exact / fuzzy / mint) so that
    a slug-target edge carries the same low confidence no matter how the
    matcher arrived at the slug. Without this, the second time a name like
    "Nestlé" appeared the alias would hit B2's exact path and ESCAPE the cap.
    """
    if not canonical_id.startswith("slug:"):
        return None
    node = registry.nodes_by_id.get(canonical_id)
    if not node:
        return None
    meta = node.get("metadata") if isinstance(node, dict) else getattr(node, "metadata", None)
    meta = meta or {}
    return PROVISIONAL_CONFIDENCE_CAP if meta.get("provisional") else None


def _fuzzy_top_matches(target_norm: str, registry: Registry, *, limit: int = 5):
    """Return up to `limit` (canonical_id, score, normalized_name) by score."""
    # process.extract returns list of (choice, score, key) where choice is the
    # value from `choices` and key is the dict key (the canonical id here).
    if not target_norm:
        return []
    choices = {e.canonical_id: e.normalized_name for e in registry.entries}
    if not choices:
        return []
    return process.extract(
        target_norm, choices, scorer=fuzz.token_set_ratio, limit=limit
    )


def resolve_target(
    candidate: CandidateEdge,
    registry: Registry,
    *,
    review: list[ReviewItem],
) -> Optional[ResolutionDecision]:
    """Resolve a CandidateEdge's target_raw to a canonical id.

    Returns None if the edge should be dropped (queued for review). Otherwise
    a ResolutionDecision the caller uses to build the final Edge.
    """
    target_raw = candidate.target_raw
    candidate_type = candidate.type if isinstance(candidate.type, str) else candidate.type.value

    # B1: regulated_by edges already carry the canonical id.
    if candidate_type == EdgeType.regulated_by.value:
        return ResolutionDecision(target_id=target_raw, action="exact")

    # B2: exact normalized lookup against the alias table.
    exact = registry.lookup(target_raw)
    if exact:
        return ResolutionDecision(
            target_id=exact,
            action="exact",
            confidence_cap=_cap_for_target(exact, registry),
        )

    # B3: fuzzy match.
    target_norm = normalize(target_raw)
    matches = _fuzzy_top_matches(target_norm, registry)
    above_high = [m for m in matches if m[1] >= HIGH_BAR]
    if len(above_high) == 1:
        canonical_id = above_high[0][2]
        # Persist the new alias so re-runs are deterministic and the alias
        # table grows monotonically -- single-node invariant earns its keep.
        registry.add_alias(target_raw, canonical_id, source="resolve:fuzzy-auto")
        return ResolutionDecision(
            target_id=canonical_id, action="fuzzy-auto",
            confidence_cap=_cap_for_target(canonical_id, registry),
            suggestions=[(m[2], int(m[1])) for m in above_high],
        )
    if len(above_high) >= 2:
        review.append(_build_review_item(candidate, target_raw, target_norm, above_high))
        return None
    # Ambiguous band: 1+ matches in [LOW_BAR, HIGH_BAR).
    band = [m for m in matches if LOW_BAR <= m[1] < HIGH_BAR]
    if band:
        review.append(_build_review_item(candidate, target_raw, target_norm, band))
        return None

    # B4: no match -> mint provisional slug node.
    slug_body = to_slug(target_norm) or to_slug(normalize(target_raw))
    if not slug_body:
        # Pathological: name normalizes to empty after suffix stripping.
        review.append(_build_review_item(candidate, target_raw, target_norm, matches[:3]))
        return None
    canonical_id = f"slug:{slug_body}"
    if canonical_id not in registry.nodes_by_id:
        accession = candidate.provenance.filing
        node = Node(
            id=canonical_id,
            type=NodeType.Company,
            name=str(target_raw).strip(),
            aliases=[str(target_raw).strip()],
            identifiers={},
            metadata={
                "provisional": True,
                "identity_unverified": True,
                "first_seen_filing": accession,
            },
        )
        registry.add_node(node.model_dump())
        registry.entries.append(
            RegistryEntry(
                canonical_id=canonical_id,
                normalized_name=target_norm,
                raw_name=str(target_raw),
                type="Company",
            )
        )
        registry.add_alias(target_raw, canonical_id, source="mint:slug")
        registry.add_alias(target_norm, canonical_id, source="mint:slug-normalized")
    else:
        # Repeat name -- single-node invariant means we reuse the existing slug.
        registry.add_alias(target_raw, canonical_id, source="mint:slug-reuse")
    return ResolutionDecision(
        target_id=canonical_id,
        action="minted-slug",
        confidence_cap=PROVISIONAL_CONFIDENCE_CAP,
    )


def _build_review_item(
    candidate: CandidateEdge,
    target_raw: str,
    target_norm: str,
    matches,
) -> ReviewItem:
    return ReviewItem(
        target_raw=target_raw,
        target_normalized=target_norm,
        source_id=candidate.source_id,
        source_filing=candidate.provenance.filing,
        edge_type=candidate.type if isinstance(candidate.type, str) else candidate.type.value,
        suggestions=[
            {"canonical_id": m[2], "matched_normalized": m[0], "score": int(m[1])}
            for m in matches
        ],
    )


# ---------------------------------------------------------------------------
# Edge construction + competes_with dedupe (Part C)
# ---------------------------------------------------------------------------

def _build_edge(
    candidate: CandidateEdge,
    source_id: str,
    target_id: str,
    decision: ResolutionDecision,
) -> Edge:
    confidence = float(candidate.confidence)
    if decision.confidence_cap is not None:
        confidence = min(confidence, decision.confidence_cap)
    edge_type = candidate.type if isinstance(candidate.type, str) else candidate.type.value
    directed = True  # supplies, regulated_by are directed; competes_with treated as undirected post-dedupe
    return Edge(
        source=source_id,
        target=target_id,
        type=EdgeType(edge_type),
        directed=directed,
        confidence=max(0.0, min(1.0, confidence)),
        provenance=candidate.provenance,
    )


def dedupe_directed(edges: list[Edge]) -> tuple[list[Edge], int, int]:
    """Collapse duplicate directed edges sharing the SAME (source, target, type).

    Two raw candidates can land here when one filer names the same target via
    multiple aliases (e.g. "Walmart Inc." and "Walmart Stores, Inc." both
    resolve to cik:0000104169, both as supplies). Without this pass the
    SQLite UNIQUE index on (source, target, type) would fire at load time.
    Keeps the highest-confidence row; folds the others' provenance into
    additional_provenance.
    """
    competes: list[Edge] = []
    by_triple: dict[tuple[str, str, str], Edge] = {}
    before_directed = 0
    for e in edges:
        et = e.type if isinstance(e.type, str) else e.type.value
        if et == EdgeType.competes_with.value:
            competes.append(e)
            continue
        before_directed += 1
        key = (e.source, e.target, et)
        existing = by_triple.get(key)
        if existing is None:
            by_triple[key] = e
            continue
        if e.confidence > existing.confidence:
            primary, other = e, existing
        else:
            primary, other = existing, e
        merged_extra = list(primary.additional_provenance)
        merged_extra.append(other.provenance)
        merged_extra.extend(other.additional_provenance)
        by_triple[key] = Edge(
            id=primary.id,
            source=primary.source,
            target=primary.target,
            type=EdgeType(et),
            directed=True,
            confidence=max(primary.confidence, other.confidence),
            provenance=primary.provenance,
            additional_provenance=merged_extra,
        )
    return competes + list(by_triple.values()), before_directed, len(by_triple)


def dedupe_competes_with(edges: list[Edge]) -> tuple[list[Edge], int, int]:
    """Collapse competes_with edges sharing an unordered pair.

    Keeps the highest-confidence row as the primary, folds the others'
    provenance into `additional_provenance`. Returns (deduped, before, after).
    """
    other: list[Edge] = []
    by_pair: dict[frozenset[str], Edge] = {}
    before_competes = 0
    for e in edges:
        et = e.type if isinstance(e.type, str) else e.type.value
        if et != EdgeType.competes_with.value:
            other.append(e)
            continue
        before_competes += 1
        key = frozenset({e.source, e.target})
        existing = by_pair.get(key)
        if existing is None:
            by_pair[key] = e
            continue
        # Merge. Keep the higher-confidence Edge as primary, fold the other's
        # provenance (and any additional provenance it already carries) into
        # the primary's additional_provenance list.
        if e.confidence > existing.confidence:
            new_primary = e
            other_edge = existing
        else:
            new_primary = existing
            other_edge = e
        merged_extra = list(new_primary.additional_provenance)
        merged_extra.append(other_edge.provenance)
        merged_extra.extend(other_edge.additional_provenance)
        # Edge is immutable in Pydantic by default. Rebuild.
        by_pair[key] = Edge(
            id=new_primary.id,
            source=new_primary.source,
            target=new_primary.target,
            type=EdgeType.competes_with,
            directed=False,  # treat undirected after dedupe
            confidence=max(new_primary.confidence, other_edge.confidence),
            provenance=new_primary.provenance,
            additional_provenance=merged_extra,
        )
    deduped_competes = list(by_pair.values())
    return other + deduped_competes, before_competes, len(deduped_competes)


# ---------------------------------------------------------------------------
# I/O + orchestrator
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: Iterable[Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            if hasattr(r, "model_dump_json"):
                fh.write(r.model_dump_json())
            elif isinstance(r, dict):
                fh.write(json.dumps(r))
            else:
                fh.write(json.dumps(r.__dict__ if hasattr(r, "__dict__") else r))
            fh.write("\n")
            n += 1
    return n


def confidence_histogram(confidences: list[float]) -> list[tuple[float, float, int]]:
    """Return list of (low_inclusive, high_exclusive, count) in 0.1 bands."""
    bands: list[tuple[float, float, int]] = []
    bins = [round(0.1 * i, 1) for i in range(11)]
    for low, high in zip(bins, bins[1:] + [1.0001]):
        count = sum(1 for c in confidences if low <= c < high)
        bands.append((low, min(high, 1.0), count))
    return bands


def assert_single_node_invariant(registry: Registry) -> tuple[bool, list[tuple[str, str, str]]]:
    """Walk the alias table; assert no normalized alias maps to two ids."""
    norm_to_ids: dict[str, set[str]] = defaultdict(set)
    for alias in registry.aliases:
        norm_to_ids[alias["alias_normalized"]].add(alias["canonical_id"])
    collisions = [
        (n, *sorted(ids)) for n, ids in norm_to_ids.items() if len(ids) > 1
    ]
    return (not collisions), collisions


@dataclass
class ResolveReport:
    candidates_in: int
    edges_built: int
    edges_above_threshold: int
    edges_below_threshold: int
    queued: int
    auto_exact: int
    auto_fuzzy: int
    provisional_slugs: int
    nodes_total: int
    competes_before_dedupe: int
    competes_after_dedupe: int
    histogram: list[tuple[float, float, int]]
    walmart_collapse_id: Optional[str]
    walmart_fragments_resolved: list[str]
    invariant_pass: bool
    invariant_collisions: list[tuple[str, str, str]]
    cutoff: float


def run_resolve(
    *,
    data_root: Path,
    cutoff: float,
) -> ResolveReport:
    companies = load_jsonl(data_root / "companies.jsonl")
    regulators = load_jsonl(data_root / "regulator_nodes.jsonl")
    candidates_raw = load_jsonl(data_root / "edges_raw.jsonl")
    candidates: list[CandidateEdge] = [
        CandidateEdge.model_validate(c) for c in candidates_raw
    ]
    log.info(
        "Loaded %d companies, %d regulators, %d candidates",
        len(companies), len(regulators), len(candidates),
    )

    registry = seed_registry(companies, regulators)
    log.info(
        "Seeded registry: %d nodes, %d aliases", len(registry.entries), len(registry.aliases),
    )

    review: list[ReviewItem] = []
    edges: list[Edge] = []
    actions = Counter()
    walmart_targets: list[str] = []
    walmart_id: Optional[str] = None

    for ce in candidates:
        decision = resolve_target(ce, registry, review=review)
        if decision is None:
            actions["queued"] += 1
            continue
        actions[decision.action] += 1
        # Track Walmart collapse for the acceptance report.
        if "walmart" in normalize(ce.target_raw) or "wal mart" in normalize(ce.target_raw):
            walmart_targets.append(ce.target_raw)
            walmart_id = decision.target_id
        try:
            edge = _build_edge(ce, ce.source_id, decision.target_id, decision)
        except Exception as exc:
            log.warning("Edge construction failed for %s -> %s: %s",
                        ce.source_id, decision.target_id, exc)
            continue
        edges.append(edge)

    log.info("Resolution actions: %s", dict(actions))
    log.info("Edges built before dedupe: %d", len(edges))

    edges, before_cw, after_cw = dedupe_competes_with(edges)
    log.info("competes_with dedupe: %d -> %d", before_cw, after_cw)
    # Directed-edge triple dedupe (supplies + regulated_by). Catches the
    # case where a filer's aliases for the same target both surface as
    # candidates (e.g. "Walmart Inc." and "Walmart Stores, Inc.").
    edges, before_dir, after_dir = dedupe_directed(edges)
    log.info("directed dedupe (supplies/regulated_by): %d -> %d", before_dir, after_dir)

    # Threshold
    above, below = [], []
    for e in edges:
        if e.confidence >= cutoff:
            above.append(e)
        else:
            below.append(e)
    log.info(
        "Confidence cutoff %.2f: %d above, %d below", cutoff, len(above), len(below),
    )

    invariant_ok, collisions = assert_single_node_invariant(registry)
    if not invariant_ok:
        log.error("Single-node invariant FAILED: %d collisions", len(collisions))
        for n, *ids in collisions[:10]:
            log.error("  alias %r maps to %s", n, ids)

    # Write outputs
    write_jsonl(data_root / "nodes.jsonl", registry.nodes_by_id.values())
    write_jsonl(data_root / "edges.jsonl", above)
    write_jsonl(data_root / "edges_below_threshold.jsonl", below)
    write_jsonl(data_root / "aliases.jsonl", registry.aliases)
    write_jsonl(data_root / "review_queue.jsonl",
                [r.__dict__ for r in review])

    histogram = confidence_histogram([e.confidence for e in edges])

    return ResolveReport(
        candidates_in=len(candidates),
        edges_built=len(edges),
        edges_above_threshold=len(above),
        edges_below_threshold=len(below),
        queued=actions["queued"],
        auto_exact=actions["exact"],
        auto_fuzzy=actions["fuzzy-auto"],
        provisional_slugs=actions["minted-slug"],
        nodes_total=len(registry.nodes_by_id),
        competes_before_dedupe=before_cw,
        competes_after_dedupe=after_cw,
        histogram=histogram,
        walmart_collapse_id=walmart_id,
        walmart_fragments_resolved=sorted(set(walmart_targets)),
        invariant_pass=invariant_ok,
        invariant_collisions=collisions,
        cutoff=cutoff,
    )


def print_report(report: ResolveReport) -> None:
    print("=" * 72)
    print("Phase 3 resolution report")
    print("=" * 72)
    print(f"Candidates in:                         {report.candidates_in}")
    print(f"Edges built (post dedupe):             {report.edges_built}")
    print(f"  -- above cutoff ({report.cutoff:.2f}):   {report.edges_above_threshold}")
    print(f"  -- below cutoff:                     {report.edges_below_threshold}")
    print(f"Queued for review:                     {report.queued}")
    print()
    print(f"Auto-matched (exact):                  {report.auto_exact}")
    print(f"Auto-matched (fuzzy):                  {report.auto_fuzzy}")
    print(f"Provisional slug nodes minted:         {report.provisional_slugs}")
    print(f"Total canonical nodes:                 {report.nodes_total}")
    print()
    print(f"competes_with dedupe: {report.competes_before_dedupe} -> {report.competes_after_dedupe}")
    print()
    print("Confidence histogram (post-dedupe, all edges, 0.1 bands):")
    for low, high, count in report.histogram:
        bar = "#" * min(60, count)
        print(f"  [{low:.1f} .. {high:.1f})  {count:>4}  {bar}")
    print()
    print(f"Walmart collapse: {len(report.walmart_fragments_resolved)} fragments -> {report.walmart_collapse_id!r}")
    for frag in report.walmart_fragments_resolved:
        print(f"  - {frag!r}")
    print()
    msg = "PASS" if report.invariant_pass else f"FAIL ({len(report.invariant_collisions)} collisions)"
    print(f"Single-node invariant: {msg}")
    if not report.invariant_pass:
        for n, *ids in report.invariant_collisions[:10]:
            print(f"  alias {n!r} maps to {ids}")
    print("=" * 72)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EconGraph Phase 3 resolution")
    parser.add_argument("--data-root", default="data")
    parser.add_argument(
        "--cutoff",
        type=float,
        default=0.75,
        help="Strict confidence cutoff for edges.jsonl (default 0.75; first "
             "run prints the histogram so you can lock the value).",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    report = run_resolve(
        data_root=Path(args.data_root),
        cutoff=args.cutoff,
    )
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
