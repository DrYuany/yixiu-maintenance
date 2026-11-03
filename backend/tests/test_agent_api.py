from __future__ import annotations

import os
import json
import re
import time

from fastapi.testclient import TestClient
import pytest

from app.core.agent_runtime import AgentRuntime


def _chart_spec(content: str) -> dict:
    match = re.search(r"```chart\s*(\{.*?\})\s*```", content, flags=re.DOTALL)
    assert match, "environment answer should contain a chart block"
    return json.loads(match.group(1))


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("未来三天风速", 3),
        ("接下来一周", 7),
        ("未来两周", 14),
        ("未来一个月", 30),
        ("未来10天", 7),
        ("未来60天", 30),
        ("两周和30天都看看", 30),
        ("风速和电价可视化", 7),
    ],
)
def test_environment_chart_range_is_deterministic(content: str, expected: int) -> None:
    assert AgentRuntime._environment_chart_range(content) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("窗口02的风况怎么样", "窗口02"),
        ("窗口 2 的天气", "窗口02"),
        ("目标窗口的可用小时", "target"),
        ("接下来一周的风速", None),
    ],
)
def test_environment_window_reference_is_deterministic(content, expected) -> None:
    assert AgentRuntime._environment_window(content) == expected


def _report(status: str = "OPTIMAL", has_solution: bool = True) -> dict:
    recommendation = {
        "完成任务数": 1,
        "待修总数": 1,
        "未安排": [],
        "最晚完工": "2026-10-23T12:00:00+08:00",
        "船舶艘天": 1,
        "船租": 12000.0,
        "人工": 600.0,
        "备件": 1000.0,
        "其他费用": 0.0,
        "发电损失": 4000.0,
        "总成本": 17600.0,
        "现金支出": 13600.0,
        "复核通过": True,
        "排程": [{"任务编号": "任务001", "风机编号": "风机001", "部件名称": "叶片", "严重等级": "高", "窗口编号": "窗口01", "开始": 480, "完成": 720, "作业段": [[480, 720]]}],
    } if has_solution else None
    return {
        "版本": "3.0",
        "策略": "高风险优先抢修",
        "目标模式": "分层",
        "数据": {"输入哈希": "input-1", "运行哈希": "run-1"},
        "求解": {"有解": has_solution, "分层全部最优": status == "OPTIMAL", "总耗时秒": 0.2, "阶段": [{"状态": status, "目标值": 17600.0, "界": 17600.0}]},
        "窗口": [{"编号": "窗口01", "开始": 0, "结束": 1440, "开始日期": "2026-10-23T00:00:00+08:00", "结束时间不含": "2026-10-24T00:00:00+08:00"}],
        "人工基准": {"严重性优先": {"完成任务数": 1, "总成本": 20000.0}, "低风速优先": {"完成任务数": 1, "总成本": 21000.0}},
        "推荐": recommendation,
        "模型产物": {
            "schemaVersion": "maintenance-model-artifact-v1",
            "modelType": "CP-SAT bounded integer scheduling with linear cost",
            "problemStatement": "测试模型",
            "strategy": "高风险优先抢修",
            "mode": "分层",
            "weights": None,
            "partialDiagnostic": False,
            "modelFingerprint": "model-artifact-1",
            "dataSnapshot": {"asOf": "2026-10-23T18:00:00+08:00"},
            "dimensions": {"tasks": 1, "windows": 1, "workLines": 1, "candidateChoices": 1},
            "variables": {
                "total": 2,
                "groups": [{
                    "id": "task_choice",
                    "name": "task_choice",
                    "type": "binary",
                    "count": 1,
                    "lowerBound": 0,
                    "upperBound": 1,
                    "meaning": "任务选择变量",
                    "samples": ["x_任务001_0_1"],
                }],
            },
            "objectives": {
                "mode": "分层",
                "stages": [{
                    "priority": 1,
                    "name": "总经济成本",
                    "sense": "minimize",
                    "formula": "总经济成本 = 固定损失 + Σ任务成本 × x",
                    "expressionCharacters": 100,
                }],
                "weights": None,
            },
            "constraints": {
                "total": 1,
                "groups": [{
                    "kind": "linear",
                    "count": 1,
                    "meaning": "任务选择约束",
                    "names": [],
                }],
            },
            "parameters": [{
                "id": "船租（元每艘每天）",
                "label": "船租",
                "value": 12000,
                "unit": "元/艘/天",
                "source": "基座参数快照",
                "baseSource": "测试数据",
                "dataNature": "合成",
                "asOf": "2026-10-23T18:00:00+08:00",
                "override": False,
            }],
            "solverParameters": {"num_search_workers": 1},
            "provenance": {"generatedFrom": "test"},
            "rawModel": {"file": "solver_model.pbtxt", "sha256": "a", "sizeBytes": 1},
        },
    }


