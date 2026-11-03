"""小一修槽位注册表：唯一事实来源。

纪律（改这个文件的三条硬规矩）：
1. **没有落点的槽位不许注册**。注册即承诺：校验器认、注入口编译得了、对账函数查得出来。
2. **注册项不全不许上线**。人话名、索引种类、默认值、落点、消费点、对账函数名——缺一项就在
   ``audit_registry()`` 里报出来。这一条沿用大一修 parameters.py 的教训。
3. **对账函数必须登记**。槽位族固定，所以「新加了族却忘了补对账」在结构上不可能发生——
   前提是对账函数是注册表的必填项，而不是另写一张表。

索引种类的两种性质，决定提示词与校验代码怎么写：
  * 离散：能从固定清单里挑，写错当场拒，还能在提示词里附上快照真实数字当护栏
  * 开放：要模型自己填内容（任务清单、日期区间），只能事后查，需单独解析代码
仅两类是开放的：任务清单（任意组合）、日期区间（任意起止）。形状不受影响——
不管点哪三个任务，写出来的还是同一句「加起来最多一个」。
"""
from __future__ import annotations

import json
from pathlib import Path

from dataclasses import dataclass, field
from typing import Any, Callable

# ── 索引种类 ────────────────────────────────────────────────────────────
# 每个种类登记：提示词里怎么描述、它的取值是离散枚举还是开放填写。
INDEX_KINDS: dict[str, dict[str, str]] = {
    "group": {"说明": "按什么类别圈定一批任务", "性质": "混合"},
    "group_or_bucket": {"说明": "类别或时段皆可（只限 scope_only 圈范围用）", "性质": "混合"},
    "bucket": {"说明": "哪一段时间（按开工时间计）", "性质": "混合"},
    "task": {"说明": "点名一个任务编号", "性质": "离散"},
    "task_seq": {"说明": "有序任务序列，长度≥2", "性质": "开放"},
    "task_set": {"说明": "任务集合，长度≥2，无序", "性质": "开放"},
    "severity": {"说明": "严重度层（高/中/低）", "性质": "离散"},
    "sea": {"说明": "海况（常态/恶劣但安全）", "性质": "离散"},
    "severity_pair": {"说明": "两个严重度层，如 高→中", "性质": "离散"},
    "metric": {"说明": "14 个已登记目标量之一", "性质": "离散"},
    "budget_口径": {"说明": "花钱的口径", "性质": "离散"},
    "cap_维度": {"说明": "被占用实例的类别", "性质": "离散"},
    "day": {"说明": "规划期内的一天", "性质": "离散"},
    "part": {"说明": "部件编号或名称", "性质": "离散"},
    "moment": {"说明": "规划期内的一个时刻", "性质": "开放"},
    "window": {"说明": "窗口编号", "性质": "离散"},
    "strategy": {"说明": "四种目标策略之一", "性质": "离散"},
    "项": {"说明": "改的是哪一个量（风速/浪高/电价）", "性质": "离散"},
    "端": {"说明": "窗口的开始还是结束", "性质": "离散"},
    "粒度": {"说明": "按天还是按窗口判定", "性质": "离散"},
}

# 可选索引的取值域：给了就校验，不给就按该维全改/默认处理。
OPTIONAL_ENUMS: dict[str, tuple[str, ...]] = {
    "项": ("风速", "浪高", "电价"),
    "端": ("开始", "结束"),
    "粒度": ("天", "窗口"),
    "严重度": ("高", "中", "低"),
    "海况": ("常态", "恶劣但安全"),
}

# 组（group）能取的类别。全部离散，除「任务清单」开放。
GROUP_KINDS = ("全部", "严重度", "部件", "风机", "任务", "任务清单", "作业线")
# 桶（bucket）能取的类别。「日期区间」开放。
BUCKET_KINDS = ("窗口", "某日", "日期区间")
# 预算口径：前两个内核原生（走 limits），后三个走注入口。
BUDGET_KINDS = ("现金", "总成本", "人工", "备件", "租船天")
# 容量维度：某桶内被占用的实例数。
CAP_DIMENSIONS = ("作业线", "风机", "部件")

# 目标量：内核 metrics 原有 11 个 + 人工/备件/船舶艘天（内核 metrics 加三行）。
METRIC_NAMES = (
    "总经济成本", "现金支出", "完成任务数", "最后完工时间", "完工时间和",
    "高严重完成数", "中严重完成数", "低严重完成数",
    "高严重完工时间和", "中严重完工时间和", "低严重完工时间和",
    "人工费", "备件费", "船舶艘天",
)

_STRATEGY_FALLBACK: tuple[str, ...] = ()


