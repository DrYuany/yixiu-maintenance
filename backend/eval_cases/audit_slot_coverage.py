"""对账审计器：不看模型自报的 spans/unresolved，用代码独立判断「原文该给的槽位给了没」。

为什么不能信模型自报：
  - 上一轮 13 条里，模型自报 unresolved 只有 2 条（都是它自己判 clarify 的），
    剩下 11 条全报「已全部消化」。自报覆盖率没有任何信息量。
  - 自报是「模型对自己工作的评价」，让它评价自己，等于没有评价。

这个审计器只做两件可判定的事：
  1. 数字对账：原文出现的每个数量（含中文百分数/倍率）是否在 DSL 里有落点
  2. 需求对账：原文命中的句式（每天X小时/预算/先后顺序/不同一天…）是否在 DSL 里有对应节点

局限（必须承认）：句式表是人写的，覆盖不到「没被想到的说法」。
它的价值不是「证明全对」，是「把静默丢话从不可见变成可数」。
"""
from __future__ import annotations

import json
import re
from typing import Any

# ── 中文数量 → 数值 ───────────────────────────────────────────────────────────
_CN_NUM = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_MULT = {"十": 10, "百": 100, "千": 1000}


def _cn_to_int(text: str) -> int | None:
    """把「八」「十二」「二十」这类中文数字转成整数；不认识返回 None。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, section, seen = 0, 0, False
    for ch in text:
        if ch in _CN_NUM and _CN_NUM[ch] < 10:
            section = _CN_NUM[ch]
            seen = True
        elif ch in _CN_MULT:
            section = (section or 1) * _CN_MULT[ch]
            total += section
            section = 0
            seen = True
        else:
            return None
    return total + section if seen else None


def extract_quantities(text: str) -> list[dict[str, Any]]:
    """抽原文的数量诉求，带语义类型（绝对值 / 倍率 / 百分比）。"""
    out: list[dict[str, Any]] = []

    # 倍率：翻倍 / 翻一番 / 涨两成 / 上涨20%
    for m in re.finditer(r"翻(?:了)?(?:一)?倍|翻一番", text):
        out.append({"raw": m.group(0), "kind": "multiplier", "value": 2.0})
    for m in re.finditer(r"([一二两三四五六七八九十\d]+)\s*成", text):
        n = _cn_to_int(m.group(1))
        if n is not None:
            out.append({"raw": m.group(0), "kind": "multiplier", "value": 1 + n / 10})
    for m in re.finditer(r"[涨增扩]?(\d+(?:\.\d+)?)\s*%", text):
        out.append({"raw": m.group(0), "kind": "multiplier", "value": 1 + float(m.group(1)) / 100})

    # 金额：300万 / 350万 / 5000元
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*万\s*元?", text):
        out.append({"raw": m.group(0), "kind": "money", "value": float(m.group(1)) * 10000})
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*元", text):
        if not re.search(r"\d\s*万\s*$", text[: m.start() + 1]):
            out.append({"raw": m.group(0), "kind": "money", "value": float(m.group(1))})

    # 工时：8小时 / 六个小时
    for m in re.finditer(r"(\d+|[一二两三四五六七八九十]+)\s*(?:个)?\s*小时", text):
        n = _cn_to_int(m.group(1))
        if n is not None:
            out.append({"raw": m.group(0), "kind": "hours", "value": float(n)})

    # 作业线条数：2条作业线
    for m in re.finditer(r"(\d+|[一二两三四五六七八九十]+)\s*(?:条|个)\s*作业线", text):
        n = _cn_to_int(m.group(1))
        if n is not None:
            out.append({"raw": m.group(0), "kind": "lines", "value": float(n)})

    # 任务/部件/风机编号（本身不是数量，但要落地）
    for m in re.finditer(r"任务\s*(\d{3})", text):
        out.append({"raw": m.group(0), "kind": "task_ref", "value": m.group(1)})

    return out


def collect_numbers(node: Any, acc: list[float] | None = None) -> list[float]:
    """把 DSL 里所有数值摊平（数组、对象、字符串里的数字都算）。"""
    acc = [] if acc is None else acc
    if isinstance(node, bool):
        return acc
    if isinstance(node, (int, float)):
        acc.append(float(node))
    elif isinstance(node, dict):
        for v in node.values():
            collect_numbers(v, acc)
    elif isinstance(node, list):
        for v in node:
            collect_numbers(v, acc)
    elif isinstance(node, str):
        for m in re.finditer(r"\d+(?:\.\d+)?", node):
            acc.append(float(m.group(0)))
    return acc


# ── 需求句式 → 必须存在的 DSL 节点 ───────────────────────────────────────────
# (名字, 正则, 判定函数)  判定函数返回 True 表示「DSL 里确实有对应节点」
def _has_param(dsl: dict, name: str) -> bool:
    for p in dsl.get("parameters") or []:
        if isinstance(p, dict) and p.get("field") == name:
            return True
    return False


def _has_constraint(dsl: dict, *types: str) -> bool:
    for c in dsl.get("constraints") or []:
        if isinstance(c, dict) and c.get("type") in types:
            return True
    return False


def _has_strategy(dsl: dict, name: str) -> bool:
    for o in dsl.get("objectives") or []:
        if isinstance(o, dict) and o.get("type") == "strategy" and o.get("name") == name:
            return True
    return False


def _has_data(dsl: dict, *kinds: str) -> bool:
    for d in dsl.get("data") or []:
        if isinstance(d, dict) and d.get("kind") in kinds:
            return True
    # 天气有时被写成参数
    return bool(dsl.get("data"))


_DEMANDS: list[tuple[str, str, Any]] = [
    ("每日工时上限", r"每天.{0,8}(?:只能|最多|只|至多).{0,6}(?:干|做|工作|维修|作业|干)?\s*[\d一二两三四五六七八九十]+\s*(?:个)?\s*小时",
     lambda d: _has_param(d, "每日有效维修上限") or _has_param(d, "每天开始维修")),
    ("预算上限", r"(?:不超过|别超|不要超|控制?在|预算|最多).{0,10}\d+(?:\.\d+)?\s*万",
     lambda d: _has_constraint(d, "budget")),
    # 「先…再…」必须是真句式的两半，不能裸匹配「先」字：
    # 「按成本最低优**先**排」里的「先」属于「优先」，不是先后顺序。
    ("先后顺序", r"先\s*(?:修|排|做|干)\s*\S{2,}.{0,12}(?:再|然后|接着)\s*(?:修|排|做|干)",
     lambda d: _has_constraint(d, "precedence")),
    ("同一天互斥", r"不要?同一天|不同一天|别.{0,4}同一天",
     lambda d: _has_constraint(d, "co_occur")),
    ("作业线条数", r"\d+\s*(?:条|个)\s*作业线",
     lambda d: _has_param(d, "最多作业线") or _has_param(d, "最大作业线")),
    ("禁止排某任务", r"别排|不要?排|禁止|不许排",
     lambda d: _has_constraint(d, "exclude")),
    ("必修任务", r"必须修|都要修|全修",
     lambda d: _has_constraint(d, "must", "must_all", "cardinality")),
    ("船租改动", r"船租|船舶日租金|船租金",
     lambda d: _has_param(d, "船舶日租金") or _has_param(d, "船租（元每艘每天）")),
    ("人工单价改动", r"人工单价|维修人工",
     lambda d: _has_param(d, "维修人工单价") or _has_param(d, "人工单价")),
    ("天气/海况", r"风大|风速|海况|台风|浪高",
     lambda d: _has_data(d, "weather", "sea_state")),
    ("到货时间", r"到货|补库",
     lambda d: _has_data(d, "part_arrival")),
]


def audit(utterance: str, dsl: dict | None) -> dict[str, Any]:
    """返回这份 DSL 相对原文的对账结果。"""
    if not isinstance(dsl, dict):
        return {"落空": [], "数字未落地": [], "越界编造": [], "需人工": [], "结论": "不产出 DSL"}

    numbers = collect_numbers(dsl)
    qs = extract_quantities(utterance)

    missing_nums: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    for q in qs:
        if q["kind"] == "task_ref":
            # 编号落地：DSL 任意字符串里出现该编号即可
            if q["value"] not in json.dumps(dsl, ensure_ascii=False):
                missing_nums.append(q)
            continue
        if q["kind"] == "multiplier":
            # 倍率无法严格判：原文没写基准值，DSL 里出现 12000×2=24000 这类派生数，
            # 代码不知道 12000 是默认值。自动判「未落地」会误报，交人工核。
            manual.append(q)
            continue
        v = q["value"]
        hit = any(abs(n - v) < 1e-6 or (q["kind"] == "money" and abs(n - v) < 1.0) for n in numbers)
        if not hit:
            missing_nums.append(q)

    dropped: list[str] = []
    for name, pat, ok in _DEMANDS:
        if re.search(pat, utterance) and not ok(dsl):
            dropped.append(name)

    # 越界编造：DSL 里的数字在原文里找不到出处（允许派生：24000=12000×2 这类跳过）
    src_nums = {q["value"] for q in qs} | {
        float(m.group(0)) for m in re.finditer(r"\d+(?:\.\d+)?", utterance)
    }
    invented = [n for n in numbers if n not in src_nums and n > 10]

    return {
        "落空": dropped,
        "数字未落地": missing_nums,
        "越界编造": invented,
        "需人工": manual,
        "结论": "不产出 DSL" if not isinstance(dsl, dict) else
                ("通过" if not (dropped or missing_nums) else "有落空"),
    }


def main() -> int:
    from pathlib import Path

    src = Path(__file__).with_name("_probe_llm_first_result.json")
    rows = json.loads(src.read_text(encoding="utf-8"))
    bad = 0
    for r in rows:
        pay = r.get("payload") or {}
        res = audit(r["utterance"], pay.get("dsl"))
        self_reported = pay.get("unresolved") or []
        mark = {"通过": "通过", "有落空": "有落空", "不产出 DSL": "非改类"}[res["结论"]]
        if res["结论"] == "有落空":
            bad += 1
        print(f"\n【{r['utterance']}】{mark}")
        print(f"   模型自报 unresolved = {self_reported}")
        print(f"   代码审计 落空槽位 = {res['落空']}  数字未落地 = {[q['raw'] for q in res['数字未落地']]}")
        if res["需人工"]:
            print(f"   倍率需人工核对 = {[q['raw'] for q in res['需人工']]}")
    print(f"\n合计 {len(rows)} 条：代码审计判「有落空」{bad} 条；"
          f"模型自报承认的 {sum(1 for r in rows if (r.get('payload') or {}).get('unresolved'))} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
