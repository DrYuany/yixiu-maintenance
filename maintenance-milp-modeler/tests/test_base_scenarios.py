import csv
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from generate_base_scenarios import DEFAULT_CONFIG, generate, main, turbine_power


@unittest.skipUnless(DEFAULT_CONFIG.exists(), 'project base configuration is not bundled with the portable skill')
class BaseScenarioTests(unittest.TestCase):
    def test_reference_10mw_power_curve_boundaries(self):
        curve=json.loads(DEFAULT_CONFIG.read_text(encoding='utf-8'))['功率曲线']
        self.assertEqual(turbine_power(3.99,curve),0)
        self.assertAlmostEqual(turbine_power(4.0,curve),0)
        self.assertAlmostEqual(turbine_power(11.4,curve),10)
        self.assertEqual(turbine_power(25.0,curve),0)

    def test_generates_five_configured_bases_with_complete_hourly_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'bases'
            hashes=generate(DEFAULT_CONFIG,output)
            self.assertEqual(len(hashes),5)
            config=json.loads(DEFAULT_CONFIG.read_text(encoding='utf-8'))
            for scenario in config['场景']:
                folder=output/scenario['场景编号']
                with (folder/'海事检修窗口.csv').open(encoding='utf-8-sig',newline='') as stream:
                    windows=list(csv.DictReader(stream))
                with (folder/'每日海况预测.csv').open(encoding='utf-8-sig',newline='') as stream:
                    seas=list(csv.DictReader(stream))
                with (folder/'每小时电价风速功率.csv').open(encoding='utf-8-sig',newline='') as stream:
                    hourly=list(csv.DictReader(stream))
                snapshot=json.loads((folder/'场景配置快照.json').read_text(encoding='utf-8'))
                as_of=datetime.fromisoformat(config['规划开始日期']+'T18:00:00+08:00')
                observation_end=as_of+timedelta(days=90)
                first_hour=datetime.fromisoformat(hourly[0]['时间'])
                hourly_end=datetime.fromisoformat(hourly[-1]['时间'])+timedelta(hours=1)
                daily_end=datetime.fromisoformat(seas[-1]['日期']+'T00:00:00+08:00')+timedelta(days=1)
                self.assertLessEqual(first_hour,as_of)
                self.assertGreaterEqual(hourly_end,observation_end)
                self.assertGreaterEqual(daily_end,observation_end)
                self.assertTrue(any(
                    datetime.fromisoformat(row['开始日期']+'T00:00:00+08:00') >= as_of+timedelta(days=7)
                    and datetime.fromisoformat(row['结束日期（不含）']+'T00:00:00+08:00') <= observation_end
                    for row in windows
                ))
                self.assertEqual(len({row['窗口编号'] for row in windows}),len(windows))
                self.assertTrue(all(row['审批状态'] for row in windows))
                self.assertTrue(all(row['数据来源'] for row in windows))
                self.assertTrue(all(row['发布时间'] for row in windows))
                self.assertTrue(all(float(row['曲线计算出力（兆瓦）']) <= 10 for row in hourly))
                self.assertTrue(all(row['数据性质']=='合成测试数据' for row in hourly))
                self.assertEqual(snapshot['场景']['场景编号'],scenario['场景编号'])
                self.assertGreaterEqual(snapshot['规划天数'],91)
                self.assertEqual(len(snapshot['部件故障剩余出力系数']),9)

    def test_cli_reports_configured_horizon_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'bases'
            previous=list(sys.argv)
            sys.argv=['generate_base_scenarios.py','--config',str(DEFAULT_CONFIG),'--output',str(output)]
            try:
                captured=io.StringIO()
                with contextlib.redirect_stdout(captured):
                    main()
            finally:
                sys.argv=previous
            payload=json.loads(captured.getvalue())
            self.assertEqual(payload['每日海况行数'],91)
            self.assertEqual(payload['每小时数据行数'],2184)


if __name__=='__main__':
    unittest.main()