def _load_strategy_names() -> tuple[str, ...]:
    """策略名从内核的目标模板文件反向读，不在小一修这边另编一份。

    自己编名字的代价是灾难性的：内核按模板编号查表，查不到就 StopIteration 崩在求解里，
    而且崩点离用户的那句「越早干完越好」隔着三层。名字只有内核这一份事实来源。
    """
    path = (Path(__file__).resolve().parents[2] / "maintenance-milp-modeler"
            / "references" / "四种目标模板.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        names = tuple(t["模板编号"] for t in payload["模板"])
    except (OSError, ValueError, KeyError, TypeError):
        return ()
    return names


STRATEGY_NAMES = _load_strategy_names() or _STRATEGY_FALLBACK


@dataclass(frozen=True)
class Slot:
    """一个可写槽位族的完整登记。"""

    name: str
    group: str
    label: str                      # 人话名
    route: str                      # params|strategy|weights|limits|closure|data|timeline|partial
    index: tuple[tuple[str, str], ...] = ()   # ((索引名, 种类), ...)
    default: Any = None             # 不生效时的值：None = 不加约束
    default_note: str = ""          # 默认值的人话说明
    unit: str = ""
    value_type: str = "int"         # int|float|bool|enum|str|minutes
    bounds: tuple[float, float] | None = None
    consumption: str = "model"      # model（只重建模型）| candidates（要重生成候选）
    strength: str = "hard"          # hard|soft|both
    reconcile: str = ""             # 对账函数名，注册即承诺，不许空
    examples: tuple[str, ...] = ()
    note: str = ""
    optional: tuple[str, ...] = ()  # 可省略的索引维度（省略 = 该维全改/取默认）


# ── A 组：标量参数（走 overrides） ──────────────────────────────────────
# 槽位名 → 系统参数表口径。白名单来自 product_data.OVERRIDES，改动前先看那里。
PARAM_ALIASES: dict[str, str] = {
    "船租": "船租（元每艘每天）",
    "人工单价": "人工单价（元每人时）",
    "最多作业线数": "最多作业线数",
    "人员总上限": "人员总上限",
    "每天最多维修小时数": "每天最多维修小时数",
    "风机间转移小时数": "风机间转移小时数",
    "每天开工时间": "每天维修开始时间",
    "每天收工时间": "每天维修结束时间",
    "候选开工粒度": "候选开工粒度",
    "跨天暂停": "跨天暂停",
    "允许夜间转移": "允许夜间转移",
    "常态风速上限": "常态最大风速（米每秒）",
    "常态浪高上限": "常态最大有效波高（米）",
    "安全风速上限": "安全最大风速（米每秒）",
    "安全浪高上限": "安全最大有效波高（米）",
}

# 标量槽位单独一张表：它们没有索引维度，形状是「改一个系数」。
# 参数槽位。「说明」不是装饰——提示词靠它告诉模型这个开关到底管什么：
# 光给「跨天暂停｜是/否」，模型会把「一个活儿不许跨天干」翻成「是」（它以为暂停=允许跨天），
# 语义正好反了。参数没有形态可推，说明就是唯一的事实来源。
PARAM_SLOTS: dict[str, dict[str, Any]] = {
    "船租": {"单位": "元/艘/天", "类型": "float", "界": (0, 10_000_000), "消费点": "model",
             "说明": "一条船一天的租金"},
    "人工单价": {"单位": "元/人时", "类型": "float", "界": (0, 100_000), "消费点": "model",
                 "说明": "一个工人干一小时的钱"},
    "最多作业线数": {"单位": "条", "类型": "int", "界": (1, 100), "消费点": "model",
                     "说明": "整轮最多同时派几条船（不带日期的「最多开几条船」落这里）"},
    "人员总上限": {"单位": "人", "类型": "int", "界": (1, 100), "消费点": "model",
                   "说明": "总共能派多少人"},
    "每天最多维修小时数": {"单位": "小时", "类型": "float", "界": (0, 24), "消费点": "candidates",
                           "说明": "一条船一天最多干几小时"},
    "风机间转移小时数": {"单位": "小时", "类型": "float", "界": (0, 24), "消费点": "candidates",
                         "说明": "从一台风机挪到下一台要多久"},
    "每天开工时间": {"单位": "HH:MM", "类型": "str", "界": None, "消费点": "candidates",
                     "说明": "每天几点开始干活"},
    "每天收工时间": {"单位": "HH:MM", "类型": "str", "界": None, "消费点": "candidates",
                     "说明": "每天几点收工"},
    "候选开工粒度": {"单位": "分钟", "类型": "int", "界": (1, 1440), "消费点": "candidates",
                     "说明": "开工时间按几分钟一档去试；调小更精细但更慢"},
    "跨天暂停": {"单位": "是/否", "类型": "bool", "界": None, "消费点": "candidates",
                 "说明": "是否**允许**一个活儿干不完先停、第二天接着干。"
                         "「一个活儿不许跨天干」= 否"},
    "允许夜间转移": {"单位": "是/否", "类型": "bool", "界": None, "消费点": "candidates",
                     "说明": "是否**允许**晚上在风机之间挪船。"
                             "「晚上不许转场」= 否"},
    "常态风速上限": {"单位": "米/秒", "类型": "float", "界": (0, 100), "消费点": "candidates",
                     "说明": "风速超过这个值就不能正常作业"},
    "常态浪高上限": {"单位": "米", "类型": "float", "界": (0, 100), "消费点": "candidates",
                     "说明": "浪高超过这个值就不能正常作业"},
    "安全风速上限": {"单位": "米/秒", "类型": "float", "界": (0, 100), "消费点": "candidates",
                     "说明": "风速超过这个值连安全作业都停（比常态更严）"},
    "安全浪高上限": {"单位": "米", "类型": "float", "界": (0, 100), "消费点": "candidates",
                     "说明": "浪高超过这个值连安全作业都停（比常态更严）"},
}


# ── B~J 组：约束族 ─────────────────────────────────────────────────────
SLOTS: dict[str, Slot] = {
    # B 组 · 目标
    "strategy": Slot(
        name="strategy", group="B", label="排程策略", route="strategy",
        default="成本最低优先", default_note="成本最低优先",
        value_type="enum", consumption="model", strength="both",
        reconcile="reconcile_strategy",
        examples=("按成本最低排", "整体按高风险抢修的思路排", "越早干完越好", "尽量多修几个"),
        note="只在用户要**整体切换排程风格**时用它（四选一）。"
             "「高危优先」「先保高严重」这类只点了一个偏好量的，落 weight（软），不要动 strategy",
    ),
    "weight": Slot(
        name="weight", group="B", label="目标权重", route="weights",
        index=(("量", "metric"),), default=0, default_note="0，即该量不参与权衡",
        value_type="float", bounds=(0, 1e6), consumption="model", strength="soft",
        reconcile="reconcile_weight",
        examples=("高危优先", "省钱比赶时间重要", "尽量压租船天数"),
        note="软偏好通道。用户说「必须/不许」时不要落这里（见 README 第六节硬软消歧）",
    ),
    # C 组 · 修多少
    "count_min": Slot(
        name="count_min", group="C", label="至少修几个", route="closure",
        index=(("组", "group"),), default=None, default_note="0，恒成立即不加约束",
        unit="个", value_type="int", bounds=(0, 999), consumption="model", strength="hard",
        reconcile="reconcile_count",
        examples=("至少修 10 个", "高危全都要修", "齿轮箱至少修 2 个",
                  "风机001 必须全修完", "任务018 一定得排进去", "这三个里至少挑一个"),
        note="桶恒为全程；要限定某段时间请落 bucket_min（见 README 边界）",
    ),
    "count_max": Slot(
        name="count_max", group="C", label="最多修几个", route="closure",
        index=(("组", "group"),), default=None, default_note="任务总数，等于不限制",
        unit="个", value_type="int", bounds=(0, 999), consumption="model", strength="hard",
        reconcile="reconcile_count",
        examples=("最多修 8 个", "风机003 最多排 2 个", "任务022 这轮先不修",
                  "这三个里最多挑一个"),
        note="拧到 0 = 整类不修；会触发部分排程模式（判据只有一份，见 apply.needs_partial）",
    ),
    # D 组 · 何时修
    "bucket_min": Slot(
        name="bucket_min", group="D", label="某时段内至少开工几个", route="closure",
        index=(("桶", "bucket"), ("组", "group")), default=None, default_note="0，不生效",
        unit="个", value_type="int", bounds=(0, 999), consumption="model", strength="hard",
        reconcile="reconcile_bucket",
        examples=("11月第二周必须修叶片", "窗口03 里高危至少修 3 个",
                  "这周风机003 的活全干完", "8号到10号必须开工两个齿轮箱"),
        note="按候选的开工时间落桶，口径写死在 README",
    ),
    "bucket_max": Slot(
        name="bucket_max", group="D", label="某时段内最多开工几个", route="closure",
        index=(("桶", "bucket"), ("组", "group")), default=None, default_note="不设上限",
        unit="个", value_type="int", bounds=(0, 999), consumption="model", strength="hard",
        reconcile="reconcile_bucket",
        examples=("窗口01 不修齿轮箱", "11月5号不安排新开工", "2号船周三别排",
                  "这周叶片最多修 1 个"),
    ),
    # E 组 · 时间界
    "earliest": Slot(
        name="earliest", group="E", label="最早何时开工", route="closure",
        index=(("组", "group"),), default=None, default_note="0，不生效",
        unit="时刻", value_type="minutes", consumption="model", strength="hard",
        reconcile="reconcile_earliest",
        examples=("任务018 最早 11月5号开工", "高危的也要等下周才能动"),
    ),
    "deadline_delta": Slot(
        name="deadline_delta", group="E", label="维修期限", route="data",
        index=(("组", "group"),), default=None, default_note="任务自带期限",
        unit="时刻", value_type="minutes", consumption="candidates", strength="hard",
        reconcile="reconcile_deadline",
        examples=("高危一周内干完", "齿轮箱提前到 20 号前完成", "这批活 18 号前必须收尾"),
        note="只走数据补丁，不走注入口：verify_schedule 按 task['截止分钟'] 复算，"
             "走注入口会出现两边口径不一致（R2）。"
             "且**只能提前不能放宽**——内核的期限补丁只做收紧，放宽会明确拒绝",
    ),
    # F 组 · 用哪条船
    "line_fixed": Slot(
        name="line_fixed", group="F", label="固定作业线", route="closure",
        index=(("任务", "task"),), default=None, default_note="自由选择作业线",
        value_type="int", bounds=(1, 100), consumption="model", strength="hard",
        reconcile="reconcile_line_fixed",
        examples=("任务018 只上 2 号船",),
    ),
    "cap_max": Slot(
        name="cap_max", group="F", label="同时占用的实例数上限", route="closure",
        index=(("维度", "cap_维度"), ("桶", "bucket")), default=None, default_note="跟随作业线数",
        unit="个", value_type="int", bounds=(0, 999), consumption="model", strength="hard",
        reconcile="reconcile_cap",
        examples=("11月8号最多开两条船", "这周每天最多动 3 台风机"),
        note="默认不是无穷大，是跟随 A 组作业线数——否则会把内核的 NoOverlap 语义改掉。"
             "桶必须是一个明确时段（某日/窗口）；**不带时段的「最多开 N 条船」是参数「最多作业线数」，不落这里**。"
             "「维度」只能填 作业线/风机/部件 这三个名字（不是编号）；"
             "点名了具体哪条船/哪台风机的是 bucket_max，不是这里",
    ),
    # G 组 · 花多少
    "budget_max": Slot(
        name="budget_max", group="G", label="预算上限", route="limits_or_closure",
        index=(("口径", "budget_口径"), ("组", "group")), default=None, default_note="不设上限",
        unit="元或艘天", value_type="float", bounds=(0, 1e12), consumption="model",
        strength="hard", reconcile="reconcile_budget", optional=("组",),
        examples=("现金不超 80 万", "总成本压到 200 万内", "人工费单列不超 50 万",
                  "高危备件费不超 30 万", "租船最多 5 天"),
        note="现金/总成本走内核 limits；人工/备件/租船天走注入口",
    ),
    # H 组 · 先后同异（N 元）
    "before_seq": Slot(
        name="before_seq", group="H", label="先后顺序与间隔", route="closure",
        index=(("序列", "task_seq"),), default=None, default_note="不设顺序",
        unit="小时", value_type="float", bounds=(0, 240), consumption="model", strength="hard",
        reconcile="reconcile_before_seq",
        examples=("先修 018 再修 022", "按 018→022→025 的顺序做，中间隔 2 小时"),
        note="N 元是零新形态：链式展开成 N-1 条二元约束。"
             "value 的**单位是小时**，没说间隔就填 0（别填 1，那是 1 小时）",
    ),
    "same_bucket": Slot(
        name="same_bucket", group="H", label="安排在同一天/同一窗口", route="closure",
        index=(("集合", "task_set"), ("粒度", "粒度")), default=None, default_note="不设共现",
        value_type="act", consumption="model", strength="hard",
        reconcile="reconcile_same_bucket", optional=("粒度",),
        examples=("018 和 022 同一天", "这三个放同一个窗口"),
        note="N 元同样是链式相等，零新形态",
    ),
    "all_diff": Slot(
        name="all_diff", group="H", label="分在不同天", route="closure",
        index=(("集合", "task_set"), ("粒度", "粒度")), default=None, default_note="不设互异",
        value_type="act", consumption="model", strength="hard",
        reconcile="reconcile_all_diff", optional=("粒度",),
        examples=("018 和 022 不许同一天", "这三台风机分在不同天"),
        note="新形态（CP-SAT AllDifferent），已过加族评审；二元时吃掉原来的 diff_bucket",
    ),
    "sev_before": Slot(
        name="sev_before", group="H", label="严重度层间硬顺序", route="closure",
        index=(("层对", "severity_pair"),), default=None, default_note="关",
        value_type="act", consumption="model", strength="hard",
        reconcile="reconcile_sev_before",
        examples=("高危没干完不许开中危",),
    ),
    "sev_gap": Slot(
        name="sev_gap", group="H", label="严重度层间最小间隔", route="closure",
        index=(("层对", "severity_pair"),), default=None, default_note="不设间隔",
        unit="小时", value_type="float", bounds=(0, 240), consumption="model", strength="hard",
        reconcile="reconcile_sev_gap",
        examples=("高危干完隔半天再开中危（12）", "高危干完隔一天再开中危（24）"),
        note="value 的**单位是小时**，半天=12、一天=24。别写成分钟（720 会被当成 720 小时）",
    ),
    # I 组 · 只修哪些
    "scope_only": Slot(
        name="scope_only", group="I", label="只修这些", route="closure",
        index=(("范围", "group_or_bucket"),), default=None, default_note="全部",
        value_type="act", consumption="model", strength="hard",
        reconcile="reconcile_scope_only",
        examples=("这轮只修叶片", "只在窗口03 里干", "只修风机001", "只修这五个任务"),
        note="编译成「补集压零」。与 bucket_max 互补：话里有「只」用这个，有「不/别」用 bucket_max",
    ),
    # J 组 · 改数据
    "part_hours": Slot(
        name="part_hours", group="J", label="部件维修工时", route="data",
        index=(("部件", "part"), ("严重度", "severity"), ("海况", "sea")),
        default=None, default_note="部件库原值", unit="小时", value_type="float",
        bounds=(0.01, 1000), consumption="candidates", strength="hard",
        reconcile="reconcile_part_hours", optional=("严重度", "海况"),
        examples=("齿轮箱高危按 20 小时算", "恶劣海况工时翻倍"),
    ),
    "part_arrival": Slot(
        name="part_arrival", group="J", label="备件到货时间", route="data",
        index=(("部件", "part"),), default=None, default_note="库存流水原值",
        unit="时刻", value_type="minutes", consumption="candidates", strength="hard",
        reconcile="reconcile_part_arrival",
        examples=("叶片备件 12 号才能到",),
    ),
    "weather": Slot(
        name="weather", group="J", label="某日海况假设", route="timeline",
        index=(("日期", "day"), ("项", "项")), default=None, default_note="快照原值",
        value_type="float", consumption="candidates", strength="hard",
        reconcile="reconcile_weather", optional=("项",),
        examples=("11月8号风速按 15 米算", "那天浪高 2.5 米"),
    ),
    "hourly": Slot(
        name="hourly", group="J", label="某时刻电价/风速假设", route="timeline",
        index=(("时刻", "moment"), ("项", "项")), default=None, default_note="快照原值",
        value_type="float", consumption="candidates", strength="hard",
        reconcile="reconcile_hourly", optional=("项",),
        examples=("明天上午电价按 1.2 元算",),
    ),
    "window": Slot(
        name="window", group="J", label="窗口起止假设", route="timeline",
        index=(("窗口", "window"), ("端", "端")), default=None, default_note="快照原值",
        value_type="minutes", consumption="candidates", strength="hard",
        reconcile="reconcile_window", optional=("端",),
        examples=("窗口03 推迟两天开始",),
    ),
    # A 组里唯一不是系数的：部分排程开关
    "allow_partial": Slot(
        name="allow_partial", group="A", label="允许少修几个", route="partial",
        default=False, default_note="否（完整排程）", value_type="bool",
        consumption="model", strength="hard", reconcile="reconcile_partial",
        examples=("排不下就少修几个吧",),
        note="也可由 count_max/scope_only 隐式触发，判据只有一份（apply.needs_partial）",
    ),
}


def audit_registry() -> list[str]:
    """注册表自检：登记项不全的槽位直接报出来，不许带病上线。

    对账函数为空是最严重的一类——它会让「加了族却没对账」这个大一修的窟窿重新出现。
    """
    errors: list[str] = []
    if not STRATEGY_NAMES:
        errors.append("策略清单没从内核「四种目标模板.json」读到——小一修不许自己编策略名")
    elif "成本最低优先" not in STRATEGY_NAMES:
        errors.append(f"内核策略模板里没有「成本最低优先」，读到的是：{'、'.join(STRATEGY_NAMES)}")
    for name, slot in SLOTS.items():
        if not slot.label:
            errors.append(f"{name}: 缺人话名")
        if not slot.route:
            errors.append(f"{name}: 缺落点")
        if not slot.reconcile:
            errors.append(f"{name}: 缺对账函数（注册即承诺，不许空）")
        if not slot.default_note:
            errors.append(f"{name}: 缺默认值说明（默认必须等于「不生效」）")
        if not slot.examples:
            errors.append(f"{name}: 缺业务话术例子（提示词与回归集都靠它）")
        for index_name, kind in slot.index:
            if kind not in INDEX_KINDS:
                errors.append(f"{name}: 索引 {index_name} 的种类 {kind} 未登记")
    for alias, system_name in PARAM_ALIASES.items():
        if alias not in PARAM_SLOTS:
            errors.append(f"参数 {alias}→{system_name} 没有单位/界/消费点登记")
        elif PARAM_SLOTS[alias].get("消费点") not in ("model", "candidates"):
            errors.append(f"参数 {alias} 的消费点未登记")
    return errors


def slots_by_route(route: str) -> list[Slot]:
    return [s for s in SLOTS.values() if s.route == route or
            (route == "limits" and s.route == "limits_or_closure") or
            (route == "closure" and s.route == "limits_or_closure")]


def slot_names() -> list[str]:
    return sorted(SLOTS) + sorted(PARAM_SLOTS)


# ── 索引解析 ────────────────────────────────────────────────────────────
# 组与桶最终都是「作用在候选行上的谓词」——这是全部族共用的唯一抽象。
# 候选行字段（product_planner.candidates + build_model 补的作业线）：
#   任务编号、风机编号、部件编号、部件名称、严重等级、窗口编号、开始、完成、作业线

def _resolve_entities():
    """延迟导入 dsl_core 的实体解析器（中立项，不引入大一修的编译器）。"""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from dsl_core.registry import entities  # noqa: PLC0415
    return entities


def group_predicate(data, spec: dict[str, Any]) -> tuple[Callable[[dict], bool] | None, str | None]:
    """把「组」索引解析成候选行谓词。返回 (谓词, 错误)；错误时谓词为 None。

    精确匹配，不做模糊匹配（沿用 part_catalog 的纪律）：「叶片」不许顺手匹配掉「叶片备件」。
    """
    entities = _resolve_entities()
    kind = spec.get("kind")
    value = spec.get("value")
    if kind == "全部" or kind is None:
        return (lambda c: True), None
    if kind == "严重度":
        if value not in ("高", "中", "低"):
            return None, f"严重度只能是 高/中/低，收到 {value!r}"
        return (lambda c, v=value: c["严重等级"] == v), None
    if kind == "部件":
        pid = entities.resolve_part(data, str(value))
        if pid is None:
            return None, f"部件 {value!r} 不在部件库里"
        return (lambda c, p=pid: c["部件编号"] == p), None
    if kind == "风机":
        tid = entities.resolve_turbine(data, str(value))
        if tid is None:
            return None, f"风机 {value!r} 不在告警快照里"
        return (lambda c, t=tid: c["风机编号"] == t), None
    if kind == "任务":
        tid = entities.resolve_task(data, str(value))
        if tid is None:
            return None, f"任务 {value!r} 不在告警快照里"
        return (lambda c, t=tid: c["任务编号"] == t), None
    if kind == "任务清单":  # 开放：任意组合，只能事后查
        if not isinstance(value, (list, tuple)) or len(value) < 1:
            return None, f"任务清单至少要给一个任务编号，收到 {value!r}"
        ids = []
        for item in value:
            tid = entities.resolve_task(data, str(item))
            if tid is None:
                return None, f"任务 {item!r} 不在告警快照里"
            ids.append(tid)
        return (lambda c: c["任务编号"] in ids), None
    if kind == "作业线":
        try:
            line = int(value)
        except (TypeError, ValueError):
            return None, f"作业线必须是数字，收到 {value!r}"
        if not 1 <= line <= min(data.params["最多作业线数"], data.params["人员总上限"]):
            return None, f"作业线 {line} 超出当前可用线数"
        # 候选行分两种：内核 build_model 展开后的（带「作业线」）和原始候选（不带——
        # 线是求解时才定的）。不带线意味着「可能上任何一条船」，静态计数按可能为真算。
        return (lambda c, ln=line: c.get("作业线") is None or c["作业线"] == ln), None
    return None, f"未知的组类别 {kind!r}，可填：{'、'.join(GROUP_KINDS)}"


def describe_group(data, spec: dict[str, Any] | None) -> str:
    """把「组」索引翻译成人话，给对账和回显用。

    对账只说「排了 2 个、要求至少 1」是不够的——用户看不出数的是哪个格子，
    证据链不完整。这里把格子点名：部件→部件名称、作业线→几号船。
    """
    if not isinstance(spec, dict):
        # 解析层已经挡掉了裸字符串；这里再兜一层：回显不该因为拿不到结构就崩
        return str(spec)
    spec = spec or {}
    kind = spec.get("kind")
    value = spec.get("value")
    if kind in (None, "全部"):
        return "全部任务"
    if kind == "严重度":
        return f"{value}严重度的任务"
    if kind == "部件":
        if data is None:
            # 没有快照就把原始写法说出来（可能就是「齿轮箱」），别去查部件库——
            # describe_patch 承诺过「拿不到快照也要能回显」
            return f"{value}的任务"
        pid = _resolve_entities().resolve_part(data, str(value))
        name = data.parts.get(pid or "", {}).get("部件名称") or str(value)
        return f"{name}的任务"
    if kind == "风机":
        return f"{value}的活" if str(value).startswith("风机") else f"风机{value}的活"
    if kind == "任务":
        return str(value)
    if kind == "任务清单":
        count = len(value) if isinstance(value, (list, tuple)) else 0
        return f"点名的 {count} 个任务"
    if kind == "作业线":
        return f"{value} 号船"
    return str(value) if value is not None else "全部任务"


_INDEX_WORDS = {
    "口径": "口径", "维度": "维度", "量": "量", "项": "项", "端": "端",
    "粒度": "粒度", "严重度": "严重度", "海况": "海况", "部件": "部件",
    "日期": "日期", "时刻": "时刻", "窗口": "窗口", "任务": "任务", "数量": "数量",
}


def _bucket_label(data, spec: dict[str, Any]) -> str:
    kind = spec.get("kind")
    value = spec.get("value")
    if kind in (None, "全程"):
        return "全程"
    if kind == "窗口":
        return str(value)
    if kind == "某日":
        return f"{value} 那天"
    if kind == "日期区间":
        return f"{value[0]} 到 {value[1]}" if isinstance(value, (list, tuple)) and len(value) == 2 else str(value)
    return str(value)


def _human_minutes(minutes: float) -> str:
    """分钟数换成人话：整天的说天，整小时的说小时，其余说分钟。"""
    total = abs(int(minutes))
    if total == 0:
        return "0 分钟"
    if total % 1440 == 0:
        return f"{total // 1440} 天"
    if total % 60 == 0:
        return f"{total // 60} 小时"
    return f"{total} 分钟"


def describe_patch(patch, data=None, *, show_value: bool = True) -> str:
    """把一条补丁说成人话：**哪一格、拧到几**。

    专门给「当前生效的旋钮」清单用。要求有两件事：格子必须点名（不能只说
    「一条上限约束」），值必须带口径（不能只说「2」）——否则用户看不出
    「我刚才那句话，现在到底按什么在算」。

    ``data`` 缺省时按索引里的原始写法说（不查部件名），保证这个函数
    在任何上下文都调得动——回显不该因为拿不到快照就崩掉。
    """
    slot = SLOTS.get(patch.slot)
    if slot is None:
        if patch.slot in PARAM_SLOTS:
            spec = PARAM_SLOTS[patch.slot]
            return f"参数 {patch.slot} = {patch.value} {spec['单位']}".strip()
        return f"{patch.slot} = {patch.value}"

    # 目标类：策略与权重没有「值」的强弱，说清点了哪个量就够
    if patch.slot == "strategy":
        return f"整体排程风格按「{patch.value}」"
    if patch.slot == "weight":
        return f"「{patch.index.get('量', {}).get('value')}」参与权衡（权重 {patch.value}）"

    cells: list[str] = []
    index = patch.index or {}
    if "组" in index:
        cells.append(describe_group(data, index["组"]))
    if "范围" in index:
        spec = index["范围"] or {}
        cells.append(describe_group(data, spec) if (spec.get("kind") or "全部") in GROUP_KINDS
                     else _bucket_label(data, spec))
    if "桶" in index:
        cells.append(_bucket_label(data, index["桶"] or {}))
    for key in ("序列", "集合"):
        if key in index:
            ids = (index[key] or {}).get("value") or []
            joiner = " → " if key == "序列" else "、"
            cells.append("任务" + joiner.join(str(i) for i in ids))
    if "层对" in index:
        pair = (index["层对"] or {}).get("value") or []
        cells.append(" → ".join(str(p) for p in pair) + " 之间")
    for key, word in _INDEX_WORDS.items():
        if key in index and key not in ("部件",):
            value = (index[key] or {}).get("value")
            if value is not None:
                cells.append(f"{word}={value}")
    if "部件" in index:
        raw = (index["部件"] or {}).get("value")
        pid = _resolve_entities().resolve_part(data, str(raw)) if data is not None else None
        name = (data.parts.get(pid or "", {}).get("部件名称") if data is not None else None) or raw
        cells.append(f"部件={name}")

    head = "｜".join(cells) if cells else "全程"
    if slot.value_type == "act" or not show_value:
        return f"{head}｜{slot.label}"
    # 时刻类的量：内部一律是「距规划起点的分钟数」。直接印 -3 就是「-3 时刻」，
    # 用户读不出这是「提前三天」还是「提前三分钟」——回显必须换算成人能读的单位。
    if slot.value_type == "minutes":
        if isinstance(patch.value, str) and len(patch.value) >= 10:
            # 校验前是原始写法（ISO 时刻串），说日期就够，别把时区和纳秒也印出来
            return f"{head}｜{slot.label}定在 {patch.value[:10]}"
        if isinstance(patch.value, (int, float)) and not isinstance(patch.value, bool):
            if patch.op in ("+", "-"):
                word = "提前" if (patch.op == "-" or patch.value < 0) else "推后"
                return f"{head}｜{slot.label}{word} {_human_minutes(patch.value)}"
            if patch.op == "=" and data is not None and hasattr(data, "stamp"):
                try:
                    return f"{head}｜{slot.label}定在 {data.stamp(int(patch.value))[:10]}"
                except Exception:      # 值不是合法时刻就退回原样，不在回显里抛错
                    pass
    if isinstance(patch.value, bool):
        shown = "是" if patch.value else "否"
    elif patch.value is None:
        shown = ""
    else:
        shown = str(patch.value)
    unit = slot.unit
    if unit == "元或艘天":  # 预算两种口径的量纲不同，说错单位比不说更糟
        unit = "艘天" if (index.get("口径") or {}).get("value") == "租船天" else "元"
    tail = f" {unit}" if unit and shown else ""
    return f"{head}｜{slot.label} = {shown}{tail}" if shown else f"{head}｜{slot.label}"


def scope_predicate(data, spec: dict[str, Any]) -> tuple[Callable[[dict], bool] | None, str | None]:
    """scope_only 的「范围」：既能是类别（部件/风机/严重度…）也能是时段（窗口/某日/区间）。

    只有 scope_only 开这个口子——它的语义就是「圈一个范围」，范围天然可跨类别/时段。
    其他槽位的组仍然不认时段，R1 边界（整轮 vs 某段时光）不因此被破坏。
    """
    if (spec.get("kind") or "全部") in GROUP_KINDS:
        return group_predicate(data, spec)
    return bucket_predicate(data, spec)


def _resolve_window(data, ref: str) -> str | None:
    """窗口按 ``data.windows`` 匹配，不用 entities.resolve_window。

    后者查的是 ``observation_windows``——那里面还留着已过规划边界、被跳过的窗口
    （本次快照里窗口01/02 就是 STARTS_BEFORE_PLANNING_BOUNDARY）。候选行只可能落在
    ``data.windows`` 里，按观察窗口解析会让「窗口01 必须修几个」通过校验、
    却在注入口里匹配到零个候选，静默变成一条恒真的空约束。
    """
    ref = str(ref).strip()
    ids = {w["编号"] for w in data.windows}
    if ref in ids:
        return ref
    plain = ref.lstrip("窗口").zfill(2)
    return f"窗口{plain}" if f"窗口{plain}" in ids else None


def bucket_predicate(data, spec: dict[str, Any]) -> tuple[Callable[[dict], bool] | None, str | None]:
    """把「桶」索引解析成候选行谓词，按**开工时间**落桶（口径写死）。"""
    entities = _resolve_entities()
    kind = spec.get("kind")
    value = spec.get("value")
    if kind is None or kind == "全程":
        return (lambda c: True), None
    if kind == "窗口":
        wid = _resolve_window(data, value)
        if wid is None:
            ids = "、".join(w["编号"] for w in data.windows)
            return None, f"窗口 {value!r} 不在本次规划范围内，当前可用：{ids}"
        return (lambda c, w=wid: c["窗口编号"] == w), None
    if kind == "某日":
        day = entities.resolve_day(data, str(value))
        if day is None:
            return None, f"日期 {value!r} 不在规划期内"
        return (lambda c, d=day: c["开始"] // 1440 == d), None
    if kind == "日期区间":  # 开放：任意起止，只能事后查
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None, f"日期区间要给起止两个值，收到 {value!r}"
        lo = entities.resolve_day(data, str(value[0]))
        hi = entities.resolve_day(data, str(value[1]))
        if lo is None or hi is None:
            return None, f"日期区间 {value!r} 有端点不在规划期内"
        if lo > hi:
            return None, f"日期区间起点 {value[0]} 晚于终点 {value[1]}"
        return (lambda c: lo <= c["开始"] // 1440 <= hi), None
    return None, f"未知的桶类别 {kind!r}，可填：{'、'.join(BUCKET_KINDS)}"


def group_task_ids(data, spec: dict[str, Any]) -> list[str] | None:
    """组索引对应的在册任务编号（静态预检用：这格里到底有几个任务）。"""
    entities = _resolve_entities()
    kind = spec.get("kind")
    value = spec.get("value")
    if kind in (None, "全部"):
        return [t["任务编号"] for t in data.tasks]
    if kind == "严重度":
        return [t["任务编号"] for t in data.tasks if t["严重等级"] == value]
    if kind == "部件":
        pid = entities.resolve_part(data, str(value))
        return [t["任务编号"] for t in data.tasks if t["部件编号"] == pid] if pid else None
    if kind == "风机":
        tid = entities.resolve_turbine(data, str(value))
        return [t["任务编号"] for t in data.tasks if t["风机编号"] == tid] if tid else None
    if kind == "任务":
        tid = entities.resolve_task(data, str(value))
        return [tid] if tid else None
    if kind == "任务清单":
        ids = [entities.resolve_task(data, str(v)) for v in value]
        return [i for i in ids if i] or None
    if kind == "作业线":
        return [t["任务编号"] for t in data.tasks]  # 作业线是求解时才定的，按全部算
    return None
