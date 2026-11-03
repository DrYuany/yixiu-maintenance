from __future__ import annotations

from app.core.version_compare import compare_versions, render_compare_summary
from app.schemas.agent import CandidateWindow, RepairScheduleItem, WindowVersion


def _solver_info(
    *,
    status: str = "OPTIMAL",
    cost_gap: float = 0.0,
    unproven: list[str] | None = None,
) -> dict:
    """一份完整的求解记录：收敛状态是"能不能说谁更省"的依据，不能缺。"""
    unproven = unproven or []
    stages = [
        {"目标": "总经济成本", "状态": "OPTIMAL" if cost_gap == 0 else "FEASIBLE", "相对差距": cost_gap},
        {"目标": "完成任务数", "状态": "OPTIMAL", "相对差距": 0.0},
    ]
    return {
        "解状态": status,
        "终止原因": "COMPLETED" if not unproven else "STAGE_TIME_LIMIT",
        "总耗时秒": 12.0,
        "最优性": "已证明最优" if status == "OPTIMAL" else "未证明最优",
        "计划阶段数": len(stages) + len(unproven),
        "执行阶段数": len(stages) + len(unproven),
        "分层进度": f"{len(stages) + len(unproven)}/{len(stages) + len(unproven)}",
        "最大相对差距": cost_gap,
        "容差内阶段": [],
        "未达最优阶段": unproven,
        "预算": {"seconds": 60, "gapLimit": 0.01, "secondsSource": "参数总表", "gapSource": "参数总表"},
        "阶段": stages,
        "求解器参数": {"num_search_workers": 1, "max_time_in_seconds": 60, "relative_gap_limit": 0.01},
    }


def _version(
    version_id: str,
    *,
    tasks: list[str],
    total_cost: float,
    input_hash: str = "hash-a",
    generated_at: str = "2026-10-27T10:00:00+08:00",
    unscheduled: list[dict] | None = None,
    solver: dict | None = None,
) -> WindowVersion:
    return WindowVersion(
        id=version_id,
        conversationId="conv_test",
        generatedAt=generated_at,
        userPrompt="测试方案",
        focusTag="成本最低优先",
        intentType="generate_version",
        parameterSnapshot={},
        resultSummary={
            "strategy": "成本最低优先",
            "objectiveMode": "分层",
            "topWindowId": "窗口02",
            "totalTaskCount": 5,
            "scheduledTaskCount": len(tasks),
            "totalCost": total_cost,
            "cashCost": 100_000,
            "generationLoss": total_cost - 100_000,
            "latestCompletion": "2026-11-10T18:00:00+08:00",
            "vesselDays": 26,
            "verified": True,
            "baselineSavings": {"严重性优先": None, "低风速优先": 0.0},
            "unscheduledTasks": unscheduled or [{"任务编号": "任务003", "原因": "竞争未入选"}],
            "solver": solver if solver is not None else _solver_info(),
            "data": {"输入哈希": input_hash, "规划时点": "2026-10-23T18:00:00+08:00"},
        },
        repairSchedule=[
            RepairScheduleItem(
                taskId=task,
                turbineId=index + 1,
                componentName="叶片",
                startHour=100 + index,
                endHour=104 + index,
                durationHours=4,
                startTime=f"2026-11-02 {8 + index}:00",
                endTime=f"2026-11-02 {12 + index}:00",
            )
            for index, task in enumerate(tasks)
        ],
        windows=[
            CandidateWindow(
                id=f"cand_{version_id}",
                windowId="窗口02",
                versionId=version_id,
                conversationId="conv_test",
                generatedAt=generated_at,
                focusTag="成本最低优先",
                generatedFromPrompt="测试",
                displayOrder=1,
                metrics={
                    "duration": 240,
                    "opportunityCost": 3_000_000,
                    "directCost": 100_000,
                    "riskLevel": "high",
                    "startTime": "2026-11-02 00:00",
                    "endTime": "2026-11-12 00:00",
                },
            )
        ],
        modelFingerprint=f"fp-{version_id}",
    )


def test_compare_reports_metrics_and_task_diff() -> None:
    left = _version("ver_left0001", tasks=["任务001", "任务002"], total_cost=5_110_000)
    right = _version(
        "ver_right001",
        tasks=["任务001", "任务004"],
        total_cost=4_720_000,
        unscheduled=[{"任务编号": "任务002", "原因": "竞争未入选"}],
    )

    result = compare_versions(left, right, left_number=1, right_number=2)

    metrics = {item["key"]: item for item in result["layer1Facts"]["metrics"]}
    assert metrics["totalCost"]["delta"] == -39.0
    assert metrics["totalCost"]["winner"] == "right"
    assert metrics["scheduledTasks"]["delta"] == 0

    task_set = result["layer2Drivers"]["taskSet"]
    assert [item["taskId"] for item in task_set["onlyLeft"]] == ["任务002"]
    assert [item["taskId"] for item in task_set["onlyRight"]] == ["任务004"]
    assert task_set["onlyLeft"][0]["otherReason"] == "竞争未入选"