def _client(temp_db_path, monkeypatch, report: dict | None = None) -> TestClient:
    os.environ["MWA_DB_PATH"] = str(temp_db_path)
    monkeypatch.setattr(
        "app.core.maintenance_product_executor.MaintenanceProductExecutor.execute",
        lambda self, request, job_id, model_fingerprint=None, dsl=None, utterance=None: report or _report(),
    )
    from app.main import create_app
    return TestClient(create_app())


def _wait_for_job(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        job = client.get(f"/api/agent/jobs/{job_id}").json()
        if job["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def _preview_and_confirm(
    client: TestClient,
    conversation_id: str,
    message: str,
    selected_version_id: str | None = None,
) -> tuple[dict, dict]:
    preview = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation_id,
            "message": message,
            "selectedVersionId": selected_version_id,
        },
    ).json()
    assert preview["actionType"] == "change_preview"
    confirmed = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation_id,
            "message": "确认生成沙盘",
            "selectedVersionId": selected_version_id,
        },
    ).json()
    assert confirmed["actionType"] == "solve_started"
    return preview, confirmed


def test_planning_request_returns_immediately_without_llm_dependency(temp_db_path, monkeypatch) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "new"}).json()
    preview = client.post(
        "/api/agent/message",
        json={"conversationId": conversation["id"], "message": "按高风险优先重新排程"},
    )
    response = client.post(
        "/api/agent/message",
        json={"conversationId": conversation["id"], "message": "确认生成沙盘"},
    )

    assert preview.status_code == 200
    assert preview.json()["actionType"] == "change_preview"
    assert response.status_code == 200
    payload = response.json()
    assert payload["actionType"] == "solve_started"
    assert payload["solveJob"]["request"]["strategy"] == "高风险优先抢修"
    assert payload["solveJob"]["modelFingerprint"] == payload["decisionArtifact"]["modelFingerprint"]


def test_overtime_daily_hours_phrase_returns_preview_without_llm(temp_db_path, monkeypatch) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "overtime"}).json()

    preview = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "每天可以加班，最多维修10小时",
        },
    ).json()
    detail = client.get(f"/api/conversations/{conversation['id']}").json()

    assert preview["actionType"] == "change_preview"
    pending = detail["workingMemory"]["activeFilters"]["pendingChange"]
    assert pending["operations"] == [{
        "kind": "parameter",
        "key": "每天最多维修小时数",
        "before": None,
        "after": 10.0,
    }]


def test_capability_question_is_answered_without_any_solution(temp_db_path, monkeypatch) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "capability"}).json()

    response = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "制定排程时，我有哪些目标函数可以设置权重",
        },
    ).json()

    assert response["actionType"] == "capability_answer"
    content = response["assistantMessage"]["content"]
    assert "高风险优先抢修" in content
    assert "总经济成本" in content
    assert "请先生成方案" not in content
    assert response["solveJob"] is None


def test_environment_question_returns_a_chart_without_llm(temp_db_path, monkeypatch) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "environment"}).json()

    response = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "接下来一周的风速和电价可视化一下",
        },
    ).json()

    assert response["actionType"] == "environment_answer"
    content = response["assistantMessage"]["content"]
    assert "风速" in content and "电价" in content
    chart = _chart_spec(content)
    assert chart["defaultRange"] == 7
    assert chart["availableRanges"] == [3, 7, 14, 30]
    assert response["solveJob"] is None

    detail = client.get(f"/api/conversations/{conversation['id']}").json()
    saved = detail["messages"][-1]["content"]
    assert saved == content
    assert _chart_spec(saved) == chart


