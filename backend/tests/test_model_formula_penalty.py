# -*- coding: utf-8 -*-
"""用户问「给我模型公式」时，渲染出来的数学必须和真模型一致。

改造前 LaTeX 里写死 Σx = 1、目标里没有不修项；现在每个任务有 y_i（渲染里
记作 z_i，避开租船日 y_{l,d}），公式要显式写出 Σ P_i·z_i 和 Σx + z_i = 1。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.agent_tools import AgentToolbox
from app.schemas.agent import WindowVersion


def _version() -> WindowVersion:
    return WindowVersion(
        id="ver_formula01",
        conversationId="conv_test",
        generatedAt="2026-11-09T10:00:00+08:00",
        userPrompt="看下模型公式",
        focusTag="成本最低优先",
        intentType="generate_version",
        parameterSnapshot={},
        resultSummary={"solver": {"解状态": "FEASIBLE"}},
    )


def _toolbox() -> AgentToolbox:
    return AgentToolbox(
        conversation_id="conv_test",
        versions=SimpleNamespace(list_by_conversation=lambda cid: []),
        model_artifacts=None,
    )


def _artifact() -> dict:
    return {
        "schemaVersion": "maintenance-model-artifact-v2",
        "mode": "加权",
        "modelFingerprint": "fp-test",
        "weights": {
            "总经济成本": 0.7,
            "完成任务数": 0.15,
            "最后完工时间": 0.1,
            "完工时间和": 0.05,
        },
        "objectives": {
            "stages": [
                {"priority": 1, "name": "标准化加权目标", "sense": "minimize"},
            ]
        },
        "dimensions": {"tasks": 13, "windows": 1, "candidateChoices": 100},
        "variables": {"total": 100},
        "constraints": {"total": 200},
        "unrepairedPenalty": {
            "horizonDays": 14,
            "alphaSource": "部件库业务表",
            "alphaNature": "部件×严重度剩余出力系数；初版模拟假设，需现场数据校准",
            "penaltyTotal": 1620823.67,
            "pricingWindow": {
                "anchorStamp": "2026-11-15T08:00:00+08:00",
                "endStamp": "2026-11-29T08:00:00+08:00",
                "pricedDays": 14.0,
                "truncatedByForecastCoverage": False,
            },
            "records": [
                {
                    "任务编号": "任务001",
                    "部件名称": "叶片",
                    "严重等级": "高",
                    "剩余出力系数": 0.55,
                    "不修惩罚P（元）": 225811.35,
                }
            ],
            "note": "全模型共用一个计价窗。",
        },
    }


def test_formula_shows_explicit_unrepaired_branch() -> None:
    toolbox = _toolbox()
    latex = toolbox._render_math_latex(_version(), _artifact())
    objective = latex["objective"]
    assert r"P_i\,z_i" in objective
    assert "z_i" in latex["variables"]
    assert r"x_{i,k,l}+z_i=1" in latex["constraints"]
    # 旧的「每个任务必须选一个」硬约束不该再出现
    assert r"x_{i,k,l}=1" not in latex["constraints"]
    # 未修任务在完工时间类指标里不许白送 0
    assert r"T_{\mathrm{end}}\,z_i" in latex["constraints"]


def test_markdown_reports_penalty_values_and_sources() -> None:
    markdown = _toolbox()._render_math_markdown(_version(), _artifact())
    assert "**目标函数（加权）**" in markdown
    assert "不修惩罚" in markdown
    assert "14 天" in markdown
    assert "部件库业务表" in markdown
    assert "实际 14.0 天" in markdown
    assert "¥1,620,823.67" in markdown
    assert "P=¥225,811.35" in markdown
    assert "$P_i$" in markdown and "$z_i$" in markdown
    assert "总经济成本 = 0.7" in markdown
    assert "\\frac{w_{1}}" in markdown


def test_old_artifact_does_not_fake_penalty_numbers() -> None:
    old = _artifact()
    old.pop("unrepairedPenalty")
    old["schemaVersion"] = "maintenance-model-artifact-v1"
    markdown = _toolbox()._render_math_markdown(_version(), old)
    assert "产自旧模型" in markdown
    assert "不能事后推算冒充原模型" in markdown
    assert "¥1,620,823.67" not in markdown
