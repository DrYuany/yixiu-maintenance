from __future__ import annotations

from app.core.result_normalizer import ResultNormalizer


def test_build_versions_from_update_result_persists_repair_schedule() -> None:
    normalizer = ResultNormalizer()

    versions = normalizer.build_versions_from_update_result(
        conversation_id="conv_1",
        user_prompt="经济性调高点",
        focus_tag="economic_priority",
        payload={
            "metrics": {
                "makespan": 12,
                "total_loss": 100.0,
                "direct_cost": 80.0,
                "risk_cost": 50.0,
            },
            "summary": "summary",
            "applied_updates": {"weights": {"loss": 1.1}},
            "component_library": {
                "发电机": {
                    "duration_hours": 8,
                }
            },
            "assignments": [
                {
                    "task_id": "T1-发电机",
                    "start_hour": 2,
                }
            ],
        },
    )

    assert len(versions) == 1
    schedule = versions[0].repairSchedule
    assert len(schedule) == 1
    assert schedule[0].taskId == "T1-发电机"
    assert schedule[0].turbineId == 1
    assert schedule[0].componentName == "发电机"
    assert schedule[0].startHour == 2
    assert schedule[0].endHour == 10
    assert schedule[0].durationHours == 8


def test_build_versions_from_compare_result_persists_repair_schedule() -> None:
    normalizer = ResultNormalizer()

    versions = normalizer.build_versions_from_compare_result(
        conversation_id="conv_1",
        user_prompt="比较经济优先和风险优先",
        focus_tag="strategy_compare",
        payload={
            "comparisons": [
                {
                    "label": "economic_priority",
                    "updates": {"weights": {"loss": 1.2}},
                    "metrics": {
                        "makespan": 10,
                        "total_loss": 100.0,
                        "direct_cost": 80.0,
                        "risk_cost": 50.0,
                    },
                    "summary": "summary",
                    "component_library": {
                        "齿轮箱": {
                            "duration_hours": 12,
                        }
                    },
                    "assignments": [
                        {
                            "task_id": "T2-齿轮箱",
                            "start_hour": 4,
                        }
                    ],
                }
            ]
        },
    )

    assert len(versions) == 1
    schedule = versions[0].repairSchedule
    assert len(schedule) == 1
    assert schedule[0].taskId == "T2-齿轮箱"
    assert schedule[0].turbineId == 2
    assert schedule[0].componentName == "齿轮箱"
