"""求解预算与容差的唯一口径。

曾经有三份互相对不上的数字：对话里公告「最长求解 300 秒」，参数总表写
「求解时间上限 60 秒」，实际执行时被 MWA_SOLVE_SECONDS 压到 30 秒；参数总表
里那行「允许相对差距 1%」更是一次都没传给求解器，只做了展示。

现在只在这里合成一次，所有出口都取同一个结果：

    调用方显式指定（用户在对话里说了「最多 90 秒」/ DSL solver.seconds）
      ↓ 未指定则用参数总表
    参数总表（求解时间上限（秒）、求解相对差距）
      ↓ 部署上限再压一次（MWA_SOLVE_SECONDS，演示环境用）
    生效值（公告、job、模型产物参数表、参数总表展示、版本对比都读它）

环境变量只做「压」，不做「抬」：它代表这台机器愿意等多久，不能反过来把
参数总表往上改。
"""
from __future__ import annotations

from typing import Any

DEFAULT_SECONDS = 60
DEFAULT_GAP_LIMIT = 0.01
MIN_SECONDS = 5
MAX_SECONDS = 1800
MAX_GAP_LIMIT = 1.0

SECONDS_PARAM = "求解时间上限（秒）"
GAP_PARAM = "求解相对差距"
ENV_SECONDS = "MWA_SOLVE_SECONDS"

SOURCE_REQUEST = "调用方指定"
SOURCE_TABLE = "参数总表"
SOURCE_DEFAULT = "代码默认"
SOURCE_ENV = "环境变量上限"


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _env_seconds() -> int | None:
    import os

    raw = (os.environ.get(ENV_SECONDS) or "").strip()
    if not raw:
        return None
    return _int(raw)


def resolve(
    params: dict[str, Any] | None = None,
    *,
    requested_seconds: int | None = None,
    requested_gap: float | None = None,
) -> dict[str, Any]:
    """合成一次求解真正会用的时限与容差。

    返回：
        seconds     实际生效的秒数（求解器会用这个数）
        gapLimit    成本阶段允许的相对差距（其余阶段一律要求证明最优）
        secondsSource / gapSource   这个数从哪来，用于对用户如实交代
        requested / table / envCap  三个输入的原值，便于排查「为什么不是我要的数」
    """
    params = params or {}
    table_seconds = _int(params.get(SECONDS_PARAM))
    table_gap = _float(params.get(GAP_PARAM))

    if requested_seconds is not None:
        seconds, seconds_source = _int(requested_seconds), SOURCE_REQUEST
    elif table_seconds is not None:
        seconds, seconds_source = table_seconds, SOURCE_TABLE
    else:
        seconds, seconds_source = DEFAULT_SECONDS, SOURCE_DEFAULT

    if requested_gap is not None:
        gap, gap_source = _float(requested_gap), SOURCE_REQUEST
    elif table_gap is not None:
        gap, gap_source = table_gap, SOURCE_TABLE
    else:
        gap, gap_source = DEFAULT_GAP_LIMIT, SOURCE_DEFAULT

    seconds = max(MIN_SECONDS, min(MAX_SECONDS, int(seconds or DEFAULT_SECONDS)))
    gap = max(0.0, min(MAX_GAP_LIMIT, float(gap if gap is not None else DEFAULT_GAP_LIMIT)))

    env_cap = _env_seconds()
    if env_cap is not None:
        env_cap = max(MIN_SECONDS, min(MAX_SECONDS, env_cap))
        if env_cap < seconds:
            seconds = env_cap
            seconds_source = SOURCE_ENV

    return {
        "seconds": seconds,
        "gapLimit": round(gap, 6),
        "secondsSource": seconds_source,
        "gapSource": gap_source,
        "requestedSeconds": requested_seconds,
        "tableSeconds": table_seconds,
        "envCap": env_cap,
        "tableGap": table_gap,
    }


def describe(budget: dict[str, Any]) -> str:
    """一句话交代预算，供对话公告使用。"""
    seconds = budget.get("seconds")
    gap = budget.get("gapLimit")
    source = budget.get("secondsSource")
    text = f"{seconds} 秒（{source}）"
    if isinstance(gap, (int, float)) and gap > 0:
        text += f"，成本阶段容差 {gap:.0%}"
    return text
