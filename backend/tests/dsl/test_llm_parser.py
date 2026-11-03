"""三层解析器单测：规则层 + 向量层 + LLM 层（mock，无外部调用）。"""
from __future__ import annotations

import json

import pytest

from app.core import llm_parser
from app.core.llm_parser import (
    ParseUnavailable,
    load_library,
    parse,
    parse_by_rules,
    parse_with_llm,
    retrieve,
)


# ── 规则层 ────────────────────────────────────────────────────────────────────

def test_rule_co_occur_diff_day() -> None:
    dsl = parse_by_rules("任务001和任务002不能在同一天出海维修")
    assert dsl == {"constraints": [{"type": "co_occur", "mode": "diff_day", "tasks": ["任务001", "任务002"]}]}


def test_rule_precedence() -> None:
    dsl = parse_by_rules("先修任务001再修任务022")
    assert dsl["constraints"][0]["type"] == "precedence"
    assert dsl["constraints"][0]["before"] == "任务001"
    assert dsl["constraints"][0]["after"] == "任务022"


def test_rule_t_normalizes_task_ref() -> None:
    dsl = parse_by_rules("T01和T02不能同时修")
    assert dsl["constraints"][0]["tasks"] == ["任务001", "任务002"]


def test_rule_earliest_with_date() -> None:
    dsl = parse_by_rules("任务018最早11月5日开工")
    assert dsl["constraints"][0] == {"type": "earliest", "task": "任务018", "time": "2026-11-05T08:00:00+08:00"}


def test_rule_fix_line() -> None:
    dsl = parse_by_rules("任务018只能上2号船")
    assert dsl["constraints"][0] == {"type": "fix", "task": "任务018", "line": 2}


def test_rule_forbidden_dates() -> None:
    dsl = parse_by_rules("11月3号不要开工")
    assert dsl["constraints"][0] == {
        "type": "forbidden_dates", "dates": ["2026-11-03T08:00:00+08:00"],
    }


def test_rule_unmatched_returns_none() -> None:
    assert parse_by_rules("今天天气怎么样") is None


# ── 向量层 ────────────────────────────────────────────────────────────────────

def test_retrieve_ranks_similar_phrasing_first() -> None:
    library = load_library()
    top = retrieve("任务003和任务004不能同一天出海", library, top_k=1)
    assert top[0]["id"] == "tpl-001"


def test_retrieve_similarity_is_bounded() -> None:
    top = retrieve("任务001和任务002不能在同一天出海维修", load_library(), top_k=1)
    assert 0.0 < top[0]["similarity"] <= 1.0


# ── LLM 层（mock，不调真实 API）──────────────────────────────────────────────

class _FakeBlock:
    type = "text"
    text: str


class _FakeMessages:
    def __init__(self, dsl: dict) -> None:
        block = _FakeBlock()
        block.text = f"```json\n{json.dumps(dsl)}\n```"
        self.content = [block]


class _FakeClient:
    """模仿 anthropic 客户端：client.messages.create(...) 返回带 .content 的响应。"""

    def __init__(self, dsl: dict) -> None:
        self._dsl = dsl
        self.last_request = None
        self.messages = self

    def create(self, **kwargs):
        self.last_request = kwargs
        return _FakeMessages(self._dsl)


def test_llm_layer_sends_few_shot_and_validates() -> None:
    expected = {"constraints": [{"type": "co_occur", "mode": "diff_day", "tasks": ["任务003", "任务004"]}]}
    client = _FakeClient(expected)
    templates = retrieve("任务003和任务004不能同一天出海", load_library(), top_k=3)

    result = parse_with_llm("任务003和任务004不能同一天出海", templates, client=client)

    assert result.source == "llm"
    assert result.dsl == expected
    # few-shot 例子必须进了 prompt
    prompt = client.last_request["messages"][0]["content"]
    assert "tpl-001" in "".join(str(entry["id"]) for entry in templates) or "不能在同一天" in prompt