def test_environment_question_uses_requested_range_as_chart_default(
    temp_db_path,
    monkeypatch,
) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "environment"}).json()

    response = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "把接下来两周的风速和电价画出来",
        },
    ).json()

    content = response["assistantMessage"]["content"]
    assert response["actionType"] == "environment_answer"
    chart = _chart_spec(content)
    assert chart["defaultRange"] == 14
    assert chart["availableRanges"] == [3, 7, 14, 30]


def test_window_environment_question_uses_the_selected_window(
    temp_db_path,
    monkeypatch,
) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "window-env"}).json()

    response = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "窗口02的风况和电价怎么样",
        },
    ).json()

    assert response["actionType"] == "environment_answer"
    content = response["assistantMessage"]["content"]
    assert "窗口02 的风况与电价" in content
    assert "2026-11-02 00:00" in content
    assert "2026-11-12 00:00" in content
    assert "```chart" not in content


def test_alarm_question_is_answered_from_site_data_without_llm(
    temp_db_path,
    monkeypatch,
) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "alarms"}).json()

    response = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "现在告警的有哪些",
        },
    ).json()

    assert response["actionType"] == "alarm_answer"
    content = response["assistantMessage"]["content"]
    assert "待安排告警 11 项" in content
    assert "高 4" in content and "中 4" in content and "低 3" in content
    assert "任务001" in content
    assert "合成" in content
    assert "没有告警" not in content
    assert "请先生成方案" not in content
    assert response["solveJob"] is None


def test_site_lookup_questions_are_answered_without_llm(
    temp_db_path,
    monkeypatch,
) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "site"}).json()

    def ask(message: str) -> dict:
        return client.post(
            "/api/agent/message",
            json={"conversationId": conversation["id"], "message": message},
        ).json()

    rent = ask("船租多少钱一天")
    assert rent["actionType"] == "site_answer"
    assert "12000" in rent["assistantMessage"]["content"]
    assert "元每艘每天" in rent["assistantMessage"]["content"]

    rent_source = ask("船租怎么取的")
    assert rent_source["actionType"] == "site_answer"
    assert "船舶日租金" in rent_source["assistantMessage"]["content"]
    assert "演示默认" in rent_source["assistantMessage"]["content"]

    granularity = ask("候选开工粒度是多少")
    assert granularity["actionType"] == "site_answer"
    assert "候选开工粒度" in granularity["assistantMessage"]["content"]
    assert "60" in granularity["assistantMessage"]["content"]

    windows = ask("下一个窗口是什么时候")
    assert windows["actionType"] == "site_answer"
    assert "窗口02" in windows["assistantMessage"]["content"]
    assert "7 天准备期" in windows["assistantMessage"]["content"]

    inventory = ask("库存还有多少")
    assert inventory["actionType"] == "site_answer"
    assert "备件库存" in inventory["assistantMessage"]["content"]
    assert "叶片" in inventory["assistantMessage"]["content"]


def test_closed_alarm_question_does_not_return_the_open_backlog(
    temp_db_path,
    monkeypatch,
) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "history"}).json()

    response = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "已完成的告警有哪些",
        },
    ).json()

    assert response["actionType"] == "alarm_answer"
    content = response["assistantMessage"]["content"]
    assert "已完成告警 0 项" in content
    assert "待安排告警 11 项" not in content


def test_yesterday_alarm_question_returns_that_day_only(
    temp_db_path,
    monkeypatch,
) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "yesterday"}).json()

    response = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "昨天有什么告警",
        },
    ).json()

    assert response["actionType"] == "alarm_answer"
    content = response["assistantMessage"]["content"]
    assert "2026-10-22 新增告警 10 项" in content
    assert "任务011" not in content


