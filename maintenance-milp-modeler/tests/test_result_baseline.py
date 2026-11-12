"""结果级基线回归 + 结果契约判分口径。

两类断言，别混：
- `test_frozen_results_are_reproducible`：证明「这次改动有没有改变排程结果」，
  是回归防线。基线红了先查原因，不许直接刷基线。
- `test_result_contract_*`：证明「算出来的结果满足用户要求」，是判分口径。
  它不关心模型长什么样，只关心用户点名的任务有没有排、组外任务有没有混进来。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "maintenance-milp-modeler" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 并行搜索在超时截断时可能取到不同中间解；基线录制与回放都必须单线程。
os.environ["MWA_SOLVER_WORKERS"] = "1"

import product_planner  # noqa: E402
from result_contract import check, snapshot  # noqa: E402
from yxcore.pipeline import run as yx_run  # noqa: E402

BASELINE = Path(__file__).resolve().parent / "result_baseline.json"


def _differences(expected, actual, path="结果"):
    """把两份快照的差异摊成人能读的一行行，别只丢一句 assertEqual 不相等。"""
    if type(expected) is not type(actual):
        return [f"{path}: 类型不同 {type(expected).__name__} / {type(actual).__name__}"]
    if isinstance(expected, dict):
        out = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                out.append(f"{path}.{key}: 一侧缺字段")
            else:
                out.extend(_differences(expected[key], actual[key], f"{path}.{key}"))
        return out
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{path}: 条数 {len(expected)} → {len(actual)}"]
        out = []
        for i, (a, b) in enumerate(zip(expected, actual)):
            out.extend(_differences(a, b, f"{path}[{i}]"))
        return out
    return [] if expected == actual else [f"{path}: {expected!r} → {actual!r}"]


def _fake_result(placed_ids, total=16):
    """造一份不跑求解器的最小结果，专门用来验判分器本身。"""
    all_ids = [f"任务{i:03d}" for i in range(1, total + 1)]
    rows = [{"任务编号": tid, "风机编号": "风机X", "部件名称": "齿轮箱",
             "严重等级": "高", "窗口编号": "窗口01", "作业线": 1,
             "开始": 0, "完成": 60} for tid in placed_ids]
    return {
        "排程": rows,
        "推荐": {"未安排": [{"任务编号": t} for t in all_ids if t not in placed_ids],
                 "待修总数": total, "总成本": 0},
        "求解": {}, "数据": {},
    }


@unittest.skipUnless((PROJECT / "data/phase1-demo").exists(), "project fixture not bundled")
class ResultBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._workers = mock.patch.object(product_planner, "WORKERS", 1)
        cls._workers.start()

    @classmethod
    def tearDownClass(cls):
        cls._workers.stop()

    def _run(self, as_of, seconds):
        with tempfile.TemporaryDirectory() as out:
            return yx_run([], as_of=as_of, seconds=seconds, output=Path(out))

    def test_frozen_results_are_reproducible(self):
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(data["基线版本"], "1.0")
        for entry in data["基线"]:
            with self.subTest(断点=entry["名称"]):
                actual = snapshot(self._run(entry["规划时点"], entry["时限秒"]))
                diff = _differences(entry["快照"], actual)
                self.assertFalse(
                    diff,
                    "排程结果与冻结基线不一致：\n  " + "\n  ".join(diff[:25])
                    + "\n若这是有意改动，人工确认新结果满足用户要求后重录基线，"
                      "并在提交说明里交代改了哪处、为什么。",
                )

    def test_baseline_states_optimality_honestly(self):
        """基线锁的是「算出来的方案」，不能把未证最优写成已证明最优。"""
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        for entry in data["基线"]:
            with self.subTest(断点=entry["名称"]):
                solve = entry["快照"]["求解"]
                if solve["解状态"] != "OPTIMAL":
                    self.assertNotEqual(solve["最优性"], "已证明最优")
                self.assertTrue({"解状态", "最优性", "终止原因"} <= set(solve))

    def test_contract_catches_out_of_scope_tasks(self):
        """大一修 v0.17 陷阱：Σx=8 不等于「只做 8 个」。

        这里 16 个任务排了 12 个，要求「只修前 8 个」。
        计数型断言（恰好修 8）当然会红；关键是排除型断言必须指出**组外任务**，
        而不是只报个数不对。
        """
        first8 = [f"任务{i:03d}" for i in range(1, 9)]
        placed = [f"任务{i:03d}" for i in range(1, 13)]
        result = _fake_result(placed)

        problems = check(result, {"只修任务": first8})
        self.assertTrue(any("组外任务" in p for p in problems), problems)
        self.assertIn("任务009", " ".join(problems))

        # 只写「恰好修 8」时会报个数不对，但不会点出混进来的具体任务。
        count_only = check(result, {"恰好修": 8})
        self.assertTrue(any("实际排了 12" in p for p in count_only))
        self.assertFalse(any("组外任务" in p for p in count_only))

        # 结果确实只含这 8 个时，排除型断言应该放行。
        self.assertEqual(check(_fake_result(first8), {"只修任务": first8}), [])

    def test_contract_checks_self_consistency_and_unknown_keys(self):
        result = _fake_result(["任务001", "任务002"])
        result["推荐"]["未安排"].append({"任务编号": "任务001"})
        self.assertTrue(any("既排了又列为未安排" in p for p in check(result, {})))
        with self.assertRaises(ValueError):
            check(result, {"写了没人判的键": 1})


if __name__ == "__main__":
    unittest.main()
