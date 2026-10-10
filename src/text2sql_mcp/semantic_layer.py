"""Load and validate the YAML semantic model."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class SemanticError(ValueError):
    """A request referenced something the semantic model doesn't define.

    The message is written for the LLM: it names the bad value and suggests
    the closest valid ones, so the model can correct itself on the next call.
    """


@dataclass(frozen=True)
class Join:
    name: str
    table: str
    alias: str
    condition: str


@dataclass(frozen=True)
class Metric:
    name: str
    sql: str
    description: str = ""
    format: str = "number"


@dataclass(frozen=True)
class Dimension:
    name: str
    sql: str
    description: str = ""
    type: str = "string"
    join: str | None = None


@dataclass
class Limits:
    default_rows: int = 100
    max_rows: int = 1000
    timeout_seconds: float = 10


@dataclass
class SemanticModel:
    base_table: str
    base_alias: str
    time_dimension: str
    joins: dict[str, Join]
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    user_scopes: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    default_scope: dict[str, list[str]] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def from_yaml(cls, path: str | Path) -> SemanticModel:
        raw = yaml.safe_load(Path(path).read_text())
        model = cls(
            base_table=raw["base_table"],
            base_alias=raw["base_alias"],
            time_dimension=raw["time_dimension"],
            joins={k: Join(name=k, **v) for k, v in raw.get("joins", {}).items()},
            metrics={k: Metric(name=k, **v) for k, v in raw["metrics"].items()},
            dimensions={k: Dimension(name=k, **v) for k, v in raw["dimensions"].items()},
            user_scopes=raw.get("access", {}).get("users", {}) or {},
            default_scope=raw.get("access", {}).get("default_scope", {}) or {},
            limits=Limits(**raw.get("limits", {})),
        )
        model._validate()
        return model

    def _validate(self) -> None:
        for dim in self.dimensions.values():
            if dim.join and dim.join not in self.joins:
                raise ValueError(f"dimension '{dim.name}' uses unknown join '{dim.join}'")
        if self.time_dimension not in self.dimensions:
            raise ValueError(f"time_dimension '{self.time_dimension}' is not a dimension")
        for user, scope in self.user_scopes.items():
            for dim in scope:
                if dim not in self.dimensions:
                    raise ValueError(f"access scope for '{user}' uses unknown dimension '{dim}'")

    def metric(self, name: str) -> Metric:
        if name not in self.metrics:
            raise SemanticError(_unknown("metric", name, self.metrics))
        return self.metrics[name]

    def dimension(self, name: str) -> Dimension:
        if name not in self.dimensions:
            raise SemanticError(_unknown("dimension", name, self.dimensions))
        return self.dimensions[name]

    def scope_for(self, user: str | None) -> dict[str, list[str]]:
        if user and user in self.user_scopes:
            return self.user_scopes[user]
        return self.default_scope

    def catalog(self) -> dict:
        """What the LLM sees when it asks what it can query."""
        return {
            "metrics": {m.name: m.description for m in self.metrics.values()},
            "dimensions": {d.name: d.description for d in self.dimensions.values()},
            "time_dimension": self.time_dimension,
            "time_grains": ["day", "week", "month", "quarter", "year"],
            "filter_operators": sorted(OPERATORS),
            "max_rows": self.limits.max_rows,
        }


OPERATORS = {"eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "contains"}


def _unknown(kind: str, name: str, valid: dict) -> str:
    close = difflib.get_close_matches(name, valid, n=3, cutoff=0.4)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return f"Unknown {kind} '{name}'.{hint} Valid {kind}s: {', '.join(sorted(valid))}."
