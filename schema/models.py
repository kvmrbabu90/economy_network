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
_ID_PREFIXES = ("cik:", "wikidata:", "slug:", "regulator:", "seg:")

# Prefix-specific body shapes. Keep each strict so a malformed body fails
# validation rather than silently passing.
_ID_BODY_RE = {
    "cik": re.compile(r"^\d{10}$"),
    "wikidata": re.compile(r"^Q\d+$"),
    "slug": re.compile(r"^[a-z0-9][a-z0-9\-]*$"),
    "regulator": re.compile(r"^[a-z0-9][a-z0-9\-]*$"),
    # seg body: "cik<10-digit-parent>:<lower-kebab-slug>"
    "seg": re.compile(r"^cik\d{10}:[a-z0-9][a-z0-9\-]*$"),
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
    """Source citation for every edge. No edge enters the graph without one."""

    model_config = ConfigDict(extra="forbid")

    filing: str
    url: str
    snippet: str
    extracted_by: Literal["llm", "rule", "manual"]

    @field_validator("filing", "url", "snippet")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("provenance fields (filing, url, snippet) must be non-empty")
        return v


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
    weight: Optional[float] = None

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
