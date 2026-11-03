from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SolveStatus = Literal["OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE"]


@dataclass(frozen=True)
class SolveEvidence:
    status: SolveStatus
    has_incumbent: bool
    proof: bool
    elapsed_seconds: float
    phase: str
    reason: str | None
    running: bool = False
    objective_value: float | None = None
    best_bound: float | None = None


@dataclass(frozen=True)
class SolvePresentation:
    status: SolveStatus
    title: str
    detail: str
    running: bool
    needs_user_decision: bool
    actions: tuple[str, ...]


class SolvePolicy:
    def __init__(self, soft_threshold_seconds: float = 180) -> None:
        self.soft_threshold_seconds = soft_threshold_seconds

    def present(self, evidence: SolveEvidence) -> SolvePresentation:
        status = evidence.status
        if status == "INFEASIBLE" and not evidence.proof:
            status = "UNKNOWN"

        title, detail = self._wording(status, evidence.has_incumbent, evidence.proof)
        needs_decision = (
            evidence.running
            and evidence.elapsed_seconds >= self.soft_threshold_seconds
            and status in {"UNKNOWN", "FEASIBLE"}
        )
        actions: tuple[str, ...] = ()
        if needs_decision:
            actions = ("wait", "background")
            if evidence.has_incumbent:
                actions += ("use_incumbent",)
            actions += ("stop",)

        return SolvePresentation(
            status=status,
            title=title,
            detail=detail,
            running=evidence.running,
            needs_user_decision=needs_decision,
            actions=actions,
        )

    @staticmethod
    def _wording(
        status: SolveStatus,
        has_incumbent: bool,
        proof: bool,
    ) -> tuple[str, str]:
        if status == "OPTIMAL":
            return "已证明最优", "当前方案已完成可行性复核，并已证明目标最优。"
        if status == "FEASIBLE" or status == "UNKNOWN" and has_incumbent:
            return "已找到可行方案", "当前方案可以使用，但尚未证明最优，求解可继续。"
        if status == "INFEASIBLE" and proof:
            return "已证明当前约束不可行", "求解器已完成不可行性证明，可调整约束后重试。"
        return "尚未找到可行方案", "搜索尚未结束或已被停止；当前状态未证明不可行。"
