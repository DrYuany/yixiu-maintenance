"""DSL 增量能力的 API 级集成测试（第一期接线验证）。

验证路由链：自然语言 → DSL 解析（规则层）→ 预览 → 确认 → 求解任务携带 DSL 载荷
→ 方案版本快照含 DSL（证据链）。求解本身被 mock（约束生效的客观验证在
backend/tests/dsl/test_end_to_end*.py，用真实 CP-SAT 小规模子集证明）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.maintenance_product_executor import MaintenanceProductExecutor


def _report() -> dict:
    return {
        "版本": "3.0",
        "策略": "成本最低优先",
        "目标模式": "分层",
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


def _client(temp_db_path, monkeypatch, captured: dict) -> TestClient:
    os.environ["MWA_DB_PATH"] = str(temp_db_path)

    def fake_execute(self, request, job_id, model_fingerprint=None, dsl=None, utterance=None):
        captured["dsl"] = dsl
        captured["request"] = request.to_payload()
        return _report()

    monkeypatch.setattr(MaintenanceProductExecutor, "execute", fake_execute)
    from app.main import create_app
    return TestClient(create_app())


def test_dsl_constraint_message_goes_preview_confirm_solve(temp_db_path, monkeypatch) -> None:
    captured: dict = {}
    client = _client(temp_db_path, monkeypatch, captured)
    conversation = client.post("/api/conversations", json={"title": "dsl"}).json()

    # 1. 约束类修改 → dsl_preview（规则层解析，无需 API key）
    preview = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "任务001和任务002不能在同一天出海维修",
        },
    ).json()
    assert preview["actionType"] == "dsl_preview", preview
    content = preview["assistantMessage"]["content"]
    # 预览必须写出 mode 本身（不同天），不能只说一句“互斥”：same_day 也是互斥，
    # 那样两种相反的约束在预览里会一模一样。
    assert "不同天" in content and "尚未求解" in content

    # 2. 确认 → solve_started，任务载荷携带 DSL（request 字段保持顶层兼容）
    confirmed = client.post(
        "/api/agent/message",
        json={"conversationId": conversation["id"], "message": "确认生成沙盘"},
    ).json()
    assert confirmed["actionType"] == "solve_started", confirmed
    job = confirmed["solveJob"]
    assert job["request"]["kind"] == "dsl"
    assert job["request"]["dsl"]["constraints"][0]["type"] == "co_occur"
    assert job["request"]["strategy"] == "成本最低优先"  # 顶层兼容

    # 3. 求解完成 → 方案版本快照含 DSL（证据链）
    job_id = job["id"]
    for _ in range(100):
        status = client.get(f"/api/agent/jobs/{job_id}").json()
        if status["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        time.sleep(0.01)
    assert status["status"] == "COMPLETED"
    assert captured["dsl"] is not None
    assert captured["dsl"]["constraints"][0]["type"] == "co_occur"

    versions = client.get(f"/api/conversations/{conversation['id']}/versions").json()
    saved = [v for v in versions if v.get("solveJobId") == job_id]
    assert saved and saved[0]["parameterSnapshot"]["dsl"]["constraints"][0]["type"] == "co_occur"

    # 公告、任务预算、产物参数表必须是同一个数。
    # 以前公告说 300 秒、参数总表说 60 秒、实际跑 30 秒，三处各写各的。
    budget = job["request"]["budgetSeconds"]
    assert budget and budget >= 5
    assert f"{budget} 秒" in confirmed["assistantMessage"]["content"]
    assert job["request"]["budget"]["seconds"] == budget
    # 用户没点名时限，请求里就不该写死一个数（求解器与产物参数表按同一份预算走）。
    assert captured["request"]["seconds"] is None


def test_dsl_cancellation_clears_pending(temp_db_path, monkeypatch) -> None:
    captured: dict = {}
    client = _client(temp_db_path, monkeypatch, captured)
    conversation = client.post("/api/conversations", json={"title": "dsl-cancel"}).json()

    preview = client.post(
        "/api/agent/message",
        json={
            "conversationId": conversation["id"],
            "message": "先修任务001再修任务002",
        },
    ).json()
    assert preview["actionType"] == "dsl_preview"

    cancelled = client.post(
        "/api/agent/message",
        json={"conversationId": conversation["id"], "message": "取消"},
    ).json()
    assert cancelled["actionType"] == "change_cancelled"
    assert captured.get("dsl") is None  # 未触发求解


def test_dsl_query_message_does_not_trigger_preview(temp_db_path, monkeypatch) -> None:
    """查询/解释类话术不得误入 DSL 预览（意图门控）。"""
    captured: dict = {}
    client = _client(temp_db_path, monkeypatch, captured)
    conversation = client.post("/api/conversations", json={"title": "dsl-query"}).json()

    for message in ("现在有哪些告警", "为什么任务018没排上", "对比方案01和02"):
        response = client.post(
            "/api/agent/message",
            json={"conversationId": conversation["id"], "message": message},
        ).json()
        assert response["actionType"] != "dsl_preview", f"{message} 误入 DSL: {response}"
