"""多轮对话场景（任务三）：上下文累加、纠错、取消、基版绑定、批量。

求解被 mock（速度），验证的是会话状态机与路由，不验证求解正确性
（后者由 tests/dsl/test_end_to_end*.py 真求解覆盖）。
"""
from __future__ import annotations

import os
import time

from fastapi.testclient import TestClient

from app.core.maintenance_product_executor import MaintenanceProductExecutor


def _report(strategy: str = "成本最低优先") -> dict:
    return {
        "版本": "3.0", "策略": strategy, "目标模式": "分层",
        "数据": {"输入哈希": "input-1", "运行哈希": "run-1"},
        "求解": {"有解": True, "分层全部最优": True, "总耗时秒": 0.2,
                 "阶段": [{"状态": "OPTIMAL", "目标值": 17600.0, "界": 17600.0}]},
        "窗口": [{"编号": "窗口01", "开始": 0, "结束": 1440,
                 "开始日期": "2026-10-23T00:00:00+08:00", "结束时间不含": "2026-10-24T00:00:00+08:00"}],
        "人工基准": {"严重性优先": {"完成任务数": 1, "总成本": 20000.0},
                     "低风速优先": {"完成任务数": 1, "总成本": 21000.0}},
        "推荐": {
            "完成任务数": 1, "待修总数": 1, "未安排": [],
            "最晚完工": "2026-10-23T12:00:00+08:00", "船舶艘天": 1,
            "船租": 12000.0, "人工": 600.0, "备件": 1000.0, "其他费用": 0.0,
            "发电损失": 4000.0, "总成本": 17600.0, "现金支出": 13600.0,
            "复核通过": True,
            "排程": [{"任务编号": "任务001", "风机编号": "风机001", "部件编号": "部件01",
                     "部件名称": "叶片", "严重等级": "高", "窗口编号": "窗口01",
                     "开始": 480, "完成": 720, "作业线": 1,
                     "下项最早开始": 1080, "作业段": [[480, 720]]}],
        },
        "模型产物": None,
    }


def _client(temp_db_path, monkeypatch) -> TestClient:
    os.environ["MWA_DB_PATH"] = str(temp_db_path)
    captured: dict = {}

    def fake_execute(self, request, job_id, model_fingerprint=None, dsl=None, utterance=None):
        captured["request"] = request
        captured["dsl"] = dsl
        return _report(request.strategy)

    monkeypatch.setattr(MaintenanceProductExecutor, "execute", fake_execute)
    from app.main import create_app

    client = TestClient(create_app())
    client.captured = captured  # type: ignore[attr-defined]
    return client


def _say(client: TestClient, conv_id: str, message: str, selected: str | None = None) -> dict:
    return client.post(
        "/api/agent/message",
        json={"conversationId": conv_id, "message": message,
              **({"selectedVersionId": selected} if selected else {})},
    ).json()


def _wait_job(client: TestClient, job_id: str) -> dict:
    for _ in range(150):
        status = client.get(f"/api/agent/jobs/{job_id}").json()
        if status["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return status
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def _solve_once(client: TestClient, conv_id: str, message: str) -> str:
    preview = _say(client, conv_id, message)
    assert preview["actionType"] == "change_preview", preview
    started = _say(client, conv_id, "确认生成沙盘")
    assert started["actionType"] == "solve_started", started
    _wait_job(client, started["solveJob"]["id"])
    return started["solveJob"]["id"]


def test_pending_changes_accumulate_across_turns(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "multi"}).json()

    first = _say(client, conv["id"], "船租上涨20%")
    assert first["actionType"] == "change_preview", first
    second = _say(client, conv["id"], "最多两条作业线")
    assert second["actionType"] == "change_preview", second

    memory = client.get(f"/api/conversations/{conv['id']}").json()["workingMemory"]
    request = memory["activeFilters"]["pendingChange"]["request"]
    assert request["relative_overrides"].get("船租（元每艘每天）") == 1.2
    assert request["overrides"].get("最多作业线数") == 2


