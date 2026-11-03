import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from solve_model_spec import solve_model_spec
try:
    from objective_templates import apply_objective_template
except ImportError:
    apply_objective_template = None


def fixture():
    # 小型数学对照例，用于隔离验证目标语义，不代表全场检修排程。
    tasks = [
        {'任务编号': '甲', '严重等级': '高', '候选': [{'变量编号':'甲', '完工分钟':8}]},
        {'任务编号': '乙', '严重等级': '低', '候选': [{'变量编号':'乙', '完工分钟':2}]},
        {'任务编号': '丙', '严重等级': '低', '候选': [{'变量编号':'丙', '完工分钟':3}]},
        {'任务编号': '丁', '严重等级': '低', '候选': [{'变量编号':'丁', '完工分钟':4}]},
    ]
    def expression(coeff):
        return {'constant':0,'terms':[{'variable':key,'coefficient':value} for key,value in coeff.items()]}
    cost = expression({'甲':100,'乙':40,'丙':20,'丁':30})
    spec = {'schema_version':'1.0','status':'ready','problem_statement':'四目标固定语义对照', 'data':{},
            'variables':[{'id':name,'type':'binary','lower_bound':0,'upper_bound':1,'unit':'1'} for name in ['甲','乙','丙','丁']],
            'objectives':[{'id':'旧目标','priority':1,'sense':'minimize','expression':cost}],
            'constraints':[{'id':'资源上限','kind':'linear_row','meaning':'有效资源总量不超过8',
                            'lhs':expression({'甲':8,'乙':2,'丙':3,'丁':4}),'sense':'<=','rhs':8}],
            'unresolved_data':[],'assumptions':[],'provenance':[]}
    return spec,tasks,cost