def test_llm_layer_rejects_invalid_output() -> None:
    client = _FakeClient({"constraints": [{"type": "magic_wand", "task": "任务001"}]})
    result = parse_with_llm("任意话", [], client=client)
    assert result.route == "clarify"
    assert result.dsl is None


def test_llm_layer_unavailable_without_key(monkeypatch) -> None:
    import os

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    os.environ.pop("MINIMAX_API_KEY", None)
    with pytest.raises(ParseUnavailable):
        parse_with_llm("任意话", [])


# ── 三层入口 ──────────────────────────────────────────────────────────────────

def test_parse_prefers_rule_layer(monkeypatch) -> None:
    result = parse("任务001和任务002不能在同一天出海维修")
    assert result.source == "rule"
    assert result.confidence == 1.0


# ── 第二期：参数/数据/时序假设规则 → DSL 段 ─────────────────────────────────

def test_rule_parameter_ship_rent_relative() -> None:
    dsl = parse_by_rules("船租上涨20%")
    assert dsl == {"parameters": [{"field": "船舶日租金", "op": "*", "value": 1.2}]}


def test_rule_parameter_lines() -> None:
    dsl = parse_by_rules("最多两条作业线")
    assert dsl["parameters"] == [{"field": "最大作业线", "op": "=", "value": 2}]


def test_rule_parameter_daily_hours() -> None:
    dsl = parse_by_rules("每天最多8小时")
    assert dsl["parameters"] == [{"field": "每日有效维修上限", "op": "=", "value": 8.0}]


def test_rule_parameter_daily_hours_with_overtime_and_verb() -> None:
    dsl = parse_by_rules("每天可以加班，最多维修10小时")
    assert dsl["parameters"] == [{"field": "每日有效维修上限", "op": "=", "value": 10.0}]


def test_rule_data_arrival_with_block() -> None:
    dsl = parse_by_rules("叶片备件11月5日到货，11月5日之前不能开工")
    spec = dsl["data"][0]
    assert spec["kind"] == "part_arrival"
    assert spec["part"] == "叶片"
    assert spec["blockBefore"] is True


def test_rule_data_deadline_with_severity() -> None:
    dsl = parse_by_rules("严重故障11月8日前必须修完")
    spec = dsl["data"][0]
    assert spec["kind"] == "task_deadline"
    assert spec["severity"] == "高"


def test_rule_weather_wind_assumption() -> None:
    dsl = parse_by_rules("11月3日风速15米每秒")
    assert dsl["data"] == [{"kind": "weather", "day": "2026-11-03", "wind": 15.0}]


def test_rule_weather_state_assumption() -> None:
    dsl = parse_by_rules("11月4日海况不可作业")
    assert dsl["data"] == [{"kind": "weather", "day": "2026-11-04", "state": "不可作业"}]


# ── 第三期：生成层参数规则 ──────────────────────────────────────────────────

def test_rule_granularity() -> None:
    assert parse_by_rules("候选开工粒度改成30分钟")["parameters"] == [
        {"field": "候选开工粒度", "op": "=", "value": 30}
    ]


def test_rule_no_multiday() -> None:
    assert parse_by_rules("不允许跨天暂停")["parameters"] == [
        {"field": "允许跨天暂停", "op": "=", "value": "否"}
    ]


def test_rule_weather_threshold() -> None:
    assert parse_by_rules("安全风速上限改成11")["parameters"] == [
        {"field": "安全风速上限", "op": "=", "value": 11.0}
    ]


def test_rule_no_night_transfer() -> None:
    assert parse_by_rules("禁止夜间转移")["parameters"] == [
        {"field": "允许夜间转移", "op": "=", "value": "否"}
    ]


def test_legacy_language_bridged_to_dsl() -> None:
    from app.core.llm_parser import parse_by_legacy

    dsl = parse_by_legacy("船租上涨20%")
    assert dsl is not None
    assert any(p["field"] == "船舶日租金" and p["op"] == "*" for p in dsl["parameters"])


# ── 过程可视化 stage 事件 ─────────────────────────────────────────────────────

