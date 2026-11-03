"""多场景排程：一句话要两版时，必须真的出两版。

这几条对应一次真实故障：用户说「加权和分层，都以成本最低优先，算一版，对比」，
系统只给了一版、而且是错的那一版。根因不是模型没懂，是拆分正则、工具入参、
待确认结构三处各断一节。测试盯住这三节。
"""
from __future__ import annotations

from pathlib import Path

from app.core.agent_tools import AgentToolbox
from app.core.model_change import (
    build_scenario_batch_from_segments,
    is_scenario_batch,
    split_scenarios,
)
from app.core.planning_intent import PlanningRequest, PlanningIntentCompiler
from app.db.database import Database
from app.db.models import ModelArtifactRepository, VersionRepository


def _toolbox(temp_db_path: Path, user_message: str = "") -> AgentToolbox:
    db = Database(temp_db_path)
    return AgentToolbox(
        conversation_id="conv_batch",
        versions=VersionRepository(db),
        model_artifacts=ModelArtifactRepository(db),
        user_message=user_message,
    )


# ── 拆分 ────────────────────────────────────────────────────────────────


def test_split_noun_coordination() -> None:
    """「A 和 B，都…」是名词并列，动词并列那套正则一条也匹配不到。"""
    segments = split_scenarios("加权和分层，都以成本最低优先，算一版，对比")

    assert len(segments) == 2
    assert segments[0].startswith("加权")
    assert segments[1].startswith("分层")
    # 共享谓语必须接回去，否则「加权」两个字编译不出任何东西。
    assert all("成本最低优先" in item for item in segments)
    assert is_scenario_batch("加权和分层，都以成本最低优先，算一版，对比") is True


def test_split_keeps_verb_coordination() -> None:
    segments = split_scenarios("按加权算一版，再按分层算一版，然后对比")

    assert segments == ["加权", "分层"]


def test_split_does_not_eat_unrelated_conjunction() -> None:
    """「船租和人工都算进去」是参数描述，不是两个场景。"""
    assert is_scenario_batch("船租和人工都算进去，重新排一版") is False
    assert is_scenario_batch("成本最低优先和最短完成优先都要考虑") is False


# ── 编译与护栏 ──────────────────────────────────────────────────────────


def test_bare_mode_compiles_to_two_versions() -> None:
    batch = build_scenario_batch_from_segments(
        ["加权", "分层"],
        PlanningRequest(),
        None,
        PlanningIntentCompiler(),
    )

    assert batch.blocked is False
    assert len(batch.scenarios) == 2
    assert [item.label for item in batch.scenarios] == ["加权", "分层"]
    # 两版必须是两个不同的模型契约，不能是同一份东西换了个说法。
    assert (
        batch.scenarios[0].request.to_payload()
        != batch.scenarios[1].request.to_payload()
    )


def test_identical_scenarios_are_blocked() -> None:
    """「加权和加权」拆得出两段，但编译结果相同——宁可报错，不给两份一样的。"""
    batch = build_scenario_batch_from_segments(
        ["改成加权", "改成加权"],
        PlanningRequest(),
        None,
        PlanningIntentCompiler(),
    )

    assert batch.blocked is True
    assert "完全相同" in " ".join(batch.scenarios[1].errors)


def test_mode_word_wins_over_parser_wording() -> None:
    """模型常写「按加权算一版」，解析器不认这种说法。

    场景里既然点了名，就必须按它编译——否则两段都沿用当前模式，
    被护栏当成重复拦下，用户看到的是“这两版一模一样”。
    """
    batch = build_scenario_batch_from_segments(
        ["按加权算一版，成本最低优先", "按分层算一版，成本最低优先"],
        PlanningRequest(),
        None,
        PlanningIntentCompiler(),
    )

    assert batch.blocked is False
    assert [item.request.mode for item in batch.scenarios] == ["加权", "分层"]


def test_negated_mode_is_not_a_declaration() -> None:
    """「不要加权」不能被当成“要加权”。"""
    batch = build_scenario_batch_from_segments(
        ["不要加权，成本最低优先", "成本最低优先，加权"],
        PlanningRequest(),
        None,
        PlanningIntentCompiler(),
    )

    assert [item.request.mode for item in batch.scenarios] == ["分层", "加权"]


# ── 工具层 ──────────────────────────────────────────────────────────────


def test_solve_promotes_parallel_instruction_to_batch(temp_db_path: Path) -> None:
    """模型就算只会用 instruction，也要能拿到批量预览。"""
    box = _toolbox(temp_db_path, "加权和分层，都以成本最低优先，算一版，对比")

    result = box._solve({"instruction": "加权和分层，都以成本最低优先，算一版，对比"})

    assert result["status"] == "needs_confirmation"
    assert box.pending_change is not None
    assert box.pending_change["kind"] == "batch"
    assert len(box.pending_change["scenarios"]) == 2
    assert box.pending_change["compare"] is True


