"""第 1 期：业务话术回归集。

两层判分，一层都不含糊：

1. **离线层**（不调大模型，每次都跑）：gold 补丁必须通过校验器。
   gold 自己都过不了校验，说明宪法和话术对不齐，是设计问题不是模型问题——
   这层的作用就是让「话术表」和「注册表」不可能各说各话。
2. **在线层**（配了 key 才跑）：让大模型翻译话术，跟 gold 比对槽位与落点。
   没配 key 就跳过，绝不假装跑过。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from yxcore import PROJECT_ROOT, solver_modules
from yxcore.parse import configured
from yxcore.patch import Patch, parse_patch
from yxcore.validate import validate

HERE = Path(__file__).resolve().parent
CASES = json.loads((HERE.parent / "eval" / "utterances.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snapshot():
    modules = solver_modules()
    data = modules["load_project"](PROJECT_ROOT, base_id="基座01")
    options = {t["任务编号"]: modules["candidates"](data, t) for t in data.tasks}
    return data, options


def gold_patches(case: dict) -> list[Patch]:
    patches = []
    for raw in case["期望"]:
        payload = dict(raw)
        payload.setdefault("utterance", case["话术"])
        payload.setdefault("strength", "hard")
        patch, error = parse_patch(payload)
        assert patch is not None, f"gold 补丁形状不对（{case['话术']}）：{error}"
        patches.append(patch)
    return patches


def test_gold_cases_are_registered(snapshot):
    """每条话术的 gold 槽位都必须在册——话术表和注册表不许各说各话。"""
    from yxcore import slots as S

    for case in CASES:
        for patch in gold_patches(case):
            assert patch.slot in S.SLOTS or patch.slot in S.PARAM_SLOTS, \
                f"「{case['话术']}」的 gold 槽位 {patch.slot} 不在注册表里"


def test_gold_cases_pass_validation(snapshot):
    """gold 补丁必须能被校验器接受。"""
    data, options = snapshot
    for case in CASES:
        if not case["期望"]:
            continue
        errors, _ = validate(gold_patches(case), data, options)
        assert not errors, f"「{case['话术']}」的 gold 补丁没过校验：{errors}"


def test_refusal_cases_have_no_gold(snapshot):
    """拒绝类话术必须真的没有 gold——有 gold 就说明它在偷偷用近似槽位糊弄。"""
    refusals = [c for c in CASES if not c["期望"]]
    assert len(refusals) >= 4, "边界类话术至少要有 4 条，否则回归集覆盖不到拒绝这一侧"
    for case in refusals:
        assert case.get("拒绝"), f"「{case['话术']}」没写拒绝理由"


def test_utterance_coverage():
    """每个约束族至少被一条话术覆盖——族没人说话，就是设计里没人用的族。"""
    from yxcore import slots as S

    covered = set()
    for case in CASES:
        for patch in gold_patches(case):
            covered.add(patch.slot)
    missing = [name for name, slot in S.SLOTS.items()
               if slot.route not in ("data", "timeline") and name not in covered]
    assert not missing, f"这些族没有任何话术覆盖：{missing}"


@pytest.mark.skipif(not configured(), reason="未配置 DEEPSEEK_API_KEY，跳过在线翻译回归")
def test_llm_matches_gold(snapshot):
    """在线层：大模型翻出来的槽位和落点必须和 gold 一致（值可以不同口径，槽位不许差）。"""
    from yxcore.parse import translate

    data, options = snapshot
    mismatched = []
    for case in CASES:
        if not case["期望"]:
            continue
        patches, reason = translate(case["话术"], data, options)
        if reason or not patches:
            mismatched.append(f"「{case['话术']}」被拒：{reason}")
            continue
        got = {p.slot for p in patches}
        want = {p.slot for p in gold_patches(case)}
        if got != want:
            mismatched.append(f"「{case['话术']}」槽位落错：得到 {got}，应为 {want}")
    assert not mismatched, "\n".join(mismatched)