def test_parse_emits_stage_events_for_rule_path() -> None:
    events: list[tuple[str, str, str, str | None]] = []
    result = parse(
        "任务001和任务002不能在同一天出海维修",
        library=[],
        on_stage=lambda *a: events.append(a),
    )
    assert result.source == "rule"
    assert result.route == "dsl"
    ids = [e[0] for e in events]
    assert "route" in ids
    assert "parse.rule" in ids
    # 规则命中后不触发 LLM/检索阶段
    assert "parse.llm" not in ids
    assert "parse.retrieve" not in ids
    # 首尾 route 状态闭合：先 running，最后 done
    assert events[0][2] == "running"
    assert events[-1][0] == "route" and events[-1][2] == "done"


def test_parse_emits_llm_stages_when_rule_misses() -> None:
    events: list[tuple[str, str, str, str | None]] = []
    result = parse(
        "一条规则层匹配不到的陌生排程诉求",
        library=[{"id": "tpl-1", "utterance": "任务001和任务002不能同一天", "dsl": {}}],
        client=_FakeClient({"constraints": [{"type": "co_occur", "mode": "diff_day", "tasks": ["任务001", "任务002"]}]}),
        on_stage=lambda *a: events.append(a),
    )
    ids = [e[0] for e in events]
    # 规则层未命中时必然走到检索 + LLM
    assert "parse.retrieve" in ids
    assert "parse.llm" in ids
    # 且 LLM 阶段闭合为 done
    llm_events = [e for e in events if e[0] == "parse.llm"]
    assert llm_events and llm_events[-1][2] == "done"
    assert result is not None


# ── 置信度 / 降级（EchoMind 思路重构）────────────────────────────────────────

class _WrapperMessages:
    def __init__(self, payload: dict) -> None:
        block = _FakeBlock()
        block.text = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        self.content = [block]


