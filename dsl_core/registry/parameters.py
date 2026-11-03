"""参数注册表：读参数总表.csv（122 项），登记消费点、落点与可改性。

消费点是路由判据的唯一事实来源：改的输入被 candidates()（生成层/时间维度）
还是 build_model()（选择层/成本组合维度）消费。本表是 §6.3 四类重归的机器可读版。

**落点（APPLY_ROUTE）是可改性的唯一事实来源。**
一个参数"能改"，指的是它有一条从 DSL 到模型的实际通路，而不是"设计文档把它
归进了可改那一栏"。历史上这里出过事：GENERATION_LAYER/SELECTION_LAYER 手抄了
设计文档的 51 项"可改"，而执行侧的 load_project.OVERRIDES 白名单只认其中 16 项，
提示词按 51 项对外宣称，用户改剩下 35 项必然报"未支持参数"。三份手抄清单之间
没有任何测试比对，于是"提示词说能改、代码说不支持"活了很久。

现在改为：登记时必须同时登记落点；没有落点的参数 is_changeable() 为假，
提示词不会把它列进"你能改什么"。
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any

_CSV_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "模型参数" / "参数总表.csv"
)

# 生成层参数（改了必须重新生成候选）——来源：设计文档 §6.3 的 ✅生成层 列
GENERATION_LAYER: frozenset[str] = frozenset({
    "候选开工粒度",
    "每天开始维修", "每天结束维修", "每日有效维修上限",
    "风机间转移", "允许夜间转移", "允许跨天暂停",
    "常态风速上限", "常态浪高上限", "安全风速上限", "安全浪高上限", "天气安全缓冲",
    "高严重度截止窗口序号", "中严重度截止窗口序号", "低严重度截止窗口序号",
    "补库延期量",
})

# 选择层参数（改了复用候选，只重建模型）
SELECTION_LAYER: frozenset[str] = frozenset({
    "候选风机", "候选部件",
    "最大作业线", "可调动人员总数",
    "船舶日租金", "维修人工单价", "每任务耗材附加费", "船租是否包含燃油",
    "故障等待损失比例", "检修期间停机比例", "潜在功率整体倍率", "电价整体倍率",
    "正式目标", "自动允许超期", "未知期限任务是否可提前修",
    "发电损失计入总经济成本", "船租计入总经济成本", "人工计入总经济成本", "维修包计入总经济成本",
    "标准目标策略", "共同最低完成任务数",
    "审批预计等待", "真实预测过期阈值",
    "总求解预算", "允许相对差距", "最多对比方案数",
    "预算限制启用", "费用预算上限", "预算覆盖口径",
    "强制维修任务", "禁止新开工风机", "成本敏感性倍率列表",
    "指定任务截止时间", "固定使用的批准窗口", "禁止新增开工日期",
})


# 122 项注册表的"参数名称" → 模型内部参数键（系统参数表键名）。
# 注册表面向业务（船舶日租金），模型内部用另一套键（船租（元每艘每天））。
#
# 这份表必须是**唯一**的改名依据：此前阈值四件套和跨天开关只在
# product_data.PARAM_ALIASES 里登记，编译层原样把"安全风速上限"传下去，
# 到 load_project 才被改写。后果有两个：
#   1. 对账按注册表名去 data.params 里找，找不到（"参数没生效"的假警报）；
#   2. op="*" 的倍率换算按 base.params[注册表名] 取值，直接 KeyError。
# 现在统一在这里改，求解器侧那份别名表退化成兼容旧调用方的兜底。
PARAM_NAME_TO_KEY: dict[str, str] = {
    "船舶日租金": "船租（元每艘每天）",
    "维修人工单价": "人工单价（元每人时）",
    "最大作业线": "最多作业线数",
    "可调动人员总数": "人员总上限",
    "每日有效维修上限": "每天最多维修小时数",
    "风机间转移": "风机间转移小时数",
    "每天开始维修": "每天维修开始时间",
    "每天结束维修": "每天维修结束时间",
    "常态风速上限": "常态最大风速（米每秒）",
    "常态浪高上限": "常态最大有效波高（米）",
    "安全风速上限": "安全最大风速（米每秒）",
    "安全浪高上限": "安全最大有效波高（米）",
    "允许跨天暂停": "跨天暂停",
}


def to_internal_key(name: str) -> str:
    return PARAM_NAME_TO_KEY.get(name, name)


# ── 落点：注册表说"能改"，必须同时说清"改到哪去" ──────────────────────────
#
#   parameters             → 直接进参数覆盖；load_project.OVERRIDES 必须收这个键
#   objectives.strategy    → 落 PlanningRequest.strategy（四选一）
#   constraints.budget     → 落 limits（总经济成本/现金支出上限）
#   constraints.cardinality→ 落 limits["最低完成数"]
#   constraints.must       → 落 limits["必修任务"]
#   unsupported            → 登记在册但未接入，提示词必须明说"当前不支持"
#
# 注意：parameters 这一档的键名是"系统参数表口径"，见 PARAM_NAME_TO_KEY。
APPLY_ROUTE: dict[str, str] = {
    # —— 直接改参数（15 项，与 load_project.OVERRIDES 一一对应）——
    "候选开工粒度": "parameters",
    "每日有效维修上限": "parameters",
    "每天开始维修": "parameters",
    "每天结束维修": "parameters",
    "最大作业线": "parameters",
    "可调动人员总数": "parameters",
    "风机间转移": "parameters",
    "船舶日租金": "parameters",
    "维修人工单价": "parameters",
    "常态风速上限": "parameters",
    "常态浪高上限": "parameters",
    "安全风速上限": "parameters",
    "安全浪高上限": "parameters",
    "允许跨天暂停": "parameters",
    "允许夜间转移": "parameters",
    # —— 换一节写才生效 ——
    "标准目标策略": "objectives.strategy",
    "费用预算上限": "constraints.budget",
    "共同最低完成任务数": "constraints.cardinality",
    "强制维修任务": "constraints.must",
    # —— 未接入（35 项里的其余）：不列进"你能改什么" ——
    "候选风机": "unsupported",
    "候选部件": "unsupported",
    "天气安全缓冲": "unsupported",
    "高严重度截止窗口序号": "unsupported",
    "中严重度截止窗口序号": "unsupported",
    "低严重度截止窗口序号": "unsupported",
    "每任务耗材附加费": "unsupported",
    "船租是否包含燃油": "unsupported",
    "故障等待损失比例": "unsupported",
    "检修期间停机比例": "unsupported",
    "潜在功率整体倍率": "unsupported",
    "电价整体倍率": "unsupported",
    "补库延期量": "unsupported",
    "正式目标": "unsupported",
    "自动允许超期": "unsupported",
    "未知期限任务是否可提前修": "unsupported",
    "发电损失计入总经济成本": "unsupported",
    "船租计入总经济成本": "unsupported",
    "人工计入总经济成本": "unsupported",
    "维修包计入总经济成本": "unsupported",
    "审批预计等待": "unsupported",
    "总求解预算": "unsupported",
    "允许相对差距": "unsupported",
    "最多对比方案数": "unsupported",
    "真实预测过期阈值": "unsupported",
    "预算限制启用": "unsupported",
    "预算覆盖口径": "unsupported",
    "禁止新开工风机": "unsupported",
    "指定任务截止时间": "unsupported",
    "固定使用的批准窗口": "unsupported",
    "禁止新增开工日期": "unsupported",
    "成本敏感性倍率列表": "unsupported",
}

# 不支持时的替代说法。只登记"确实有别的出口"的，其余留空——不编造出口。
UNSUPPORTED_HINT: dict[str, str] = {
    "指定任务截止时间": "改用 constraints.deadline 逐任务指定",
    "禁止新增开工日期": "改用 constraints.forbidden_dates",
    "禁止新开工风机": "改用 constraints.exclude 逐任务排除",
    "候选风机": "改用 constraints.exclude 排除其它风机的任务",
    "候选部件": "改用 constraints.exclude 排除其它部件的任务",
    "总求解预算": "改用 solver.seconds（30~1800 秒）",
    "电价整体倍率": "电价只能按小时改：data 段 kind=hourly",
    "补库延期量": "到货日期只能逐件改：data 段 kind=part_arrival",
}

ROUTE_LABELS: dict[str, str] = {
    "objectives.strategy": "不是 parameters 项，写进 objectives：{type:strategy,name:四选一}",
    "constraints.budget": "不是 parameters 项，写进 constraints：{type:budget,quantity:现金支出}（口径默认现金支出，含发电损失时写总经济成本）",
    "constraints.cardinality": "不是 parameters 项，写进 constraints：{type:cardinality,op:'>=',value:N}",
    "constraints.must": "不是 parameters 项，写进 constraints：{type:must,task:任务编号}",
}


def route_of(name: str) -> str:
    """参数落点；未登记者按 unsupported 处理（不猜）。"""
    return APPLY_ROUTE.get(name, "unsupported")


@lru_cache(maxsize=1)
def _rows() -> tuple[dict[str, Any], ...]:
    with _CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        return tuple(csv.DictReader(f))


def count() -> int:
    return len(_rows())


def lookup(name: str) -> dict[str, Any] | None:
    for row in _rows():
        if row["参数名称"] == name:
            return dict(row)
    return None


def consumption_point(name: str) -> str:
    """消费点查表：candidates / build_model / objectives / constraints / none。

    这是"改了要不要重算候选"的唯一入口：含 candidates 消费的参数 → 重新生成候选。
    没有落点的参数消费点是 none——它压根没进模型。
    """
    route = APPLY_ROUTE.get(name)
    if route is None or route == "unsupported":
        return "none"
    if route == "parameters":
        return "candidates" if name in GENERATION_LAYER else "build_model"
    return route


def is_changeable(name: str) -> bool:
    """能否改：唯一判据是"有没有落点"，不是设计文档的分类栏。

    unsupported（含未登记的）返回 False；提示词据此决定列不列。
    """
    route = APPLY_ROUTE.get(name)
    return route is not None and route != "unsupported"


def is_unsupported(name: str) -> bool:
    """登记在册但当前改不动——提示词要能明确告诉模型"别试"。"""
    return name in APPLY_ROUTE and APPLY_ROUTE[name] == "unsupported"


def unsupported_reason(name: str) -> str:
    return UNSUPPORTED_HINT.get(name, "已登记但尚未接入任何可写位置")

