import importlib.util
import unittest
from pathlib import Path

path = Path(__file__).with_name('运行场景.py')
module = None
if path.exists():
    spec = importlib.util.spec_from_file_location('scenario_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


class CostTests(unittest.TestCase):
    def test_calendar_day_rent_uses_half_open_interval(self):
        self.assertIsNotNone(module, '尚无业务费用编译与场景运行入口')
        self.assertEqual(module.rental_days(23*60, 25*60), 2)
        self.assertEqual(module.rental_days(23*60, 24*60), 1)

    def test_outage_cost_includes_waiting_but_labor_does_not(self):
        self.assertIsNotNone(module)
        candidate = {'开始': 8*60, '完成': 26*60, '作业段': [(8*60, 10*60)]}
        forecast = [{'预测电价（元每兆瓦时）': 400, '单台风机潜在功率（兆瓦）': 6} for _ in range(48)]
        loss, labor = module.variable_cost(candidate, forecast, 150, 1)
        self.assertEqual(loss, 43200)
        self.assertEqual(labor, 300)


if __name__ == '__main__':
    unittest.main()
