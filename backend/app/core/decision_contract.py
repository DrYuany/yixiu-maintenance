from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


VOLATILE_KEYS = {
    "created_at",
    "request_id",
    "timestamp",
    "trace_id",
    "updated_at",
}
ORDERED_LIST_KEYS = {"history", "messages", "objectives", "stages"}


def _normalize_number(value: int | float | Decimal) -> int | str:
    decimal = Decimal(str(value)).normalize()
    if decimal == decimal.to_integral():
        return int(decimal)
    return format(decimal, "f")


def _canonicalize(value: Any, parent_key: str | None = None) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        return _normalize_number(value)
    if isinstance(value, dict):
        return {
            key: _canonicalize(value[key], key)
            for key in sorted(value)
            if key not in VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple, set)):
        items = [_canonicalize(item) for item in value]
        if parent_key not in ORDERED_LIST_KEYS:
            items.sort(
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return items
    return str(value).strip() if isinstance(value, str) else value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecisionArtifact:
    canonical_context: dict[str, Any]
    canonical_decision: dict[str, Any]
    canonical_model: dict[str, Any]
    context_fingerprint: str
    decision_fingerprint: str
    model_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DecisionCompiler:
    def compile(
        self,
        context: dict[str, Any],
        decision: dict[str, Any],
        model: dict[str, Any],
    ) -> DecisionArtifact:
        canonical_context = _canonicalize(context)
        canonical_decision = _canonicalize(decision)
        canonical_model = _canonicalize(model)
        return DecisionArtifact(
            canonical_context=canonical_context,
            canonical_decision=canonical_decision,
            canonical_model=canonical_model,
            context_fingerprint=fingerprint(canonical_context),
            decision_fingerprint=fingerprint(canonical_decision),
            model_fingerprint=fingerprint(canonical_model),
        )