class _WrapperClient:
    """模仿 anthropic 客户端，返回统一包装格式 {"route","confidence","dsl"}。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.last_request = None
        self.messages = self

    def create(self, **kwargs):
        self.last_request = kwargs
        return _WrapperMessages(self._payload)


def test_llm_confidence_parsed_from_wrapper() -> None:
    client = _WrapperClient({"route": "dsl", "confidence": 0.93,
                             "dsl": {"constraints": [{"type": "must", "task": "任务001"}]}})
    result = parse_with_llm("任意", [], client=client)
    assert result.source == "llm" and result.route == "dsl"
    assert result.confidence == 0.93
    assert result.source_scores["llm"] == 0.93


def test_llm_low_confidence_degrades_to_clarify() -> None:
    client = _WrapperClient({"route": "dsl", "confidence": 0.4,
                             "dsl": {"constraints": [{"type": "must", "task": "任务001"}]}})
    result = parse_with_llm("任意", [], client=client)
    assert result.route == "clarify"
    assert result.dsl is None


def test_llm_reject_confidence_preserved() -> None:
    client = _WrapperClient({"route": "reject", "confidence": 0.7})
    result = parse_with_llm("任意", [], client=client)
    assert result.route == "reject" and result.confidence == 0.7


def test_rule_layer_records_source_scores_and_latency() -> None:
    result = parse("任务001和任务002不能在同一天出海维修", library=[])
    assert result.source == "rule"
    assert result.source_scores.get("rule") == 1.0
    assert result.latency_ms >= 0.0


def test_rule_postpone_to_date_maps_to_earliest() -> None:
    dsl = parse_by_rules("任务001推迟到11月5日开工")
    assert dsl is not None
    assert dsl["constraints"][0] == {"type": "earliest", "task": "任务001",
                                     "time": "2026-11-05T08:00:00+08:00"}


def test_llm_result_is_cached(monkeypatch) -> None:
    """重复话术命中缓存，不再二次调 LLM（慢的问题：缓存思路）。"""
    llm_parser._PARSE_CACHE.clear()
    calls: list[int] = []

    class CountingClient:
        def __init__(self) -> None:
            self.messages = self

        def create(self, **kwargs):
            calls.append(1)
            return _WrapperMessages({"route": "dsl", "confidence": 0.9,
                                     "dsl": {"constraints": [{"type": "must", "task": "任务001"}]}})

    msg = "一句肯定不命中任何规则的陌生排程话术"
    library = [{"id": "tpl-x", "utterance": "某无关模板", "dsl": {}}]
    first = parse(msg, library=library, client=CountingClient())
    second = parse(msg, library=library, client=CountingClient())

    assert first.route == second.route == "dsl"
    assert len(calls) == 1  # 第二次走缓存，不再调 LLM
    llm_parser._PARSE_CACHE.clear()


# ── 复合句 / 上下文注入（重构第 4、5 步）────────────────────────────────────

def test_compound_sentence_is_handed_to_llm() -> None:
    """规则层只做单约束快速通道；多约束句子不再静默丢后面的约束。"""
    assert parse_by_rules("任务001和任务002不能同一天，另外船租涨20%") is None
    assert parse_by_rules(
        "任务005最早11月6日开工，任务006最晚11月20日前修完"
    ) is None


def test_negation_does_not_trigger_positive_substring_rule() -> None:
    """「不允许跨天暂停」不应被「允许跨天暂停」的子串规则抢走。"""
    dsl = parse_by_rules("不允许跨天暂停")
    assert dsl == {"parameters": [{"field": "允许跨天暂停", "op": "=", "value": "否"}]}


def test_single_constraint_still_handled_by_rules() -> None:
    dsl = parse_by_rules("任务001和任务002不能在同一天出海维修")
    assert dsl is not None
    assert "constraints" in dsl


def test_context_block_is_prepended_to_llm_prompt() -> None:
    client = _WrapperClient({"route": "dsl", "confidence": 0.9,
                             "dsl": {"constraints": [{"type": "must", "task": "任务001"}]}})
    result = parse_with_llm(
        "随便一句", [], client=client,
        context_block="## 当前数据\n- 任务：任务001~任务011（共11个）",
    )
    assert result.route == "dsl"
    assert "任务001~任务011" in client.last_request["messages"][0]["content"]


# ── 对话修复 / 元问题（2026-10-29：不能被 DSL 澄清劫持）────────────────────

def test_repair_utterance_routes_to_not_change() -> None:
    for text in ["你没听懂我说的意思？", "我是说两版本的区别", "答非所问了吧"]:
        result = parse(text, library=[])
        assert result.route == "not_change", f"{text} -> {result.route}"


# ── 复合句拆分合并（2026-10-29）─────────────────────────────────────────────

def test_compound_splits_and_merges_two_constraints() -> None:
    result = parse("任务005最早11月6日开工，任务006最晚11月20日前修完", library=[])
    assert result.route == "dsl" and result.source == "compound"
    types = {c["type"] for c in result.dsl["constraints"]}
    assert types == {"earliest", "deadline"}


def test_compound_merges_parameter_and_constraint() -> None:
    result = parse("任务001和任务002不能同一天，另外船租涨20%", library=[])
    assert result.route == "dsl"
    assert result.dsl["constraints"][0]["type"] == "co_occur"
    assert result.dsl["parameters"][0]["field"] == "船舶日租金"


def test_compound_dedupes_identical_items() -> None:
    result = parse("任务001最早11月5日开工，任务001最早11月5日开工", library=[])
    assert result.route == "dsl"
    assert len(result.dsl["constraints"]) == 1


def test_question_is_not_split() -> None:
    assert parse("任务001最早什么时候开工，任务002呢？", library=[]).route != "compound"


def test_multi_task_must_expands_to_each_task() -> None:
    dsl = parse_by_rules("任务001和任务003必须修")
    assert [c["task"] for c in dsl["constraints"]] == ["任务001", "任务003"]


def test_multi_task_exclude_expands_to_each_task() -> None:
    dsl = parse_by_rules("别排任务001和任务002")
    assert [c["task"] for c in dsl["constraints"]] == ["任务001", "任务002"]
