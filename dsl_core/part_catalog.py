"""业务零件/严重度的注册名：模型侧内部编号 → 人话别名（共享事实）。

两个产品都要知道「叶片 = 部件01」这类映射，所以放在中立包 dsl_core，
而不是 backend 的规则理解层里。
"""
from __future__ import annotations

# 业务零件的注册名：模型侧内部编号 → 人话别名。
# 大模型只会看到别名，永远看不到“部件04”这类内部编号；查不到就报错，不许猜。
_PART_ALIASES = {
    "部件01": ("部件01", "叶片", "叶片备件", "桨叶"),
    "部件02": ("部件02", "轮毂", "轮毂备件"),
    "部件03": ("部件03", "主轴", "主轴备件"),
    "部件04": ("部件04", "齿轮箱", "齿轮箱备件"),
    "部件05": ("部件05", "发电机", "发电机备件"),
    "部件06": ("部件06", "偏航电机", "偏航"),
    "部件07": ("部件07", "变桨电机", "变桨"),
    "部件08": ("部件08", "变频器"),
}
_PART_ALIAS_TO_ID = {
    alias: part_id for part_id, aliases in _PART_ALIASES.items() for alias in aliases
}
# 长别名优先，避免“叶片”抢先匹配掉“叶片备件”。
_PART_ALIAS_KEYS = sorted(_PART_ALIAS_TO_ID, key=len, reverse=True)

_SEVERITY_ALIASES = (
    (("严重故障", "高风险", "高严重", "高严重度", "高危"), "高"),
    (("中等故障", "中风险", "中严重", "中严重度"), "中"),
    (("轻微故障", "低风险", "低严重", "低严重度"), "低"),
)


def part_catalog_text() -> str:
    """给大模型看的零件名清单（能力目录的一部分）。"""
    return "、".join(aliases[1] if len(aliases) > 1 else aliases[0] for aliases in _PART_ALIASES.values())
