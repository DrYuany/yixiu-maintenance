"""Business acceptance tests; fixture inputs never mutate project data."""
import inspect
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from product_data import load_project
from product_planner import recommend, solve, verify_schedule
from product_store import Store

PROJECT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((PROJECT / 'data/phase1-demo').exists(), 'project fixture not bundled')
class ProductRuntimeTests(unittest.TestCase):
    def test_current_snapshot_does_not_read_future_alarms(self):
        data = load_project(PROJECT, as_of='2026-10-23T18:00:00+08:00')
        # 数据在 a27db1c 调整为"初始 10 条、之后每 4 天一条"，此日即为 11 条。
        # 这里的数字只用来确认"未来告警没被读进来"，跟随数据版本走。
        self.assertEqual(len(data.tasks), 11)
        self.assertEqual(len(data.windows), 1)
        self.assertGreater(len(data.observation_windows), 1)
        self.assertEqual(data.windows[0]['编号'], '窗口02')
        self.assertEqual(data.meta['规划边界'], '2026-10-30T18:00:00+08:00')
        self.assertEqual(data.meta['目标窗口编号'], '窗口02')
        self.assertTrue(all(task['截止来源窗口编号'] for task in data.tasks))
        self.assertEqual(data.weather[0]['海况类别'], '不可作业')
        self.assertEqual(data.params['船租（元每艘每天）'], 12000)

    def test_daily_snapshot_is_frozen_from_future_alerts(self):
        earlier = load_project(PROJECT, as_of='2026-10-23T18:00:00+08:00')
        earlier_ids = [task['任务编号'] for task in earlier.tasks]
        later = load_project(PROJECT, as_of='2026-10-27T18:00:00+08:00')

        self.assertGreaterEqual(len(later.tasks), len(earlier.tasks))
        self.assertEqual([task['任务编号'] for task in earlier.tasks], earlier_ids)
        self.assertNotEqual(earlier.meta['告警水位线'], later.meta['告警水位线'])
        self.assertNotEqual(earlier.meta['运行哈希'], later.meta['运行哈希'])

    def test_actual_solver_and_independent_verification(self):
        data = load_project(PROJECT, as_of='2026-10-23T18:00:00+08:00')
        data.tasks = data.tasks[:3]
        with tempfile.TemporaryDirectory() as out:
            # 只有一个固定加权目标，不再有分层慢路径。
            result = recommend(data, Path(out), seconds=30)
            self.assertTrue(result['求解']['有解'])
            self.assertIn(result['求解']['解状态'], {'OPTIMAL', 'FEASIBLE'})
            self.assertIn(result['求解']['终止原因'], {'COMPLETED', 'TIME_LIMIT'})
            self.assertTrue(result['推荐']['复核通过'])
            self.assertEqual(result['推荐']['完成任务数'], 3)
            self.assertEqual(len(result['人工基准']), 2)
            self.assertEqual(len(result['窗口']), 1)
            self.assertEqual({row['窗口编号'] for row in result['推荐']['排程']}, {data.windows[0]['编号']})
            self.assertTrue((Path(out) / '推荐报告.html').exists())
            verify_schedule(data, result['推荐']['排程'])

    def test_weighted_default_skips_tie_break_and_stays_optimal(self):
        """决胜段已整块删除，最优性只由加权目标决定。

        以前加权也跟着跑几十段决胜，某一段没证到最优（TIE_NOT_PROVEN）就把
        已经 OPTIMAL 的加权目标连坐降成 FEASIBLE / 未证明最优。现在那段代码
        不存在了；产物里「确定性决胜完成」恒记「不适用」（历史字段名，读侧还在用），
        也不该出现 solver_model_tie_*.pbtxt。
        """
        data = load_project(PROJECT, as_of='2026-10-23T18:00:00+08:00')
        data.tasks = data.tasks[:3]
        with tempfile.TemporaryDirectory() as out:
            result = recommend(data, Path(out), seconds=30)
            info = result['求解']
            self.assertTrue(info['有解'])
            self.assertEqual(info['目标模式'], '加权')
            self.assertEqual(info['解状态'], 'OPTIMAL')
            self.assertEqual(info['最优性'], '已证明最优')
            self.assertEqual(info['终止原因'], 'COMPLETED')
            self.assertEqual(info['确定性决胜完成'], '不适用')
            self.assertEqual(info['分层进度'], '1/1')
            self.assertFalse([s for s in info['阶段']
                              if str(s['目标']).startswith('确定性决胜')])
            self.assertEqual(list(Path(out).glob('solver_model_tie_*.pbtxt')), [])
            self.assertTrue(result['推荐']['复核通过'])
            self.assertEqual(result['推荐']['完成任务数'], 3)
            verify_schedule(data, result['推荐']['排程'])

    def test_same_input_same_model_without_tie_break(self):
        """决胜段撤销后：模型本身仍确定，但「排程逐条目复现」这句承诺作废。

        旧用例跑 mode='分层'，靠几十段决胜在同分方案里挑编号最小那张，再断言
        两次排程完全相同。分层已撤销，那条保证不再存在，也不许假装还在。
        这里守住仍然成立的部分：同一输入同一模型指纹、单阶段、零决胜产物、
        结果如实记「不适用」。
        """
        data = load_project(PROJECT, as_of='2026-10-23T18:00:00+08:00')
        data.tasks = data.tasks[:2]
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            result_a = recommend(data, Path(first), seconds=8)
            result_b = recommend(data, Path(second), seconds=8)
            for info, out in ((result_a['求解'], Path(first)), (result_b['求解'], Path(second))):
                self.assertEqual(info['确定性决胜完成'], '不适用')
                self.assertEqual(list(out.glob('solver_model_tie_*.pbtxt')), [])
                self.assertFalse([s for s in info['阶段']
                                  if str(s['目标']).startswith('确定性决胜')])
                self.assertEqual(len(info['阶段']), 1)
                self.assertTrue(info['权重'])
            self.assertEqual(result_a['求解']['解状态'], 'OPTIMAL')
            self.assertEqual(result_b['求解']['解状态'], 'OPTIMAL')
            self.assertEqual(result_a['求解']['模型产物指纹'],
                             result_b['求解']['模型产物指纹'])

    def test_layered_mode_and_dead_hooks_are_gone(self):
        """分层入口明确报错，custom_stages 死钩子不再挂在签名上。"""
        data = load_project(PROJECT, as_of='2026-10-23T18:00:00+08:00')
        data.tasks = data.tasks[:2]
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(ValueError, '已撤销'):
                recommend(data, Path(out), seconds=8, mode='分层')
        self.assertNotIn('custom_stages', inspect.signature(solve).parameters)
        self.assertNotIn('custom_stages', inspect.signature(recommend).parameters)
        self.assertNotIn('custom_stages', inspect.signature(
            __import__('product_planner').build_model).parameters)

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            load_project(PROJECT, overrides={'船租（元每艘每天）': -1})
        with self.assertRaises(ValueError):
            load_project(PROJECT, overrides={'不存在参数': 10})

    def test_approval_documents_inventory_and_audit(self):
        data=load_project(PROJECT,as_of='2026-10-23T18:00:00+08:00')
        data.tasks=data.tasks[:3]
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp);store=Store(out/'state.sqlite3')
            data=store.overlay(data)
            result=recommend(data,out/'run',seconds=8)
            run_id=store.save_run(result,data,out/'run')
            order=store.draft(run_id,key='draft-1',actor='制单员')
            self.assertEqual(store.draft(run_id,key='draft-1',actor='制单员'),order)
            oid=order['检修单号']
            with self.assertRaises(ValueError):store.approve(oid,PROJECT,key='self',actor='制单员')
            approved=store.approve(oid,PROJECT,key='approval-1',actor='审批员')
            self.assertEqual(approved['状态'],'已批准')
            self.assertIn('出海清单',store.documents(oid))
            self.assertIn('库存变化单',store.documents(oid))
            start=min(s['开始'] for s in result['推荐']['排程'])
            finish=max(s['完成'] for s in result['推荐']['排程'])
            store.execute(oid,'领料','issue-1',actor='执行员',at=data.stamp(start))
            rolled=store.overlay(load_project(PROJECT,as_of=data.stamp(start)))
            self.assertEqual(len(rolled.issued),3)
            self.assertEqual(len(rolled.fixed),3)
            store.execute(oid,'完成','complete-1',actor='执行员',at=data.stamp(finish))
            later=store.overlay(load_project(PROJECT,as_of=data.stamp(finish)))
            self.assertFalse({s['任务编号'] for s in result['推荐']['排程']} & {t['任务编号'] for t in later.tasks})
            self.assertTrue(store.audit()['审计链通过'])


if __name__ == '__main__':
    unittest.main()
