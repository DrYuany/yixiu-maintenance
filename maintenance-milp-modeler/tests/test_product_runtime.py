"""Business acceptance tests; fixture inputs never mutate project data."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from product_data import load_project
from product_planner import recommend, verify_schedule
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

    def test_completed_optimal_runs_have_identical_schedule(self):
        data = load_project(PROJECT, as_of='2026-10-23T18:00:00+08:00')
        data.tasks = data.tasks[:2]
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            result_a = recommend(data, Path(first), seconds=8)
            result_b = recommend(data, Path(second), seconds=8)

        self.assertEqual(result_a['求解']['解状态'], 'OPTIMAL')
        self.assertEqual(result_b['求解']['解状态'], 'OPTIMAL')
        self.assertEqual(result_a['推荐']['排程'], result_b['推荐']['排程'])

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
