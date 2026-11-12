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
    turn.澄清 = [{"问": "前五个按什么顺序", "候选": [
        {"意图": "按截止最早", "patches": []},
        {"意图": "按严重度最高", "patches": []},
    ]}]
    lines = turn.echo()
    assert any("需要澄清" in line and "前五个按什么顺序" in line for line in lines)
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


def test_resolved_intent_is_supplement_not_replacement(data):
    """主模型补出的完整意思只进「本次补充」，原话一字不改地进翻译。"""
    from unittest.mock import MagicMock, patch

    fake = MagicMock(return_value=([], None, None, [{"标签": "占位"}]))
    with patch("yxcore.parse.translate", fake):
        Session().say("可以", data=data, options={},
                      resolved_intent="把船租单价上调两成，按现状排一版")
    assert fake.call_args.args[0] == "可以"
    extra = fake.call_args.kwargs["extra"]
    assert "把船租单价上调两成" in extra
    assert "不许从这里翻出用户没提过的新排程要求" in extra
    assert "不许用它替换原话" in extra


def test_no_resolved_intent_leaves_extra_empty(data):
    """第一轮没有需要消解的指代时，提示词里不出现这一节。"""
    from unittest.mock import MagicMock, patch

    fake = MagicMock(return_value=([], None, None, [{"标签": "占位"}]))
    with patch("yxcore.parse.translate", fake):
        Session().say("船租涨两成", data=data, options={})
    assert fake.call_args.kwargs["extra"] == ""


# ── 规格状态机：哪份要求算数 ────────────────────────────────────────────
# 不真调求解器：造一份带事实层的求解结果，并允许中途改成抛错。
# session.run() 是在函数内 from .pipeline import run，桩模块属性即可生效。

def _stub_solver(monkeypatch, *, as_of="2026-11-08T18:00:00+08:00",
                 watermark="2026-11-08T17:00:00+08:00"):
    import yxcore.pipeline as pipeline

    class Stub:
        def __init__(self):
            self.fail = False
            self.calls = []
            self.as_of = as_of
            self.watermark = watermark

        def __call__(self, patches, **kwargs):
            self.calls.append([p.to_json() for p in patches])
            if self.fail:
                raise RuntimeError("求解器没算出结果")
            return {
                "数据": {"规划时点": self.as_of, "时点来源": "指定时点",
                         "告警水位线": self.watermark,
                         "输入哈希": "hash-" + self.watermark[:10]},
                "警告": [], "排程": [], "求解": {},
            }

    stub = Stub()
    monkeypatch.setattr(pipeline, "run", stub)
    return stub


