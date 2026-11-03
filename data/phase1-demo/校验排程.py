"""演示数据可行性审计。不是生产求解适配器，不证明成本最优。"""
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from fractions import Fraction
import math

from ortools.linear_solver import pywraplp

ROOT = Path(__file__).resolve().parent
DATA = json.loads((ROOT / '初版完整数据.json').read_text(encoding='utf-8'))
BASE = datetime.fromisoformat('2026-10-23T00:00:00+08:00')
END = 35 * 1440
PARTS = {c['部件编号']: c for c in DATA['部件库']}
WEATHER = DATA['每日海况预测']


def minute(value):
    return int((datetime.fromisoformat(value) - BASE).total_seconds() / 60)


def stamp(value):
    return (BASE + timedelta(minutes=value)).isoformat()


def make_candidates(task, now):
    part = PARTS[task['部件编号']]
    config = {r['参数名称']: r['参数值'] for r in DATA['系统参数']}
    def clock(value):
        hour, minute = map(int, value.split(':'))
        return hour * 60 + minute
    day_start = clock(config['每天维修开始时间'])
    day_end = min(clock(config['每天维修结束时间']),
                  day_start + int(config['每天最多维修小时数'] * 60))
    required = {state: Fraction(str(part[f"{task['严重等级']}严重度{state}维修工时（小时）"])) * 60
                for state in ['常态', '恶劣但安全']}
    if any(value <= 0 for value in required.values()):
        raise ValueError('维修工时必须大于零')
    release = max(now, minute(task['首次规划时间']))
    deadline = END if task['截止时间（不含）'] == '待确定' else minute(task['截止时间（不含）'])
    result = []
    for window in DATA['海事检修窗口']:
        lo, hi = minute(window['开始时间']), minute(window['结束时间（不含）'])
        for start in range(max(lo, (release + 59) // 60 * 60), min(hi, deadline), 60):
            day, hour = start // 1440, start % 1440 // 60
            if not day_start <= start % 1440 < day_end or WEATHER[day]['海况类别'] == '不可作业':
                continue
            remain, cur, work = Fraction(1), start, []
            while remain > 0 and cur < min(hi, deadline):
                day, within = cur // 1440, cur % 1440
                state = WEATHER[day]['海况类别']
                if state == '不可作业' or within >= day_end:
                    cur = (day + 1) * 1440
                    continue
                if within < day_start:
                    cur = day * 1440 + day_start
                    continue
                duration = min(day_end - within, math.ceil(remain * required[state]))
                work.append((cur, cur + duration))
                remain -= Fraction(duration) / required[state]
                cur += duration
            if remain > 0:
                continue
            finish, ready, transfer = cur, cur, int(config['风机间转移小时数'] * 60)
            while transfer > 0 and ready < hi:
                day = ready // 1440
                duration = min((day + 1) * 1440, hi) - ready
                if WEATHER[day]['海况类别'] != '不可作业':
                    duration = min(duration, transfer)
                    transfer -= duration
                ready += duration
            result.append({'任务编号': task['任务编号'], '窗口编号': window['窗口编号'],
                           '开始': start, '完成': finish, '下项最早开始': ready,
                           '作业段': work, '部件编号': task['部件编号']})
    return result


def solve(tasks, now, fixed):
    solver = pywraplp.Solver.CreateSolver('CBC')
    solver.SetTimeLimit(3000)
    options = []
    constraints = {}
    for task in tasks:
        candidates = [fixed[task['任务编号']]] if task['任务编号'] in fixed else make_candidates(task, now)
        locked = task['任务编号'] in fixed
        row = solver.RowConstraint(1 if locked else 0, 1, task['任务编号'])
        for candidate in candidates:
            var = solver.BoolVar(str(len(options)))
            row.SetCoefficient(var, 1)
            options.append((candidate, var))
    # 区间图最大重叠数<=3即能分配至3条线。每个任务区间包含后续6小时安全转移。
    # 末任务的转移保留至窗口结束即可，无须真的执行返回或无目的转移。
    events = sorted({c[k] for c, _ in options for k in ['开始', '下项最早开始']})
    for at in events:
        active = [(c, v) for c, v in options if c['开始'] <= at < c['下项最早开始']]
        if active:
            config = {r['参数名称']: r['参数值'] for r in DATA['系统参数']}
            row = solver.RowConstraint(0, min(config['最多作业线数'], config['人员总上限']), '')
            for _, var in active:
                row.SetCoefficient(var, 1)
    stock = {r['部件编号']: r['现有库存数量'] for r in DATA['库存快照']}
    movements = DATA['库存收发计划']
    times = sorted({c['开始'] for c, _ in options} | {minute(e['计划时间']) for e in movements})
    for part, initial in stock.items():
        relevant = [(c, v) for c, v in options if c['部件编号'] == part]
        for at in times:
            capacity = initial + sum(e['数量'] * (1 if e['收发方向'] == '补库' else -1)
                                     for e in movements if e['部件编号'] == part and minute(e['计划时间']) <= at)
            row = solver.RowConstraint(-solver.infinity(), capacity, '')
            for candidate, var in relevant:
                if candidate['开始'] <= at:
                    row.SetCoefficient(var, 1)
    # 审计用：先最大化高/中/低完成数，再尽早完成。并非产品总成本目标。
    # 总任务数44，权重10000/100/1有明确支配界；总时间扰动<0.044。
    weights = {'高': 10000, '中': 100, '低': 1}
    by_id = {t['任务编号']: t for t in tasks}
    solver.Minimize(solver.Sum(var * (-weights[by_id[c['任务编号']]['严重等级']]
                                     + c['完成'] / (END * 1000)) for c, var in options))
    status = solver.Solve()
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        raise RuntimeError(f'求解未返回可行解：状态码{status}，规划时点{stamp(now)}')
    selected = [c for c, v in options if v.solution_value() > .5]
    return selected, {'状态': '最优（仅审计完成数辅助目标）' if status == 0 else '可行，未证明辅助目标最优',
                      '候选数量': len(options), '求解耗时毫秒': solver.WallTime()}


def verify(schedule, tasks):
    config = {r['参数名称']: r['参数值'] for r in DATA['系统参数']}
    day_start = sum(int(n) * scale for n, scale in zip(config['每天维修开始时间'].split(':'), [60, 1]))
    day_end = min(sum(int(n) * scale for n, scale in zip(config['每天维修结束时间'].split(':'), [60, 1])),
                  day_start + int(config['每天最多维修小时数'] * 60))
    line_count = min(config['最多作业线数'], config['人员总上限'])
    by_id = {t['任务编号']: t for t in tasks}
    assert len(schedule) == len({s['任务编号'] for s in schedule})
    assert all(s['任务编号'] in by_id for s in schedule)
    used = defaultdict(int)
    for s in schedule:
        task = by_id[s['任务编号']]
        assert s['开始'] >= minute(task['首次规划时间'])
        deadline = END if task['截止时间（不含）'] == '待确定' else minute(task['截止时间（不含）'])
        assert s['完成'] <= deadline
        window = next(w for w in DATA['海事检修窗口'] if w['窗口编号'] == s['窗口编号'])
        assert minute(window['开始时间']) <= s['开始'] < s['完成'] <= minute(window['结束时间（不含）'])
        progress = 0
        for lo, hi in s['作业段']:
            day = lo // 1440
            assert WEATHER[day]['海况类别'] != '不可作业'
            assert day_start <= lo % 1440 < hi % 1440 <= day_end
            used[(s['任务编号'], day)] += hi - lo
            required = PARTS[task['部件编号']][f"{task['严重等级']}严重度{WEATHER[day]['海况类别']}维修工时（小时）"]
            progress += (hi - lo) / 60 / required
        assert progress >= 1 - 1e-9
    assert all(minutes <= config['每天最多维修小时数'] * 60 for minutes in used.values())
    for window in DATA['海事检修窗口']:
        free = [0] * line_count
        for s in sorted([s for s in schedule if s['窗口编号'] == window['窗口编号']], key=lambda s: s['开始']):
            line = next((i for i, at in enumerate(free) if at <= s['开始']), None)
            assert line is not None
            free[line] = s['下项最早开始']
            s['作业线'] = line + 1
        for line in range(1, line_count + 1):
            sequence = sorted([s for s in schedule if s['窗口编号'] == window['窗口编号']
                               and s['作业线'] == line], key=lambda s: s['开始'])
            for before, after in zip(sequence, sequence[1:]):
                assert before['完成'] <= after['开始']
                safe_minutes = 0
                for day in range(before['完成']//1440, after['开始']//1440+1):
                    if day < len(WEATHER) and WEATHER[day]['海况类别'] != '不可作业':
                        safe_minutes += max(0, min(after['开始'], (day+1)*1440)
                                            - max(before['完成'], day*1440))
                assert safe_minutes >= config['风机间转移小时数'] * 60
    for snap in DATA['库存快照']:
        part = snap['部件编号']
        ledger = [(minute(e['计划时间']), 0 if e['收发方向'] == '补库' else 1,
                   e['数量'] * (1 if e['收发方向'] == '补库' else -1))
                  for e in DATA['库存收发计划'] if e['部件编号'] == part]
        ledger += [(s['开始'], 1, -1) for s in schedule if s['部件编号'] == part]
        balance = snap['现有库存数量']
        for _, _, delta in sorted(ledger):
            balance += delta
            assert balance >= 0, part


def main():
    tasks = DATA['告警任务']
    previous, rounds, seen = [], [], set()
    # 初始规划+35次每日回放，不读取未来告警；开工任务锁定，未开工任务允许改排。
    for day in range(-1, 35):
        now = day * 1440 + 18 * 60
        known = [t for t in tasks if minute(t['首次规划时间']) <= now]
        fixed = {s['任务编号']: s for s in previous if s['开始'] <= now}
        if len(known) == len(seen):
            verify(previous, known)
            rounds.append({'规划时间': stamp(now), '已知任务数': len(known), '新增任务数': 0,
                           '结果': '复核已有可行计划；静态预测未变，无需重复求解'})
            continue
        schedule, info = solve(known, now, fixed)
        verify(schedule, known)
        assert all(next(s for s in schedule if s['任务编号'] == key)['作业线'] == value['作业线']
                   for key, value in fixed.items())
        rounds.append({'规划时间': stamp(now), '已知任务数': len(known), '新增任务数': len(known)-len(seen),
                       '锁定已开工任务数': len(fixed), **info})
        previous, seen = schedule, {t['任务编号'] for t in known}
        print(stamp(now), len(known), info, flush=True)
    verify(previous, tasks)
    output = []
    for s in sorted(previous, key=lambda s: s['开始']):
        task = next(t for t in tasks if t['任务编号'] == s['任务编号'])
        output.append({'任务编号': s['任务编号'], '风机编号': task['风机编号'], '部件编号': task['部件编号'],
                       '严重等级': task['严重等级'], '窗口编号': s['窗口编号'], '作业线编号': s['作业线'],
                       '开工时间': stamp(s['开始']), '完工时间': stamp(s['完成']),
                       '有效维修工时': sum(b-a for a,b in s['作业段'])/60,
                       '原截止时间': task['截止时间（不含）'],
                       '作业明细': '；'.join(stamp(a)+'至'+stamp(b) for a,b in s['作业段'])})
    with (ROOT / '审计可行排程.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(output[0]))
        writer.writeheader()
        writer.writerows(output)
    selected_ids = {s['任务编号'] for s in previous}
    leftovers = []
    for task in tasks:
        if task['任务编号'] in selected_ids:
            continue
        candidates = make_candidates(task, minute(task['首次规划时间']))
        if task['截止时间（不含）'] == '待确定':
            reason = '后续批准窗口未提供，截止时间未知；不能判为业务不可行'
        elif not candidates:
            reason = '在已知窗口、每日海况、接收时间和期限下，单项任务无可行开完工时段'
        else:
            reason = '有单项候选但本次审计方案未选入；可能受库存、作业线或优先级影响，未做不可行原因证明'
        leftovers.append({'任务编号': task['任务编号'], '严重等级': task['严重等级'],
                          '首次规划时间': task['首次规划时间'], '截止时间': task['截止时间（不含）'],
                          '单项候选数量': len(candidates), '未安排说明': reason})
    with (ROOT/'审计未安排任务.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(leftovers[0]))
        writer.writeheader()
        writer.writerows(leftovers)
    result = {'数据文件哈希': hashlib.sha256((ROOT/'初版完整数据.json').read_bytes()).hexdigest(),
              '求解器': 'OR-Tools CBC', '求解器版本': pywraplp.Solver.CreateSolver('CBC').SolverVersion(),
              '检查目的': '滚动数据可行性；未计算或证明总成本最优', '规划轮数': len(rounds),
              '最终已安排任务数': len(output), '最终未安排任务数': len(leftovers),
              '全部任务在五周内完成': len(output) == len(tasks), '未安排任务': leftovers,
              '待补后续截止窗口任务数': sum(t['截止时间（不含）']=='待确定' for t in tasks),
              '逐轮结果': rounds}
    (ROOT/'排程审计结果.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS: rolling audit; scheduled', len(output), 'unscheduled', len(leftovers), flush=True)


if __name__ == '__main__':
    main()
