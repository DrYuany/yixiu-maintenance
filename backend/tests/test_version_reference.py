from __future__ import annotations

from types import SimpleNamespace

from app.core.version_reference import resolve_version_reference


def _version(version_id: str, generated_at: str):
    return SimpleNamespace(id=version_id, generatedAt=generated_at)


def test_explicit_chinese_plan_number_maps_to_chronological_version() -> None:
    older = _version("ver_aaaaaaaa", "2026-10-27T10:00:00+08:00")
    newer = _version("ver_bbbbbbbb", "2026-10-27T12:00:00+08:00")

    first, kind_first = resolve_version_reference("方案01的数学模型", [newer, older])
    second, kind_second = resolve_version_reference("方案 2 呢", [newer, older])

    assert kind_first == "ordinal"
    assert kind_second == "ordinal"
    assert first.id == "ver_aaaaaaaa"
    assert second.id == "ver_bbbbbbbb"


def test_explicit_version_id_wins_over_selected_version() -> None:
    older = _version("ver_aaaaaaaa", "2026-10-27T10:00:00+08:00")
    newer = _version("ver_bbbbbbbb", "2026-10-27T12:00:00+08:00")

    resolved, kind = resolve_version_reference(
        "看看 ver_aaaaaaaa 的目标函数",
        [newer, older],
        selected_version=newer,
    )

    assert kind == "id"
    assert resolved.id == "ver_aaaaaaaa"


def test_out_of_range_plan_number_is_not_silently_replaced() -> None:
    versions = [_version("ver_aaaaaaaa", "2026-10-27T10:00:00+08:00")]

    resolved, kind = resolve_version_reference("方案09的模型", versions)

    assert kind == "ordinal"
    assert resolved is None