def test_求解成功才把要求定稿(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    s = Session(data)
    assert s.spec_status()["状态"] == "空"
    s.apply([budget(800_000, utterance="现金不超80万")])
    assert s.spec_status()["状态"] == "未验证"
    s.run(seconds=5)
    status = s.spec_status()
    assert status["状态"] == "已验证"
    assert status["生效补丁"] == 1
    assert status["基座"] == "基座01"
    assert status["规划时点"] == stub.as_of
    assert status["告警水位线"] == stub.watermark
    assert status["时点来源"] == "指定时点"
    assert status["已验证版本数"] == 1
    trace = s.spec_trace()
    assert len(trace) == 1 and trace[0]["告警水位线"] == stub.watermark


def test_求解失败撤回到已验证规格(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    s = Session(data)
    s.apply([budget(800_000, utterance="现金不超80万")])
    s.run(seconds=5)                                   # 定稿 A
    s.apply([budget(900_000, utterance="改成90万")])
    assert s.spec_status()["状态"] == "未验证"
    stub.fail = True
    with pytest.raises(RuntimeError, match="求解器没算出结果"):
        s.run(seconds=5)
    assert s.patches()[0].value == 800_000            # 回到 A
    assert s.spec_status()["状态"] == "已验证"
    assert "已撤回到上一份算出过方案的规格" in s.last_rollback
    assert "改过的" in s.last_rollback
    assert s.spec_status()["最近失败说明"] == s.last_rollback


def test_第一次求解失败不擅自丢掉用户要求(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    stub.fail = True
    s = Session(data)
    s.apply([budget(800_000)])
    with pytest.raises(RuntimeError):
        s.run(seconds=5)
    assert len(s.state) == 1
    assert s.spec_status()["状态"] == "未验证"
    assert "第一次求解" in s.last_rollback


def test_回到上一版规格(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    s = Session(data)
    s.apply([budget(800_000)])
    s.run(seconds=5)                                   # A
    s.apply([budget(900_000)])
    s.run(seconds=5)                                   # B
    assert s.spec_status()["已验证版本数"] == 2
    assert s.patches()[0].value == 900_000
    note = s.restore_previous()
    assert "已回到上一版规格" in note
    assert s.patches()[0].value == 800_000
    assert s.spec_status()["状态"] == "已验证"
    assert s.spec_status()["已验证版本数"] == 1


def test_只有一版时回不去且说明白(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    s = Session(data)
    s.run(seconds=5)
    assert "暂时回不去" in s.restore_previous()
    assert s.spec_status()["已验证版本数"] == 1


def test_主动回到已验证规格(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    s = Session(data)
    s.apply([budget(800_000)])
    s.run(seconds=5)
    s.apply([budget(900_000)])
    assert "已撤回到上一份算出过方案的规格" in s.restore_verified()
    assert s.patches()[0].value == 800_000
    assert s.spec_status()["状态"] == "已验证"


def test_水位线推进要提醒且不新增版本(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    s = Session(data)
    s.apply([budget(800_000)])
    s.run(seconds=5)
    stub.watermark = "2026-11-11T18:00:00+08:00"
    result = s.run(seconds=5)
    assert any("告警台账已经更新" in w for w in result["警告"])
    assert s.spec_status()["已验证版本数"] == 1       # 要求没变，不算新版本
    assert s.spec_status()["告警水位线"] == stub.watermark


def test_规格指纹带水位线(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    a, b = Session(data), Session(data)
    a.apply([budget(800_000)])
    b.apply([budget(800_000)])
    assert a.fingerprint() == b.fingerprint()          # 都还没定稿
    a.run(seconds=5)
    stub.watermark = "2026-11-11T18:00:00+08:00"
    b.run(seconds=5)
    assert a.fingerprint() != b.fingerprint()          # 同要求、不同台账


def test_clear_连锚点一起清(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    s = Session(data)
    s.apply([budget(800_000)])
    s.run(seconds=5)
    s.clear()
    assert s.state == {} and s.turns == []
    assert s.spec_status()["已验证版本数"] == 0
    assert s.spec_status()["状态"] == "空"
    assert s.spec_trace() == []
    assert "暂时回不去" in s.restore_previous()


def test_事实层推进后规格被标为过期(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    monkeypatch.setitem(data.meta, "规划时点", stub.as_of)
    s = Session(data)
    s.apply([budget(800_000)])
    s.run(seconds=5)
    assert s.spec_status()["事实层已推进"] is False
    monkeypatch.setitem(data.meta, "规划时点", "2026-11-12T18:00:00+08:00")
    status = s.spec_status()
    assert status["事实层已推进"] is True
    assert status["状态"] == "已验证"              # 标过期，不擅自改成未验证
    assert "重排一次" in status["提醒"]


# ── 规格状态导出 / 导入（持久化的前置能力）─────────────────────────────
# 这里只验「能无损带走、坏数据不半恢复」，不验「重启后自动接上」——
# 那是产品语义决策，见 docs/规格锚点持久化评估.md。

def test_规格状态导出导入无损(data, monkeypatch):
    stub = _stub_solver(monkeypatch)
    s = Session(data)
    s.apply([budget(800_000, utterance="现金不超80万")])
    s.run(seconds=5)                                   # 第 1 版算通过
    s.apply([budget(900_000, utterance="改成90万")])
    s.run(seconds=5)                                   # 第 2 版算通过

    # 制造一次求解失败：回滚说明要一起带走，state 也退回已验证那版。
    s.apply([budget(700_000, utterance="再改成70万")])
    stub.fail = True
    with pytest.raises(RuntimeError):
        s.run(seconds=5)
    stub.fail = False
    assert s.last_rollback

    # 再放一条「用户说过但没算通过」的要求，导出时必须和已验证那份分开。
    s.apply([budget(600_000, utterance="先记着，没算")])
    payload = json.loads(json.dumps(s.export_spec(), ensure_ascii=False))
    assert payload["格式版本"] == 1
    assert len(payload["已验证历史"]) == 2
    assert payload["已验证"]["生效补丁"] != payload["当前要求"]

    t = Session(data)
    assert "已恢复" in t.import_spec(payload)
    assert t.state == s.state, "当前要求表要原样回来（含未验证的那条）"
    assert t.spec_status() == s.spec_status()
    assert t.spec_trace() == s.spec_trace(), "按版本记的规格台账不能丢"
    assert payload["轮数"] == len(s.turns)
    # 逐轮对话记录不在规格快照里，fingerprint() 含轮数，所以不会相等；
    # 接线方不能拿它当跨重启的等价判据（见规格锚点持久化评估）。
    assert t.last_rollback == s.last_rollback
    assert "已回到上一版" in t.restore_previous(), "恢复后仍要能回到上一版"


def test_导入坏数据整份拒绝不半恢复(data, monkeypatch):
    _stub_solver(monkeypatch)
    s = Session(data)
    s.apply([budget(800_000)])
    s.run(seconds=5)
    good = s.export_spec()

    t = Session(data)
    t.apply([budget(500_000, utterance="这是本会话原本的状态")])
    before = t.export_spec()

    # 槽位名不在注册表是校验层的活，parse_patch 只管形状；坏数据要用形状错误。
    bad_patch = json.loads(json.dumps(good, ensure_ascii=False))
    bad_patch["当前要求"] = ["这不是一个补丁对象"]
    assert "没恢复" in t.import_spec(bad_patch)
    assert t.export_spec() == before, "坏数据不能只改一半；原状态要原封不动"

    bad_history = json.loads(json.dumps(good, ensure_ascii=False))
    bad_history["已验证"]["生效补丁"] = [{"槽位": "count_max", "运算": "不存在的运算"}]
    assert "没恢复" in t.import_spec(bad_history), "历史规格里的坏补丁也要挡"

    assert "没恢复" in t.import_spec({**good, "格式版本": 999})
    assert "没恢复" in t.import_spec({**good, "已验证历史": "不是列表"})
    assert t.export_spec() == before


def test_空会话也能导出导入(data):
    payload = json.loads(json.dumps(Session(data).export_spec(), ensure_ascii=False))
    assert payload["已验证"] is None and payload["已验证历史"] == []
    t = Session(data)
    assert "已恢复" in t.import_spec(payload)
    assert t.state == {}
    assert t.spec_status()["状态"] == "空"