def test_weight_outside_current_strategy_blocks_instead_of_solving_wrong_model(
    temp_db_path,
    monkeypatch,
) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "weights"}).json()

    response = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "改成加权，高严重完成数 0.8，总经济成本 0.2，重新排程",
        },
    ).json()

    assert response["actionType"] == "change_clarification"
    assert "不在「成本最低优先」的可加权指标里" in response["assistantMessage"]["content"]
    assert response["solveJob"] is None
    assert response["newVersions"] == []


def test_explicit_solve_command_starts_real_default_job_and_returns_progress_payload(
    temp_db_path,
    monkeypatch,
) -> None:
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "default solve"}).json()

    response = client.post(
        "/api/agent/message",
        json={"conversationId": conversation["id"], "message": "你发起求解"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["actionType"] == "solve_started"
    assert payload["solveJob"]["id"].startswith("job_")
    assert payload["solveJob"]["status"] in {"QUEUED", "RUNNING"}
    assert payload["solveJob"]["progressPercent"] >= 0
    assert payload["solveEvidence"]["running"] is True
    persisted = client.get(f"/api/agent/jobs/{payload['solveJob']['id']}")
    assert persisted.status_code == 200


def test_background_job_persists_real_result_and_dashboard_version(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "new"}).json()
    _, started = _preview_and_confirm(client, conversation["id"], "按高风险优先重新排程")
    completed = _wait_for_job(client, started["solveJob"]["id"])
    dashboard = client.get(f"/api/dashboard/{conversation['id']}").json()
    detail = client.get(f"/api/conversations/{conversation['id']}").json()

    assert completed["status"] == "COMPLETED"
    assert completed["solverStatus"] == "OPTIMAL"
    assert completed["hasIncumbent"] is True
    assert len(dashboard["versions"]) == 1
    assert dashboard["versions"][0]["resultSummary"]["totalCost"] == 17600.0
    assert dashboard["versions"][0]["solveJobId"] == started["solveJob"]["id"]
    assert dashboard["versions"][0]["modelFingerprint"] == started["decisionArtifact"]["modelFingerprint"]
    assert "总成本：1.76 万元" in detail["messages"][-1]["content"]
    assert detail["messages"][-1]["versionIds"] == [completed["result"]["versionId"]]
    assert detail["messages"][-1]["jobId"] == started["solveJob"]["id"]
    model_artifact = client.get(
        f"/api/versions/{completed['result']['versionId']}/model"
    )
    assert model_artifact.status_code == 200
    assert model_artifact.json()["modelType"].startswith("CP-SAT")


def test_change_preview_does_not_mutate_versions_or_start_a_job(
    temp_db_path,
    monkeypatch,
) -> None:
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "preview"}).json()

    preview = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "船租上涨20%，其他不变",
        },
    ).json()
    dashboard = client.get(f"/api/dashboard/{conversation['id']}").json()
    detail = client.get(f"/api/conversations/{conversation['id']}").json()

    assert preview["actionType"] == "change_preview"
    assert preview["solveJob"] is None
    assert dashboard["versions"] == []
    pending = detail["workingMemory"]["activeFilters"]["pendingChange"]
    assert pending["operations"] == [{
        "kind": "relative_parameter",
        "key": "船租",
        "before": None,
        "after": 1.2,
    }]


def test_model_question_uses_selected_version_and_real_artifact(
    temp_db_path,
    monkeypatch,
) -> None:
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "model"}).json()
    _, started = _preview_and_confirm(client, conversation["id"], "按高风险优先重新排程")
    completed = _wait_for_job(client, started["solveJob"]["id"])
    version_id = completed["result"]["versionId"]

    formula = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "把这次求解对应的数学模型写出来",
            "selectedVersionId": version_id,
        },
    ).json()
    parameter = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "船租这个参数怎么来的",
            "selectedVersionId": version_id,
        },
    ).json()

    assert formula["actionType"] == "model_formula"
    assert version_id in formula["assistantMessage"]["content"]
    assert "CP-SAT" in formula["assistantMessage"]["content"]
    assert parameter["actionType"] == "model_parameter"
    assert "12000" in parameter["assistantMessage"]["content"]
    assert "基座参数快照" in parameter["assistantMessage"]["content"]


