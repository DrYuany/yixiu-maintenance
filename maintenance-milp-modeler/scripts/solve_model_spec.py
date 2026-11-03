"""Validated LP/MILP ModelSpec to an actual OR-Tools CBC solver."""
import argparse
import json
import math
import time
from pathlib import Path

from validate_model_spec import validate_model_spec


def solve_model_spec(spec, time_limit_seconds=60, tolerance=1e-6):
    errors = validate_model_spec(spec)
    if errors:
        return {'状态': '校验未通过', '错误': errors, '变量值': {}}
    if not math.isfinite(time_limit_seconds) or time_limit_seconds <= 0:
        return {'状态': '校验未通过', '错误': ['求解时限必须是有限正数'], '变量值': {}}
    if not math.isfinite(tolerance) or not 0 < tolerance <= 1e-3:
        return {'状态': '校验未通过', '错误': ['数值容差必须在0到0.001之间'], '变量值': {}}
    try:
        from ortools.linear_solver import pywraplp
        solver = pywraplp.Solver.CreateSolver('CBC')
    except ImportError:
        return {'状态': '求解器不可用', '变量值': {}}
    if solver is None:
        return {'状态': '求解器不可用', '变量值': {}}
    started = time.monotonic()
    variables = {}
    for item in spec['variables']:
        create = solver.NumVar if item['type'] == 'continuous' else solver.IntVar
        variables[item['id']] = create(item['lower_bound'], item['upper_bound'], item['id'])

    def number(value):
        return spec['data'][value['data']] if isinstance(value, dict) else value

    def coefficients(expression):
        result = {}
        for term in expression['terms']:
            key = term['variable']
            result[key] = result.get(key, 0) + number(term['coefficient'])
        return result

    for row in spec['constraints']:
        rhs = number(row['rhs']) - number(row['lhs']['constant'])
        lower = rhs if row['sense'] in ('>=', '==') else -solver.infinity()
        upper = rhs if row['sense'] in ('<=', '==') else solver.infinity()
        constraint = solver.RowConstraint(lower, upper, row['id'])
        for key, value in coefficients(row['lhs']).items():
            constraint.SetCoefficient(variables[key], value)
    objective_contract = next((item for item in reversed(spec['provenance'])
                               if isinstance(item, dict) and '目标模式' in item), {})
    result = {'求解器': 'OR-Tools CBC', '求解器版本': solver.SolverVersion(), '变量值': {},
              '各级目标': [], '独立复核通过': False,
              '目标模式': objective_contract.get('目标模式', '分层'),
              '权重': objective_contract.get('权重'),
              '标准化范围': objective_contract.get('标准化范围')}
    locks = []
    for stage in sorted(spec['objectives'], key=lambda o: o['priority']):
        remaining = time_limit_seconds - (time.monotonic() - started)
        if remaining <= 0:
            result['状态'] = '可行但分层未完成' if result['变量值'] else '未求得可行解'
            break
        solver.SetTimeLimit(max(1, int(remaining * 1000)))
        objective = solver.Objective()
        objective.Clear()
        coeff = coefficients(stage['expression'])
        scale = max((abs(v) for v in coeff.values()), default=0) or 1
        for key, value in coeff.items():
            objective.SetCoefficient(variables[key], value / scale)
        constant = number(stage['expression']['constant'])
        objective.SetOffset(constant / scale)
        objective.SetMinimization() if stage['sense'] == 'minimize' else objective.SetMaximization()
        status = solver.Solve()
        if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
            # 若锁定前级后异常，保留前级解但不冒称完整分层成功。
            result['状态'] = ('分层求解异常' if result['变量值'] else
                            '不可行' if status == pywraplp.Solver.INFEASIBLE else '未求得可行解')
            result['状态码'] = status
            break
        result['变量值'] = {key: var.solution_value() for key, var in variables.items()}
        value = constant + sum(c * result['变量值'][key] for key, c in coeff.items())
        bound = objective.BestBound() * scale
        result['各级目标'].append({'目标编号': stage['id'], '优先级': stage['priority'],
                                   '目标值': value, '最优界': bound,
                                   '已证明最优': status == pywraplp.Solver.OPTIMAL})
        if status != pywraplp.Solver.OPTIMAL:
            result['状态'] = '可行但未证明最优'
            break  # 前级未最优时禁止继续并宣称严格分层。
        normalized = (value - constant) / scale
        lock = solver.RowConstraint(normalized, normalized, '')
        for key, coefficient in coeff.items():
            lock.SetCoefficient(variables[key], coefficient / scale)
        locks.append((stage['id'], coeff, value-constant, scale))
        result['状态'] = '最优'
    if result['变量值']:
        values = result['变量值']
        violations = []
        for item in spec['variables']:
            value = values[item['id']]
            if not math.isfinite(value) or value < item['lower_bound'] - tolerance or value > item['upper_bound'] + tolerance:
                violations.append(item['id'])
            if item['type'] != 'continuous' and abs(value - round(value)) > tolerance:
                violations.append(item['id'])
        for row in spec['constraints']:
            lhs = number(row['lhs']['constant']) + sum(c * values[key] for key, c in coefficients(row['lhs']).items())
            rhs = number(row['rhs'])
            if ((row['sense'] == '<=' and lhs > rhs + tolerance) or
                (row['sense'] == '>=' and lhs < rhs - tolerance) or
                (row['sense'] == '==' and abs(lhs-rhs) > tolerance)):
                violations.append(row['id'])
        result['独立复核通过'] = not violations
        lock_errors = [name for name, coeff, target, scale in locks
                       if abs(sum(c*values[key] for key,c in coeff.items())-target) / scale > tolerance]
        result['目标优先级复核通过'] = not lock_errors
        result['分层复核通过'] = not lock_errors if result['目标模式'] == '分层' else None
        if lock_errors:
            violations.extend('前级目标锁定:'+name for name in lock_errors)
        if violations:
            result['状态'] = '结果复核失败'
            result['复核错误'] = violations
            result['独立复核通过'] = False
        result['数值容差'] = tolerance
        stages = {stage['id']: stage for stage in spec['objectives']}
        for recorded in result['各级目标']:
            expression = stages[recorded['目标编号']]['expression']
            recorded['最终解下目标值'] = number(expression['constant']) + sum(c*values[key] for key,c in coefficients(expression).items())
    result['总耗时秒'] = round(time.monotonic() - started, 3)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model_spec')
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--output')
    args = parser.parse_args()
    result = solve_model_spec(json.loads(Path(args.model_spec).read_text(encoding='utf-8')), args.seconds)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered, encoding='utf-8')
    print(rendered)