def test_correction_replaces_previous_value(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "correct"}).json()

    _say(client, conv["id"], "船租上涨20%")
    corrected = _say(client, conv["id"], "不对，船租改成50000")
    assert corrected["actionType"] == "change_preview", corrected

    memory = client.get(f"/api/conversations/{conv['id']}").json()["workingMemory"]
    request = memory["activeFilters"]["pendingChange"]["request"]
    assert request["overrides"].get("船租（元每艘每天）") == 50000
    assert "船租（元每艘每天）" not in request["relative_overrides"]


def test_query_between_preview_and_confirm_keeps_pending(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "interleave"}).json()

    preview = _say(client, conv["id"], "任务001和任务002不能在同一天出海维修")
    assert preview["actionType"] == "dsl_preview", preview

    query = _say(client, conv["id"], "现在有哪些告警")
    assert query["actionType"] != "dsl_preview"

    confirmed = _say(client, conv["id"], "确认生成沙盘")
    assert confirmed["actionType"] == "solve_started", confirmed
    _wait_job(client, confirmed["solveJob"]["id"])
    assert client.captured["dsl"]["constraints"][0]["type"] == "co_occur"


def test_cancel_then_confirm_does_not_solve(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "cancel"}).json()

    _say(client, conv["id"], "任务001和任务002不能在同一天出海维修")
    cancelled = _say(client, conv["id"], "取消")
    assert cancelled["actionType"] == "change_cancelled", cancelled

    after = _say(client, conv["id"], "确认生成沙盘")
    assert after["actionType"] != "solve_started", after


def test_dsl_preview_binds_selected_base_version(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "base"}).json()

    _solve_once(client, conv["id"], "按高风险优先抢修")     # 方案01
    _solve_once(client, conv["id"], "按成本最低优先抢修")   # 方案02
    versions = client.get(f"/api/conversations/{conv['id']}/versions").json()
    assert len(versions) == 2
    plan01 = next(v for v in versions if v["id"] and v["parameterSnapshot"]["strategy"] == "高风险优先抢修")

    preview = _say(client, conv["id"],
                   "任务001和任务002不能在同一天出海维修", selected=plan01["id"])
    assert preview["actionType"] == "dsl_preview", preview
    confirmed = _say(client, conv["id"], "确认生成沙盘", selected=plan01["id"])
    assert confirmed["actionType"] == "solve_started", confirmed
    _wait_job(client, confirmed["solveJob"]["id"])
    assert client.captured["request"].strategy == "高风险优先抢修"


def test_batch_scenario_multi_turn(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "batch"}).json()

    preview = _say(client, conv["id"], "先按成本优先算一遍，再按风险优先算一遍，然后对比")
    assert preview["actionType"] == "change_preview", preview
    started = _say(client, conv["id"], "确认生成沙盘")
    assert started["actionType"] == "solve_started", started
    job = client.get(f"/api/agent/jobs/{started['solveJob']['id']}").json()
    assert job["batchTotal"] == 2

    for _ in range(200):
        versions = client.get(f"/api/conversations/{conv['id']}/versions").json()
        if len(versions) >= 2:
            break
        time.sleep(0.02)
    assert len(versions) >= 2


def test_new_dsl_preview_accumulates_with_pending(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "replace"}).json()

    _say(client, conv["id"], "任务001和任务002不能在同一天出海维修")
    second = _say(client, conv["id"], "先修任务001再修任务002")
    assert second["actionType"] == "dsl_preview"

    confirmed = _say(client, conv["id"], "确认生成沙盘")
    assert confirmed["actionType"] == "solve_started"
    _wait_job(client, confirmed["solveJob"]["id"])
    types = {c["type"] for c in client.captured["dsl"]["constraints"]}
    assert types == {"co_occur", "precedence"}


