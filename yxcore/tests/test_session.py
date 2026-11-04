"""会话层（多轮）测试：覆盖、叠加、撤销、原子落地、状态进提示词。

这些是纯确定性用例，不调大模型——多轮的**语义**必须先在本地锁死，
在线评测才有意义（否则模型答对了也说不清是运气还是设计对）。
"""
from __future__ import annotations

import json

import pytest

from yxcore import PROJECT_ROOT, solver_modules
from yxcore.patch import normalize_patch, parse_patch
from yxcore.session import Session


@pytest.fixture(scope="module")
def data():
    return solver_modules()["load_project"](PROJECT_ROOT, base_id="基座01")


def P(**kwargs):
    patch, error = parse_patch(kwargs)
    assert patch is not None, error
    return patch


def budget(value, 口径="现金", 组=None, utterance=""):
    return P(slot="budget_max", value=value, utterance=utterance,
             index={"口径": {"kind": "budget_口径", "value": 口径},
                    "组": 组 or {"kind": "全部"}})


def test_同格覆盖是修订不是报错(data):
    s = Session(data)
    s.apply([budget(800_000, utterance="现金不超80万")])
    turn = s.apply([budget(1_000_000, utterance="改成100万")])
    assert len(s.state) == 1
    assert turn.新增 == []
    assert len(turn.修订) == 1
    assert s.patches()[0].value == 1_000_000
    assert "改主意了" in turn.echo()[0]


def test_同一轮同格写两次仍然报错(data):
    s = Session(data)
    turn = s.apply([budget(800_000), budget(900_000)])
    assert turn.错误 and "同一句话里同一格被写了两次" in turn.错误[0]
    assert s.state == {}


def test_相对量叠加在当前生效值上(data):
    s = Session(data)
    base = data.params["船租（元每艘每天）"]
    s.apply([P(slot="船租", op="*", value=1.2, utterance="船租涨两成")])
    s.apply([P(slot="船租", op="*", value=1.1, utterance="再涨一成")])
    assert s.patches()[0].value == pytest.approx(base * 1.2 * 1.1, rel=1e-9)
    # 相对量已经折成绝对值落库，模型里不留相对数（D2）
    assert s.patches()[0].op == "="
    s.apply([P(slot="船租", op="+", value=1000, utterance="再加1000")])
    assert s.patches()[0].value == pytest.approx(base * 1.2 * 1.1 + 1000, rel=1e-9)


def test_浮点尾数被抹平(data):
    s = Session(data)
    s.apply([P(slot="船租", op="*", value=1.1, utterance="涨一成")])
    value = s.patches()[0].value
    assert value == round(value, 6)


def test_撤销已生效的格子(data):
    s = Session(data)
    s.apply([budget(800_000, utterance="现金不超80万")])
    turn = s.apply([P(slot="budget_max", op="reset", utterance="现金上限不要了",
                      index={"口径": {"kind": "budget_口径", "value": "现金"},
                             "组": {"kind": "全部"}})])
    assert s.state == {}
    assert len(turn.撤销) == 1
    assert "撤掉了" in turn.echo()[0]


def test_撤销本来就没设的格子要如实说(data):
    s = Session(data)
    turn = s.apply([P(slot="budget_max", op="reset", utterance="现金上限不要了",
                      index={"口径": {"kind": "budget_口径", "value": "现金"},
                             "组": {"kind": "全部"}})])
    assert turn.空撤销 and not turn.撤销
    assert "本来就没设" in turn.echo()[0]
    # 回显不许出现「= None」这种机器话
    assert "None" not in turn.echo()[0]


def test_同一批补丁两条路径到同一个状态(data):
    """先设 80 万再改 100 万，与直接设 100 万，生效补丁集必须一样。"""
    a = Session(data)
    a.apply([budget(800_000, utterance="现金不超80万")])
    a.apply([budget(1_000_000, utterance="改成100万")])
    b = Session(data)
    b.apply([budget(1_000_000, utterance="现金不超100万")])
    strip = lambda ps: [{k: v for k, v in p.to_json().items() if k != "原话"} for p in ps]
    assert strip(a.patches()) == strip(b.patches())
    # 但会话指纹要能分辨「改过主意」——不然审计时看不出用户反悔过
    assert a.fingerprint() != b.fingerprint()


def test_状态进提示词用补丁原格式(data):
    s = Session(data)
    s.apply([budget(800_000, utterance="现金不超80万")])
    text = s.prompt_state()
    assert "第 2 轮" in text
    assert '"槽位": "budget_max"' in text
    assert '"kind": "budget_口径"' in text
    assert json.dumps({"kind": "全部", "value": None}, ensure_ascii=False) in text


def test_首轮提示词里没有状态段(data):
    s = Session(data)
    assert s.prompt_state() == ""
    from yxcore.prompt import build_prompt
    options = {t["任务编号"]: solver_modules()["candidates"](data, t) for t in data.tasks}
    assert "当前已生效的旋钮（多轮累积）" not in build_prompt(data, options)
    s.apply([budget(800_000)])
    assert "当前已生效的旋钮（多轮累积）" in build_prompt(data, options, session_text=s.prompt_state())


