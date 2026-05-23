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


class EdgeType(str, Enum):
    # NOTE: no `customer_of` — it is derived from `supplies` at query time
    # (invariant #2 in CLAUDE.md). Storing it would duplicate state.
    supplies = "supplies"
    competes_with = "competes_with"
    regulated_by = "regulated_by"


# Canonical ID prefixes per CLAUDE.md §8.
_ID_PREFIXES = ("cik:", "wikidata:", "slug:", "regulator:")
_ID_RE = re.compile(r"^(cik|wikidata|slug|regulator):[A-Za-z0-9][A-Za-z0-9._\-]*$")


def _validate_canonical_id(value: str, field: str = "id") -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if not value.startswith(_ID_PREFIXES):
        raise ValueError(
            f"{field} {value!r} must start with one of {_ID_PREFIXES} "
            "(canonical-ID format per CLAUDE.md §8)"
        )
    if not _ID_RE.match(value):
        raise ValueError(
            f"{field} {value!r} is not a well-formed canonical ID "
            "(prefix:slug, alphanumeric/._- only)"
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
    def _check_regulated_by(self) -> "Edge":
        if self.type == EdgeType.regulated_by.value or self.type == EdgeType.regulated_by:
            if not self.target.startswith("regulator:"):
                raise ValueError(
                    "Edge.target for type=regulated_by must start with 'regulator:' "
                    f"(got {self.target!r})"
                )
        if self.source == self.target:
            raise ValueError("Edge source and target must differ (no self-loops)")
        return self