def test_solve_accepts_explicit_scenarios(temp_db_path: Path) -> None:
    box = _toolbox(temp_db_path, "加权和分层各算一版")

    box._solve({"scenarios": ["改成加权，成本最低优先", "改成分层，成本最低优先"]})

    assert box.pending_change is not None
    assert box.pending_change["kind"] == "batch"
    assert [item["label"] for item in box.pending_change["scenarios"]] == ["加权", "分层"]


def test_single_scenario_stays_single(temp_db_path: Path) -> None:
    box = _toolbox(temp_db_path, "船租上涨 20%")

    box._solve({"instruction": "船租上涨 20%"})

    assert box.pending_change is not None
    assert box.pending_change.get("kind") != "batch"
    assert "request" in box.pending_change


def test_failed_solve_clears_stale_pending(temp_db_path: Path) -> None:
    """上一轮留着一份待确认，这一轮没产出新预览，就必须作废。

    不清的话，用户接着一句「确认」，派出去的是上一句的变更。
    """
    box = _toolbox(temp_db_path, "先来一个")
    box._solve({"instruction": "船租上涨 20%"})
    assert box.pending_change is not None

    result = box._solve({"instruction": "今天天气不错"})

    assert result["status"] == "clarify"
    assert box.pending_change is None


def test_submit_routes_batch_to_batch_solver(temp_db_path: Path) -> None:
    """批量预览必须走批量派活，否则一确认就报「待确认的变更已经失效」。"""
    captured: dict[str, object] = {}
    box = _toolbox(temp_db_path, "加权和分层各算一版")
    box.jobs = object()  # 只要非空，证明接了求解服务

    def fake_submit(conversation_id: str, pending: dict, reason: str) -> dict:
        captured["kind"] = pending.get("kind")
        captured["count"] = len(pending.get("scenarios") or [])
        return {"ok": True, "jobId": "job_fake", "message": "已排队"}

    box.on_submit_change = fake_submit
    box._solve({"scenarios": ["改成加权", "改成分层"]})
    result = box._submit_change({"reason": "确认生成沙盘"})

    assert result["status"] == "submitted"
    assert captured == {"kind": "batch", "count": 2}


def test_default_weights_wording_is_not_treated_as_default_rerun() -> None:
    """线上真实故障：模型写「可加权指标按默认权重（…）」，被当成默认配置重跑。

    「默认权重」是加权模式的正常说明，不是「按默认配置再来一遍」。
    一旦误判，这一版就沿用当前的分层模式，和另一版撞成同一个，护栏报错。
    """
    segments = [
        "改成加权模式，策略选成本最低优先，可加权指标按默认权重（总经济成本 0.70、完成任务数 0.15、最后完工时间 0.10、完工时间和 0.05）",
        "改成分层模式，策略选成本最低优先",
    ]
    batch = build_scenario_batch_from_segments(
        segments,
        PlanningRequest(),
        None,
        PlanningIntentCompiler(),
    )

    assert batch.blocked is False, batch.scenarios[1].errors
    assert len(batch.scenarios) == 2
    modes = [item.request.mode for item in batch.scenarios]
    assert modes == ["加权", "分层"]
    assert batch.scenarios[0].request.to_payload() != batch.scenarios[1].request.to_payload()


def test_plain_default_rerun_still_works() -> None:
    """收紧措辞不能把真正的「按默认配置算一遍」也弄丢。"""
    batch = build_scenario_batch_from_segments(
        ["按默认配置算一遍"],
        PlanningRequest(),
        None,
        PlanningIntentCompiler(),
    )

    assert batch.blocked is False
    assert batch.scenarios[0].label == "默认配置"
    assert batch.scenarios[0].operations[0]["kind"] == "rerun"


def test_default_weights_wording_is_not_treated_as_default_rerun() -> None:
    """线上真实故障：模型写「可加权指标按默认权重（…）」，被当成默认配置重跑。

    「默认权重」是加权模式的正常说明，不是「按默认配置再来一遍」。
    一旦误判，这一版就沿用当前的分层模式，和另一版撞成同一个，护栏报错。
    """
    segments = [
        "改成加权模式，策略选成本最低优先，可加权指标按默认权重（总经济成本 0.70、完成任务数 0.15、最后完工时间 0.10、完工时间和 0.05）",
        "改成分层模式，策略选成本最低优先",
    ]
    batch = build_scenario_batch_from_segments(
        segments,
        PlanningRequest(),
        None,
        PlanningIntentCompiler(),
    )

    assert batch.blocked is False, batch.scenarios[1].errors
    assert len(batch.scenarios) == 2
    modes = [item.request.mode for item in batch.scenarios]
    assert modes == ["加权", "分层"]
    assert batch.scenarios[0].request.to_payload() != batch.scenarios[1].request.to_payload()


def test_plain_default_rerun_still_works() -> None:
    """收紧措辞不能把真正的「按默认配置算一遍」也弄丢。"""
    batch = build_scenario_batch_from_segments(
        ["按默认配置算一遍"],
        PlanningRequest(),
        None,
        PlanningIntentCompiler(),
    )

    assert batch.blocked is False
    assert batch.scenarios[0].label == "默认配置"
    assert batch.scenarios[0].operations[0]["kind"] == "rerun"
