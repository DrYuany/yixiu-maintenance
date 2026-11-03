from __future__ import annotations

from pathlib import Path

from app.core.agent_tools import AgentToolbox
from app.db.database import Database
from app.db.models import ModelArtifactRepository, VersionRepository
from app.schemas.agent import CandidateWindow, RepairScheduleItem, WindowVersion


def _version(
    version_id: str,
    generated_at: str,
    *,
    total_cost: float = 4_725_671.66,
    scheduled: int = 2,
    status: str = "FEASIBLE",
    strategy: str = "成本最低优先",
    task_ids: tuple[str, ...] = ("任务018",),
) -> WindowVersion:
    return WindowVersion(
        id=version_id,
        conversationId="conv_tools",
        generatedAt=generated_at,
        userPrompt=f"按{strategy}排一版",
        focusTag=strategy,
        intentType="generate_version",
        parameterSnapshot={"strategy": strategy, "mode": "分层"},
        resultSummary={
            "strategy": strategy,
            "objectiveMode": "分层",
            "totalTaskCount": 3,
            "scheduledTaskCount": scheduled,
            "totalCost": total_cost,
            "cashCost": 441_825.0,
            "generationLoss": total_cost - 441_825.0,
            "latestCompletion": "2026-11-10T18:00:00+08:00",
            "vesselDays": 27,
            "verified": True,
            "baselineSavings": {"严重性优先": None, "低风速优先": 0.0},
            "unscheduledTasks": [],
            "solver": {
                "解状态": status,
                "终止原因": "TIME_LIMIT",
                "总耗时秒": 300.5,
                "求解器参数": {"num_search_workers": 1, "random_seed": 17},
            },
            "data": {"输入哈希": "hash-a", "规划时点": "2026-10-23T18:00:00+08:00"},
        },
        repairSchedule=[
            RepairScheduleItem(
                taskId=task_id,
                turbineId=index + 1,
                componentName="轮毂",
                startHour=255,
                endHour=258,
                durationHours=3,
                startTime="2026-11-02 15:00",
                endTime="2026-11-02 18:00",
            )
            for index, task_id in enumerate(task_ids)
        ],
        windows=[
            CandidateWindow(
                id=f"cand_{version_id}",
                windowId="窗口02",
                versionId=version_id,
                conversationId="conv_tools",
                generatedAt=generated_at,
                focusTag=strategy,
                generatedFromPrompt="测试",
                displayOrder=1,
                metrics={
                    "duration": 240,
                    "opportunityCost": total_cost - 441_825.0,
                    "directCost": 441_825.0,
                    "riskLevel": "high",
                    "startTime": "2026-11-02 00:00",
                    "endTime": "2026-11-12 00:00",
                },
            )
        ],
        modelFingerprint=f"fp-{version_id}",
    )


def _artifact(fingerprint: str = "fp-artifact") -> dict:
    return {
        "modelFingerprint": fingerprint,
        "mode": "分层",
        "dimensions": {
            "tasks": 25,
            "windows": 1,
            "workLines": 3,
            "candidateChoices": 5310,
        },
        "variables": {
            "total": 5402,
            "groups": [
                {
                    "id": "task_choice",
                    "name": "task_choice",
                    "type": "binary",
                    "count": 5310,
                    "lowerBound": 0,
                    "upperBound": 1,
                    "meaning": "任务选择",
                }
            ],
        },
        "objectives": {
            "stages": [
                {
                    "priority": 1,
                    "name": "总经济成本",
                    "sense": "minimize",
                    "expressionTermCount": 5341,
                },
                {
                    "priority": 2,
                    "name": "完成任务数",
                    "sense": "maximize",
                    "expressionTermCount": 5310,
                },
            ]
        },
        "constraints": {
            "total": 11472,
            "groups": [{"kind": "linear", "count": 747, "meaning": "线性约束"}],
            "samples": [
                {
                    "id": "linear_1",
                    "expression": "x_1 = 1",
                    "lowerBound": 1,
                    "upperBound": 1,
                }
            ],
        },
        "parameters": [
            {
                "id": "船租",
                "label": "船租",
                "value": 12000,
                "unit": "元/艘天",
                "source": "基础数据",
                "asOf": "2026-10-23T18:00:00+08:00",
            }
        ],
        "dataSnapshot": {
            "asOf": "2026-10-23T18:00:00+08:00",
            "inputHash": "hash-a",
        },
    }