def test_compare_blocks_savings_claim_when_task_sets_differ() -> None:
    left = _version("ver_left0001", tasks=["任务001"], total_cost=5_000_000)
    right = _version("ver_right001", tasks=["任务001", "任务002"], total_cost=4_000_000)

    result = compare_versions(left, right, left_number=1, right_number=2)

    comparability = result["layer1Facts"]["comparability"]
    assert comparability["costComparable"] is True
    assert comparability["savingsClaimAllowed"] is False
    verdict = result["layer1Facts"]["quickVerdict"]
    assert verdict["cheaper"] in {"left", "right", "tie", None}
    assert verdict["cheaperCertainty"] in {"proven", "beyondTolerance", "inconclusive", "unverified", "incomparable"}


def test_compare_allows_savings_when_snapshot_and_tasks_match() -> None:
    left = _version("ver_left0001", tasks=["任务001", "任务002"], total_cost=5_000_000)
    right = _version("ver_right001", tasks=["任务001", "任务002"], total_cost=4_000_000)

    result = compare_versions(left, right, left_number=1, right_number=2)

    assert result["layer1Facts"]["comparability"]["savingsClaimAllowed"] is True
    assert result["layer1Facts"]["quickVerdict"]["cheaper"] == "right"


def test_compare_marks_different_snapshots_as_not_comparable() -> None:
    left = _version("ver_left0001", tasks=["任务001"], total_cost=5_000_000, input_hash="a")
    right = _version("ver_right001", tasks=["任务001"], total_cost=4_000_000, input_hash="b")

    result = compare_versions(left, right, left_number=1, right_number=2)

    assert result["layer1Facts"]["comparability"]["sameDataSnapshot"] is False
    assert result["layer1Facts"]["comparability"]["costComparable"] is False
    assert result["layer1Facts"]["quickVerdict"]["cheaper"] is None


def test_compare_is_deterministic_for_same_pair() -> None:
    left = _version("ver_left0001", tasks=["任务001"], total_cost=5_000_000)
    right = _version("ver_right001", tasks=["任务001"], total_cost=4_000_000)

    first = compare_versions(left, right, left_number=1, right_number=2)
    second = compare_versions(left, right, left_number=1, right_number=2)

    assert first["comparisonId"] == second["comparisonId"]
    assert first["layer1Facts"] == second["layer1Facts"]


def test_compare_payload_stays_bounded() -> None:
    left = _version(
        "ver_left0001",
        tasks=[f"任务{index:03d}" for index in range(1, 41)],
        total_cost=5_000_000,
    )
    right = _version(
        "ver_right001",
        tasks=[f"任务{index:03d}" for index in range(30, 70)],
        total_cost=4_000_000,
    )

    result = compare_versions(left, right, left_number=1, right_number=2)

    assert result["payloadBytes"] < 16_000
    assert len(result["layer2Drivers"]["taskSet"]["onlyLeft"]) == 12
    assert result["layer2Drivers"]["taskSet"]["onlyLeftCount"] == 29


def test_compare_summary_is_usable_without_an_llm() -> None:
    left = _version("ver_left0001", tasks=["任务001", "任务002"], total_cost=5_000_000)
    right = _version("ver_right001", tasks=["任务001"], total_cost=4_000_000)

    payload = compare_versions(left, right, left_number=1, right_number=2)
    summary = render_compare_summary(payload)

    assert "方案01" in summary and "方案02" in summary
    assert "总成本" in summary
    assert "不能直接宣称节省" in summary


def test_compare_refuses_cost_conclusion_when_gap_covers_difference() -> None:
    """两版都没证明最优，差值小于容差：可以给数，不能说谁更省。"""
    left = _version(
        "ver_left0001",
        tasks=["任务001", "任务002"],
        total_cost=2_425_200,
        solver=_solver_info(status="FEASIBLE", cost_gap=0.012, unproven=["最后完工时间"]),
    )
    right = _version(
        "ver_right001",
        tasks=["任务001", "任务002"],
        total_cost=2_431_700,
        solver=_solver_info(status="FEASIBLE", cost_gap=0.008, unproven=["完工时间和"]),
    )

    result = compare_versions(left, right, left_number=1, right_number=2)
    comparability = result["layer1Facts"]["comparability"]

    assert comparability["costCertainty"] == "inconclusive"
    assert comparability["savingsClaimAllowed"] is False
    assert result["layer1Facts"]["quickVerdict"]["cheaper"] is None
    assert comparability["costRange"]["left"]["gap"] == 0.012

    summary = render_compare_summary(result)
    assert "不能据此判定谁更省" in summary
    assert "收敛" in summary
    assert "分层" in summary


