"""回答里的数字有没有出处。

不猜意图。早期版本在模型前面挂过关键词词表，猜「这句话像不像在问数据」：
会漏（换个说法就不命中）、会误伤、会腐烂；那些词表已经整块删除。

防线在这一层：只看模型最后写出来的东西——答案里那些像业务数据的数字，
能不能在本次工具返回的内容里找到出处。找不到的原样标出来，不替模型背书。

为什么是「标注」不是「拦截」：
  1. 拦不住该拦的，反倒先拦了不该拦的——用户自己报的数（「船租压到 8 万」）
     和模型编的数在这条路上长得一样；
  2. 转述时换单位是正常的（工具给 12345 元，人话写 1.23 万元），一刀切会误判；
  3. 状态诚实性要的是「没核实就不能说成已核实」，不是「宁可不说」。
     标出来，用户自己决定信不信。

只做字符串和浮点比较，不调模型、不联网，结果可复算。
"""
from __future__ import annotations

import re
from typing import Any, Sequence

# ---------- 先剥掉「不是数据」的数字 ----------
# 日期、时间、编号、版本号里也有数字，但它们不是从工具里取出来的事实。
# 不剥掉会把「2026-11-02」「任务001」「08:30」当成待核实的业务数据。
_NOISE_PATTERNS = (
    r"\d{4}-\d{1,2}-\d{1,2}",
    r"(?:19|20)\d{2}\s*年",
    r"\d{1,2}\s*月\s*\d{1,2}\s*日",
    r"\d{1,2}\s*[:：]\s*\d{2}",
    r"\d{1,2}\s*点\s*\d{0,2}",
    r"(?:任务|方案|窗口|风机|部件|第)\s*\d+",
    r"ver_[A-Za-z0-9]+",
)

# ---------- 什么算「像业务数据」的数字 ----------
# 三类：带单位的（8 万、15 小时、1.2 米）、带小数的（0.85）、
# 五位以上的整数（25000、1280000）。
# 刻意不放进来的：光秃秃的小整数。序号、条款号、「四项成本」都是这类，
# 放进来的话每条回答都会被标一遍，用户很快就学会无视这个提示了。
_UNIT = r"(?:万元|亿元|元|万|小时|分钟|天|米每秒|米|%|％)"
_DATA_NUMBER = re.compile(rf"\d+(?:\.\d+)?\s*{_UNIT}|\d+\.\d+|\d{{5,}}")
_PLAIN_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# 单位换算：元↔万元、%↔小数。转述时换单位不算「编数字」。
_SCALES = (1.0, 10_000.0, 1 / 10_000, 100.0, 1 / 100)
# 浮点与四舍五入的容差：工具给 12.3456，人话写 12.35，不该算没出处。
_TOLERANCE = 0.005


def _strip_noise(text: str) -> str:
    cleaned = text or ""
    for pattern in _NOISE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned)
    return cleaned


def data_numbers(text: str) -> list[str]:
    """答案里所有「像业务数据」的数字，按出现顺序，原样返回（带单位）。"""
    return [match.group(0).strip() for match in _DATA_NUMBER.finditer(_strip_noise(text))]


def _numeric_values(text: str) -> set[float]:
    values: set[float] = set()
    for token in _PLAIN_NUMBER.findall(text or ""):
        try:
            values.add(float(token))
        except ValueError:  # pragma: no cover - 正则保证了可解析
            continue
    return values


def _close(left: float, right: float) -> bool:
    if left == right:
        return True
    return abs(left - right) <= max(abs(left), abs(right)) * _TOLERANCE


def _has_source(value: float, pool: set[float]) -> bool:
    if not pool:
        return False
    for known in pool:
        for scale in _SCALES:
            if _close(value, known * scale):
                return True
    return False


def grounding_report(
    answer: str,
    sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """答案里的数字，有多少追不到出处。

    sources：本轮所有工具返回的原始内容（外加用户原话——用户自己报的数
    不算模型编的）。给不出出处就记进「未溯源」。
    """
    pool: set[float] = set()
    for source in sources or ():
        pool |= _numeric_values(source)

    ungrounded: list[str] = []
    seen: set[str] = set()
    for token in data_numbers(answer):
        found = _PLAIN_NUMBER.search(token)
        if not found:  # pragma: no cover - 带单位的前面一定有数字
            continue
        value = float(found.group(0))
        if not _has_source(value, pool) and token not in seen:
            seen.add(token)
            ungrounded.append(token)

    checked = len(data_numbers(answer))
    return {
        "检查数字": checked,
        "未溯源": ungrounded,
        "有未溯源": bool(ungrounded),
    }


def caveat_line(report: dict[str, Any], limit: int = 5) -> str:
    """给用户看的一行提示。没有未溯源数字时返回空串。"""
    items = [str(item) for item in (report.get("未溯源") or [])]
    if not items:
        return ""
    shown = "、".join(items[:limit])
    tail = f" 等 {len(items)} 处" if len(items) > limit else ""
    return (
        f"注意：{shown}{tail} 这几个数字在本次的工具结果里没有找到出处，"
        "未经系统核实，请以工具返回的表格为准。"
    )