class ObjectiveTemplateTests(unittest.TestCase):
    def compile(self, strategy, **kwargs):
        self.assertIsNotNone(apply_objective_template, '尚无四目标确定性模板生成器')
        spec,tasks,cost=fixture()
        return apply_objective_template(spec,strategy,tasks,cost,horizon_minutes=10,minimum_completed=1,**kwargs)

    def selected(self, strategy):
        result=solve_model_spec(self.compile(strategy))
        self.assertEqual(result['状态'],'最优')
        self.assertTrue(result['分层复核通过'])
        return {k for k,v in result['变量值'].items() if k in {'甲','乙','丙','丁'} and v>.5}

    def test_risk_first_selects_high_severity(self):
        self.assertEqual(self.selected('高风险优先抢修'),{'甲'})

    def test_cost_first_selects_cheapest(self):
        self.assertEqual(self.selected('成本最低优先'),{'丙'})

    def test_shortest_means_last_completion_not_sum_of_duration(self):
        self.assertEqual(self.selected('最短完成优先'),{'乙'})

    def test_maximum_components_counts_tasks_not_turbines(self):
        self.assertEqual(self.selected('检修最多器件优先'),{'丙','丁'})

    def test_identical_input_produces_identical_model(self):
        a=self.compile('高风险优先抢修');b=self.compile('高风险优先抢修')
        self.assertEqual(json.dumps(a,ensure_ascii=False),json.dumps(b,ensure_ascii=False))

    def test_cost_without_service_contract_requires_clarification(self):
        self.assertIsNotNone(apply_objective_template)
        spec,tasks,cost=fixture()
        with self.assertRaisesRegex(ValueError,'服务'):
            apply_objective_template(spec,'成本最低优先',tasks,cost,horizon_minutes=10)

    def test_existing_hard_constraints_and_input_are_preserved(self):
        self.assertIsNotNone(apply_objective_template)
        spec,tasks,cost=fixture();original=copy.deepcopy(spec)
        result=apply_objective_template(spec,'成本最低优先',tasks,cost,horizon_minutes=10,minimum_completed=1)
        self.assertEqual(spec,original)
        self.assertIn(original['constraints'][0],result['constraints'])

    def test_required_task_remains_required_under_cost_strategy(self):
        model=self.compile('成本最低优先',required_tasks=['甲'])
        result=solve_model_spec(model)
        self.assertEqual(result['变量值']['甲'],1)

    def test_conflicting_required_tasks_are_not_silently_dropped(self):
        model=self.compile('检修最多器件优先',required_tasks=['甲','乙'])
        result=solve_model_spec(model)
        self.assertEqual(result['状态'],'不可行')

    def test_weighted_mode_builds_one_normalized_objective(self):
        custom={'总经济成本':0.6,'完成任务数':0.4,'最后完工时间':0,'完工时间和':0}
        model=self.compile('成本最低优先',objective_mode='加权',weights=custom)
        self.assertEqual(len(model['objectives']),1)
        self.assertEqual(model['objectives'][0]['sense'],'minimize')
        provenance=model['provenance'][-1]
        self.assertEqual(provenance['目标模式'],'加权')
        self.assertEqual(provenance['权重'],custom)
        self.assertEqual(set(provenance['标准化范围']),set(custom))

    def test_weighted_mode_allows_tradeoff_and_respects_minimum_service(self):
        custom={'总经济成本':0.6,'完成任务数':0.4,'最后完工时间':0,'完工时间和':0}
        model=self.compile('成本最低优先',objective_mode='加权',weights=custom)
        result=solve_model_spec(model)
        self.assertEqual(result['状态'],'最优')
        self.assertEqual(result['目标模式'],'加权')
        self.assertEqual(result['权重'],custom)
        self.assertGreaterEqual(sum(result['变量值'][key] for key in ['甲','乙','丙','丁']),1)
        self.assertEqual({key for key in ['甲','乙','丙','丁'] if result['变量值'][key]>.5},{'丙','丁'})

    def test_all_four_strategies_have_registered_weighted_defaults(self):
        for strategy in ['高风险优先抢修','成本最低优先','最短完成优先','检修最多器件优先']:
            with self.subTest(strategy=strategy):
                model=self.compile(strategy,objective_mode='加权')
                self.assertEqual(len(model['objectives']),1)
                self.assertTrue(model['provenance'][-1]['权重'])
                self.assertTrue(model['provenance'][-1]['标准化范围'])
                result=solve_model_spec(model)
                self.assertEqual(result['状态'],'最优')
                self.assertEqual(result['目标模式'],'加权')
                self.assertTrue(result['独立复核通过'])
                self.assertIsNone(result['分层复核通过'])

    def test_weighted_mode_rejects_negative_or_zero_weights(self):
        spec,tasks,cost=fixture()
        for weights in ({'总经济成本':-1},{'总经济成本':0,'完成任务数':0}):
            with self.subTest(weights=weights), self.assertRaisesRegex(ValueError,'权重'):
                apply_objective_template(spec,'成本最低优先',tasks,cost,horizon_minutes=10,
                                         minimum_completed=1,objective_mode='加权',weights=weights)

    def test_weighted_mode_requires_declared_metric_names(self):
        spec,tasks,cost=fixture()
        with self.assertRaisesRegex(ValueError,'指标'):
            apply_objective_template(spec,'成本最低优先',tasks,cost,horizon_minutes=10,
                                     minimum_completed=1,objective_mode='加权',weights={'未知指标':1})

    def test_weighted_custom_weights_must_list_every_strategy_metric(self):
        spec,tasks,cost=fixture()
        with self.assertRaisesRegex(ValueError,'全部指标'):
            apply_objective_template(spec,'成本最低优先',tasks,cost,horizon_minutes=10,
                                     minimum_completed=1,objective_mode='加权',
                                     weights={'总经济成本':0.6,'完成任务数':0.4})

    def test_lexicographic_mode_remains_default(self):
        model=self.compile('成本最低优先')
        self.assertGreater(len(model['objectives']),1)
        self.assertEqual(model['provenance'][-1]['目标模式'],'分层')


if __name__=='__main__':
    unittest.main()
