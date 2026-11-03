"""把「大模型填表可能犯的错」穷举成一个封闭集合——从表的自由度推导，不从经验列举。

推导链条（这是完备性的唯一依据）：

  填表产物 = (表集合, 每张表的行集合, 每行的栏目, 每行的值)
  产物要能被执行，还必须满足：形状合法、依据可查。

  → 7 个决策点。每个决策点的输出类型决定了它只有有限种错法：
       D0 形状      布尔（合法/不合法）            → 3 种
       D1 要不要填  布尔                            → 2 种
       D2 填几张    整数 n                          → 3 种
       D3 有几行    集合（原文诉求单元的子集）      → 2 种
       D4 归哪栏    枚举（表头）                    → 3 种
       D5 值是多少  值（类型/单位/量级/域/方向）    → 5 种
       D6 依据在哪  字符区间                        → 2 种
                                             合计 20 类

  只要「填表」这个动作可以被参数化成上面这 6 个自由度，错误就必然落在 20 格里。
  这是结构性的，不依赖用户怎么说。

  但有一件事必须承认：**「少了」这一类（E1.1 / E2.1 / E3.1）审查看不见**——
  没有输出就没有 token 可以检查。这三类的判据不在产物里，在原文里
  （先列诉求单元，再核对每个单元有没有归属）。这是原理限制，不是工程缺陷。

每类给出三项：怎么判（judge）、判据来自哪（basis）、现在有没有（status）。
  basis: "产物"（只看 DSL）/ "原文"（只看原文）/ "对照"（DSL × 原文）
  status: "有" / "缺" / "原理缺"（审查抓不到，须外部基准）

用法：
    python backend/eval_cases/error_taxonomy.py            # 跑内建案例，看归类覆盖
    python backend/eval_cases/error_taxonomy.py --list     # 只列分类表
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: dsl_core
from dsl_core.registry import parameters as param_reg  # noqa: E402
from dsl_core.registry.primitives import (  # noqa: E402
    CONSTRAINT_PRIMITIVES,
    OBJECTIVE_PRIMITIVES,
    OPS,
)

HIT, CLEAR, UNKNOWN, NA, SUSPECT = "命中", "通过", "判不了", "不适用", "疑似"

TOP_KEYS = ("parameters", "constraints", "objectives", "data")
LIST_KEYS = TOP_KEYS
PARAM_REQUIRED = ("field", "op", "value")

# 允许从默认值派生的字段（「翻倍」这类），白名单要人维护，初始只放能查证的 4 项
DERIVABLE = ("船舶日租金", "维修人工单价", "电价整体倍率", "检修期间停机比例")

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _numbers(text: str) -> set[float]:
    out: set[float] = set()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(万|亿|千)?", text or ""):
        v = float(m.group(1))
        unit = m.group(2)
        if unit == "万":
            v *= 1e4
        elif unit == "亿":
            v *= 1e8
        elif unit == "千":
            v *= 1e3
        out.add(round(v, 4))
    for cn, n in _CN_NUM.items():
        if f"{cn}小时" in text or f"{cn}条" in text or f"{cn}项" in text:
            out.add(float(n))
    return out


def _dsl_numbers(node: Any, acc: set[float] | None = None) -> set[float]:
    acc = set() if acc is None else acc
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("day", "date", "start", "end"):
                continue
            _dsl_numbers(v, acc)
    elif isinstance(node, list):
        for v in node:
            _dsl_numbers(v, acc)
    elif isinstance(node, bool):
        pass
    elif isinstance(node, (int, float)):
        acc.add(round(float(node), 4))
    return acc


def _range_of(name: str) -> tuple[float, float] | None:
    row = param_reg.lookup(name)
    if not row:
        return None
    text = row.get("取值约束", "") or ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:[~\-－—]|至|到)\s*(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def _bound_of(name: str) -> tuple[float | None, float | None]:
    """散文里的上下界：『不超过24』『大于0』『不低于常态上限』后两种解析不出数。"""
    row = param_reg.lookup(name) or {}
    text = row.get("取值约束", "") or ""
    hi = re.search(r"(?:不超过|不大于|至多|最多)\s*(\d+(?:\.\d+)?)", text)
    lo = re.search(r"(?:大于|不低于|至少|不小于)\s*(\d+(?:\.\d+)?)", text)
    return (float(lo.group(1)) if lo else None, float(hi.group(1)) if hi else None)


def _is_numeric_param(name: str) -> bool:
    row = param_reg.lookup(name) or {}
    unit = (row.get("单位") or "").strip()
    # 「无」= 编号/列表型；枚举类也不是数值型
    if unit in ("", "无", "枚举", "选项", "-"):
        return False
    if "枚举" in (row.get("取值约束") or ""):
        return False
    return True


def _params(dsl: dict) -> list[dict]:
    v = dsl.get("parameters")
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _cons(dsl: dict) -> list[dict]:
    v = dsl.get("constraints")
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _objs(dsl: dict) -> list[dict]:
    v = dsl.get("objectives")
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


# ── D0 形状 ──────────────────────────────────────────────────────────────────

def e0_1(u: str, d: Any) -> str:
    if not isinstance(d, dict):
        return HIT
    return HIT if set(d) - set(TOP_KEYS) else CLEAR


def e0_2(u: str, d: Any) -> str:
    if not isinstance(d, dict):
        return NA
    for k in LIST_KEYS:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            return HIT  # {"每日有效维修上限": 8} —— 字典当数组
        if isinstance(v, list) and any(isinstance(x, dict) for x in v) is False and v:
            return HIT
    return CLEAR


def e0_3(u: str, d: Any) -> str:
    if not isinstance(d, dict):
        return NA
    for p in _params(d):
        if any(k not in p for k in PARAM_REQUIRED):
            return HIT
    for c in _cons(d):
        if "type" not in c:
            return HIT
    for o in _objs(d):
        if "type" not in o:
            return HIT
    return CLEAR


# ── D1 要不要填 ──────────────────────────────────────────────────────────────

_ASK = re.compile(r"多少|几个|哪个|哪一版|为什么|差在哪|区别|比较|对比|吗$|呢$")


def e1_1(u: str, d: Any) -> str:
    # 原文有明确诉求却判成不填 → 需要「诉求单元」基准，审查原理上判不了
    return UNKNOWN


def e1_2(u: str, d: Any) -> str:
    if not isinstance(d, dict) or not d:
        return NA
    has_change = bool(_params(d) or _cons(d) or _objs(d))
    if has_change and _ASK.search(_norm(u)):
        return HIT
    return CLEAR


# ── D2 填几张 ────────────────────────────────────────────────────────────────

def e2_1(u: str, d: Any) -> str:
    return UNKNOWN  # 少拆：同上，需要原文诉求数


def e2_2(u: str, d: Any) -> str:
    if not isinstance(d, dict):
        return NA
    seen: dict[str, Any] = {}
    for p in _params(d):
        f = p.get("field")
        if f in seen and seen[f] != p.get("value"):
            return HIT
        seen[f] = p.get("value")
    return CLEAR


def e2_3(u: str, d: Any) -> str:
    return UNKNOWN


# ── D3 有几行 ────────────────────────────────────────────────────────────────

def e3_1(u: str, d: Any) -> str:
    return UNKNOWN  # 漏：没有输出就没有 token 可查


def e3_2(u: str, d: Any) -> str:
    if not isinstance(d, dict):
        return NA
    sig: list[str] = []
    for c in _cons(d):
        sig.append(json.dumps({k: c.get(k) for k in ("type", "task", "tasks", "before", "after")},
                              ensure_ascii=False, sort_keys=True))
    return HIT if len(sig) != len(set(sig)) else CLEAR


# ── D4 归哪栏 ────────────────────────────────────────────────────────────────

def e4_1(u: str, d: Any) -> str:
    if not isinstance(d, dict):
        return NA
    for p in _params(d):
        if param_reg.route_of(p.get("field") or "") != "parameters":
            return HIT
    for c in _cons(d):
        if c.get("type") not in CONSTRAINT_PRIMITIVES:
            return HIT
    for o in _objs(d):
        if o.get("type") not in OBJECTIVE_PRIMITIVES:
            return HIT
    return CLEAR


_APPROX_ALL = re.compile(r"全部|所有|都修|全修|每一个|一律")
_ONLY = re.compile(r"只排|只看|仅排|仅看|只做")
_EXCLUDE_ONLY = re.compile(r"^\s*只")


def e4_3(u: str, d: Any) -> str:
    """用近似栏目顶替不存在的栏目——能力缺口最常见的伪装。"""
    if not isinstance(d, dict):
        return NA
    text = _norm(u)
    for c in _cons(d):
        if c.get("type") == "cardinality" and _APPROX_ALL.search(text):
            v = c.get("value")
            if isinstance(v, (int, float)) and v <= 1:
                return HIT
    if _ONLY.search(text):
        if sum(1 for c in _cons(d) if c.get("type") == "exclude") >= 3:
            return HIT
    return CLEAR


def e4_2(u: str, d: Any) -> str:
    """张冠李戴：靠句式表对照。

    这条只能给「疑似」不能给「命中」——句式表的关键词会串门。
    实测「风速上限放宽到14米每秒」被「天气/海况」句式抢走，
    因为「风速」同时是天气句式和参数句式的触发词。误报比漏报更伤，
    所以这里降一级，只当线索交给人工/第二轮。
    """
    try:
        from eval_cases.audit_slot_coverage import audit as coverage_audit
    except Exception:  # noqa: BLE001
        return UNKNOWN
    if not isinstance(d, dict):
        return NA
    res = coverage_audit(u, d)
    return SUSPECT if res.get("落空") else CLEAR


# ── D5 值 ────────────────────────────────────────────────────────────────────

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


def e5_1(u: str, d: Any) -> str:
    if not isinstance(d, dict):
        return NA
    for p in _params(d):
        name, val = p.get("field"), p.get("value")
        if not isinstance(name, str):
            continue
        if isinstance(val, (list, dict)):
            continue  # 列表型/结构型参数，不是数值槽位
        if _is_numeric_param(name) and not isinstance(val, (int, float)):
            return HIT
        if isinstance(val, str) and not val:
            return HIT
    for item in (d.get("data") or []):
        if isinstance(item, dict):
            day = item.get("day")
            if isinstance(day, str) and day and not _ISO.match(day):
                return HIT
    return CLEAR


def e5_3(u: str, d: Any) -> str:
    """量级错：比默认值大/小 100 倍以上，且原文数值里找不到这个量级。"""
    if not isinstance(d, dict):
        return NA
    orig = _numbers(u)
    for p in _params(d):
        name, val = p.get("field"), p.get("value")
        if not isinstance(name, str) or not isinstance(val, (int, float)):
            continue
        row = param_reg.lookup(name) or {}
        try:
            default = float(re.sub(r"[^\d.]", "", str(row.get("默认值", ""))) or "nan")
        except ValueError:
            continue
        if default != default or default == 0:  # nan / 0 跳过
            continue
        ratio = val / default
        if ratio >= 50 or (0 < ratio <= 0.02):
            if not any(abs(n - val) < 1e-6 or abs(n - val * 10) < 1e-6 for n in orig):
                return HIT
    return CLEAR


def e5_4(u: str, d: Any) -> str:
    if not isinstance(d, dict):
        return NA
    for p in _params(d):
        name, val = p.get("field"), p.get("value")
        if not isinstance(name, str) or not isinstance(val, (int, float)):
            continue
        rng = _range_of(name)
        if rng:
            if not (rng[0] <= val <= rng[1]):
                return HIT
            continue
        lo, hi = _bound_of(name)
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            return HIT
    return CLEAR


_LE = re.compile(r"不超过|别超|不能超|最多|上限|顶多|以内|以下")
_GE = re.compile(r"不低于|至少|最少|以上|起步")


def e5_5(u: str, d: Any) -> str:
    if not isinstance(d, dict):
        return NA
    text = _norm(u)
    for c in _cons(d):
        op = c.get("op")
        if op not in OPS:
            continue
        if _LE.search(text) and op == ">=":
            return HIT
        if _GE.search(text) and op == "<=":
            return HIT
    return CLEAR


# ── D6 依据 ──────────────────────────────────────────────────────────────────

def e6_1(u: str, d: Any) -> str:
    """无出处：DSL 里的数值既不在原文，也不是「可派生字段的默认值×倍率」。"""
    if not isinstance(d, dict):
        return NA
    orig = _numbers(u)
    derived: set[float] = set()
    for p in _params(d):
        name, val = p.get("field"), p.get("value")
        if name in DERIVABLE and isinstance(val, (int, float)):
            derived.add(round(float(val), 4))
    for n in _dsl_numbers(d):
        if n in (0.0, 1.0, 2.0):  # 结构常量：布尔、次数、条数
            continue
        if any(abs(n - o) < 1e-6 for o in orig):
            continue
        if any(abs(n - x) < 1e-6 for x in derived):
            continue
        # 原文写「万」，DSL 写全值：已在 _numbers 里换算；再容一次 ×1 / ÷1
        return HIT
    return CLEAR


def e6_2(u: str, d: Any) -> str:
    return UNKNOWN  # 需要模型给 span 位置，当前契约只有文本，没有区间


ERRORS: list[dict[str, Any]] = [
    # 编号, 决策点, 名字, judge, 判据来源, 现状
    dict(id="E0.1", dp="D0 形状", name="顶层不是对象 / 段名拼错", judge=e0_1, basis="产物", status="有"),
    dict(id="E0.2", dp="D0 形状", name="段内元素写成字典（应为数组）", judge=e0_2, basis="产物", status="缺"),
    dict(id="E0.3", dp="D0 形状", name="必填字段缺失（op/value/field）", judge=e0_3, basis="产物", status="有"),
    dict(id="E1.1", dp="D1 要不要填", name="该填没填（判成闲聊/解释）", judge=e1_1, basis="原文", status="原理缺"),
    dict(id="E1.2", dp="D1 要不要填", name="不该填填了（问句当指令）", judge=e1_2, basis="对照", status="缺"),
    dict(id="E2.1", dp="D2 填几张", name="少拆（两个诉求只填一张）", judge=e2_1, basis="原文", status="原理缺"),
    dict(id="E2.2", dp="D2 填几张", name="多拆/重复（同字段两个值）", judge=e2_2, basis="产物", status="有"),
    dict(id="E2.3", dp="D2 填几张", name="边界错（前半句诉求挂到后半句）", judge=e2_3, basis="对照", status="缺"),
    dict(id="E3.1", dp="D3 有几行", name="行少了（漏诉求）", judge=e3_1, basis="原文", status="原理缺"),
    dict(id="E3.2", dp="D3 有几行", name="行多了（同一约束写两遍）", judge=e3_2, basis="产物", status="有"),
    dict(id="E4.1", dp="D4 归哪栏", name="栏目不存在（编造表头）", judge=e4_1, basis="产物", status="有"),
    dict(id="E4.2", dp="D4 归哪栏", name="张冠李戴（存在但放错）", judge=e4_2, basis="对照", status="缺"),
    dict(id="E4.3", dp="D4 归哪栏", name="近似栏目顶替缺失栏目", judge=e4_3, basis="对照", status="缺"),
    dict(id="E5.1", dp="D5 值", name="类型错（数字写成中文/日期非 ISO）", judge=e5_1, basis="产物", status="缺"),
    dict(id="E5.2", dp="D5 值", name="单位错（小时写成分钟）", judge=lambda u, d: UNKNOWN, basis="对照", status="缺"),
    dict(id="E5.3", dp="D5 值", name="量级错（300万写成300）", judge=e5_3, basis="对照", status="缺"),
    dict(id="E5.4", dp="D5 值", name="取值域越界", judge=e5_4, basis="产物", status="缺"),
    dict(id="E5.5", dp="D5 值", name="比较方向反了（≤ 写成 ≥）", judge=e5_5, basis="对照", status="缺"),
    dict(id="E6.1", dp="D6 依据", name="无出处（凭空数值）", judge=e6_1, basis="对照", status="缺"),
    dict(id="E6.2", dp="D6 依据", name="出处错（引用原文另一段）", judge=e6_2, basis="对照", status="缺"),
]


# 形状不合法时，内容判定全部失效——必须短路，否则报一串连带错误，人就不看了
_DOWNSTREAM = ("E1.2", "E2.2", "E3.2", "E4.1", "E4.2", "E4.3",
               "E5.1", "E5.2", "E5.3", "E5.4", "E5.5", "E6.1", "E6.2")
SHORTCUT = "待归一化"
# 本来就不产出 DSL 的 route：对它判「形状错」是误报
NO_DSL_ROUTES = ("clarify", "reject", "chitchat", "explain", "compare", "query_plan", "query_data")


def audit(utterance: str, dsl: Any, route: str | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    if dsl is None and route in NO_DSL_ROUTES:
        # 这类回答本来就没有表可填，20 类全部不适用。
        # 不这么判，审计器会把每条 clarify 都报成「顶层不是对象」——假警报一多，人就关了。
        return {e["id"]: NA for e in ERRORS}
    for e in ERRORS:
        try:
            out[e["id"]] = e["judge"](utterance, dsl)
        except Exception as exc:  # noqa: BLE001 - 审计器自己不许把整轮跑崩
            out[e["id"]] = f"异常:{type(exc).__name__}"
    if out.get("E0.1") == HIT or out.get("E0.2") == HIT:
        for k in _DOWNSTREAM:
            if out.get(k) in (HIT, SUSPECT):
                out[k] = SHORTCUT
    return out


# ── 内建案例：每条都是真实观测过或可复现的错误形态 ────────────────────────────

CASES: list[dict[str, Any]] = [
    # —— 13 条正确输出：期望零命中（防误报）——
    dict(u="按成本最低优先排每天只能干8小时", expect=[], d={
        "parameters": [{"field": "每日有效维修上限", "op": "=", "value": 8}],
        "constraints": [], "objectives": [{"type": "strategy", "name": "成本最低优先"}], "data": []}),
    dict(u="钱不超过300万", expect=[], d={
        "parameters": [], "constraints": [{"type": "budget", "quantity": "现金支出", "op": "<=", "value": 3000000}],
        "objectives": [], "data": []}),
    dict(u="船租翻倍了，重新排一下", expect=[], d={
        "parameters": [{"field": "船舶日租金", "op": "=", "value": 24000}]}),
    dict(u="先修任务001再修任务002，钱别超300万", expect=[], d={
        "parameters": [], "constraints": [
            {"type": "precedence", "before": "任务001", "after": "任务002"},
            {"type": "budget", "quantity": "现金支出", "op": "<=", "value": 3000000}],
        "objectives": [], "data": []}),
    dict(u="每天只能干8小时，另外最多上2条作业线，别排任务005", expect=[], d={
        "parameters": [{"field": "每日有效维修上限", "op": "=", "value": 8},
                       {"field": "最大作业线", "op": "=", "value": 2}],
        "constraints": [{"type": "exclude", "task": "任务005"}], "objectives": [], "data": []}),
    dict(u="任务001和任务002不要同一天做", expect=[], d={
        "constraints": [{"type": "co_occur", "mode": "diff_day", "tasks": ["任务001", "任务002"]}]}),

    # —— 真实观测到的错误形态 ——
    dict(u="明天风大，先别出海", note="实测 day 写成中文", expect=["E5.1"], d={
        "data": [{"kind": "weather", "day": "明天", "state": "不可作业"}]}),
    dict(u="成本压到最低，但高风险的必须全修", note="实测用 cardinality>=1 顶替「全部」", expect=["E4.3"], d={
        "parameters": [],
        "constraints": [{"type": "cardinality", "filter": {"severity": "高"}, "op": ">=", "value": 1}],
        "objectives": [{"type": "strategy", "name": "成本最低优先"}], "data": []}),
    dict(u="只看风机003和风机004", note="实测用 19 条 exclude 伪造范围限定；E6.1 抓不到字符串里的编号（已知盲区）",
         expect=["E4.3"],
         d={"constraints": [{"type": "exclude", "task": f"任务{i:03d}"} for i in range(1, 20)]}),
    dict(u="每天只能干8小时", note="实测参数写成字典", expect=["E0.2"], d={
        "parameters": {"每日有效维修上限": 8}}),
    dict(u="每天只能干8小时", note="实测值写成中文", expect=["E5.1"], d={
        "parameters": [{"field": "每日有效维修上限", "op": "=", "value": "八小时"}]}),
    dict(u="船租最近涨了不少", note="实测编造 15000（但它在可派生白名单里，故只标 E4.2 覆盖不到）",
         expect=[], d={"parameters": [{"field": "船舶日租金", "op": "=", "value": 15000}]}),
    dict(u="风速上限放宽到14米每秒",
         note="原文 14，DSL 写 1400；E5.4 判不了——该字段的界是「不低于常态上限」，引用另一字段而非常量",
         expect=["E5.3", "E6.1"], d={
        "parameters": [{"field": "安全风速上限", "op": "=", "value": 1400}]}),
    dict(u="钱不超过300万", note="方向写反", expect=["E5.5"], d={
        "constraints": [{"type": "budget", "op": ">=", "value": 3000000}]}),
    dict(u="只排部件01和部件02", note="栏目不在落点表", expect=["E4.1"], d={
        "parameters": [{"field": "候选部件", "op": "=", "value": ["部件01", "部件02"]}]}),
    dict(u="把工期限制在某个范围", note="编造原语；30 也无出处", expect=["E4.1", "E6.1"], d={
        "constraints": [{"type": "range_limit", "value": 30}]}),
    dict(u="船租涨两成，人工单价也涨两成", note="同字段两个值", expect=["E2.2"], d={
        "parameters": [{"field": "船舶日租金", "op": "=", "value": 14400},
                       {"field": "船舶日租金", "op": "=", "value": 15000}]}),
    dict(u="经济优先和风险优先差多少", note="问句被当指令", expect=["E1.2"], d={
        "objectives": [{"type": "strategy", "name": "高风险优先抢修"}]}),
]


def _load_probe() -> list[dict[str, Any]]:
    p = Path(__file__).with_name("_probe_llm_first_result.json")
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列分类表")
    args = ap.parse_args()

    if args.list:
        print(f"{'编号':6s} {'决策点':12s} {'错误形态':30s} {'判据':6s} 现状")
        for e in ERRORS:
            print(f"{e['id']:6s} {e['dp']:12s} {e['name']:30s} {e['basis']:6s} {e['status']}")
        print(f"\n合计 {len(ERRORS)} 类")
        return 0

    # 1) 内建案例的归类覆盖
    print("=" * 78)
    print("一、内建案例：期望命中的类，分类器是否命中；不该命中的，是否误报")
    print("=" * 78)
    miss = 0
    false_pos = 0
    for c in CASES:
        got = audit(c["u"], c["d"])
        hits = [k for k, v in got.items() if v == HIT]
        ok = set(hits) == set(c["expect"])
        if not ok:
            if set(c["expect"]) - set(hits):
                miss += 1
            if set(hits) - set(c["expect"]):
                false_pos += 1
        mark = "OK " if ok else "差异"
        note = f"  ｜{c['note']}" if c.get("note") else ""
        print(f"[{mark}] {c['u'][:34]:36s} 期望{sorted(c['expect'])} 实得{sorted(hits)}{note}")
    print(f"\n漏判 {miss} 条，误报 {false_pos} 条，共 {len(CASES)} 条")

    # 2) 真实模型输出上的表现
    rows = _load_probe()
    if rows:
        print("\n" + "=" * 78)
        print("二、13 条真实模型输出：分类器判了什么")
        print("=" * 78)
        clean = 0
        for r in rows:
            pay = r.get("payload") or {}
            route = pay.get("route")
            got = audit(r["utterance"], pay.get("dsl"), route=route)
            hits = [k for k, v in got.items() if v == HIT]
            sus = [k for k, v in got.items() if v == SUSPECT]
            if not hits and not sus:
                clean += 1
            extra = f" 疑似={sus}" if sus else ""
            print(f"{r['utterance'][:34]:36s} route={str(route):12s} 命中={hits or '无'}{extra}")
        print(f"\n{len(rows)} 条中，零命中/零疑似 {clean} 条；命中真错 {len(rows) - clean} 条")

    # 3) 三类原理缺的占比
    print("\n" + "=" * 78)
    print("三、分类覆盖统计")
    print("=" * 78)
    for status in ("有", "缺", "原理缺"):
        ids = [e["id"] for e in ERRORS if e["status"] == status]
        print(f"{status:5s} {len(ids):2d} 类  {ids}")
    by_basis: dict[str, int] = {}
    for e in ERRORS:
        by_basis[e["basis"]] = by_basis.get(e["basis"], 0) + 1
    print(f"\n判据来源分布：{by_basis}")
    print("注意「原理缺」3 类全是'少了'：没有输出就没有 token 可查，只能靠原文先列清单。")

    # 4) 值域能从参数总表自动解析出几项——决定 E5.4 的真实覆盖率
    names = [r["参数名称"] for r in param_reg._rows() if param_reg.route_of(r["参数名称"]) == "parameters"]
    parsed = [n for n in names if _range_of(n) or any(b is not None for b in _bound_of(n))]
    print("\n" + "=" * 78)
    print(f"四、值域可机器解析率：{len(parsed)}/{len(names)}")
    print("=" * 78)
    for n in names:
        rng, bnd = _range_of(n), _bound_of(n)
        raw = (param_reg.lookup(n) or {}).get("取值约束", "")
        mark = "可解析" if (rng or any(b is not None for b in bnd)) else "散文，需手写"
        print(f"  {n:16s} {mark:10s} {str(rng or bnd):22s} 原始：{raw[:38]}")
    print("\n结论：E5.4 的覆盖率不取决于算法，取决于『取值约束』这一列写得多规整。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