def test_model_question_is_explicit_when_artifact_is_missing(
    temp_db_path,
    monkeypatch,
) -> None:
    report = _report()
    report.pop("模型产物", None)
    client = _client(temp_db_path, monkeypatch, report)
    conversation = client.post("/api/conversations", json={"title": "missing model"}).json()
    _, started = _preview_and_confirm(client, conversation["id"], "按高风险优先重新排程")
    completed = _wait_for_job(client, started["solveJob"]["id"])
    version_id = completed["result"]["versionId"]

    response = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "把这次求解对应的数学模型写出来",
            "selectedVersionId": version_id,
        },
    ).json()

    assert response["actionType"] == "model_formula"
    assert "没有保存完整数学模型产物" in response["assistantMessage"]["content"]
    assert "我现在" not in response["assistantMessage"]["content"]
    detail = client.get(f"/api/conversations/{conversation['id']}").json()
    pending = detail["workingMemory"]["activeFilters"]["pendingChange"]
    assert pending["baseVersionId"] == version_id
    assert pending["operations"][0]["kind"] == "artifact_rebuild"


def test_unknown_is_not_reported_as_infeasible(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch, _report("UNKNOWN", has_solution=False))
    conversation = client.post("/api/conversations", json={"title": "new"}).json()
    _, started = _preview_and_confirm(client, conversation["id"], "按高风险优先重新排程")
    completed = _wait_for_job(client, started["solveJob"]["id"])
    detail = client.get(f"/api/conversations/{conversation['id']}").json()

    assert completed["solverStatus"] == "UNKNOWN"
    assert "未证明无解" in detail["messages"][-1]["content"]


def test_each_completed_run_appends_a_new_message_linked_version(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "history"}).json()

    _, first = _preview_and_confirm(client, conversation["id"], "按高风险优先重新排程")
    _wait_for_job(client, first["solveJob"]["id"])
    _, second = _preview_and_confirm(client, conversation["id"], "每天最多维修 8 小时")
    _wait_for_job(client, second["solveJob"]["id"])

    dashboard = client.get(f"/api/dashboard/{conversation['id']}").json()
    detail = client.get(f"/api/conversations/{conversation['id']}").json()
    linked_messages = [
        message for message in detail["messages"]
        if message["role"] == "assistant" and message["versionIds"]
    ]

    assert len(dashboard["versions"]) == 2
    assert len(linked_messages) == 2
    assert {
        version_id
        for message in linked_messages
        for version_id in message["versionIds"]
    } == {version["id"] for version in dashboard["versions"]}
    assert [message["jobId"] for message in linked_messages] == [
        first["solveJob"]["id"],
        second["solveJob"]["id"],
    ]
    first_version_id = dashboard["versions"][-1]["id"]
    selected_answer = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "把这次求解对应的数学模型写出来",
            "selectedVersionId": first_version_id,
        },
    ).json()
    assert selected_answer["actionType"] == "model_formula"
    assert first_version_id in selected_answer["assistantMessage"]["content"]

    textual_second = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "方案02的数学模型呢",
        },
    ).json()
    assert textual_second["actionType"] == "model_formula"
    assert dashboard["versions"][0]["id"] in textual_second["assistantMessage"]["content"]

    switched = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "切换到方案01我们聊聊",
        },
    ).json()
    switched_detail = client.get(f"/api/conversations/{conversation['id']}").json()
    assert switched["actionType"] == "context_switch"
    assert first_version_id in switched["assistantMessage"]["content"]
    assert switched_detail["workingMemory"]["selectedVersionIds"] == [first_version_id]


def test_plain_chat_returns_product_guidance(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "chat"}).json()
    response = client.post("/api/agent/message", json={"conversationId": conversation["id"], "message": "你用的什么算法求解"}).json()

    assert response["actionType"] == "assistant_reply"
    assert response["solveJob"] is None
    assert "OR-Tools CP-SAT" in response["assistantMessage"]["content"]