def test_整轮原子落地_非法补丁不进状态(data):
    s = Session(data)
    s.apply([budget(800_000, utterance="现金不超80万")])
    # 这一句里第一条合法、第二条非法（部件库里没有「叶片备件」这个名字）
    turn = s.apply([
        P(slot="count_min", value=2, index={"组": {"kind": "严重度", "value": "高"}},
          utterance="高危至少修2个"),
        P(slot="part_hours", value=10, index={"部件": {"kind": "part", "value": "叶片备件"}},
          utterance="叶片备件按10小时算"),
    ])
    # 注册层管不到部件名，编译层能——所以 check 必须是两道一起过
    errors, _ = s.check()
    assert errors and "叶片备件" in "".join(errors)


def test_人话回显点名格子(data):
    s = Session(data)
    turn = s.apply([P(slot="bucket_min", value=1, utterance="窗口03必须修叶片",
                      index={"桶": {"kind": "窗口", "value": "窗口03"},
                             "组": {"kind": "部件", "value": "叶片"}})])
    text = turn.echo()[0]
    assert "窗口03" in text and "叶片" in text and "至少开工" in text


def test_budget_unit_follows_口径(data):
    s = Session(data)
    s.apply([budget(5, 口径="租船天", utterance="租船最多5天")])
    assert "艘天" in s.summary()[0]
    s2 = Session(data)
    s2.apply([budget(800_000, 口径="现金")])
    assert s2.summary()[0].endswith("元")


# ── 本轮新增：空补丁 / 原子性 / 索引形状 / 改主意提示 ──────────────────


def test_空补丁不崩_整句不落地(data):
    s = Session(data)
    turn = s.apply([None])
    assert turn.错误 and "空" in turn.错误[0]
    assert s.state == {}
    assert any("没照做" in line for line in turn.echo())


def test_一条坏补丁整轮回滚(data):
    s = Session(data)
    ok = budget(800_000)
    # 同一轮同格写两次是落地级错误（D2），必须整轮回滚，不能留下第一条
    turn = s.apply([ok, budget(900_000)])
    assert turn.错误 and "同一句话里同一格被写了两次" in turn.错误[0]
    assert s.state == {}           # 好的那条也不许留
    assert turn.新增 == [] and turn.修订 == []


def test_索引裸字符串在解析层就被挡下():
    patch, error = parse_patch({"slot": "count_max", "op": "=", "value": 8,
                                "index": {"组": "全部"}})
    assert patch is None
    assert "要说清是哪一类" in error and "kind" in error
    # 单类别维度写裸值是**能**归一的（注册表里那一维只有一种 kind，不是猜）
    ok, err = parse_patch({"slot": "budget_max", "op": "=", "value": 100,
                           "index": {"口径": "现金"}})
    assert err is None
    assert normalize_patch(ok).index["口径"]["kind"] == "budget_口径"


def test_回显带错误():
    from yxcore.session import Turn
    turn = Turn(原话="随便说点什么")
    turn.错误.append("这一格不在册")
    assert any("没照做" in line and "不在册" in line for line in turn.echo())


def test_回显澄清():
    """指代不清时枚举候选让用户选，不是「没接」也不是「没照做」。"""
    from yxcore.session import Turn
    turn = Turn(原话="只完成前五个任务")
    turn.澄清 = [{"意图": "按截止最早取前五个", "patches": []},
                 {"意图": "按严重度最高取前五个", "patches": []}]
    lines = turn.echo()
    assert any("你是想" in line and "按截止最早" in line and "按严重度最高" in line for line in lines)
    assert not turn.拒绝


def test_改主意忘撤旧的要提示(data):
    s = Session(data)
    s.apply([P(slot="count_max", value=8,
               index={"组": {"kind": "全部"}})])
    turn = s.apply([P(slot="count_max", value=2,
                      index={"组": {"kind": "风机", "value": "风机003"}})])
    assert turn.提示 and "两条限制同时在" in turn.提示[0]
    assert any("提醒一句" in line for line in turn.echo())


def test_同一轮撤了旧的不提示(data):
    s = Session(data)
    s.apply([P(slot="count_max", value=8, index={"组": {"kind": "全部"}})])
    turn = s.apply([
        P(slot="count_max", op="reset", index={"组": {"kind": "全部"}}),
        P(slot="count_max", value=2, index={"组": {"kind": "风机", "value": "风机003"}}),
    ])
    assert turn.提示 == []
    assert len(s.state) == 1