def test_compare_concludes_when_difference_exceeds_tolerance() -> None:
    left = _version("ver_left0001", tasks=["任务001"], total_cost=5_000_000,
                    solver=_solver_info(status="FEASIBLE", cost_gap=0.01, unproven=["完工时间和"]))
    right = _version("ver_right001", tasks=["任务001"], total_cost=4_000_000,
                     solver=_solver_info(status="FEASIBLE", cost_gap=0.01, unproven=["完工时间和"]))

    result = compare_versions(left, right, left_number=1, right_number=2)
    comparability = result["layer1Facts"]["comparability"]

    assert comparability["costCertainty"] == "beyondTolerance"
    assert comparability["savingsClaimAllowed"] is True
    assert result["layer1Facts"]["quickVerdict"]["cheaper"] == "right"


def test_compare_without_convergence_record_does_not_claim() -> None:
    legacy = {
        "解状态": "FEASIBLE",
        "终止原因": "TIME_LIMIT",
        "总耗时秒": 300.0,
        "求解器参数": {"num_search_workers": 1},
    }
    left = _version("ver_left0001", tasks=["任务001"], total_cost=5_000_000, solver=legacy)
    right = _version("ver_right001", tasks=["任务001"], total_cost=4_000_000, solver=legacy)

    result = compare_versions(left, right, left_number=1, right_number=2)
    comparability = result["layer1Facts"]["comparability"]

    assert comparability["costCertainty"] == "unverified"
    assert comparability["savingsClaimAllowed"] is False
    assert result["layer1Facts"]["quickVerdict"]["cheaper"] is None
    assert "未核实" in render_compare_summary(result)


def test_compare_proven_states_mode_scope_in_summary() -> None:
    """两版各自证明最优：可以给结论，但得点明目标口径不同，别写成「超出容差」。"""
    left = _version("ver_left0001", tasks=["任务001"], total_cost=4_800_000,
                    solver=_solver_info(status="OPTIMAL", cost_gap=0.0))
    right = _version("ver_right001", tasks=["任务001"], total_cost=5_000_000,
                     solver=_solver_info(status="OPTIMAL", cost_gap=0.0))

    result = compare_versions(left, right, left_number=1, right_number=2)
    assert result["layer1Facts"]["comparability"]["costCertainty"] == "proven"
    summary = render_compare_summary(result)
    assert "目标口径不同" in summary
    assert "超出求解容差" not in summary


def test_weighted_mode_is_not_labelled_as_proven_cost() -> None:
    """加权版没有独立成本阶段：证明的是加权目标，不能写成「成本已证明最优」。"""
    from app.core.version_compare import convergence_of

    weighted_solver = {
        "解状态": "FEASIBLE",
        "终止原因": "TIME_LIMIT",
        "总耗时秒": 3.0,
        "最优性": "未证明最优",
        "计划阶段数": 1,
        "执行阶段数": 1,
        "分层进度": "1/1",
        "最大相对差距": 0.0,
        "容差内阶段": [],
        "未达最优阶段": [],
        "预算": {"seconds": 20, "gapLimit": 0.01, "secondsSource": "环境变量上限"},
        "阶段": [
            {"目标": "标准化加权目标", "状态": "OPTIMAL", "相对差距": 0.0},
            {"目标": "确定性决胜1", "状态": "FEASIBLE", "相对差距": 0.0},
        ],
        "求解器参数": {"num_search_workers": 1, "max_time_in_seconds": 20, "relative_gap_limit": 0.01},
    }
    left = _version("ver_left0001", tasks=["任务001"], total_cost=4_800_000,
                    solver=_solver_info(status="OPTIMAL", cost_gap=0.0))
    right = _version("ver_right001", tasks=["任务001"], total_cost=5_000_000,
                     solver=weighted_solver)

    assert convergence_of(right)["costGapMetric"] == "标准化加权目标"
    summary = render_compare_summary(compare_versions(left, right, left_number=1, right_number=2))
    assert "标准化加权目标已证明最优" in summary
    assert "总经济成本已证明最优" in summary