def test_dsl_and_parameter_accumulate_across_types(temp_db_path, monkeypatch) -> None:
    """先加互斥约束，再改船租 → 两条都保留在待确认 DSL 中。"""
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "cross"}).json()

    _say(client, conv["id"], "任务001和任务002不能在同一天出海维修")
    second = _say(client, conv["id"], "船租上涨20%")
    assert second["actionType"] in {"dsl_preview", "change_preview"}, second

    confirmed = _say(client, conv["id"], "确认生成沙盘")
    assert confirmed["actionType"] == "solve_started", confirmed
    _wait_job(client, confirmed["solveJob"]["id"])
    dsl = client.captured["dsl"]
    types = {c["type"] for c in dsl["constraints"]}
    assert "co_occur" in types
    params = {p["field"] for p in dsl.get("parameters", [])}
    assert "船舶日租金" in params


def test_three_parameters_accumulate(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "three"}).json()

    for message in ("船租上涨20%", "最多两条作业线", "每天最多8小时"):
        reply = _say(client, conv["id"], message)
        assert reply["actionType"] == "change_preview", reply

    memory = client.get(f"/api/conversations/{conv['id']}").json()["workingMemory"]
    request = memory["activeFilters"]["pendingChange"]["request"]
    assert request["relative_overrides"].get("船租（元每艘每天）") == 1.2
    assert request["overrides"].get("最多作业线数") == 2
    assert request["overrides"].get("每天最多维修小时数") == 8


def test_cancel_then_new_dsl_preview_works(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "cancel2"}).json()

    _say(client, conv["id"], "任务001和任务002不能在同一天出海维修")
    assert _say(client, conv["id"], "取消")["actionType"] == "change_cancelled"
    again = _say(client, conv["id"], "先修任务001再修任务002")
    assert again["actionType"] == "dsl_preview", again
    confirmed = _say(client, conv["id"], "确认生成沙盘")
    _wait_job(client, confirmed["solveJob"]["id"])
    assert {c["type"] for c in client.captured["dsl"]["constraints"]} == {"precedence"}


def test_followup_inherits_solved_version_parameters(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "inherit"}).json()

    _solve_once(client, conv["id"], "船租上涨20%")
    follow = _say(client, conv["id"], "最多两条作业线")
    assert follow["actionType"] == "change_preview", follow
    confirmed = _say(client, conv["id"], "确认生成沙盘")
    _wait_job(client, confirmed["solveJob"]["id"])
    # 方案快照里船租已被换算成绝对值 14400，后续修改应继承它
    assert client.captured["request"].overrides.get("船租（元每艘每天）") == 14400


def test_repeated_same_parameter_is_idempotent(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "idem"}).json()

    _say(client, conv["id"], "船租上涨20%")
    _say(client, conv["id"], "船租上涨20%")
    memory = client.get(f"/api/conversations/{conv['id']}").json()["workingMemory"]
    request = memory["activeFilters"]["pendingChange"]["request"]
    assert request["relative_overrides"].get("船租（元每艘每天）") == 1.2


def test_confirm_twice_only_solves_once(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "twice"}).json()

    _say(client, conv["id"], "任务001和任务002不能在同一天出海维修")
    first = _say(client, conv["id"], "确认生成沙盘")
    assert first["actionType"] == "solve_started"
    _wait_job(client, first["solveJob"]["id"])
    second = _say(client, conv["id"], "确认生成沙盘")
    assert second["actionType"] != "solve_started", second


def test_switch_version_clears_pending_dsl(temp_db_path, monkeypatch) -> None:
    client = _client(temp_db_path, monkeypatch)
    conv = client.post("/api/conversations", json={"title": "switch"}).json()

    _solve_once(client, conv["id"], "按高风险优先抢修")     # 方案01
    _solve_once(client, conv["id"], "按成本最低优先抢修")   # 方案02（latest）
    preview = _say(client, conv["id"], "任务001和任务002不能在同一天出海维修")
    assert preview["actionType"] == "dsl_preview"

    versions = client.get(f"/api/conversations/{conv['id']}/versions").json()
    plan01 = next(v for v in versions if v["parameterSnapshot"]["strategy"] == "高风险优先抢修")
    switched = _say(client, conv["id"], f"切到方案01", selected=plan01["id"])
    assert switched["actionType"] in {"context_switch", "assistant_reply"}, switched

    after = _say(client, conv["id"], "确认生成沙盘")
    assert after["actionType"] != "solve_started", after