def test_翻译失败不毁会话(data, monkeypatch):
    import yxcore.parse as parse_mod

    def boom(*_a, **_k):
        raise parse_mod.LLMUnavailable("未配置 DEEPSEEK_API_KEY")

    monkeypatch.setattr(parse_mod, "translate", boom)
    s = Session(data)
    turn = s.say("现金不超80万")
    assert turn.错误 and "DEEPSEEK_API_KEY" in turn.错误[0]
    assert s.state == {}                      # 表没被动过
    assert any("没照做" in line for line in turn.echo())


def test_时刻类回显说人话(data):
    s = Session(data)
    turn = s.apply([P(slot="deadline_delta", op="+", value=-4320,
                      index={"组": {"kind": "任务", "value": "任务001"}})])
    text = turn.echo()[0]
    assert "提前 3 天" in text and "时刻" not in text


def test_不带索引的reset撤掉整族(data):
    s = Session(data)
    s.apply([P(slot="count_max", value=8, index={"组": {"kind": "全部"}}),
             P(slot="count_max", value=2,
               index={"组": {"kind": "风机", "value": "风机003"}})])
    turn = s.apply([P(slot="count_max", op="reset", index={})])
    assert len(turn.撤销) == 2 and s.state == {}
    text = "；".join(turn.echo())
    assert "全部任务" in text and "风机003" in text


def test_数据补丁的增量叠加不是折成绝对量(data):
    idx = {"组": {"kind": "任务", "value": "任务001"}}
    s = Session(data)
    s.apply([P(slot="deadline_delta", op="+", value=-4320, index=idx)])   # 提前 3 天
    turn = s.apply([P(slot="deadline_delta", op="+", value=-2880, index=idx)])  # 再提前 2 天
    stored = list(s.state.values())[0]
    # 存的是「累计增量」，不是「把期限定到 -7200」——后者语义整个翻掉
    assert stored.op == "+" and stored.value == -7200
    assert "提前 5 天" in turn.echo()[0]


def test_时刻型回显说日期不说ISO串(data):
    idx = {"组": {"kind": "任务", "value": "任务001"}}
    s = Session(data)
    s.apply([P(slot="earliest", value="2026-11-19T00:00:00+08:00", index=idx)])
    turn = s.apply([P(slot="earliest", value="2026-11-20", index=idx)])
    text = turn.echo()[0]
    assert "2026-11-20" in text and "T00:00" not in text and "时刻" not in text


def test_reset带值照样撤_值丢掉(data):
    """模型照抄上一条只把 op 改成 reset，值也带过来——别为这个把整句打回去。"""
    p, error = parse_patch({"slot": "strategy", "op": "reset", "value": "成本最低优先"})
    assert error is None and p.value is None
    s = Session(data)
    s.apply([P(slot="strategy", value="成本最低优先")])
    turn = s.apply([p])
    assert len(turn.撤销) == 1 and s.state == {}


def test_日期区间只给一天就当成那一天(data):
    p, error = parse_patch({"slot": "bucket_min", "value": 1,
                            "index": {"桶": {"kind": "日期区间", "value": "2026-11-18"},
                                      "组": {"kind": "部件", "value": "叶片"}}})
    assert error is None
    assert normalize_patch(p).index["桶"]["value"] == ["2026-11-18", "2026-11-18"]


def test_回显对每个槽位都不崩_有快照和没快照都要能说(data):
    """回显是最后一道「能不能说人话」的关：任何槽位、任何值形态都不许抛异常。"""
    from yxcore import slots as S
    from yxcore.patch import Patch

    samples = {
        "组": {"kind": "全部"}, "桶": {"kind": "窗口", "value": "窗口03"},
        "维度": {"kind": "cap_维度", "value": "作业线"},
        "口径": {"kind": "budget_口径", "value": "现金"},
        "量": {"kind": "metric", "value": "高严重完成数"},
        "任务": {"kind": "任务", "value": "任务001"},
        "部件": {"kind": "部件", "value": "齿轮箱"},
        "序列": {"kind": "task_seq", "value": ["任务001", "任务002"]},
        "集合": {"kind": "task_set", "value": ["任务001", "任务002"]},
        "层对": {"kind": "severity_pair", "value": ["高", "中"]},
        "粒度": {"kind": "粒度", "value": "天"},
        "范围": {"kind": "部件", "value": "叶片"},
        "日期": {"kind": "day", "value": "2026-11-16"},
        "时刻": {"kind": "moment", "value": "08:00"},
        "窗口": {"kind": "window", "value": "窗口03"},
        "端": {"kind": "端", "value": "开始"},
        "项": {"kind": "项", "value": "浪高"},
        "严重度": {"kind": "严重度", "value": "高"},
        "海况": {"kind": "海况", "value": "恶劣"},
    }
    for name, slot in S.SLOTS.items():
        index = {key: samples.get(key, {"kind": key}) for key, _ in slot.index}
        for value in (1, "2026-11-18", True, None):
            patch = Patch(slot=name, index=index, op="=", value=value, strength="hard")
            for snapshot in (data, None):     # 有快照、没快照都得说得出来
                assert S.describe_patch(patch, snapshot)
