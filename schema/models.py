"""Pydantic v2 models for EconGraph nodes and edges.

Mirrors the data model in docs/PRD.md §4 exactly. See CLAUDE.md for the hard
invariants — in particular: no `customer_of` edge type (it is derived at query
time by reversing `supplies`), every Edge must carry provenance, and every Node
id must use the canonical lowercase-prefixed format.
"""

from __future__ import annotations

import re
import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NodeType(str, Enum):
    Company = "Company"
    Commodity = "Commodity"
    Material = "Material"
    Region = "Region"
    Regulator = "Regulator"
    # Segment: a distinct operating business inside a single SEC filer
    # (e.g. Alphabet -> "Google Cloud"; Berkshire -> "BNSF"). Schema headroom
    # only in Phase 1; Phase 2 decides per-company whether to mint these.
    Segment = "Segment"


class EdgeType(str, Enum):
    # NOTE: no `customer_of` — it is derived from `supplies` at query time
    # (invariant #2 in CLAUDE.md). Storing it would duplicate state.
    supplies = "supplies"
    competes_with = "competes_with"
    regulated_by = "regulated_by"
    # part_of: a Segment node belongs to its parent Company filer. Edge runs
    # FROM the seg: source TO the cik: parent. Phase 1 declares this edge type
    # but populates ZERO part_of edges; Phase 2 owns segment extraction.
    part_of = "part_of"


# Canonical ID prefixes per CLAUDE.md §8 (+ "seg:" added in Phase 1 for
# operating-segment nodes — see NodeType.Segment).
#
# Segment ID format: ``seg:cik<10-digit-parent-cik>:<slug>``
#   e.g. ``seg:cik0000080424:fabric-care``  (parent CIK embedded, no colon
#   between "cik" and the digits so the canonical-ID regex stays single-colon
#   prefixed). The parent CIK is duplicated in the id for human readability
#   and joinability without a lookup; the authoritative parent link is the
#   ``part_of`` edge to ``cik:<parent>``.
_ID_PREFIXES = ("cik:", "wikidata:", "slug:", "regulator:", "seg:", "commodity:", "region:")

# Prefix-specific body shapes. Keep each strict so a malformed body fails
# validation rather than silently passing.
_ID_BODY_RE = {
    "cik": re.compile(r"^\d{10}$"),
    "wikidata": re.compile(r"^Q\d+$"),
    "slug": re.compile(r"^[a-z0-9][a-z0-9\-]*$"),
    "regulator": re.compile(r"^[a-z0-9][a-z0-9\-]*$"),
    # seg body: "cik<10-digit-parent>:<lower-kebab-slug>"
    "seg": re.compile(r"^cik\d{10}:[a-z0-9][a-z0-9\-]*$"),
    "commodity": re.compile(r"^[a-z0-9][a-z0-9\-]*$"),
    "region": re.compile(r"^[a-z0-9][a-z0-9\-]*$"),
}


def _validate_canonical_id(value: str, field: str = "id") -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if not value.startswith(_ID_PREFIXES):
        raise ValueError(
            f"{field} {value!r} must start with one of {_ID_PREFIXES} "
            "(canonical-ID format per CLAUDE.md §8)"
        )
    prefix, _, body = value.partition(":")
    body_re = _ID_BODY_RE.get(prefix)
    if body_re is None or not body_re.match(body):
        raise ValueError(
            f"{field} {value!r} is not a well-formed canonical ID for prefix "
            f"{prefix!r} (expected pattern: {body_re.pattern if body_re else 'unknown'})"
        )
    return value