def test_remove_completed_version_updates_dashboard(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "conv"}).json()
    _, started = _preview_and_confirm(client, conversation["id"], "按高风险优先重新排程")
    completed = _wait_for_job(client, started["solveJob"]["id"])
    response = client.delete(f"/api/versions/{completed['result']['versionId']}")
    dashboard = client.get(f"/api/dashboard/{conversation['id']}").json()

    assert response.status_code == 204
    assert dashboard["versions"] == []
    assert dashboard["candidateWindowPool"] == []


def test_solve_reply_calls_out_heuristic_fallback_instead_of_a_proven_solve() -> None:
    from types import SimpleNamespace

    version = SimpleNamespace(
        windows=[
            SimpleNamespace(
                windowId="窗口03",
                metrics=SimpleNamespace(startTime="2026-11-15 00:00", endTime="2026-11-20 00:00"),
            )
        ]
    )
    summary = {
        "totalCost": 4991263.49,
        "scheduledTaskCount": 15,
        "totalTaskCount": 28,
        "baselineSavings": {},
        "solver": {"解状态": "FEASIBLE", "方案来源": "HEURISTIC_FALLBACK", "终止原因": "TIME_LIMIT"},
    }

    reply = AgentRuntime._format_product_reply("FEASIBLE", summary, version)

    assert "启发式" in reply
    assert "15/28" in reply
    assert "已找到并复核可行方案" not in reply


def test_reject_route_short_circuits_without_llm(temp_db_path, monkeypatch) -> None:
    """越界诉求（非线性/无意义）应直接拒绝，不再回退旧 conversation_agent 二次调 LLM。"""
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "reject"}).json()

    resp = client.post(
        "/api/agent/message",
        json={"conversationId": conversation["id"], "message": "最小化成本的平方"},
    ).json()

    assert resp["actionType"] == "change_rejected"
    assert "超出" in resp["assistantMessage"]["content"]


def test_clarify_route_short_circuits_without_llm(temp_db_path, monkeypatch) -> None:
    """歧义/低置信诉求应直接澄清，不再回退旧 conversation_agent。"""
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "clarify"}).json()

    resp = client.post(
        "/api/agent/message",
        json={"conversationId": conversation["id"], "message": "台风要来了，先别排"},
    ).json()

    assert resp["actionType"] == "change_clarification"
    assert "拿不准" in resp["assistantMessage"]["content"]


def test_repair_utterance_is_not_hijacked_by_dsl_clarification(temp_db_path, monkeypatch) -> None:
    """用户表达不满/纠错时，不能被当成"DSL 说不清"回澄清话术。"""
    os.environ.pop("MINIMAX_API_KEY", None)
    client = _client(temp_db_path, monkeypatch)
    conversation = client.post("/api/conversations", json={"title": "repair"}).json()

    resp = client.post(
        "/api/agent/message",
        json={"conversationId": conversation["id"], "message": "你没听懂我说的意思？"},
    ).json()

    assert resp["actionType"] == "assistant_reply"
    assert "拿不准" not in resp["assistantMessage"]["content"]


def test_version_reference_helpers() -> None:
    from app.core.agent_runtime import _has_version_reference, _looks_like_modification

    assert _has_version_reference("这两版本方案的数学模型区别是什么")
    assert _has_version_reference("方案01和方案02")
    assert not _has_version_reference("今天风大吗")
    assert _looks_like_modification("顺延两天")
    assert not _looks_like_modification("你没听懂我说的意思？")


def test_agent_dsl_pending_persists_without_request_field() -> None:
    """DSL 待确认没有 request 段，落库不得因取 request["strategy"] 崩溃。"""
    from types import SimpleNamespace

    from app.core.agent_runtime import AgentRuntime

    saved: dict = {}

    class _Memories:
        def get_or_create(self, conversation_id):
            return SimpleNamespace(activeFilters={"x": 1}, activeView="P01")

        def apply_patch(self, memory, patch):
            saved.update(patch)
            return memory

    stub = SimpleNamespace(memories=_Memories())
    AgentRuntime._persist_agent_pending(
        stub, "conv_x", {"kind": "dsl", "dsl": {"constraints": []}, "preview": "p"}
    )
    assert saved["activeFilters"]["pendingChange"]["kind"] == "dsl"
    assert "activeFocusTag" not in saved
