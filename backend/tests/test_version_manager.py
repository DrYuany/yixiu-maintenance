from __future__ import annotations

from app.core.version_manager import VersionManager
from app.schemas.agent import CandidateWindow, CandidateWindowMetrics, WindowVersion


def make_version(
    version_id: str,
    generated_at: str,
    focus_tag: str,
    prompt: str,
    windows: list[CandidateWindow],
) -> WindowVersion:
    return WindowVersion(
        id=version_id,
        conversation_id="conv_1",
        source_type="user",
        generated_at=generated_at,
        user_prompt=prompt,
        focus_tag=focus_tag,
        intent_type="generate_version",
        parameter_snapshot={"weights": {"loss": 1.0}},
        result_summary={"windowCount": len(windows)},
        windows=windows,
        included_in_candidate_pool=True,
    )


def make_window(
    candidate_id: str,
    version_id: str,
    generated_at: str,
    focus_tag: str,
    order: int,
    opportunity_cost: float,
    direct_cost: float,
    risk_level: str = "low",
) -> CandidateWindow:
    return CandidateWindow(
        id=candidate_id,
        window_id=f"WIN-{candidate_id}",
        version_id=version_id,
        conversation_id="conv_1",
        generated_at=generated_at,
        focus_tag=focus_tag,
        generated_from_prompt="prompt",
        display_order=order,
        is_pinned=False,
        is_deleted=False,
        metrics=CandidateWindowMetrics(
            duration=6.0,
            opportunity_cost=opportunity_cost,
            direct_cost=direct_cost,
            risk_level=risk_level,
            start_time="2026-04-04 08:00",
            end_time="2026-04-04 14:00",
        ),
    )


def test_compute_top_recommendations_defaults_to_latest_generated_version() -> None:
    manager = VersionManager()
    older = make_version(
        "ver_1",
        "2026-04-03T09:00:00+08:00",
        "economic_priority",
        "older",
        [make_window("001", "ver_1", "2026-04-03T09:00:00+08:00", "economic_priority", 1, 100, 200)],
    )
    latest = make_version(
        "ver_2",
        "2026-04-03T10:00:00+08:00",
        "risk_priority",
        "latest",
        [
            make_window("002", "ver_2", "2026-04-03T10:00:00+08:00", "risk_priority", 1, 300, 100),
            make_window("003", "ver_2", "2026-04-03T10:00:00+08:00", "risk_priority", 2, 200, 150),
        ],
    )

    recommendations = manager.compute_top_recommendations(
        [older.windows[0], latest.windows[0], latest.windows[1]],
        [older, latest],
        sort_mode="latest_first",
    )

    assert [item.id for item in recommendations] == ["002", "003", "001"]


def test_sort_candidates_by_low_opportunity_cost() -> None:
    manager = VersionManager()
    candidates = [
        make_window("001", "ver_1", "2026-04-03T09:00:00+08:00", "economic_priority", 1, 300, 100),
        make_window("002", "ver_2", "2026-04-03T10:00:00+08:00", "risk_priority", 2, 100, 300),
    ]

    sorted_candidates = manager.sort_candidates(candidates, sort_mode="low_opportunity_cost")

    assert [item.id for item in sorted_candidates] == ["002", "001"]
