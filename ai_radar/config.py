"""Loads sources.toml and weights.toml into typed dataclasses.

Single source of truth: the toml files. Code only reads, never writes.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# TOML keys can't contain dots, so T1.5 is stored as "T1_5". Map back at load.
_TIER_FROM_TOML = {"T1": "T1", "T1_5": "T1.5", "T2": "T2"}
_TIER_TO_TOML = {v: k for k, v in _TIER_FROM_TOML.items()}


@dataclass
class Source:
    name: str
    tier: str            # "T1" | "T1.5" | "T2"
    category: str        # lab | infra | research | kol | econ | media | community | product
    url: str
    fetcher: str         # rss | arxiv | x | scrape
    active: bool = True
    notes: str | None = None


_DEFAULT_DIMS = ["hardcore", "primary_src", "density", "novelty"]


@dataclass
class Weights:
    weights: dict[str, float] = field(default_factory=dict)
    tier_multiplier: dict[str, float] = field(default_factory=dict)
    thresholds: dict[str, dict[str, float]] = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)
    # LLM scoring dimensions (column names in `scores` table). Read from
    # `[scoring].dims` in weights.toml; falls back to the historical 4-dim set.
    dims: list[str] = field(default_factory=lambda: list(_DEFAULT_DIMS))


@dataclass
class Config:
    sources: list[Source]
    weights: Weights
    project_root: Path


def _load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_sources(path: Path) -> list[Source]:
    raw = _load_toml(path)
    out: list[Source] = []
    seen: set[str] = set()
    for entry in raw.get("source", []):
        name = entry["name"]
        if name in seen:
            print(f"[config] duplicate source name: {name!r}", file=sys.stderr)
            continue
        seen.add(name)
        out.append(
            Source(
                name=name,
                tier=entry["tier"],
                category=entry["category"],
                url=entry.get("url", ""),
                fetcher=entry["fetcher"],
                active=bool(entry.get("active", True)),
                notes=entry.get("notes"),
            )
        )
    return out


def load_weights(path: Path) -> Weights:
    raw = _load_toml(path)
    scoring = raw.get("scoring", {})
    selection = raw.get("selection", {})
    tier_mul_raw = scoring.get("tier_multiplier", {})
    # Map T1_5 -> T1.5 here so downstream uses canonical tier names.
    tier_mul = {_TIER_FROM_TOML.get(k, k): float(v) for k, v in tier_mul_raw.items()}

    thresholds_raw = selection.get("thresholds", {})
    thresholds: dict[str, dict[str, float]] = {}
    for category, by_tier in thresholds_raw.items():
        thresholds[category] = {
            _TIER_FROM_TOML.get(k, k): float(v) for k, v in by_tier.items()
        }

    dims_raw = scoring.get("dims") or _DEFAULT_DIMS
    dims = [str(d) for d in dims_raw if isinstance(d, str)]
    if not dims:
        dims = list(_DEFAULT_DIMS)

    return Weights(
        weights={k: float(v) for k, v in scoring.get("weights", {}).items()},
        tier_multiplier=tier_mul,
        thresholds=thresholds,
        categories=list(raw.get("categories", {}).get("all", [])),
        dims=dims,
    )


def load_config(project_root: Path | None = None) -> Config:
    root = project_root or Path(__file__).resolve().parent.parent
    return Config(
        sources=load_sources(root / "sources.toml"),
        weights=load_weights(root / "weights.toml"),
        project_root=root,
    )
