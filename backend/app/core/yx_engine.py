"""槽位引擎适配器：把 yxcore（小一修的填表理解层）接进一修的 backend。

一修和小一修唯一该有的区别是「理解层」：
- 一修：LLM 任意构造 MILP（dsl_core 的 DSL → PlanningRequest → 求解器）
- 小一修：LLM 填 38 个预注册槽位（yxcore 的 翻译 → 补丁 → 求解器）

这个模块封装 yxcore 的 Session：每个会话一张旋钮表，say 出预览、run 出求解，
再把 yxcore 的结果映射成与一修 result_normalizer 同构的 WindowVersion 结构，
其余（查询、记忆、求解任务、台账、前端）全部共用一修的。
"""
from __future__ import annotations

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from yxcore.session import Session

from app.schemas.agent import (
    CandidateWindow,
    CandidateWindowMetrics,
    RepairScheduleItem,
    WindowVersion,
)

SHANGHAI_TZ = timezone(timedelta(hours=8))

_SOLVE_SECONDS = int(os.environ.get("XYX_SOLVE_SECONDS", "300"))


def _iso_stamp(data: Any, minute: int) -> str:
    """yxcore 快照的分钟 → 上海时区 ISO 串，与求解器 stamp 同源。"""
    try:
        return data.stamp(int(minute))
    except Exception:  # noqa: BLE001
        return ""


class YxEngine:
    """一个会话一份的槽位引擎。只做「理解 + 求解 + 结果映射」，不碰状态机。"""

    def __init__(self) -> None:
        self.session = Session()

    # ── 理解：一句话 → 补丁 → 回显 ───────────────────────────────────────
    def say(self, message: str, timeout: float = 90.0):
        """翻译 + 落地一轮，返回 yxcore 的 Turn（含回显/错误/待确认）。"""
        return self.session.say(message, timeout=timeout)

    def clear(self) -> None:
        self.session.clear()

    # ── 求解：当前旋钮表 → 求解器 ────────────────────────────────────────
    def run(self, seconds: int = _SOLVE_SECONDS) -> dict[str, Any]:
        return self.session.run(seconds=seconds)

    # ── 结果映射：yxcore 产物 → 与一修同构的 WindowVersion ────────────────
    def build_version(
        self,
        conversation_id: str,
        user_prompt: str,
        result: dict[str, Any],
        version_id: str,
        solve_job_id: str | None = None,
    ) -> WindowVersion:
        data = self.session.base_data
        recommendation = result.get("推荐") or {}
        schedule = recommendation.get("排程") or result.get("排程") or []
        generated_at = datetime.now(SHANGHAI_TZ).isoformat()
        windows = self._candidate_windows(version_id, conversation_id, generated_at,
                                          user_prompt, data, recommendation)
        return WindowVersion(
            id=version_id,
            conversationId=conversation_id,
            sourceType="user",
            generatedAt=generated_at,
            userPrompt=user_prompt,
            focusTag=str((result.get("求解") or {}).get("策略") or "槽位排程"),
            intentType="generate_version",
            parameterSnapshot={"生效旋钮": self.session.summary()},
            resultSummary=self._result_summary(result, data),
            repairSchedule=self._repair_schedule(data, schedule),
            windows=windows,
            includedInCandidatePool=True,
            solveJobId=solve_job_id,
            modelFingerprint=result.get("指纹"),
        )

    @staticmethod
    def _result_summary(result: dict[str, Any], data: Any) -> dict[str, Any]:
        rec = result.get("推荐") or {}
        info = result.get("求解") or {}
        return {
            "strategy": info.get("策略"),
            "objectiveMode": "加权" if info.get("权重") else "分层",
            "windowCount": len(data.windows or []),
            "topWindowId": (data.windows or [{}])[0].get("编号"),
            "scheduledTaskCount": rec.get("完成任务数", 0),
            "totalTaskCount": rec.get("待修总数", 0),
            "totalCost": rec.get("总成本"),
            "cashCost": rec.get("现金支出"),
            "generationLoss": rec.get("发电损失"),
            "costItems": {
                "船租": rec.get("船租"),
                "人工": rec.get("人工"),
                "备件": rec.get("备件"),
                "其他现场费用": rec.get("其他费用"),
                "发电损失": rec.get("发电损失"),
            },
            "latestCompletion": rec.get("最晚完工"),
            "vesselDays": rec.get("船舶艘天"),
            "verified": bool(rec.get("复核通过")),
            "baselineSavings": {},
            "unscheduledTasks": rec.get("未安排") or [],
            "solver": info,
            "data": data.meta or {},
        }

    @classmethod
    def _candidate_windows(cls, version_id: str, conversation_id: str, generated_at: str,
                           prompt: str, data: Any, rec: dict) -> list[CandidateWindow]:
        out: list[CandidateWindow] = []
        for index, window in enumerate(data.windows or []):
            start = _iso_stamp(data, window["开始"])
            end = _iso_stamp(data, window["结束"])
            assigned = [
                item for item in (rec.get("排程") or [])
                if item.get("窗口编号") == window["编号"]
            ]
            risk = "high" if any(item.get("严重等级") == "高" for item in assigned) else "medium"
            out.append(CandidateWindow(
                id=f"cand_{version_id}_{index}",
                windowId=window["编号"],
                versionId=version_id,
                conversationId=conversation_id,
                generatedAt=generated_at,
                focusTag="目标窗口",
                generatedFromPrompt=prompt,
                displayOrder=max(1, 100 - index),
                metrics=CandidateWindowMetrics(
                    duration=round((window["结束"] - window["开始"]) / 60, 2),
                    opportunityCost=float(rec.get("发电损失") or 0) if index == 0 else 0,
                    directCost=float(rec.get("现金支出") or 0) if index == 0 else 0,
                    riskLevel=risk,
                    startTime=start,
                    endTime=end,
                ),
            ))
        return out

    @staticmethod
    def _repair_schedule(data: Any, schedule: list[dict]) -> list[RepairScheduleItem]:
        import re

        items: list[RepairScheduleItem] = []
        for assignment in sorted(schedule, key=lambda item: (item.get("开始", 0), item.get("任务编号", ""))):
            start = int(assignment.get("开始", 0))
            end = int(assignment.get("完成", start))
            active = sum(max(0, int(b) - int(a)) for a, b in assignment.get("作业段", []))
            turbine_text = str(assignment.get("风机编号", ""))
            digits = re.search(r"(\d+)$", turbine_text)
            items.append(RepairScheduleItem(
                taskId=str(assignment.get("任务编号", "")),
                turbineId=int(digits.group(1)) if digits else 0,
                componentName=str(assignment.get("部件名称", "")),
                startHour=start // 60,
                endHour=end // 60,
                durationHours=max(1, round(active / 60)),
                startTime=_iso_stamp(data, start),
                endTime=_iso_stamp(data, end),
            ))
        return items