def _toolbox(
    temp_db_path: Path,
    with_artifact: bool = True,
    with_third: bool = False,
    user_message: str | None = None,
) -> AgentToolbox:
    db = Database(temp_db_path)
    versions = VersionRepository(db)
    artifacts = ModelArtifactRepository(db)
    versions.add(_version("ver_00000001", "2026-10-27T10:00:00+08:00"))
    versions.add(_version("ver_00000002", "2026-10-27T11:00:00+08:00"))
    if with_third:
        versions.add(
            _version(
                "ver_00000003",
                "2026-10-27T12:00:00+08:00",
                total_cost=5_110_700.0,
                scheduled=3,
                strategy="高风险优先抢修",
                task_ids=("任务018", "任务022", "任务023"),
            )
        )
    if with_artifact:
        artifacts.add(
            "ver_00000002",
            "conv_tools",
            "2026-10-27T11:00:01+08:00",
            _artifact(),
        )
    return AgentToolbox(
        conversation_id="conv_tools",
        versions=versions,
        model_artifacts=artifacts,
        user_message=user_message,
    )


def test_read_without_versions_lists_plans(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_versions", {})

    assert result["status"] == "ok"
    assert result["data"]["mode"] == "index"
    rows = result["data"]["versions"]
    assert [item["planNumber"] for item in rows] == [1, 2]
    assert rows[1]["status"] == "FEASIBLE"
    assert rows[1]["totalCostWan"] == 472.57


def test_resolve_version_accepts_ordinal_and_id(temp_db_path: Path) -> None:
    toolbox = _toolbox(temp_db_path)

    assert toolbox.resolve_version("方案02").id == "ver_00000002"
    assert toolbox.resolve_version("01").id == "ver_00000001"
    assert toolbox.resolve_version("ver_00000002").id == "ver_00000002"
    assert toolbox.resolve_version("方案09") is None


def test_read_single_version_returns_facts(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_versions", {"versions": ["方案02"]})

    assert result["status"] == "ok"
    data = result["data"]
    assert data["mode"] == "single"
    assert data["planNumber"] == 2
    assert data["label"] == "方案02"
    assert data["totalCostWan"] == 472.57


def test_read_single_includes_tasks_and_status(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_versions",
        {"versions": ["方案02"], "include": ["tasks", "status"]},
    )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["tasks"][0]["taskId"] == "任务018"
    assert data["solve"]["provenOptimal"] is False
    assert data["solve"]["elapsedSeconds"] == 300.5


def test_read_multiple_versions_returns_matrix(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path, with_third=True).execute(
        "read_versions",
        {"versions": ["方案01", "方案02", "方案03"], "focus": "成本"},
    )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["mode"] == "compare"
    assert [item["planNumber"] for item in data["plans"]] == [1, 2, 3]
    assert len(data["matrix"]["columns"]) == 3
    rows = {row["key"]: row for row in data["matrix"]["rows"]}
    assert rows["totalCost"]["values"] == [472.57, 472.57, 511.07]
    assert rows["totalCost"]["bestIndex"] == 0
    assert rows["scheduled"]["values"] == [2, 2, 3]
    assert rows["scheduled"]["bestIndex"] == 2
    assert data["differences"]["changed"]
    keys = [item["key"] for item in data["differences"]["changed"]]
    assert keys[0] == "scheduled"
    assert "totalCost" in keys
    assert data["differences"]["changed"][0]["alwaysWatch"] is True
    assert "cashCost" in data["differences"]["identical"]
    assert data["comparability"]["savingsClaimAllowed"] is False


def test_read_marks_incomparable_task_sets(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path, with_third=True).execute(
        "read_versions",
        {"versions": ["方案01", "方案03"]},
    )

    comparability = result["data"]["comparability"]
    assert comparability["sameDataSnapshot"] is True
    assert comparability["sameScheduledTasks"] is False
    assert comparability["savingsClaimAllowed"] is False
    assert any("任务集合" in note for note in comparability["notes"])


def test_read_model_include_returns_latex(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_versions",
        {"versions": ["方案02"], "include": ["model"]},
    )

    assert result["status"] == "ok"
    markdown = result["data"]["markdown"]
    assert "$$" in markdown
    assert r"\operatorname{lexmin}" in markdown
    assert r"x_{i,k,l}\in\{0,1\}" in markdown
    assert "FEASIBLE" in markdown


def test_read_model_section_limits_output(temp_db_path: Path) -> None:
    toolbox = _toolbox(temp_db_path)

    objective = toolbox.execute(
        "read_versions",
        {"versions": ["方案02"], "include": ["model"], "section": "objective"},
    )["data"]["markdown"]
    constraints = toolbox.execute(
        "read_versions",
        {"versions": ["方案02"], "include": ["model"], "section": "constraints"},
    )["data"]["markdown"]

    assert "目标函数" in objective
    assert "主要约束" not in objective
    assert "主要约束" in constraints
    assert "决策变量" not in constraints


def test_read_model_include_supported_for_multiple(temp_db_path: Path) -> None:
    """2026-10-29：多版 + include=model 不再是拒绝，而是返回结构化投影与 modelDiffs。"""
    result = _toolbox(temp_db_path).execute(
        "read_versions",
        {"versions": ["方案01", "方案02"], "include": ["model"]},
    )

    assert result["status"] == "ok"
    assert len(result["data"]["plans"]) == 2
    assert "model" in result["data"]["plans"][1]
    assert isinstance(result["data"]["modelDiffs"], list)


def test_read_missing_artifact_reports_unavailable_model(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path, with_artifact=False).execute(
        "read_versions",
        {"versions": ["方案02"], "include": ["model"]},
    )

    assert result["status"] == "ok"
    assert "没有保存完整模型产物" in result["data"]["modelUnavailable"]
    assert result["warnings"]


def test_read_parameters_include_artifact_values(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_versions",
        {"versions": ["方案02"], "include": ["parameters"]},
    )

    assert result["status"] == "ok"
    assert result["data"]["parameters"][0]["label"] == "船租"
    assert result["data"]["parameters"][0]["value"] == 12000


def test_read_include_without_versions_uses_latest(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_versions", {"include": ["parameters"]})

    assert result["status"] == "ok"
    assert result["data"]["mode"] == "single"
    assert result["data"]["planNumber"] == 2
    assert result["data"]["parameters"][0]["label"] == "船租"


def test_read_unknown_version_returns_not_found(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_versions", {"versions": ["方案09"]})

    assert result["status"] == "not_found"
    assert result["data"]["unresolved"] == ["方案09"]
    assert result["nextActions"]


def test_read_keeps_resolved_versions_and_flags_missing(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_versions",
        {"versions": ["方案01", "方案09"]},
    )

    assert result["status"] == "ok"
    assert result["data"]["mode"] == "single"
    assert result["data"]["planNumber"] == 1
    assert result["data"]["unresolved"] == ["方案09"]


def test_solve_returns_change_preview_without_solving(temp_db_path: Path) -> None:
    toolbox = _toolbox(temp_db_path)
    before = len(toolbox.versions.list_by_conversation("conv_tools"))

    result = toolbox.execute(
        "solve",
        {"instruction": "按高风险优先重新排程", "baseVersion": "方案02"},
    )

    assert result["status"] == "needs_confirmation"
    data = result["data"]
    assert data["requiresUserConfirmation"] is True
    assert data["basePlanNumber"] == 2
    assert "尚未求解" in data["preview"]
    assert data["operations"][0]["after"] == "高风险优先抢修"
    assert toolbox.pending_change is not None
    assert len(toolbox.versions.list_by_conversation("conv_tools")) == before


def test_solve_without_recognized_change_asks_for_clarification(
    temp_db_path: Path,
) -> None:
    result = _toolbox(temp_db_path).execute(
        "solve",
        {"instruction": "今天天气怎么样"},
    )

    assert result["status"] == "clarify"
    assert result["data"]["requiresUserConfirmation"] is False
    assert "还没有识别出需要修改的模型内容" in result["data"]["message"]


def test_solve_clarify_still_carries_a_contract_for_the_user(
    temp_db_path: Path,
) -> None:
    """没听懂时也必须给系统口径，不能让模型自己圆场。

    这是"模型在正文里说排除了 9-13、实际一条排除都没有"的另一半：如果 solve
    返回 clarify 却只有给模型看的 message，模型完全可以说一句"已按你说的改好"，
    用户永远看不到"我没听懂"这句话。
    """
    result = _toolbox(temp_db_path).execute("solve", {"instruction": "今天天气怎么样"})

    data = result["data"]
    assert data["markdown"] == data["message"]
    assert data["verbatim"] is True


def test_submit_change_refusal_carries_a_contract(
    temp_db_path: Path,
) -> None:
    """派不出去（或没预览就提交），拒绝理由同样要原样送到用户眼前。

    这套工具箱没接求解服务，所以先命中 unavailable；无论落在哪个分支，
    都必须带 markdown——否则模型会把"没派出去"说成"已派出"。
    """
    result = _toolbox(temp_db_path).execute("submit_change", {"reason": "直接算"})

    assert result["status"] in {"unavailable", "clarify"}
    data = result["data"]
    assert data.get("markdown"), result
    assert data["verbatim"] is True


def test_compare_pair_still_returns_three_layers(temp_db_path: Path) -> None:
    toolbox = _toolbox(temp_db_path)
    payload = toolbox.compare_pair(
        toolbox.resolve_version("方案01"),
        toolbox.resolve_version("方案02"),
    )

    assert set(payload) >= {"layer1Facts", "layer2Drivers", "layer3Index"}
    assert payload["layer1Facts"]["left"]["planNumber"] == 1
    assert payload["layer3Index"]["drilldown"]["readTool"] == "read_versions"


def test_read_environment_returns_wind_and_price_series(
    temp_db_path: Path,
) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_environment",
        {"days": 7},
    )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["hoursCovered"] == 168
    assert len(data["daily"]) >= 7
    assert data["dataNature"] in {"合成", "真实"}
    assert data["inputHash"]
    assert result["provenance"]["dataAsOf"]
    assert "风速" in data["markdown"] and "电价" in data["markdown"]
    assert data["verbatim"] is True


def test_read_environment_can_return_hourly_detail(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_environment",
        {"days": 2, "detail": "hourly"},
    )

    assert result["status"] == "ok"
    assert len(result["data"]["hourly"]) == 48
    assert set(result["data"]["hourly"][0]) == {"time", "wind", "price", "powerMw"}


def test_read_environment_chart_returns_all_switchable_ranges(
    temp_db_path: Path,
) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_environment",
        {"days": 30, "detail": "chart", "defaultRange": 7},
    )

    assert result["status"] == "ok"
    chart = result["data"]["chartSpec"]
    assert chart["schemaVersion"] == "environment-chart.v1"
    assert chart["defaultRange"] == 7
    assert chart["availableRanges"] == [3, 7, 14, 30]
    assert len(chart["hourly"]) == 168
    assert len(chart["daily"]) >= 30
    assert chart["annotations"]["planningBoundary"]
    assert chart["annotations"]["targetWindow"]["start"]
    assert "```chart" in result["data"]["markdown"]


def test_read_environment_can_scope_to_one_window(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_environment",
        {"window": "窗口02", "detail": "window"},
    )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["window"]["id"] == "窗口02"
    assert data["window"]["start"].startswith("2026-11-02")
    assert data["window"]["end"].startswith("2026-11-12")
    assert len(data["daily"]) == 10
    assert "窗口02 的风况与电价" in data["markdown"]
    assert data["verbatim"] is True


def test_read_catalog_lists_registered_strategies(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_catalog", {})

    assert result["status"] == "ok"
    names = [item["name"] for item in result["data"]["strategies"]]
    assert names == [
        "高风险优先抢修",
        "成本最低优先",
        "最短完成优先",
        "检修最多器件优先",
    ]
    assert "总经济成本" in result["data"]["sharedWeightedMetrics"]


def test_read_versions_reports_deltas_against_chosen_baseline(
    temp_db_path: Path,
) -> None:
    result = _toolbox(temp_db_path, with_third=True).execute(
        "read_versions",
        {"versions": ["方案01", "方案02", "方案03"], "baseline": "方案01"},
    )

    deltas = result["data"]["deltasVsBaseline"]
    assert deltas["baselineLabel"] == "方案01"
    row = next(item for item in deltas["rows"] if item["key"] == "totalCost")
    assert row["baseline"] == 472.57
    third = next(item for item in row["plans"] if item["label"] == "方案03")
    assert third["delta"] == 38.5
    assert third["deltaPct"] == 8.15


def test_read_versions_reports_task_presence_changes(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path, with_third=True).execute(
        "read_versions",
        {"versions": ["方案01", "方案02", "方案03"]},
    )

    presence = result["data"]["taskPresence"]
    assert presence["common"] == ["任务018"]
    assert presence["onlyIn"] == {"方案03": ["任务022", "任务023"]}
    assert "任务集合不同" in presence["universeNote"]


def test_read_versions_costs_view_explains_composition(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_versions",
        {"versions": ["方案02"], "include": ["costs"]},
    )

    assert result["status"] == "ok"
    costs = result["data"]["costs"]
    assert costs["totalCostWan"] == 472.57
    assert costs["cashCostWan"] == 44.18
    # 测试用的历史版本没有保存成本分项，必须如实说明而不是编造。
    assert "船租" in costs["missingItems"]
    assert "现金支出" in costs["markdown"]


def test_read_alarms_returns_current_backlog_only(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_alarms", {})

    assert result["status"] == "ok"
    data = result["data"]
    assert data["asOf"] == "2026-10-23 18:00"
    assert data["counts"]["open"] == 11
    assert data["counts"]["bySeverity"] == {"高": 4, "中": 4, "低": 3}
    assert len(data["rows"]) == 11
    assert data["rows"][0]["taskId"] == "任务001"
    assert "任务012" not in {item["taskId"] for item in data["rows"]}
    assert "待安排告警 11 项" in data["markdown"]
    assert "合成" in data["markdown"]
    assert result["provenance"]["dataAsOf"] == "2026-10-23 18:00"


def test_read_alarms_filters_severity_and_recent_days(temp_db_path: Path) -> None:
    toolbox = _toolbox(temp_db_path)

    high = toolbox.execute("read_alarms", {"severity": "高"})
    assert high["status"] == "ok"
    assert len(high["data"]["rows"]) == 4
    assert {item["severity"] for item in high["data"]["rows"]} == {"高"}

    recent = toolbox.execute("read_alarms", {"scope": "recent", "days": 1})
    assert recent["status"] == "ok"
    assert [item["taskId"] for item in recent["data"]["rows"]] == ["任务011"]
    assert "最近 1 天新增告警 1 项" in recent["data"]["markdown"]

    yesterday = toolbox.execute(
        "read_alarms", {"scope": "recent", "date": "yesterday"}
    )
    assert yesterday["status"] == "ok"
    assert len(yesterday["data"]["rows"]) == 10
    assert "2026-10-22 新增告警 10 项" in yesterday["data"]["markdown"]
    assert "不能据此说该日没有新增" in yesterday["data"]["markdown"]


def test_read_alarms_summary_reports_distribution(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_alarms", {"scope": "summary"})

    assert result["status"] == "ok"
    assert result["data"]["counts"]["bySeverity"]["高"] == 4
    assert "告警概况" in result["data"]["markdown"]
    assert "计划快照" in result["data"]["markdown"]


def test_read_alarms_can_query_closed_history_without_counting_it_as_open(
    temp_db_path: Path,
) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_alarms",
        {"status": "已完成", "includeClosed": True},
    )

    assert result["status"] == "ok"
    assert result["data"]["counts"]["open"] == 0
    assert result["data"]["counts"]["closed"] == 0
    assert result["data"]["rows"] == []
    assert "已完成告警 0 项" in result["data"]["markdown"]
    assert "待安排 11 项" not in result["data"]["markdown"]


def test_read_site_returns_windows_fleet_components_inventory_and_parameters(
    temp_db_path: Path,
) -> None:
    toolbox = _toolbox(temp_db_path)

    windows = toolbox.execute("read_site", {"scope": "windows"})
    assert windows["status"] == "ok"
    assert windows["data"]["targetWindow"]["windowId"] == "窗口02"
    state = {item["windowId"]: item["state"] for item in windows["data"]["rows"]}
    assert state["窗口01"] == "进行中"
    assert state["窗口02"] == "目标窗口"

    fleet = toolbox.execute("read_site", {"scope": "fleet"})
    assert fleet["status"] == "ok"
    assert len(fleet["data"]["rows"]) == 100
    assert fleet["data"]["rows"][0]["turbineId"] == "风机001"

    components = toolbox.execute("read_site", {"scope": "components"})
    assert components["status"] == "ok"
    assert len(components["data"]["rows"]) == 8
    assert components["data"]["rows"][0]["componentName"] == "叶片"

    inventory = toolbox.execute("read_site", {"scope": "inventory"})
    assert inventory["status"] == "ok"
    leaf = next(
        item for item in inventory["data"]["rows"] if item["componentId"] == "部件01"
    )
    assert leaf["stock"] == 3
    assert leaf["availableNow"] == 2
    assert leaf["availableAtTargetStart"] == 4

    parameters = toolbox.execute("read_site", {"scope": "parameters"})
    assert parameters["status"] == "ok"
    rent = next(
        item for item in parameters["data"]["rows"] if item["name"] == "船租（元每艘每天）"
    )
    assert rent["value"] == 12000


def test_read_site_reports_empty_work_orders_without_claiming_no_domain(
    temp_db_path: Path,
) -> None:
    toolbox = _toolbox(temp_db_path)

    orders = toolbox.execute("read_site", {"scope": "work_orders"})
    assert orders["status"] == "ok"
    assert orders["data"]["counts"]["total"] == 0
    assert "当前没有已下发的检修单" in orders["data"]["markdown"]

    execution = toolbox.execute("read_site", {"scope": "execution"})
    assert execution["status"] == "ok"
    assert "暂无执行反馈" in execution["data"]["markdown"]


def test_read_site_parameter_search_reads_the_full_catalog(temp_db_path: Path) -> None:
    toolbox = _toolbox(temp_db_path)

    rent = toolbox.execute(
        "read_site", {"scope": "parameters", "query": "船租怎么取的"}
    )
    assert rent["status"] == "ok"
    assert rent["data"]["query"] == "船租怎么取的"
    row = next(item for item in rent["data"]["rows"] if item["name"] == "船舶日租金")
    assert row["value"] == 12000
    assert row["sourceStatus"]
    assert "参数查询" in rent["data"]["markdown"]

    granularity = toolbox.execute(
        "read_site", {"scope": "parameters", "query": "候选开工粒度"}
    )
    assert granularity["status"] == "ok"
    match = granularity["data"]["rows"][0]
    assert match["name"] == "候选开工粒度"
    assert match["value"] == 60


def test_read_site_overview_counts_all_site_domains(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_site", {"scope": "overview"})

    assert result["status"] == "ok"
    counts = result["data"]["counts"]
    assert counts["turbines"] == 100
    assert counts["components"] == 8
    assert counts["alarms"]["open"] == 11
    assert counts["inventorySkus"] == 8
    assert counts["workOrders"] == 0
    assert "没有接入实时 SCADA" in result["data"]["markdown"]


# ── N 版模型投影（compare_versions 并入 read_versions）─────────────────────

def test_read_versions_projects_model_for_multiple_versions(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_versions",
        {"versions": ["方案01", "方案02"], "include": ["model"]},
    )

    assert result["status"] == "ok"
    plans = result["data"]["plans"]
    assert len(plans) == 2
    # 只有方案02 有模型产物
    assert plans[0]["model"]["available"] is False
    assert plans[1]["model"]["available"] is True
    assert isinstance(result["data"]["modelDiffs"], list)


def test_single_version_model_read_returns_markdown(temp_db_path: Path) -> None:
    """单版读模型仍走 detail：给 markdown/latex，不是对比矩阵。"""
    result = _toolbox(temp_db_path).execute(
        "read_versions",
        {"versions": ["方案02"], "include": ["model"]},
    )

    assert result["status"] == "ok"
    assert result["data"]["mode"] == "single"
    assert "markdown" in result["data"]
    assert result["data"]["modelFingerprint"]


def test_single_version_read_is_not_a_compare(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute(
        "read_versions",
        {"versions": ["方案02"], "include": ["model"]},
    )
    # 单版走 detail 路径，不产生差异
    assert result["data"]["mode"] != "compare"


# ── 原话 ↔ DSL：solve 是最后一道能提前拦住的口子 ──────────────────────────


def _fake_dsl_change(dsl: dict, preview: str = "变更预览：以 DSL 为准"):
    from app.core.change_planning import DslChange
    from dsl_core.planning_request import PlanningRequest

    return DslChange(
        dsl=dsl,
        request=PlanningRequest(),
        residual=True,  # expressible=False → 走 DSL 段
        preview=preview,
    )


def test_solve_refuses_when_the_dsl_drops_the_count_limit(
    temp_db_path: Path, monkeypatch
) -> None:
    """用户说"只修前 8 个"，解析成 8 条 must = 一个上限都没压住。

    这是复盘里那次真实事故：预览照常生成、用户点了确认、求解把在册任务全排上。
    现在必须在预览之前就拒绝，并且把上一轮那份 pending 清掉——否则模型还能
    直接 submit_change 把它派出去，等于绕过这次拒绝。
    """
    from app.core import change_planning

    dsl = {"constraints": [{"type": "must", "task": f"任务{i:03d}"} for i in range(1, 9)]}
    monkeypatch.setattr(change_planning, "plan_dsl_change", lambda *a, **k: _fake_dsl_change(dsl))

    toolbox = _toolbox(temp_db_path, user_message="只修前8个，其余不安排")
    toolbox.pending_change = {"kind": "dsl", "dsl": {"constraints": []}}
    result = toolbox.execute("solve", {"instruction": "只修前8个，其余不安排"})

    assert result["status"] == "clarify", result
    markdown = result["data"]["markdown"]
    assert "对不上" in markdown
    assert "没有把数量压到 8 个" in markdown
    assert result["data"]["verbatim"] is True
    assert toolbox.pending_change is None, "拒绝之后不能还留着待确认变更"


def test_solve_keeps_the_audit_in_the_preview_when_it_passes(
    temp_db_path: Path, monkeypatch
) -> None:
    """对得上也要把核对结果写进预览：用户要看的是"系统怎么理解我说的"。"""
    from app.core import change_planning

    dsl = {"constraints": [{"type": "cardinality", "filter": {}, "op": "<=", "value": 8}]}
    monkeypatch.setattr(change_planning, "plan_dsl_change", lambda *a, **k: _fake_dsl_change(dsl))

    toolbox = _toolbox(temp_db_path, user_message="只修前8个")
    result = toolbox.execute("solve", {"instruction": "只修前8个"})

    assert result["status"] == "needs_confirmation", result
    markdown = result["data"]["markdown"]
    assert "原话核对" in markdown
    assert "全部对上" in markdown
    assert toolbox.pending_change is not None
    assert toolbox.pending_change["utterance"] == "只修前8个", "原话要跟着变更一起走，求解时再核一次"


def test_solve_without_a_claim_says_it_did_not_check(temp_db_path: Path, monkeypatch) -> None:
    """参数类变更抽不到断言时，预览必须写明"没核对"，不能默认通过。"""
    from app.core import change_planning

    dsl = {"parameters": [{"field": "船舶日租金", "op": "=", "value": 50000}]}
    monkeypatch.setattr(change_planning, "plan_dsl_change", lambda *a, **k: _fake_dsl_change(dsl))

    toolbox = _toolbox(temp_db_path, user_message="船租改成5万")
    result = toolbox.execute("solve", {"instruction": "船租改成5万"})

    assert result["status"] == "needs_confirmation", result
    assert "没有系统能核对的具体诉求" in result["data"]["markdown"]
    assert "不能说它等于你要的那件事" in result["data"]["markdown"]
