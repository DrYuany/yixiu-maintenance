"""小一修评测跑分器：E1 校验层 / E2 翻译层 / E3 端到端 / E4 提示词一致性。

判分纪律（与项目发布闸门同源）：全部本地确定性判分，不用 LLM 给 LLM 打分。
E2 在线层调大模型，但判分仍是确定性的——翻出来的补丁与 gold **逐字段**比对
（槽位、索引解析结果、运算、数值、强度），不是让另一个模型说"差不多"。

用法：
    python run_eval.py                    # 跑 E1/E2离线/E5/E6/E4（快层，不求解）
    python run_eval.py --layer E3         # 端到端（要跑三十多次求解）
    python run_eval.py --online           # 加跑 E2/E7 在线层（需要 DEEPSEEK_API_KEY）
    python run_eval.py --seconds 30       # E3/E6 每个用例的求解时限
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from yxcore import PROJECT_ROOT, solver_modules  # noqa: E402
from yxcore import slots as S  # noqa: E402
from yxcore.patch import Patch, normalize_patch, parse_patch  # noqa: E402
from yxcore.validate import validate  # noqa: E402

CASES_VALIDATE = json.loads((HERE / "cases_validate.json").read_text(encoding="utf-8"))
CASES_E2E = json.loads((HERE / "cases_e2e.json").read_text(encoding="utf-8"))
CASES_MULTITURN = json.loads((HERE / "cases_multiturn.json").read_text(encoding="utf-8"))
CASES_COMPOUND = json.loads((HERE / "cases_compound.json").read_text(encoding="utf-8"))
DIALOGUES = json.loads((HERE / "cases_dialogue.json").read_text(encoding="utf-8"))
UTTERANCES = json.loads((HERE / "utterances.json").read_text(encoding="utf-8"))

_snapshot_cache: tuple | None = None


def snapshot():
    """基座01 快照（数据 + 候选），进程内只算一次。"""
    global _snapshot_cache
    if _snapshot_cache is None:
        modules = solver_modules()
        data = modules["load_project"](PROJECT_ROOT, base_id="基座01")
        options = {t["任务编号"]: modules["candidates"](data, t) for t in data.tasks}
        _snapshot_cache = (data, options)
    return _snapshot_cache


def gold_patches(case: dict) -> list[Patch]:
    patches = []
    for raw in case.get("期望") or []:
        payload = dict(raw)
        payload.setdefault("utterance", case["话术"])
        payload.setdefault("strength", "hard")
        patch, error = parse_patch(payload)
        assert patch is not None, f"gold 补丁形状不对（{case['话术']}）：{error}"
        patches.append(patch)
    return patches


# ── E1 校验层 ───────────────────────────────────────────────────────────

def run_e1() -> list[str]:
    data, options = snapshot()
    failures: list[str] = []
    for case in CASES_VALIDATE:
        name = case["名"]
        patches, errors = [], []
        for raw in case["补丁"]:
            patch, error = parse_patch(raw)
            if patch is None:
                errors.append(error or "补丁解析失败")
            else:
                patches.append(patch)
        if patches and not errors:
            verr, _ = validate(patches, data, options)
            errors = verr
        if case["期望"] == "拒":
            if not errors:
                failures.append(f"E1[{name}]：应该拦截却放行了 {case['补丁']}")
                continue
            for frag in case.get("理由含", []):
                if not any(frag in e for e in errors):
                    failures.append(f"E1[{name}]：拦了但理由不对，要含「{frag}」，实际 {errors}")
        else:
            if errors:
                failures.append(f"E1[{name}]：合法补丁被误拦：{errors}")
    return failures


# ── E2 翻译层 ───────────────────────────────────────────────────────────

def _entities():
    from dsl_core.registry import entities
    return entities


def _canon_index_value(kind: str, value, data) -> object:
    entities = _entities()
    kind = {"severity": "严重度", "sea": "海况"}.get(kind, kind)  # 英文登记/中文书写同义
    if kind in (None, "", "全部") or value is None:
        return "全部"  # 整轮口径：写「全部」和不写是一回事
    if kind == "日期区间" and isinstance(value, (list, tuple)) and len(value) == 2 \
            and value[0] == value[1]:
        # 只干一天：写成「某日」还是「区间[同一天,同一天]」是同一格
        return str(value[0])
    if kind in ("部件", "part"):
        return entities.resolve_part(data, str(value)) or f"!{value}"
    if kind == "风机":
        return entities.resolve_turbine(data, str(value)) or f"!{value}"
    if kind in ("任务", "task"):
        return entities.resolve_task(data, str(value)) or f"!{value}"
    if kind in ("任务清单", "task_seq", "task_set"):
        values = value if isinstance(value, (list, tuple)) else [value]
        if len(values) == 1:
            # 单元素清单和点名单个任务是同一格（模型两种写法都出现过）
            return entities.resolve_task(data, str(values[0])) or f"!{values[0]}"
        return tuple(sorted(entities.resolve_task(data, str(v)) or f"!{v}" for v in values))
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    return str(value)


def canonical(patch: Patch, data) -> tuple:
    """补丁的判分形态：别名归一到注册编号，数值归一到浮点，weight 的值不比（偏好刻度无标准答案）。

    gold 和模型输出都要过 normalize_patch：两边都按同一套「口语→注册值」的归一处理，
    比的才是语义，不是书写习惯（否则「恶劣」vs「恶劣但安全」会被判成落点不同）。
    """
    patch = normalize_patch(patch)
    pairs = []
    for key, spec in patch.index.items():
        value = _canon_index_value((spec or {}).get("kind", ""), (spec or {}).get("value"), data) \
            if isinstance(spec, dict) else f"!索引格式错误:{spec!r}"
        # 「组=全部」和不写组是同一格：都要么是整轮口径，要么是同一件事
        if key == "组" and value == "全部":
            continue
        pairs.append((key, value))
    idx = tuple(sorted(pairs))
    value = patch.value
    spec = S.SLOTS.get(patch.slot)
    if spec is not None and spec.value_type == "minutes" and isinstance(value, str) \
            and len(value) >= 10:
        # 用户说的是「19号」，不是「19号零点还是19号23:59」。gold 里这两种写法都有
        # （同一个意思，写的人不同），判分只比到「哪一天」，不替用户发明分钟精度。
        value = value[:10]
    if isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        value = round(float(value), 6)
    return (patch.slot, idx, patch.op, value)


def run_e2_offline() -> list[str]:
    data, options = snapshot()
    failures: list[str] = []
    refusals = 0
    for case in UTTERANCES:
        if not case["期望"]:
            refusals += 1
            assert case.get("拒绝"), f"「{case['话术']}」没写拒绝理由"
            continue
        patches = gold_patches(case)
        for patch in patches:
            if patch.slot not in S.SLOTS and patch.slot not in S.PARAM_SLOTS:
                failures.append(f"E2离线[{case['话术']}]：gold 槽位 {patch.slot} 不在注册表")
        errors, _ = validate(patches, data, options)
        if errors:
            failures.append(f"E2离线[{case['话术']}]：gold 补丁没过校验：{errors}")
    if refusals < 6:
        failures.append(f"E2离线：拒绝类话术只有 {refusals} 条，边界覆盖不足")
    return failures


def run_e2_online(*, seconds: float = 90.0) -> tuple[list[str], list[str]]:
    from yxcore.parse import translate

    data, options = snapshot()
    failures: list[str] = []
    cautions: list[str] = []
    for case in UTTERANCES:
        if not case["期望"]:
            patches, reason = translate(case["话术"], data, options, timeout=seconds)
            if patches:
                failures.append(f"E2在线[{case['话术']}]：应拒绝却给了补丁 {[p.slot for p in patches]}（糊弄）")
            continue
        patches, reason = translate(case["话术"], data, options, timeout=seconds)
        if reason or not patches:
            failures.append(f"E2在线[{case['话术']}]：被错误拒绝：{reason}")
            continue
        got = sorted(canonical(p, data) for p in patches)
        wants = [sorted(canonical(p, data) for p in gold_patches(case))]
        for alt in case.get("期望或") or []:
            alt_patches = []
            for raw in alt:
                payload = dict(raw)
                payload.setdefault("utterance", case["话术"])
                payload.setdefault("strength", "hard")
                alt_patch, err = parse_patch(payload)
                assert alt_patch is not None, f"{case['话术']} 的等价解形状不对：{err}"
                alt_patches.append(alt_patch)
            wants.append(sorted(canonical(p, data) for p in alt_patches))
        if got not in wants:
            failures.append(f"E2在线[{case['话术']}]：\n    得到 {got}\n    应为 {wants[0]}"
                            + (f"\n    或等价解 {wants[1]}" if len(wants) > 1 else ""))
            continue
        # 槽位/索引/值都对，再看强度：unsure 算过慎不算错（按 D3，拿不准要问人）
        got_strength = {p.slot: p.strength for p in patches}
        want_strength = {p.slot: p.strength for p in gold_patches(case)}
        # 目标类（策略）没有「硬约束」这回事：它就是偏好，不比强度
        objective = {name for name, s in S.SLOTS.items() if s.route == "strategy"}
        for slot in list(want_strength):
            if slot in objective:
                want_strength.pop(slot)
        for slot, want_s in want_strength.items():
            got_s = got_strength.get(slot)
            if got_s != want_s and got_s == "unsure":
                cautions.append(f"E2在线[{case['话术']}]：{slot} 应为 {want_s}，模型报了 unsure（过慎）")
            elif got_s != want_s:
                failures.append(f"E2在线[{case['话术']}]：{slot} 强度应为 {want_s}，得到 {got_s}")
    return failures, cautions


# ── E3 端到端 ───────────────────────────────────────────────────────────

CAUTIONS: list[str] = []  # 如实标注但不判失败的情形（如未证最优时的方案波动）


def run_e3(*, seconds: float = 20.0) -> list[str]:
    CAUTIONS.clear()
    from yxcore.pipeline import YxError, run

    failures: list[str] = []
    # 端到端一条要跑求解器、拦截类还要跑诊断探针，整层十几分钟起步。
    # 不逐条报进度的话，跑起来像卡死；顺带把上一条的用时打出来，能直接看出哪条变慢了。
    _prev: list = [None, time.monotonic()]
    for case in CASES_E2E:
        name = case["名"]
        if _prev[0] is not None:
            print(f"  [E3] 上一条「{_prev[0]}」用时 {time.monotonic() - _prev[1]:.1f}s",
                  flush=True)
        _prev[0], _prev[1] = name, time.monotonic()
        print(f"  [E3] {name}", flush=True)
        patches = []
        for raw in case["补丁"]:
            patch, error = parse_patch(raw)
            assert patch is not None, f"{name}：补丁形状不对 {error}"
            patches.append(patch)
        expect = case["期望"]
        try:
            if expect == "指纹一致":
                r1 = run(patches, seconds=case.get("秒", seconds))
                r2 = run(patches, seconds=case.get("秒", seconds))
                if r1["指纹"] != r2["指纹"]:
                    failures.append(f"E3[{name}]：同一输入两次指纹不同 {r1['指纹']} vs {r2['指纹']}")
                continue
            if expect == "排程一致":
                r1 = run(patches, seconds=case.get("秒", seconds))
                r2 = run(patches, seconds=case.get("秒", seconds))
                same = json.dumps(r1["排程"], ensure_ascii=False, sort_keys=True) == \
                    json.dumps(r2["排程"], ensure_ascii=False, sort_keys=True)
                if same:
                    continue
                # 没证到最优时方案本就会随时限波动——这不是缺陷，是不该被承诺的东西。
                # 反过来说：证到最优还不一致，那就是真缺陷。
                if r1.get("已证最优") and r2.get("已证最优"):
                    failures.append(f"E3[{name}]：两次都证到最优，排程却不同——复现性破了")
                elif len(r1["排程"]) != len(r2["排程"]):
                    failures.append(f"E3[{name}]：两次排程条数都不同 {len(r1['排程'])} vs {len(r2['排程'])}")
                else:
                    CAUTIONS.append(
                        f"E3[{name}]：两次排程内容不同，但求解器未证最优"
                        f"（{r1.get('未证最优目标')}）——属时限内波动，已如实标注，不判失败")
                continue
            r = run(patches, seconds=case.get("秒", seconds))
        except YxError as exc:
            reasons = list(exc.errors) + list(getattr(exc, "warnings", []) or [])
            if expect != "拦截":
                failures.append(f"E3[{name}]：期望出解却被拦：{exc.errors}")
            else:
                for frag in case.get("拦截含", []):
                    if not any(frag in e for e in reasons):
                        failures.append(f"E3[{name}]：拦了但理由要含「{frag}」，实际 {exc.errors}")
            continue
        if expect == "拦截":
            failures.append(f"E3[{name}]：期望拦截却出解了")
            continue
        sel = r["排程"] or []
        if "排程数" in case and len(sel) != case["排程数"]:
            failures.append(f"E3[{name}]：排程数 {len(sel)}，应为 {case['排程数']}")
        if "排程数最多" in case and len(sel) > case["排程数最多"]:
            failures.append(f"E3[{name}]：排程数 {len(sel)}，超过上限 {case['排程数最多']}")
        if "部分" in case and r["部分排程"] != case["部分"]:
            failures.append(f"E3[{name}]：部分排程={r['部分排程']}，应为 {case['部分']}")
        audit = r.get("对账") or ""
        for frag in case.get("对账含", []):
            if frag not in audit:
                failures.append(f"E3[{name}]：对账里找不到「{frag}」；对账全文：{audit}")
        for frag in case.get("对账不含", []):
            if frag in audit:
                failures.append(f"E3[{name}]：对账里出现了不该有的「{frag}」；对账全文：{audit}")
    return failures


# ── E5 多轮状态机（离线） ───────────────────────────────────────────────

def _parse_list(raws: list[dict], utterance: str) -> list[Patch]:
    patches = []
    for raw in raws:
        payload = dict(raw)
        payload.setdefault("utterance", utterance)
        payload.setdefault("strength", "hard")
        patch, error = parse_patch(payload)
        assert patch is not None, f"{utterance} 的补丁形状不对：{error}"
        patches.append(normalize_patch(patch))
    return patches


def run_e5() -> list[str]:
    """多轮：同一格被反复改、撤销、叠加，以及「改主意没撤旧的」这类错要拦下。

    这一层不调大模型——多轮的**语义**必须先在这一层锁死。
    在线那层（E7）才有意义：模型答对了才说明它真的会用会话状态。
    """
    from yxcore.session import Session

    data, options = snapshot()
    failures: list[str] = []
    for case in CASES_MULTITURN:
        name = case["名"]
        session = Session(data)
        echoes: list[str] = []
        for turn in case["轮"]:
            patches = _parse_list(turn["补丁"], turn["话术"])
            applied = session.apply(patches, utterance=turn["话术"])
            echoes += applied.echo()
            want = turn.get("期望") or {}
            if "错误含" in want:
                if not any(want["错误含"] in e for e in applied.错误):
                    failures.append(f"E5[{name}｜{turn['话术']}]：该报错没报"
                                    f"（要含「{want['错误含']}」），实际 {applied.错误}")
                continue
            for field, got in (("新增", len(applied.新增)), ("修订", len(applied.修订)),
                               ("撤销", len(applied.撤销)), ("空撤销", len(applied.空撤销)),
                               ("无变化", len(applied.无变化))):
                if field in want and got != want[field]:
                    failures.append(f"E5[{name}｜{turn['话术']}]：{field} {got}，应为 {want[field]}")
            if applied.错误:
                failures.append(f"E5[{name}｜{turn['话术']}]：不该报错却报了 {applied.错误}")
        final = case["终局"]
        if "生效数" in final and len(session.state) != final["生效数"]:
            failures.append(f"E5[{name}]：生效 {len(session.state)} 条，应为 {final['生效数']}")
        errors, _ = session.check(data, options)
        if final.get("校验") == "拒":
            if not errors:
                failures.append(f"E5[{name}]：应该被拦下却通过了")
            for frag in final.get("理由含", []):
                if not any(frag in e for e in errors):
                    failures.append(f"E5[{name}]：拦了但理由要含「{frag}」，实际 {errors}")
        elif errors:
            failures.append(f"E5[{name}]：终局不该被拦，实际 {errors}")
        for slot, want_value in (final.get("值") or {}).items():
            got = [p.value for p in session.patches() if p.slot == slot]
            if len(got) != 1:
                failures.append(f"E5[{name}]：终局里 {slot} 有 {len(got)} 条，应为 1 条")
            elif isinstance(want_value, (int, float)) and abs(float(got[0]) - float(want_value)) > 1e-6:
                failures.append(f"E5[{name}]：{slot} = {got[0]}，应为 {want_value}")
            elif not isinstance(want_value, (int, float)) and got[0] != want_value:
                failures.append(f"E5[{name}]：{slot} = {got[0]}，应为 {want_value}")
        text = "\n".join(echoes)
        for frag in final.get("含", []):
            if frag not in text:
                failures.append(f"E5[{name}]：回显里找不到「{frag}」；回显全文：{text}")
        for frag in final.get("不含", []):
            if frag in text:
                failures.append(f"E5[{name}]：回显里出现了不该有的「{frag}」；回显全文：{text}")

        # 多轮走到最后要能真的排出来：状态对了但求解炸了，等于白攒
        run_expect = case.get("求解")
        if run_expect and not errors:
            from yxcore.pipeline import YxError, run as run_pipeline

            try:
                result = run_pipeline(session.patches(), seconds=case.get("秒", 20))
            except YxError as exc:
                if run_expect.get("期望") != "拦截":
                    failures.append(f"E5[{name}]：终局求解被拦：{exc.errors}")
                else:
                    for frag in run_expect.get("拦截含", []):
                        if not any(frag in e for e in exc.errors):
                            failures.append(f"E5[{name}]：拦了但理由要含「{frag}」，实际 {exc.errors}")
                continue
            if run_expect.get("期望") == "拦截":
                failures.append(f"E5[{name}]：期望拦截却出解了")
                continue
            selected = result["排程"] or []
            if "排程数" in run_expect and len(selected) != run_expect["排程数"]:
                failures.append(f"E5[{name}]：排程数 {len(selected)}，应为 {run_expect['排程数']}")
            if "部分" in run_expect and result["部分排程"] != run_expect["部分"]:
                failures.append(f"E5[{name}]：部分排程={result['部分排程']}，应为 {run_expect['部分']}")
            for frag in run_expect.get("对账含", []):
                if frag not in (result.get("对账") or ""):
                    failures.append(f"E5[{name}]：对账里找不到「{frag}」")
            said = "\n".join(result.get("警告") or [])
            for frag in run_expect.get("警告含", []):
                if frag not in said:
                    failures.append(f"E5[{name}]：告警里找不到「{frag}」；告警全文：{said}")
    return failures


# ── E6 复合需求（离线 + 可选求解） ─────────────────────────────────────

def run_e6(*, seconds: float = 20.0) -> list[str]:
    """一句话里几个要求同时上：整体要么通过、要么拦下且理由对。

    复合需求的失败模式和单条不同：每一条单看都合法，合起来才矛盾。
    所以这里的用例全是「两条以上、跨族」的组合。
    """
    data, options = snapshot()
    failures: list[str] = []
    for case in CASES_COMPOUND:
        name = case["名"]
        patches = _parse_list(case["补丁"], name)
        errors, _warnings = validate(patches, data, options)
        if case["期望"] == "拒":
            if not errors:
                failures.append(f"E6[{name}]：应该拦下却放行了")
            for frag in case.get("理由含", []):
                if not any(frag in e for e in errors):
                    failures.append(f"E6[{name}]：拦了但理由要含「{frag}」，实际 {errors}")
            continue
        if errors:
            failures.append(f"E6[{name}]：合法组合被误拦：{errors}")
            continue
        solve = case.get("求解")
        if not solve:
            continue
        from yxcore.pipeline import YxError, run

        try:
            result = run(patches, seconds=case.get("秒", seconds))
        except YxError as exc:
            reasons = list(exc.errors) + list(getattr(exc, "warnings", []) or [])
            if solve.get("期望") != "拦截":
                failures.append(f"E6[{name}]：期望出解却被拦：{exc.errors}")
            else:
                for frag in solve.get("拦截含", []):
                    if not any(frag in e for e in reasons):
                        failures.append(f"E6[{name}]：拦了但理由要含「{frag}」，实际 {exc.errors}")
            continue
        if solve.get("期望") == "拦截":
            failures.append(f"E6[{name}]：期望拦截却出解了")
            continue
        selected = result["排程"] or []
        if "排程数" in solve and len(selected) != solve["排程数"]:
            failures.append(f"E6[{name}]：排程数 {len(selected)}，应为 {solve['排程数']}")
        if "排程数最多" in solve and len(selected) > solve["排程数最多"]:
            failures.append(f"E6[{name}]：排程数 {len(selected)}，超过上限 {solve['排程数最多']}")
        if "部分" in solve and result["部分排程"] != solve["部分"]:
            failures.append(f"E6[{name}]：部分排程={result['部分排程']}，应为 {solve['部分']}")
        audit = result.get("对账") or ""
        for frag in solve.get("对账含", []):
            if frag not in audit:
                failures.append(f"E6[{name}]：对账里找不到「{frag}」；对账全文：{audit}")
        for frag in solve.get("对账不含", []):
            if frag in audit:
                failures.append(f"E6[{name}]：对账里出现了不该有的「{frag}」")
        said = "\n".join(result.get("警告") or [])
        for frag in solve.get("警告含", []):
            if frag not in said:
                failures.append(f"E6[{name}]：告警里找不到「{frag}」；告警全文：{said}")
    return failures


# ── E7 多轮在线（需要大模型） ───────────────────────────────────────────

def run_e7(*, seconds: float = 90.0) -> tuple[list[str], list[str]]:
    """真对话：逐轮翻译 + 落地，最后比生效旋钮表。

    判分仍是确定性的：只看「动了几格」和「终局那张表」，
    不看模型怎么解释自己。
    """
    from yxcore.session import Session

    data, options = snapshot()
    failures: list[str] = []
    cautions: list[str] = []
    for case in DIALOGUES:
        name = case["名"]
        session = Session(data)
        broke = False
        for i, text in enumerate(case["对话"]):
            applied = session.say(text, data, options, timeout=seconds)
            want = (case.get("期望每轮") or [])[i] if i < len(case.get("期望每轮") or []) else {}
            if want.get("拒绝"):
                # 「该拒」有两种落地：模型自己说做不到（拒绝），或者补丁落地时被
                # 校验层拦下（错误 + 整轮回滚）。后者也是拒，而且更常见——
                # 用户说「高危全修，但最多 2 个」，模型照翻，拦在校验层。
                if not (applied.拒绝 or applied.错误):
                    failures.append(f"E7[{name}｜{text}]：该拒绝却落地了 {len(applied.新增)} 条")
                continue
            if applied.拒绝:
                failures.append(f"E7[{name}｜{text}]：被错误拒绝：{applied.拒绝}")
                broke = True
                break
            if applied.错误:
                failures.append(f"E7[{name}｜{text}]：落地失败 {applied.错误}")
                broke = True
                break
            for field, got in (("新增", len(applied.新增)), ("修订", len(applied.修订)),
                               ("撤销", len(applied.撤销)), ("空撤销", len(applied.空撤销))):
                if field in want and got != want[field]:
                    failures.append(f"E7[{name}｜{text}]：{field} {got}，应为 {want[field]}")
            if applied.待确认 and not want.get("待确认"):
                cautions.append(f"E7[{name}｜{text}]：模型报了 unsure，需要追问（不算错）")
        if broke:
            continue
        final = case["终局"]
        if "生效数" in final and len(session.state) != final["生效数"]:
            failures.append(f"E7[{name}]：终局生效 {len(session.state)} 条，应为 {final['生效数']}")
        gold = _parse_list(final.get("补丁") or [], name)
        got_set = sorted(canonical(p, data) for p in session.patches())
        want_set = sorted(canonical(normalize_patch(p), data) for p in gold)
        if got_set != want_set:
            failures.append(f"E7[{name}]：终局旋钮表不一致\n    得到 {got_set}\n    应为 {want_set}")
        errors, _ = session.check(data, options)
        if final.get("校验") == "拒" and not errors:
            failures.append(f"E7[{name}]：终局应该被拦下（自相矛盾）却通过了")
    return failures, cautions


# ── E4 提示词一致性 ─────────────────────────────────────────────────────

def run_e4() -> list[str]:
    from yxcore.prompt import BOUNDARY, build_prompt

    data, options = snapshot()
    text = build_prompt(data, options)
    failures: list[str] = []
    for name in S.SLOTS:
        if f"* {name}｜" not in text:
            failures.append(f"E4：提示词里没有槽位 {name}——注册表和提示词漂移了")
    for alias in S.PARAM_SLOTS:
        if f"* {alias}｜" not in text:
            failures.append(f"E4：提示词里没有参数 {alias}")
    for name, _example in BOUNDARY:
        if name not in text:
            failures.append(f"E4：提示词里没有边界条目「{name}」")
    # 护栏数字：每个有任务在册的部件都要带「N 个」
    for t in data.tasks:
        part_name = data.parts[t["部件编号"]]["部件名称"]
        if part_name not in text:
            failures.append(f"E4：提示词里没有部件 {part_name}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", default="E1,E2,E5,E6,E4")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args()
    layers = {x.strip().upper() for x in args.layer.split(",")}

    all_failures: list[str] = []
    if "E1" in layers:
        f = run_e1()
        print(f"E1 校验层   ：{len(CASES_VALIDATE)} 条，{len(CASES_VALIDATE)-len(f)} 通过")
        all_failures += f
    if "E2" in layers:
        f = run_e2_offline()
        print(f"E2 翻译离线 ：{len(UTTERANCES)} 条话术，gold 全部在册且过校验" if not f else f"E2 翻译离线 ：{f}")
        all_failures += f
    if "E3" in layers:
        f = run_e3(seconds=args.seconds)
        print(f"E3 端到端   ：{len(CASES_E2E)} 条，{len(CASES_E2E)-len(f)} 通过")
        for note in CAUTIONS:
            print("  标注：", note)
        all_failures += f
    if "E5" in layers:
        f = run_e5()
        print(f"E5 多轮状态 ：{len(CASES_MULTITURN)} 条对话，{len(CASES_MULTITURN)-len(f)} 通过")
        all_failures += f
    if "E6" in layers:
        f = run_e6(seconds=args.seconds)
        print(f"E6 复合需求 ：{len(CASES_COMPOUND)} 条组合，{len(CASES_COMPOUND)-len(f)} 通过")
        all_failures += f
    if "E4" in layers:
        f = run_e4()
        print(f"E4 提示词   ：一致性检查{'通过' if not f else '失败'}")
        all_failures += f
    if args.online and "E2" in layers:
        from yxcore.parse import configured
        if not configured():
            print("E2 翻译在线 ：未配置 DEEPSEEK_API_KEY，跳过（不假装跑过）")
        else:
            try:
                f, c = run_e2_online()
                online_total = len(UTTERANCES)
                print(f"E2 翻译在线 ：{online_total} 条话术，{online_total-len(f)} 通过"
                      + (f"，另有 {len(c)} 条过慎（unsure，不判错）" if c else ""))
                all_failures += f
                for note in c:
                    print(f"  过慎：{note}")
            except Exception as exc:  # noqa: BLE001 - 调不通就如实说，不算通过
                print(f"E2 翻译在线 ：调用失败，未跑（{exc}）")
    if args.online and "E7" in layers:
        from yxcore.parse import configured
        if not configured():
            print("E7 多轮在线 ：未配置 DEEPSEEK_API_KEY，跳过")
        else:
            try:
                f, c = run_e7()
                total = len(DIALOGUES)
                print(f"E7 多轮在线 ：{total} 段对话，{total-len(f)} 通过"
                      + (f"，另有 {len(c)} 条过慎" if c else ""))
                all_failures += f
                for note in c:
                    print(f"  过慎：{note}")
            except Exception as exc:  # noqa: BLE001
                print(f"E7 多轮在线 ：调用失败，未跑（{exc}）")

    if all_failures:
        print(f"\n── 失败 {len(all_failures)} 项 ──")
        for f in all_failures:
            print("✗", f)
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
