"""守门：同一次求解的「配置事实」在所有出口必须一致。

改造前默认权重是在最里层 build_model 才从模板取出来的，取完挂到
data.effective_weights 上；外面的 solve / recommend / 产物生成器手里仍是
那个 None 入参，于是 结果.json、model_artifact.json、公式页各写各的：
有的写 null，有的靠猜，有的把权重塞进中文字符串里。同类脱节在「时限」
上发生过一次（界面同时出现 300/60/30 三个数），当时只把那一组收敛到
solve_budget，没有把做法推广开。

现在 RunSpec.resolve() 是唯一定稿点，spec.facts() 是唯一输出映射。
这个用例真跑一次求解，把四个出口逐项对齐；以后新增任何出口忘了接，
这里当场红，而不是等用户在网站上问「公式呢？」才发现。
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

from product_data import load_project  # noqa: E402
from product_planner import (  # noqa: E402
    OBJECTIVE_MODE, RunSpec, build_model, recommend, solve,
)
from app.core.yx_engine import YxEngine  # noqa: E402


class RunSpecConsistencyTest(unittest.TestCase):
    def _run(self, seconds=8):
        data = load_project(ROOT, as_of="2026-10-23T18:00:00+08:00")
        data.tasks = data.tasks[:3]
        temp = tempfile.TemporaryDirectory()
        out = Path(temp.name)
        result = recommend(data, out, seconds=seconds)
        report = json.loads((out / "结果.json").read_text(encoding="utf-8"))
        artifact = json.loads((out / "model_artifact.json").read_text(encoding="utf-8"))
        self.addCleanup(temp.cleanup)
        return data, out, result, report, artifact

    def test_all_exits_report_the_same_facts(self):
        data, out, result, report, artifact = self._run()
        info = result["求解"]
        summary = YxEngine._result_summary(result, data)
        weights = RunSpec.resolve(data).weights
        self.assertTrue(weights)

        # 权重：四个出口都不许再是 null，也不许只藏在 formula 中文串里。
        self.assertEqual(report["自定义权重"], weights)
        self.assertEqual(report["运行配置"]["权重"], weights)
        self.assertEqual(report["运行配置"]["自定义权重"], weights)
        self.assertEqual(info["权重"], weights)
        self.assertEqual(artifact["weights"], weights)
        self.assertEqual(artifact["objectives"]["weights"], weights)
        self.assertEqual(summary["weights"], weights)
        self.assertIn("总经济成本=%s" % weights["总经济成本"],
                      artifact["objectives"]["stages"][0]["formula"])

        # 目标模式：结果.json / 求解 dict / 产物 / 结果摘要 全部一致。
        for value in (
            report["目标模式"], report["运行配置"]["目标模式"], info["目标模式"],
            artifact["mode"], artifact["objectives"]["mode"], summary["objectiveMode"],
            OBJECTIVE_MODE,
        ):
            self.assertEqual(value, "加权")

        # 展望期、时限、容差、指纹：同样只许有一个值。
        horizon = RunSpec.resolve(data).horizon_days
        self.assertEqual(report["运行配置"]["展望期天数"], horizon)
        self.assertEqual(artifact["unrepairedPenalty"]["horizonDays"], horizon)
        self.assertEqual(artifact["dataSnapshot"]["penaltyHorizonDays"], horizon)
        self.assertEqual(info["预算"]["seconds"], artifact["solverParameters"]["max_time_in_seconds"])
        self.assertEqual(info["预算"]["seconds"], report["运行配置"]["时限秒"])
        self.assertEqual(info["预算"]["gapLimit"], report["运行配置"]["容差"])
        self.assertEqual(info["模型产物指纹"], artifact["modelFingerprint"])

        # 运行配置本身就是 spec.facts()，不许有人再手拼一份。
        self.assertEqual(report["运行配置"], RunSpec.resolve(data, seconds=8).facts())

    def test_single_stage_and_no_tie_break_artifacts(self):
        _data, out, result, _report, _artifact = self._run()
        info = result["求解"]
        self.assertEqual(info["解状态"], "OPTIMAL")
        self.assertEqual(info["最优性"], "已证明最优")
        self.assertEqual(info["确定性决胜完成"], "不适用")
        self.assertEqual(len(info["阶段"]), 1)
        self.assertEqual(info["阶段"][0]["目标"], "标准化加权目标")
        self.assertEqual(list(out.glob("solver_model_tie_*.pbtxt")), [])

    def test_spec_is_the_only_place_defaults_are_resolved(self):
        data = load_project(ROOT, as_of="2026-10-23T18:00:00+08:00")
        default = RunSpec.resolve(data)
        custom = {"总经济成本": 0.5, "完成任务数": 0.3, "最后完工时间": 0.1, "完工时间和": 0.1}
        overridden = RunSpec.resolve(data, weights=custom, seconds=12, strategy="高风险优先抢修")
        self.assertNotEqual(default.weights, {})
        self.assertEqual(overridden.weights, custom)
        self.assertEqual(overridden.facts()["自定义权重"], custom)
        self.assertEqual(overridden.seconds, 12)
        self.assertEqual(overridden.strategy, "高风险优先抢修")
        with self.assertRaisesRegex(ValueError, "已撤销"):
            RunSpec.resolve(data, mode="分层")
        with self.assertRaises(ValueError):
            RunSpec.resolve(data, weights={"不存在的指标": 1})

    def test_slim_signatures(self):
        # 四层里重复携带的 8 个 kwargs 已收敛成单个 spec。
        self.assertEqual(len(inspect.signature(build_model).parameters), 3)
        for fn in (solve, recommend):
            self.assertNotIn("custom_stages", inspect.signature(fn).parameters)
        from model_artifact import build_model_artifact
        self.assertIn("spec", inspect.signature(build_model_artifact).parameters)
        self.assertNotIn("weights", inspect.signature(build_model_artifact).parameters)

    def test_agent_chain_solve_entry_uses_the_same_spec(self):
        """Agent 主链不走 recommend，直接调 solve；这条也不能漏接定稿。"""
        from product_planner import candidates
        data = load_project(ROOT, as_of="2026-10-23T18:00:00+08:00")
        data.tasks = data.tasks[:2]
        options = {t["任务编号"]: candidates(data, t) for t in data.tasks}
        with tempfile.TemporaryDirectory() as out:
            _selected, info = solve(data, options, Path(out), seconds=8)
            artifact = json.loads((Path(out) / "model_artifact.json").read_text(encoding="utf-8"))
        self.assertEqual(info["目标模式"], "加权")
        self.assertEqual(info["权重"], artifact["weights"])
        self.assertEqual(artifact["objectives"]["weights"], info["权重"])
        self.assertEqual(len(info["阶段"]), 1)


if __name__ == "__main__":
    unittest.main()
