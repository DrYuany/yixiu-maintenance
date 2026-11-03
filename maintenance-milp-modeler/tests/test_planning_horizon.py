from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from planning_horizon import PlanningHorizonError, select_target_window  # noqa: E402


def window(window_id: str, start: str, end: str, approval: str = "合成假设，未获真实审批") -> dict:
    return {"编号": window_id, "开始时间": start, "结束时间不含": end, "审批状态": approval}


AS_OF = "2026-10-23T18:00:00+08:00"


def test_skips_window_containing_planning_boundary() -> None:
    selection = select_target_window(
        AS_OF,
        [
            window("窗口01", "2026-10-28T00:00:00+08:00", "2026-11-01T00:00:00+08:00"),
            window("窗口02", "2026-11-02T00:00:00+08:00", "2026-11-07T00:00:00+08:00"),
        ],
    )

    assert selection.target_window["编号"] == "窗口02"
    assert selection.skipped_windows[0]["原因码"] == "STARTS_BEFORE_PLANNING_BOUNDARY"


def test_accepts_window_starting_exactly_at_boundary() -> None:
    selection = select_target_window(
        "2026-10-23T00:00:00+08:00",
        [window("窗口01", "2026-10-30T00:00:00+08:00", "2026-11-04T00:00:00+08:00")],
    )

    assert selection.target_window["编号"] == "窗口01"


def test_rejects_window_that_ends_after_observation_boundary() -> None:
    selection = select_target_window(
        AS_OF,
        [window("窗口01", "2027-01-20T00:00:00+08:00", "2027-01-23T00:00:00+08:00")],
    )

    assert selection.status == "NO_ELIGIBLE_WINDOW"
    assert selection.target_window is None
    assert selection.skipped_windows[0]["原因码"] == "ENDS_AFTER_OBSERVATION_BOUNDARY"


def test_uses_window_id_as_stable_tie_break() -> None:
    selection = select_target_window(
        AS_OF,
        [
            window("窗口02", "2026-11-02T00:00:00+08:00", "2026-11-07T00:00:00+08:00"),
            window("窗口01", "2026-11-02T00:00:00+08:00", "2026-11-06T00:00:00+08:00"),
        ],
    )

    assert selection.target_window["编号"] == "窗口01"


def test_production_rejects_unapproved_window_but_hypothetical_accepts_it() -> None:
    candidate = window("窗口01", "2026-11-02T00:00:00+08:00", "2026-11-07T00:00:00+08:00")

    production = select_target_window(AS_OF, [candidate], run_mode="PRODUCTION")
    hypothetical = select_target_window(AS_OF, [candidate], run_mode="HYPOTHETICAL")

    assert production.status == "NO_ELIGIBLE_WINDOW"
    assert hypothetical.target_window["编号"] == "窗口01"


@pytest.mark.parametrize(
    "windows",
    [
        [window("", "2026-11-02T00:00:00+08:00", "2026-11-07T00:00:00+08:00")],
        [
            window("窗口01", "2026-11-02T00:00:00+08:00", "2026-11-07T00:00:00+08:00"),
            window("窗口01", "2026-11-08T00:00:00+08:00", "2026-11-13T00:00:00+08:00"),
        ],
    ],
)
def test_rejects_missing_or_duplicate_window_ids(windows: list[dict]) -> None:
    with pytest.raises(PlanningHorizonError) as error:
        select_target_window(AS_OF, windows)

    assert error.value.status == "INVALID_INPUT"


def test_requires_timezone_aware_as_of() -> None:
    with pytest.raises(PlanningHorizonError) as error:
        select_target_window("2026-10-23T18:00:00", [])

    assert error.value.status == "INVALID_INPUT"
