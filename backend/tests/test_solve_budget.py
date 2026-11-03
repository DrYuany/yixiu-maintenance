"""预算口径只能有一个：参数总表 → 调用方显式 → 部署上限。

这组测试盯的是"界面上不许出现两个数"：公告、job、产物参数表、参数总表展示
都走 solve_budget.resolve，环境变量只做压制，不能反过来抬价。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "maintenance-milp-modeler" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solve_budget import (  # noqa: E402
    DEFAULT_SECONDS,
    ENV_SECONDS,
    GAP_PARAM,
    SECONDS_PARAM,
    describe,
    resolve,
)


def test_table_value_is_the_default_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SECONDS, raising=False)

    budget = resolve({SECONDS_PARAM: 60, GAP_PARAM: 0.01})

    assert budget["seconds"] == 60
    assert budget["secondsSource"] == "参数总表"
    assert budget["gapLimit"] == 0.01
    assert budget["gapSource"] == "参数总表"


def test_env_only_caps_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SECONDS, "30")
    lowered = resolve({SECONDS_PARAM: 60})
    assert lowered["seconds"] == 30
    assert lowered["secondsSource"] == "环境变量上限"
    assert lowered["envCap"] == 30

    monkeypatch.setenv(ENV_SECONDS, "600")
    raised = resolve({SECONDS_PARAM: 60})
    assert raised["seconds"] == 60, "部署上限不能把参数总表往上抬"
    assert raised["secondsSource"] == "参数总表"


def test_explicit_request_wins_over_table_but_not_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SECONDS, raising=False)
    asked = resolve({SECONDS_PARAM: 60}, requested_seconds=120)
    assert asked["seconds"] == 120
    assert asked["secondsSource"] == "调用方指定"

    monkeypatch.setenv(ENV_SECONDS, "90")
    capped = resolve({SECONDS_PARAM: 60}, requested_seconds=120)
    assert capped["seconds"] == 90


def test_gap_defaults_and_clamps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SECONDS, raising=False)
    assert resolve({})["gapLimit"] == 0.01
    assert resolve({GAP_PARAM: 0.02})["gapLimit"] == 0.02
    assert resolve({GAP_PARAM: 3})["gapLimit"] == 1.0
    assert resolve({GAP_PARAM: -1})["gapLimit"] == 0.0


def test_describe_names_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SECONDS, raising=False)
    text = describe(resolve({SECONDS_PARAM: 60}))
    assert "60 秒" in text
    assert "参数总表" in text
    assert "1%" in text


def test_missing_everything_falls_back_to_one_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SECONDS, raising=False)
    budget = resolve(None)
    assert budget["seconds"] == DEFAULT_SECONDS
    assert budget["secondsSource"] == "代码默认"


def test_table_runtime_and_deploy_cap_are_one_number() -> None:
    """参数总表、运行时基座、部署上限必须是同一个数，别再退回三处各写各的。"""
    import csv

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts"))
    from build_deploy import _solve_budget_seconds

    with (root / "data" / "模型参数" / "参数总表.csv").open(encoding="utf-8-sig") as handle:
        table = next(
            int(float(row["默认值"]))
            for row in csv.DictReader(handle)
            if row["参数编号"] == "求解01"
        )

    sys.path.insert(0, str(root / "maintenance-milp-modeler" / "scripts"))
    from product_data import load_project

    runtime = int(load_project(str(root)).params[SECONDS_PARAM])

    assert table == 180
    assert runtime == table, "运行时读的是初版完整数据.json，不能和参数总表对不上"
    assert _solve_budget_seconds() == table, "部署上限不能另写一个数"
