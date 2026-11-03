"""固定版本的四目标生成器；不负责自然语言解析或生成业务可行候选。"""
import copy
import json
import math
from pathlib import Path

from validate_model_spec import validate_model_spec

REGISTRY = Path(__file__).resolve().parents[1] / 'references/四种目标模板.json'


def apply_objective_template(spec, strategy, tasks, cost_expression, *, horizon_minutes,
                             required_tasks=None, minimum_completed=None, version='2.0',
                             objective_mode='分层', weights=None):
    registry = json.loads(REGISTRY.read_text(encoding='utf-8'))
    if version != registry['模板版本']:
        raise ValueError('未知目标模板版本，不允许自动替换版本')
    template = next((t for t in registry['模板'] if t['模板编号'] == strategy), None)
    if template is None:
        raise ValueError('未知策略，需要澄清或扩展提案，不能自动猜目标')
    if objective_mode not in ('分层', '加权'):
        raise ValueError('目标模式只能是分层或加权')
    if type(horizon_minutes) not in (int, float) or not math.isfinite(horizon_minutes) or horizon_minutes <= 0:
        raise ValueError('时间域必须是有限正数，统一使用距同一规划基点的分钟')
    if not tasks:
        raise ValueError('没有待规划任务，无需生成四目标优化模型')
    if minimum_completed is not None and (type(minimum_completed) is not int or minimum_completed < 0):
        raise ValueError('服务最低完成数量必须是非负整数')
    required = sorted(set(required_tasks or []))
    if not required and not (minimum_completed is not None and minimum_completed >= 1):
        raise ValueError('服务要求未明确：必须提供共同必修任务集合或正的最低完成数量')
    model = copy.deepcopy(spec)
    declared = {v['id']: v for v in model['variables']}
    if '策略_最后完工分钟' in declared or any(c['id'].startswith('策略_') for c in model['constraints']):
        raise ValueError('模型已有策略模板内容，请基于未套模板的原模型重新生成')
    ids = [task['任务编号'] for task in tasks]
    if len(set(ids)) != len(ids) or not set(required) <= set(ids):
        raise ValueError('任务编号重复或必修任务不在本次任务集合')
    used = set()
    metrics = {name: [] for name in ['完成任务数','完工时间和','高严重完成数','中严重完成数',
                                    '低严重完成数','高严重完工时间和','中严重完工时间和','低严重完工时间和']}

    def expression(terms):
        return {'constant': 0, 'terms': [{'variable': key, 'coefficient': value} for key, value in terms]}

    def row(name, terms, sense, rhs, meaning):
        model['constraints'].append({'id': '策略_'+name, 'kind': 'linear_row', 'meaning': meaning,
                                     'lhs': expression(terms), 'sense': sense, 'rhs': rhs})

    model['variables'].append({'id': '策略_最后完工分钟', 'type': 'continuous',
                               'lower_bound': 0, 'upper_bound': horizon_minutes, 'unit': '分钟'})
    for task in sorted(tasks, key=lambda t: t['任务编号']):
        level = task['严重等级']
        if level not in ['高','中','低']:
            raise ValueError('严重等级必须为高、中、低，不能临时编造风险权重')
        selection = []
        for candidate in sorted(task['候选'], key=lambda c: c['变量编号']):
            key, finish = candidate['变量编号'], candidate['完工分钟']
            if key in used or key not in declared or declared[key]['type'] != 'binary':
                raise ValueError('每个候选必须绑定唯一的已声明二元变量，不能跨任务重复计数')
            if type(finish) not in (int,float) or not math.isfinite(finish) or not 0 <= finish <= horizon_minutes:
                raise ValueError('候选完工时间必须是时间域内的已知有限分钟，不能使用决策变量系数')
            used.add(key)
            selection.append((key,1))
            metrics['完成任务数'].append((key,1))
            metrics[level+'严重完成数'].append((key,1))
            metrics['完工时间和'].append((key,finish))
            metrics[level+'严重完工时间和'].append((key,finish))
            row('完工上界_'+key, [(key,finish),('策略_最后完工分钟',-1)], '<=', 0,
                '最后完工时刻不早于任一已选完整维修候选的完工时刻，无变量乘积或任意大M')
        row('任务选择_'+task['任务编号'], selection, '==' if task['任务编号'] in required else '<=', 1,
            '必修任务恰选一个完整维修候选，其他任务最多选择一个，不允许部件被重复计数')
    if minimum_completed is not None:
        row('共同服务下限', metrics['完成任务数'], '>=', minimum_completed, '所有策略共用相同最低完成任务数量')
    metrics['最后完工时间'] = [('策略_最后完工分钟',1)]
    objective_provenance = {'目标模板编号': strategy, '目标模板版本': version,
                                '目标模式': objective_mode,
                                '服务约定': {'必修任务': required, '最低完成数量': minimum_completed},
                                '计数口径': '部件维修任务数，非风机台数', '时间口径': '同一基点分钟'}
    model['objectives'] = []
    if objective_mode == '分层':
        if weights is not None:
            raise ValueError('分层模式不接收加权参数')
        for priority, stage in enumerate(template['分层目标'], 1):
            expr = copy.deepcopy(cost_expression) if stage['指标'] == '总经济成本' else expression(metrics[stage['指标']])
            model['objectives'].append({'id': stage['指标'], 'priority': priority,
                                        'sense': stage['方向'], 'expression': expr})
    else:
        declared_weights = template.get('加权默认值')
        active_weights = copy.deepcopy(declared_weights if weights is None else weights)
        if not isinstance(active_weights, dict) or not active_weights:
            raise ValueError('加权模式必须提供有效指标权重')
        if any(name not in declared_weights for name in active_weights):
            raise ValueError('加权权重包含当前策略未定义的指标')
        if set(active_weights) != set(declared_weights):
            raise ValueError('自定义权重必须列出当前策略的全部指标；不参与的指标请显式设为0')
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in active_weights.values()) or not any(value > 0 for value in active_weights.values()):
            raise ValueError('权重必须是有限非负数且至少一个大于0')

        variable_bounds = {item['id']: (item['lower_bound'], item['upper_bound']) for item in model['variables']}

        def fixed_number(value):
            if isinstance(value, dict):
                return model['data'][value['data']]
            return value

        def metric_expression(name):
            stage = next(item for item in template['分层目标'] if item['指标'] == name)
            if name == '总经济成本':
                return copy.deepcopy(cost_expression), stage['方向']
            return expression(metrics[name]), stage['方向']

        combined = {}
        ranges = {}
        contributing_metrics = 0
        for name, weight in sorted(active_weights.items()):
            if weight == 0:
                ranges[name] = {'权重': 0, '下界': None, '上界': None, '跨度': None}
                continue
            expr, direction = metric_expression(name)
            lower = upper = fixed_number(expr['constant'])
            resolved_terms = []
            for term in expr['terms']:
                var_id = term['variable']
                if var_id not in variable_bounds:
                    raise ValueError(f'指标“{name}”引用未声明变量：{var_id}')
                coefficient = fixed_number(term['coefficient'])
                var_lower, var_upper = variable_bounds[var_id]
                a, b = coefficient * var_lower, coefficient * var_upper
                lower += min(a, b)
                upper += max(a, b)
                resolved_terms.append((var_id, coefficient))
            span = upper - lower
            if not math.isfinite(span) or span < 0:
                raise ValueError(f'指标“{name}”无法从有限变量边界计算标准化尺度')
            ranges[name] = {'权重': weight, '下界': lower, '上界': upper, '跨度': span}
            if span <= 1e-12:
                continue
            sign = 1 if direction == 'minimize' else -1
            factor = weight * sign / span
            for var_id, coefficient in resolved_terms:
                combined[var_id] = combined.get(var_id, 0) + factor * coefficient
            contributing_metrics += 1
        if contributing_metrics == 0:
            raise ValueError('加权目标没有任何随决策变化的有效指标')
        model['objectives'].append({'id': '标准化加权总目标', 'priority': 1,
                                    'sense': 'minimize',
                                    'expression': expression(sorted(combined.items()))})
        objective_provenance['权重'] = active_weights
        objective_provenance['标准化范围'] = ranges
        objective_provenance['标准化规则'] = '按决策变量有限上下界推导指标最小值、最大值；各指标线性映射到0至1；最大化指标取反后合并为单一最小化目标'
    model['provenance'].append(objective_provenance)
    errors = validate_model_spec(model)
    if errors:
        raise ValueError('目标模板模型未通过校验：'+'；'.join(errors))
    return model