class Node(BaseModel):
    """One row per economic entity. Never duplicated across vantage points."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: str
    type: NodeType
    name: str
    aliases: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)
    identifiers: dict[str, Any] = Field(default_factory=dict)
    sector: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _validate_canonical_id(v, "Node.id")

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Node.name must be a non-empty string")
        return v


class Provenance(BaseModel):
    """Source citation for every edge. No edge enters the graph without one.

    `snippet` and `extracted_by` are always required. `filing` and `url` are
    required for LLM- and manual-extracted edges (where they identify the
    actual SEC filing the snippet was lifted from) but may be empty strings
    for rule-extracted edges generated from local config (e.g. the
    regulated_by edges minted from config/regulators.yaml have no filing).
    """

    model_config = ConfigDict(extra="forbid")

    filing: str
    url: str
    snippet: str
    # Phase 2 widens this Literal — Phase 1 only had llm/rule/manual. The
    # llm-* variants record WHICH model produced the candidate (claude-cli on
    # the Max subscription, or a local gemma fallback). The legacy "llm" value
    # stays accepted for the Phase 0 fixtures.
    extracted_by: Literal[
        "llm",
        "llm:claude-cli",
        "llm:gemma",
        "rule",
        "manual",
        # Phase 7+ additions: deterministic derivations and external enrichment.
        # `inference:co-mention` -- a co-mention closure expansion of an LLM
        #   snippet that named >=3 competitors together. Same snippet as
        #   provenance, lower confidence cap, capped low so the strict 0.75
        #   cutoff keeps them in the audit layer by default.
        # `inference:gics-peer` -- reserved for the future GICS-sub-industry
        #   peer-defaulting rule. Not used yet.
        # `wikidata` -- pulled from Wikidata's competitor (P1830) and
        #   supplier (P:1071) relations; carries the source Q-id.
        "inference:co-mention",
        "inference:gics-peer",
        "wikidata",
        # `manual:curation` -- hand-curated edges whose source is widely-known
        #   public information (e.g. supply-chain relationships from press
        #   releases, annual reports, or analyst coverage) rather than a
        #   specific SEC filing. No filing/url required; snippet is the
        #   human-readable justification.
        "manual:curation",
    ]

    @field_validator("snippet")
    @classmethod
    def _snippet_non_empty(cls, v: str) -> str:
        # Snippet is the grounding artifact (CLAUDE.md invariant #4) — it must
        # exist. filing/url may be empty (rule-extracted edges have no filing).
        if not v or not v.strip():
            raise ValueError("Provenance.snippet must be non-empty")
        return v

    @model_validator(mode="after")
    def _check_filing_url_required_for_evidence(self) -> "Provenance":
        # Edges whose evidence is an external document (an SEC filing or a
        # Wikidata entity page) must carry both filing + url so the grounding
        # check has something to bite on. Rule-derived and gics-peer edges
        # have no external source -- they're justified by a config or by
        # GICS membership and may carry empty filing/url.
        REQUIRES_SOURCE = {
            "llm",
            "llm:claude-cli",
            "llm:gemma",
            "manual",
            "inference:co-mention",  # the LLM snippet IS the evidence; carry it
            "wikidata",              # Q-id URL is the evidence
        }
        if self.extracted_by in REQUIRES_SOURCE:
            if not self.filing or not self.filing.strip():
                raise ValueError(
                    f"Provenance.filing required when extracted_by={self.extracted_by!r}"
                )
            if not self.url or not self.url.strip():
                raise ValueError(
                    f"Provenance.url required when extracted_by={self.extracted_by!r}"
                )
        return self


class Edge(BaseModel):
    """Typed, directed relationship with required provenance."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str
    target: str
    type: EdgeType
    directed: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: Provenance
    # Phase 3 adds this: a competes_with edge can be supported by multiple
    # filings (both sides named each other). The dedupe step on the unordered
    # pair collapses them to ONE Edge row, keeping the highest-confidence
    # provenance as `provenance` and folding the others into this list.
    # Default empty; no impact on Phase 0-2 fixtures.
    additional_provenance: list[Provenance] = Field(default_factory=list)
    weight: Optional[float] = None
    # Phase K: source quality tier — set by pipeline/score_confidence.py.
    # Controls confidence floor in impact propagation and display in the UI.
    # "sec_explicit"  -- named company + explicit % in SEC filing
    # "sec_inferred"  -- mentioned in SEC filing, no explicit %
    # "manual"        -- hand-curated (manual:curation extracted_by)
    # "wikidata"      -- Wikidata SPARQL / P1830 competitor data
    # "wikipedia"     -- Wikipedia LLM extraction
    source_tier: Optional[Literal[
        "sec_explicit", "sec_inferred", "manual", "wikidata", "wikipedia"
    ]] = None
    # Phase E: geographic scope of a supply edge.  Controlled vocabulary:
    #   "US"     -- extracted from a 10-K filing (implicitly US-scoped)
    #   "global" -- Wikidata / Wikipedia source (no inherent geography)
    #   ISO-2    -- single-country scope when explicitly known
    #   None     -- unknown or not applicable (regulated_by, competes_with, ...)
    supply_geography: Optional[str] = None

    @field_validator("source", "target")
    @classmethod
    def _check_endpoint(cls, v: str, info) -> str:
        return _validate_canonical_id(v, f"Edge.{info.field_name}")

    @model_validator(mode="after")
    def _check_edge_invariants(self) -> "Edge":
        # `self.type` is a plain string here because of use_enum_values=True;
        # comparing to the str-Enum members works either way.
        if self.type == EdgeType.regulated_by or self.type == EdgeType.regulated_by.value:
            if not self.target.startswith("regulator:"):
                raise ValueError(
                    "Edge.target for type=regulated_by must start with 'regulator:' "
                    f"(got {self.target!r})"
                )
        if self.type == EdgeType.part_of or self.type == EdgeType.part_of.value:
            # We cannot inspect the source node's type from the edge alone, so
            # enforce by id-prefix convention: a Segment id starts with "seg:".
            if not self.source.startswith("seg:"):
                raise ValueError(
                    "Edge.source for type=part_of must start with 'seg:' "
                    f"(got {self.source!r})"
                )
            if not self.target.startswith("cik:"):
                raise ValueError(
                    "Edge.target for type=part_of must start with 'cik:' "
                    f"(parent must be a Company filer; got {self.target!r})"
                )
        if self.source == self.target:
            raise ValueError("Edge source and target must differ (no self-loops)")
        return self


