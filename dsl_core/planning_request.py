"""共享的排程请求数据模型。

这是「执行侧」的一部分：规则主导与 LLM 主导两个产品都把它当作编译产物
（DSL → PlanningRequest → 求解器输入），所以放在中立包 dsl_core 里，
而不是 backend 的规则理解层里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlanningRequest:
    base: str = "基座01"
    strategy: str = "成本最低优先"
    mode: str = "分层"
    # None = 用户没点名时限，交给参数总表（求解01「总求解预算」）决定；
    # 只有用户在对话里说了「最多 90 秒」或 DSL 写了 solver.seconds 才是具体数字。
    # 以前这里写死 300，于是公告永远说 300 秒，而参数总表写 60、实际跑 30。
    seconds: int | None = None
    weights: dict[str, float] | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)
    relative_overrides: dict[str, float] = field(default_factory=dict)
    # 行级数据补丁：改的是业务记录行，不是全局参数。
    # 三种：part_arrival（备件到货）、task_deadline（任务期限）、part_hours（部件工时）。
    data_patches: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "strategy": self.strategy,
            "mode": self.mode,
            "seconds": self.seconds,
            "weights": self.weights,
            "limits": dict(sorted(self.limits.items())),
            "overrides": dict(sorted(self.overrides.items())),
            "relative_overrides": dict(sorted(self.relative_overrides.items())),
            "data_patches": sorted(
                (dict(sorted(patch.items(), key=lambda kv: kv[0])) for patch in self.data_patches),
                key=lambda patch: json_key(patch),
            ),
        }


def json_key(patch: dict[str, Any]) -> str:
    import json

    return json.dumps(patch, ensure_ascii=False, sort_keys=True)
