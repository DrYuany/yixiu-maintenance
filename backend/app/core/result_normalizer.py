from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.schemas.agent import (
    CandidateWindow,
    CandidateWindowMetrics,
    RepairScheduleItem,
    WindowVersion,
)


SHANGHAI_TZ = timezone(timedelta(hours=8))


class ResultNormalizer:
    def build_version_from_product_result(
        self,
        conversation_id: str,
        user_prompt: str,
        focus_tag: str,
        parameter_snapshot: dict,
        payload: dict,
        solve_job_id: str | None = None,
        model_fingerprint: str | None = None,
    ) -> WindowVersion:
        generated_at = datetime.now(SHANGHAI_TZ).isoformat()
        version_id = f"ver_{uuid4().hex[:8]}"
        recommendation = payload.get("推荐", {})
        windows = payload.get("窗口", [])
        schedule = recommendation.get("排程", [])
        candidates = [
            self._candidate_from_product_window(
                version_id,
                conversation_id,
                generated_at,
                focus_tag,
                user_prompt,
                window,
                recommendation,
                schedule,
                index,
            )
            for index, window in enumerate(windows)
        ]
        repair_schedule = self._build_product_repair_schedule(windows, schedule)
        baselines = payload.get("人工基准", {})
        total_cost = recommendation.get("总成本")
        return WindowVersion(
            id=version_id,
            conversationId=conversation_id,
            sourceType="user",
            generatedAt=generated_at,
            userPrompt=user_prompt,
            focusTag=focus_tag,
            intentType="generate_version",
            parameterSnapshot=parameter_snapshot,
            resultSummary={
                "strategy": payload.get("策略"),
                "objectiveMode": payload.get("目标模式"),
                "windowCount": len(candidates),
                "topWindowId": candidates[0].windowId if candidates else None,
                "scheduledTaskCount": recommendation.get("完成任务数", 0),
                "totalTaskCount": recommendation.get("待修总数", 0),
                "totalCost": total_cost,
                "cashCost": recommendation.get("现金支出"),
                "generationLoss": recommendation.get("发电损失"),
                "costItems": {
                    "船租": recommendation.get("船租"),
                    "人工": recommendation.get("人工"),
                    "备件": recommendation.get("备件"),
                    "其他现场费用": recommendation.get("其他费用"),
                    "发电损失": recommendation.get("发电损失"),
                },
                "latestCompletion": recommendation.get("最晚完工"),
                "vesselDays": recommendation.get("船舶艘天"),
                "verified": recommendation.get("复核通过", False),
                "baselineSavings": {
                    name: round(float(baseline.get("总成本", 0)) - float(total_cost or 0), 2)
                    if baseline.get("完成任务数") == recommendation.get("完成任务数") and total_cost is not None
                    else None
                    for name, baseline in baselines.items()
                },
                "unscheduledTasks": recommendation.get("未安排", []),
                "solver": payload.get("求解", {}),
                "data": payload.get("数据", {}),
            },
            repairSchedule=repair_schedule,
            windows=candidates,
            includedInCandidatePool=True,
            solveJobId=solve_job_id,
            modelFingerprint=model_fingerprint,
        )

    def build_versions_from_update_result(
        self,
        conversation_id: str,
        user_prompt: str,
        focus_tag: str,
        payload: dict,
    ) -> list[WindowVersion]:
        generated_at = datetime.now(SHANGHAI_TZ).isoformat()
        version_id = f"ver_{uuid4().hex[:8]}"
        window = self._candidate_from_metrics(version_id, conversation_id, generated_at, focus_tag, user_prompt, payload["metrics"])
        repair_schedule = self._build_repair_schedule(generated_at, payload)
        return [
            WindowVersion(
                id=version_id,
                conversationId=conversation_id,
                sourceType="user",
                generatedAt=generated_at,
                userPrompt=user_prompt,
                focusTag=focus_tag,
                intentType="generate_version",
                parameterSnapshot=payload.get("applied_updates", {}),
                resultSummary={
                    "windowCount": 1,
                    "topWindowId": window.windowId,
                    "summary": payload.get("summary", ""),
                    "scheduledTaskCount": len(repair_schedule),
                },
                repairSchedule=repair_schedule,
                windows=[window],
                includedInCandidatePool=True,
            )
        ]

    def build_versions_from_compare_result(
        self,
        conversation_id: str,
        user_prompt: str,
        focus_tag: str,
        payload: dict,
    ) -> list[WindowVersion]:
        base_time = datetime.now(SHANGHAI_TZ)
        versions: list[WindowVersion] = []
        for index, comparison in enumerate(payload["comparisons"]):
            generated_at = (base_time + timedelta(seconds=index)).isoformat()
            version_id = f"ver_{uuid4().hex[:8]}"
            window = self._candidate_from_metrics(
                version_id,
                conversation_id,
                generated_at,
                comparison["label"],
                user_prompt,
                comparison["metrics"],
            )
            repair_schedule = self._build_repair_schedule(generated_at, comparison)
            versions.append(
                WindowVersion(
                    id=version_id,
                    conversationId=conversation_id,
                    sourceType="user",
                    generatedAt=generated_at,
                    userPrompt=user_prompt,
                    focusTag=comparison["label"],
                    intentType="generate_version",
                    parameterSnapshot=comparison.get("updates", {}),
                    resultSummary={
                        "windowCount": 1,
                        "topWindowId": window.windowId,
                        "summary": comparison.get("summary", ""),
                        "scheduledTaskCount": len(repair_schedule),
                    },
                    repairSchedule=repair_schedule,
                    windows=[window],
                    includedInCandidatePool=True,
                )
            )
        return versions

    def _candidate_from_metrics(
        self,
        version_id: str,
        conversation_id: str,
        generated_at: str,
        focus_tag: str,
        prompt: str,
        metrics: dict,
    ) -> CandidateWindow:
        makespan = max(int(metrics.get("makespan", 1)), 1)
        start_dt = datetime.fromisoformat(generated_at)
        end_dt = start_dt + timedelta(hours=makespan)
        return CandidateWindow(
            id=f"cand_{uuid4().hex[:8]}",
            windowId=f"WIN-{version_id[-4:]}",
            versionId=version_id,
            conversationId=conversation_id,
            generatedAt=generated_at,
            focusTag=focus_tag,
            generatedFromPrompt=prompt,
            displayOrder=1,
            metrics=CandidateWindowMetrics(
                duration=float(makespan),
                opportunityCost=float(metrics.get("total_loss", 0.0)),
                directCost=float(metrics.get("direct_cost", 0.0)),
                riskLevel=self._risk_level(float(metrics.get("risk_cost", 0.0))),
                startTime=start_dt.strftime("%Y-%m-%d %H:%M"),
                endTime=end_dt.strftime("%Y-%m-%d %H:%M"),
            ),
        )

    def _candidate_from_product_window(
        self,
        version_id: str,
        conversation_id: str,
        generated_at: str,
        focus_tag: str,
        prompt: str,
        window: dict,
        recommendation: dict,
        schedule: list[dict],
        index: int,
    ) -> CandidateWindow:
        start = datetime.fromisoformat(window["开始日期"])
        end = datetime.fromisoformat(window["结束时间不含"])
        window_id = window["编号"]
        assigned = [item for item in schedule if item.get("窗口编号") == window_id]
        risk = "high" if any(item.get("严重等级") == "高" for item in assigned) else "medium"
        return CandidateWindow(
            id=f"cand_{uuid4().hex[:8]}",
            windowId=window_id,
            versionId=version_id,
            conversationId=conversation_id,
            generatedAt=generated_at,
            focusTag=focus_tag,
            generatedFromPrompt=prompt,
            displayOrder=max(1, 100 - index),
            metrics=CandidateWindowMetrics(
                duration=(end - start).total_seconds() / 3600,
                opportunityCost=float(recommendation.get("发电损失", 0)) if index == 0 else 0,
                directCost=float(recommendation.get("现金支出", 0)) if index == 0 else 0,
                riskLevel=risk,
                startTime=start.strftime("%Y-%m-%d %H:%M"),
                endTime=end.strftime("%Y-%m-%d %H:%M"),
            ),
        )

    def _build_product_repair_schedule(
        self,
        windows: list[dict],
        schedule: list[dict],
    ) -> list[RepairScheduleItem]:
        window_by_id = {item["编号"]: item for item in windows}
        items: list[RepairScheduleItem] = []
        for assignment in sorted(schedule, key=lambda item: (item.get("开始", 0), item.get("任务编号", ""))):
            window = window_by_id.get(assignment.get("窗口编号"))
            if not window:
                continue
            base = datetime.fromisoformat(window["开始日期"]) - timedelta(minutes=float(window.get("开始", 0)))
            start_minute = int(assignment.get("开始", 0))
            end_minute = int(assignment.get("完成", start_minute))
            active_minutes = sum(max(0, int(end) - int(start)) for start, end in assignment.get("作业段", []))
            turbine_text = str(assignment.get("风机编号", ""))
            digits = re.search(r"(\d+)$", turbine_text)
            items.append(
                RepairScheduleItem(
                    taskId=str(assignment.get("任务编号", "")),
                    turbineId=int(digits.group(1)) if digits else 0,
                    componentName=str(assignment.get("部件名称", "")),
                    startHour=start_minute // 60,
                    endHour=end_minute // 60,
                    durationHours=max(1, round(active_minutes / 60)),
                    startTime=(base + timedelta(minutes=start_minute)).strftime("%Y-%m-%d %H:%M"),
                    endTime=(base + timedelta(minutes=end_minute)).strftime("%Y-%m-%d %H:%M"),
                )
            )
        return items

    @staticmethod
    def _risk_level(risk_cost: float) -> str:
        if risk_cost >= 10000:
            return "high"
        if risk_cost >= 3000:
            return "medium"
        return "low"

    def _build_repair_schedule(
        self,
        generated_at: str,
        payload: dict,
    ) -> list[RepairScheduleItem]:
        component_library = payload.get("component_library", {})
        base_time = datetime.fromisoformat(generated_at)
        schedule: list[RepairScheduleItem] = []

        for assignment in sorted(
            payload.get("assignments", []),
            key=lambda item: (item.get("start_hour", 0), item.get("task_id", "")),
        ):
            task_id = str(assignment.get("task_id", "")).strip()
            start_hour = int(assignment.get("start_hour", 0))
            turbine_id, component_name = self._parse_task_identity(task_id)
            duration_hours = int(
                component_library.get(component_name, {}).get("duration_hours", 0)
            )
            end_hour = start_hour + max(duration_hours, 0)
            start_time = base_time + timedelta(hours=start_hour)
            end_time = base_time + timedelta(hours=end_hour)
            schedule.append(
                RepairScheduleItem(
                    taskId=task_id,
                    turbineId=turbine_id,
                    componentName=component_name,
                    startHour=start_hour,
                    endHour=end_hour,
                    durationHours=duration_hours,
                    startTime=start_time.strftime("%Y-%m-%d %H:%M"),
                    endTime=end_time.strftime("%Y-%m-%d %H:%M"),
                )
            )

        return schedule

    @staticmethod
    def _parse_task_identity(task_id: str) -> tuple[int, str]:
        match = re.match(r"^T(\d+)-(.+)$", task_id)
        if not match:
            return 0, task_id
        return int(match.group(1)), match.group(2)