class CandidateEdge(BaseModel):
    """A pre-resolution edge: the target is still a raw string, not a canonical id.

    Phase 2 (extraction) emits these. Phase 3 (resolution) resolves
    ``target_raw`` against the alias table to mint a canonical id, then
    promotes the surviving candidates into real :class:`Edge` records.

    `source_id` is always a canonical filer id (``cik:...``) — we KNOW which
    company's filing produced the candidate. `target_raw` may be either a raw
    string (``"Kimberly-Clark"``) or, for rule-extracted edges from
    ``config/regulators.yaml``, an already-canonical regulator id
    (``"regulator:fda"``) which Phase 3 will pass through.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_id: str
    target_raw: str
    type: EdgeType
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: Provenance
    # The grounding gate (CLAUDE.md invariant #4): True iff a) the snippet is a
    # literal substring of the source filing text, and b) the snippet mentions
    # the target. Rule-generated edges are marked True by construction since
    # there's no LLM text to verify against.
    verified: bool = False

    @field_validator("source_id")
    @classmethod
    def _source_is_filer_or_curated(cls, v: str) -> str:
        # Phase 2 emitted only filing-derived candidates so source was always
        # cik:. As of the commodity / region rules pipeline (post-Phase 7)
        # we also accept curated source types so a candidate like
        #   commodity:steel --supplies--> cik:exxon
        # can flow through resolve. Anything outside this allow-list is
        # still rejected -- catches typos and other wiring mistakes.
        _validate_canonical_id(v, "CandidateEdge.source_id")
        # Phase B adds wikidata: companies; extend allow-list to include them.
        if not v.startswith(("cik:", "commodity:", "region:", "regulator:", "wikidata:")):
            raise ValueError(
                "CandidateEdge.source_id must start with 'cik:', 'commodity:', "
                f"'region:', 'regulator:', or 'wikidata:' (got {v!r})"
            )
        return v

    @field_validator("target_raw")
    @classmethod
    def _target_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("CandidateEdge.target_raw must be non-empty")
        return v

    @model_validator(mode="after")
    def _check_regulated_by_target(self) -> "CandidateEdge":
        # If we already know the canonical regulator id (rule path),
        # require the conventional 'regulator:' prefix so Phase 3 can pass
        # it through unchanged.
        if self.type == EdgeType.regulated_by or self.type == EdgeType.regulated_by.value:
            if not self.target_raw.startswith("regulator:"):
                raise ValueError(
                    "CandidateEdge.target_raw for type=regulated_by must "
                    f"start with 'regulator:' (got {self.target_raw!r})"
                )
        return self
