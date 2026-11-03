from __future__ import annotations

from dsl_core import objective_catalog


def test_registry_is_internally_consistent() -> None:
    assert objective_catalog.validate() == []


def test_catalog_lists_every_strategy_and_excludes_unweightable_metrics() -> None:
    text = objective_catalog.catalog_markdown()

    for strategy in objective_catalog.strategies():
        assert strategy in text
    assert "现金支出" not in text
    assert "高严重完工时间和" not in objective_catalog.all_weighted_metrics()


def test_shared_weighted_metrics_are_exactly_the_intersection() -> None:
    shared = objective_catalog.shared_weighted_metrics()

    assert "总经济成本" in shared
    assert "最后完工时间" in shared
    assert "高严重完成数" not in shared
