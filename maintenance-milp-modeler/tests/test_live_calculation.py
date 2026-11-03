import copy
import importlib.util
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
PROJECT = SKILL.parent
sys.path.insert(0, str(SKILL / 'scripts'))
try:
    from solve_model_spec import solve_model_spec
except ImportError:
    solve_model_spec = None


def load_audit():
    spec = importlib.util.spec_from_file_location('maintenance_audit', PROJECT / 'data/phase1-demo/校验排程.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def small_spec():
    return {'schema_version': '1.0', 'status': 'ready', 'problem_statement': '真实求解器回归测试',
            'data': {}, 'variables': [
                {'id': '叶片', 'type': 'binary', 'lower_bound': 0, 'upper_bound': 1, 'unit': '项'},
                {'id': '轴承', 'type': 'binary', 'lower_bound': 0, 'upper_bound': 1, 'unit': '项'}],
            'objectives': [{'id': '成本', 'priority': 1, 'sense': 'minimize',
                            'expression': {'constant': 5, 'terms': [
                                {'variable': '叶片', 'coefficient': 1200},
                                {'variable': '轴承', 'coefficient': 900}]}}],
            'constraints': [{'id': '至少一个', 'kind': 'linear_row', 'meaning': '至少完成一项',
                             'lhs': {'constant': 0, 'terms': [
                                 {'variable': '叶片', 'coefficient': 1},
                                 {'variable': '轴承', 'coefficient': 1}]}, 'sense': '>=', 'rhs': 1}],
            'unresolved_data': [], 'assumptions': [], 'provenance': []}


@unittest.skipUnless((PROJECT / 'data/phase1-demo/校验排程.py').exists(), '需要项目中的演示数据；技能安装副本仅运行可移植求解测试')
class CandidateParameterTests(unittest.TestCase):
    def setUp(self):
        self.audit = load_audit()
        self.task = copy.deepcopy(self.audit.DATA['告警任务'][0])

    def set_parameter(self, name, value):
        next(r for r in self.audit.DATA['系统参数'] if r['参数名称'] == name)['参数值'] = value

    def test_eight_hour_day_never_works_after_1600(self):
        self.set_parameter('每天维修结束时间', '16:00')
        self.set_parameter('每天最多维修小时数', 8)
        candidates = self.audit.make_candidates(self.task, -360)
        self.assertTrue(candidates)
        self.assertTrue(all(end % 1440 <= 960 for c in candidates for _, end in c['作业段']))

    def test_component_adverse_hours_are_not_hardcoded_multiplier(self):
        self.audit.PARTS['部件01']['高严重度常态维修工时（小时）'] = 2
        self.audit.PARTS['部件01']['高严重度恶劣但安全维修工时（小时）'] = 4
        candidates = self.audit.make_candidates(self.task, 1440 + 8 * 60)
        candidate = next(c for c in candidates if c['开始'] == 1440 + 8 * 60)
        self.assertEqual(candidate['完成'] - candidate['开始'], 240)

    def test_transfer_uses_configured_four_hours(self):
        self.set_parameter('风机间转移小时数', 4)
        self.audit.PARTS['部件01']['高严重度常态维修工时（小时）'] = 2
        candidates = self.audit.make_candidates(self.task, -360)
        self.assertEqual(candidates[0]['下项最早开始'] - candidates[0]['完成'], 240)


class SolverAdapterTests(unittest.TestCase):
    def test_real_solver_minimizes_cost_and_keeps_constant(self):
        self.assertIsNotNone(solve_model_spec, 'Skill缺少ModelSpec真实求解入口')
        result = solve_model_spec(small_spec())
        self.assertEqual(result['状态'], '最优')
        self.assertEqual(result['变量值']['轴承'], 1)
        self.assertAlmostEqual(result['各级目标'][0]['目标值'], 905)
        self.assertTrue(result['独立复核通过'])

    def test_lexicographic_priority_cannot_be_bought_by_cost(self):
        self.assertIsNotNone(solve_model_spec)
        spec = small_spec()
        spec['objectives'].insert(0, {'id': '服务', 'priority': 1, 'sense': 'maximize',
                                     'expression': {'constant': 0, 'terms': [{'variable': '叶片', 'coefficient': 1}]}})
        spec['objectives'][1]['priority'] = 2
        result = solve_model_spec(spec)
        self.assertEqual(result['变量值']['叶片'], 1)
        self.assertEqual(result['变量值']['轴承'], 0)
        self.assertEqual(len(result['各级目标']), 2)

    def test_true_infeasibility_is_returned_without_fake_solution(self):
        self.assertIsNotNone(solve_model_spec)
        spec = small_spec()
        spec['constraints'][0]['rhs'] = 3
        result = solve_model_spec(spec)
        self.assertEqual(result['状态'], '不可行')
        self.assertEqual(result['变量值'], {})

    def test_invalid_spec_is_blocked_before_solver(self):
        self.assertIsNotNone(solve_model_spec)
        spec = small_spec()
        spec['unresolved_data'] = ['库存未知']
        result = solve_model_spec(spec)
        self.assertEqual(result['状态'], '校验未通过')
        self.assertNotIn('求解器版本', result)

    def test_small_priority_coefficients_are_not_erased_by_lock_tolerance(self):
        self.assertIsNotNone(solve_model_spec)
        spec = small_spec()
        spec['objectives'].insert(0, {'id': '小尺度服务', 'priority': 1, 'sense': 'maximize',
                                     'expression': {'constant': 0, 'terms': [{'variable': '叶片', 'coefficient': 1e-9}]}})
        spec['objectives'][1]['priority'] = 2
        result = solve_model_spec(spec)
        self.assertEqual(result['变量值']['叶片'], 1)
        self.assertTrue(result['分层复核通过'])

    def test_malformed_reference_returns_validation_errors_instead_of_crashing(self):
        self.assertIsNotNone(solve_model_spec)
        spec = small_spec()
        spec['objectives'][0]['expression']['terms'][0]['coefficient'] = {'data': []}
        result = solve_model_spec(spec)
        self.assertEqual(result['状态'], '校验未通过')


if __name__ == '__main__':
    unittest.main()
