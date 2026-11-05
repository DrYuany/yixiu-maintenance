"""Executable finite-domain scheduling compiler, baselines and independent accounting.

CP-SAT solves bounded integer scheduling with linear cost; stored model.pbtxt is the
actual solver model, deliberately not mislabelled as canonical ModelSpec JSON.
"""
import csv
import hashlib
import html
import json
import math
import os
import time
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from ortools.sat.python import cp_model
from model_artifact import build_model_artifact, export_model_file, write_model_artifact
from solve_budget import resolve as resolve_budget

MONEY = 1000000  # micro-yuan; all costs are reported in yuan
STRATEGIES = ('成本最低优先','高风险优先抢修','最短完成优先','检修最多器件优先')

# 阶段数没有硬上限，也不该有：它是两段拼出来的
#   业务阶段 = 目标模板 references/四种目标模板.json 声明的顺序（按指标去重后），
#              当前四套模板分别 4/8/4/6 段，加 partial 前置最多 8 段；
#   决胜阶段 = 每 60 个选择变量一段，随任务数×候选数×作业线数增长，可以几十段。
# 控制手段不是"限条数"而是"限总时限"：所有阶段共用同一个预算（见 solve_budget），
# 但按剩余段数均分——前一段花超了，后一段仍有一份自己的时间；全部分完还不够，
# 就以 TIME_LIMIT 收工并如实记录，不会假装最优。
# 成本阶段额外允许参数总表里的相对差距（默认 1%），整数阶段仍要求证明最优。


def _worker_count():
    """求解线程数。默认 min(8, CPU)：多线程实测比单线程快两个数量级。

    单线程（MWA_SOLVER_WORKERS=1）留给需要"逐字段复现"的归档场景：
    并行搜索在时间被截断时可能取到不同的中间解；正常跑完的阶段不受影响。
    """
    default = min(8, os.cpu_count() or 1)
    try:
        value = int(os.environ.get('MWA_SOLVER_WORKERS', default))
    except ValueError:
        return default
    return max(1, min(32, value))


WORKERS = _worker_count()


def clock(text):
    h, m = map(int, text.split(':'))
    if not 0 <= h <= 23 or not 0 <= m < 60:
        raise ValueError('非法每日作业时间')
    return h*60+m


def work_end(data, day):
    return min(clock(data.params['每天维修结束时间']),
               clock(data.params['每天维修开始时间']) + int(data.weather[day]['可用小时']*60))


def candidates(data, task):
    part = data.parts[task['部件编号']]
    durations = {s:Fraction(str(part[f"{task['严重等级']}严重度{s}维修工时（小时）"]))*60
                 for s in ('常态','恶劣但安全')}
    deadline = task['截止分钟'] if task['截止分钟'] is not None else data.end
    release = max(data.now, task['接收分钟'], task['发生分钟'], 0)
    # 生成层规则参数（第三期）：候选开工粒度、是否允许跨天暂停、是否允许夜间转移
    step = int(data.params.get('候选开工粒度', 60))
    allow_multi_day = data.params.get('跨天暂停', '是') not in ('否', '不允许', '禁止', False)
    allow_night_transfer = data.params.get('允许夜间转移', '是') not in ('否', '不允许', False)
    result = []
    for window in data.windows:
        lo, hi = window['开始'], min(window['结束'], deadline)
        for start in range(max(lo, (release+step-1)//step*step), hi, step):
            day = start//1440
            if not clock(data.params['每天维修开始时间']) <= start%1440 < work_end(data, day):
                continue
            if data.weather[day]['海况类别']=='不可作业':
                continue
            remain, cur, work = Fraction(1), start, []
            while remain > 0 and cur < hi:
                day = cur//1440
                state = data.weather[day]['海况类别']
                day_lo = day*1440 + clock(data.params['每天维修开始时间'])
                day_hi = min(day*1440+work_end(data,day), hi)
                if state=='不可作业' or cur>=day_hi:
                    cur=(day+1)*1440
                    continue
                cur=max(cur,day_lo)
                if cur>=day_hi:
                    continue
                duration=min(day_hi-cur, math.ceil(remain*durations[state]))
                work.append([cur,cur+duration])
                remain-=Fraction(duration)/durations[state]
                cur+=duration
            if remain>0:
                continue
            if not allow_multi_day and len({a//1440 for a,b in work})>1:
                continue
            ready, transfer = cur, int(data.params['风机间转移小时数']*60)
            while transfer>0 and ready<window['结束']:
                day=ready//1440
                if data.weather[day]['海况类别']=='不可作业':
                    ready=min((day+1)*1440, window['结束'])
                    continue
                seg_hi=min((day+1)*1440, window['结束'])
                if not allow_night_transfer:
                    w_lo=day*1440+clock(data.params['每天维修开始时间'])
                    w_hi=min(day*1440+work_end(data,day), window['结束'])
                    if ready<w_lo:
                        ready=w_lo
                        continue
                    if ready>=w_hi:
                        ready=min((day+1)*1440+clock(data.params['每天维修开始时间']), window['结束'])
                        continue
                    seg_hi=min(seg_hi,w_hi)
                duration=min(seg_hi-ready,transfer)
                transfer-=duration
                ready+=duration
            result.append({'任务编号':task['任务编号'],'风机编号':task['风机编号'],
                '部件编号':task['部件编号'],'部件名称':part['部件名称'],'严重等级':task['严重等级'],
                '窗口编号':window['编号'],'开始':start,'完成':cur,'下项最早开始':ready,'作业段':work})
    return result


def stock_ok(data, schedule):
    moments={0,data.end} | {m['时间'] for m in data.movements} | {s['开始'] for s in schedule}
    return all(data.availability(p,t)-sum(s['部件编号']==p and s['开始']<=t and s['任务编号'] not in data.issued for s in schedule)>=0
               for p in data.stocks for t in moments)


def compatible(data, schedule, candidate):
    for s in schedule:
        if s['任务编号']==candidate['任务编号']:
            return False
        if s['窗口编号']==candidate['窗口编号'] and s['作业线']==candidate['作业线']:
            if not (s['下项最早开始']<=candidate['开始'] or candidate['下项最早开始']<=s['开始']):
                return False
        if s['风机编号']==candidate['风机编号'] and s['开始']<candidate['完成'] and candidate['开始']<s['完成']:
            return False
    return stock_ok(data, schedule+[candidate])


def heuristic(data, options, kind, severity_order='高中低'):
    chosen = [dict(s) for s in data.fixed]
    fixed_ids = {s['任务编号'] for s in chosen}
    lines=min(data.params['最多作业线数'],data.params['人员总上限'])
    tasks = sorted(data.tasks, key=lambda t:(severity_order.index(t['严重等级']),t['发生分钟'],t['任务编号']))
    if kind=='严重性优先':
        sequence=[c for t in tasks if t['任务编号'] not in fixed_ids
                  for c in sorted(options[t['任务编号']],key=lambda c:(c['完成'],c['开始']))]
    else:
        # Sort actual feasible work intervals, not entire windows by arbitrary daily mean.
        def wind(c):
            hours=sum(b-a for a,b in c['作业段'])
            return sum(data.hourly[h]['风速']*max(0,min(b,(h+1)*60)-max(a,h*60))
                       for a,b in c['作业段'] for h in range(a//60,(b+59)//60))/hours
        sequence=sorted([c for t in data.tasks if t['任务编号'] not in fixed_ids for c in options[t['任务编号']]],
                        key=lambda c:(wind(c),c['开始'],c['任务编号']))
    used={s['任务编号'] for s in chosen}
    for c in sequence:
        if c['任务编号'] in used:
            continue
        for line in range(1,lines+1):
            proposal=dict(c,作业线=line)
            if compatible(data,chosen,proposal):
                chosen.append(proposal);used.add(c['任务编号']);break
    return chosen


def verify_schedule(data, schedule):
    tasks={t['任务编号']:t for t in data.tasks}
    if len(schedule)!=len({s['任务编号'] for s in schedule}):
        raise ValueError('重复维修任务')
    for s in schedule:
        t=tasks[s['任务编号']]
        if not 1<=s['作业线']<=min(data.params['最多作业线数'],data.params['人员总上限']):
            raise ValueError('作业线超限')
        fixed=any(s==old for old in data.fixed)
        if s['开始'] < max(t['接收分钟'],t['发生分钟'],0 if fixed else data.now):
            raise ValueError('使用未来告警或倒排已过去时间')
        w=next(w for w in data.windows if w['编号']==s['窗口编号'])
        if not w['开始']<=s['开始']<s['完成']<=w['结束']:
            raise ValueError('任务越出窗口')
        if t['截止分钟'] is not None and s['完成']>t['截止分钟']:
            raise ValueError('违反维修期限')
        progress=0.0;per_day=defaultdict(int)
        for a,b in s['作业段']:
            day=a//1440; state=data.weather[day]['海况类别']
            if state=='不可作业' or not day*1440+clock(data.params['每天维修开始时间'])<=a<b<=day*1440+work_end(data,day):
                raise ValueError('不安全作业或超每日作业时段')
            p=data.parts[t['部件编号']]
            progress+=(b-a)/60/p[f"{t['严重等级']}严重度{state}维修工时（小时）"]
            per_day[day]+=b-a
        if not 1-1e-8<=progress<=1.02 or not s['作业段'] or s['作业段'][0][0]!=s['开始'] or s['作业段'][-1][1]!=s['完成']:
            raise ValueError('维修进度/起止时间不符')
        if any(v>data.params['每天最多维修小时数']*60 for v in per_day.values()):
            raise ValueError('每日维修工时超限')
    for i,a in enumerate(schedule):
        for b in schedule[i+1:]:
            if a['风机编号']==b['风机编号'] and a['开始']<b['完成'] and b['开始']<a['完成']:
                raise ValueError('同机同时维修冲突')
            if (a['窗口编号'],a['作业线'])!=(b['窗口编号'],b['作业线']):
                continue
            before,after=sorted((a,b),key=lambda s:s['开始'])
            safe=sum(max(0,min(after['开始'],(day+1)*1440)-max(before['完成'],day*1440))
                     for day in range(before['完成']//1440,after['开始']//1440+1)
                     if data.weather[day]['海况类别']!='不可作业')
            if before['完成']>after['开始'] or safe<data.params['风机间转移小时数']*60:
                raise ValueError('同线重叠或转场时间不足')
    for fixed in data.fixed:
        if fixed not in schedule:
            raise ValueError('已批准任务发生改变')
    if not stock_ok(data,schedule):
        raise ValueError('库存不足')
    return True


def account(data, schedule):
    """Independent interval integration: merge outage effects per turbine, never sum faults."""
    verify_schedule(data,schedule)
    by_id={s['任务编号']:s for s in schedule}
    groups=defaultdict(list)
    for t in data.tasks: groups[t['风机编号']].append(t)
    loss=0.0
    begin=max(0,data.now)
    for tasks in groups.values():
        points={begin,data.end} | set(range((begin//60+1)*60,data.end,60))
        for t in tasks:
            points.add(max(begin,t['发生分钟']))
            if t['任务编号'] in by_id:
                points.update(max(begin,by_id[t['任务编号']][k]) for k in ('开始','完成'))
        pts=sorted(p for p in points if begin<=p<=data.end)
        for a,b in zip(pts,pts[1:]):
            reductions=[]
            for t in tasks:
                s=by_id.get(t['任务编号'])
                if t['发生分钟']>a or (s and s['完成']<=a): continue
                reductions.append(1 if s and s['开始']<=a<s['完成'] else 1-t['剩余出力'])
            loss+=data.revenue(a,b)*max(reductions,default=0)
    rental_days=0
    for key in {(s['窗口编号'],s['作业线']) for s in schedule}:
        group=[s for s in schedule if (s['窗口编号'],s['作业线'])==key]
        rental_days+=(max(s['完成'] for s in group)-1)//1440-min(s['开始'] for s in group)//1440+1
    labor=sum(sum(b-a for a,b in s['作业段'])/60 for s in schedule)*data.params['人工单价（元每人时）']
    material=sum(data.parts[s['部件编号']]['维修包成本（元每套）'] for s in schedule)
    other=sum(data.parts[s['部件编号']].get('其他费用（元每任务）',0) for s in schedule)
    rent=rental_days*data.params['船租（元每艘每天）']
    missing=[{'任务编号':t['任务编号'],'风机编号':t['风机编号'],'部件名称':data.parts[t['部件编号']]['部件名称'],
              '严重等级':t['严重等级'],'期限状态':'待补后续窗口' if t['截止分钟'] is None else data.stamp(t['截止分钟'])}
             for t in data.tasks if t['任务编号'] not in by_id]
    return {'完成任务数':len(schedule),'待修总数':len(data.tasks),'未安排':missing,
            '最晚完工':data.stamp(max(s['完成'] for s in schedule)) if schedule else None,
            '船舶艘天':rental_days,'船租':round(rent,2),'人工':round(labor,2),'备件':round(material,2),
            '其他费用':round(other,2),'发电损失':round(loss,2),'总成本':round(rent+labor+material+other+loss,2),
            '现金支出':round(rent+labor+material+other,2),'复核通过':True,'排程':sorted(schedule,key=lambda s:(s['开始'],s['作业线']))}


def build_model(data, options, partial=False, strategy='成本最低优先', mode='加权', weights=None, limits=None, extra_constraints=None, custom_stages=None):
    m=cp_model.CpModel();choices=[];task_vars={};cost_terms=[];cash_terms=[];constant=0
    # 小一修（小一修/）要按「人工费 / 备件费 / 船舶艘天」三个口径设上限和权重，
    # 这三个量不进目标函数，只是登记进 metrics 供注入口闭包和目标加权取用。
    # 纯增量：不加任何约束，不改已有任何一项成本的算法。
    labor_terms=[];spare_terms=[];rent_days=[]
    line_count=min(data.params['最多作业线数'],data.params['人员总上限'])
    line_intervals=defaultdict(list); turbine_intervals=defaultdict(list)
    tasks={t['任务编号']:t for t in data.tasks}
    multi={k for k in {t['风机编号'] for t in data.tasks} if sum(t['风机编号']==k for t in data.tasks)>1}
    fixed_by_id={s['任务编号']:s for s in data.fixed}
    for task in data.tasks:
        tid=task['任务编号'];task_vars[tid]=[]
        baseline=data.revenue(max(0,data.now,task['发生分钟']),data.end)*(1-task['剩余出力'])
        if task['风机编号'] not in multi: constant+=round(baseline*MONEY)
        part=data.parts[task['部件编号']]
        pool=[fixed_by_id[tid]] if tid in fixed_by_id else options[tid]
        for ci,c in enumerate(pool):
            for line in ([fixed_by_id[tid]['作业线']] if tid in fixed_by_id else range(1,line_count+1)):
                x=m.NewBoolVar(f'x_{tid}_{ci}_{line}')
                item=dict(c,作业线=line);choices.append((item,x));task_vars[tid].append((item,x))
                interval=m.NewOptionalIntervalVar(c['开始'],c['下项最早开始']-c['开始'],c['下项最早开始'],x,f'line_{x.Name()}')
                line_intervals[(line,c['窗口编号'])].append(interval)
                turbine_intervals[task['风机编号']].append(m.NewOptionalIntervalVar(c['开始'],c['完成']-c['开始'],c['完成'],x,f'turbine_{x.Name()}'))
                labor=sum(b-a for a,b in c['作业段'])/60*data.params['人工单价（元每人时）']
                spare=part['维修包成本（元每套）']+part.get('其他费用（元每任务）',0)
                direct=labor+spare
                cash_terms.append(round(direct*MONEY)*x)
                labor_terms.append(round(labor*MONEY)*x);spare_terms.append(round(spare*MONEY)*x)
                change=0
                if task['风机编号'] not in multi:
                    change=(data.revenue(max(data.now,0,task['发生分钟']),max(data.now,c['开始']))*(1-task['剩余出力'])
                            +data.revenue(max(data.now,c['开始']),c['完成'])-baseline)
                cost_terms.append(round((direct+change)*MONEY)*x)
        m.Add(sum(x for c,x in task_vars[tid]) == 1 if not partial or tid in fixed_by_id else sum(x for c,x in task_vars[tid]) <= 1)
    for ints in line_intervals.values(): m.AddNoOverlap(ints)
    for ints in turbine_intervals.values(): m.AddNoOverlap(ints)
    moments=sorted({mvt['时间'] for mvt in data.movements}|{c['开始'] for c,x in choices})
    for part in data.parts:
        freeze = data.part_freeze.get(part)
        for at in moments:
            cap = data.availability(part,at)
            if freeze is not None and at < freeze:
                # 备件还没到，这段时间该部件一件都不能开工
                cap = 0
            m.Add(sum(x for c,x in choices if c['部件编号']==part and c['开始']<=at and c['任务编号'] not in data.issued)<=cap)
    for window in data.windows:
        for line in range(1,line_count+1):
            group=[(c,x) for c,x in choices if c['窗口编号']==window['编号'] and c['作业线']==line]
            if not group: continue
            for day in range(window['开始']//1440,window['结束']//1440):
                pre=[x for c,x in group if c['开始']<(day+1)*1440]
                post=[x for c,x in group if c['完成']>day*1440]
                a=m.NewBoolVar(f'prefix_{line}_{day}');b=m.NewBoolVar(f'suffix_{line}_{day}');y=m.NewBoolVar(f'rent_{line}_{day}')
                m.AddMaxEquality(a,pre or [0]);m.AddMaxEquality(b,post or [0])
                m.Add(y<=a);m.Add(y<=b);m.Add(y>=a+b-1)
                term=round(data.params['船租（元每艘每天）']*MONEY)*y
                cost_terms.append(term);cash_terms.append(term)
                rent_days.append(y)
    for turbine in multi:
        ts=[t for t in data.tasks if t['风机编号']==turbine]
        group=[(c,x) for c,x in choices if c['风机编号']==turbine]
        points=sorted({data.now,data.end}|set(range((data.now//60+1)*60,data.end,60))|
                      {max(data.now,c[k]) for c,x in group for k in ('开始','完成')})
        for a,b in zip(points,points[1:]):
            reductions=[0]
            for t in ts:
                if t['发生分钟']>a: continue
                done=sum(x for c,x in task_vars[t['任务编号']] if c['完成']<=a)
                reductions.append(round((1-t['剩余出力'])*10000)*(1-done))
            active=sum(x for c,x in group if c['开始']<=a<c['完成'])
            reductions.append(10000*active)
            z=m.NewIntVar(0,10000,f'loss_{turbine}_{a}');m.AddMaxEquality(z,reductions)
            cost_terms.append(round(data.revenue(a,b)*100)*z)
    cost=constant+sum(cost_terms);cash=sum(cash_terms)
    count=sum(x for c,x in choices)
    finish=m.NewIntVar(0,data.end,'最后完工时间')
    m.AddMaxEquality(finish,[c['完成']*x for c,x in choices] or [0])
    metrics={'总经济成本':cost,'现金支出':cash,'完成任务数':count,'最后完工时间':finish,
             '完工时间和':sum(c['完成']*x for c,x in choices)}
    # 小一修登记的三个口径（同上，纯增量）。空列表用常数 0 兜住，避免 CP-SAT 拿到空和式。
    metrics['人工费']=sum(labor_terms) if labor_terms else 0
    metrics['备件费']=sum(spare_terms) if spare_terms else 0
    metrics['船舶艘天']=sum(rent_days) if rent_days else 0
    for level in ('高','中','低'):
        metrics[level+'严重完成数']=sum(x for c,x in choices if c['严重等级']==level)
        metrics[level+'严重完工时间和']=sum(c['完成']*x for c,x in choices if c['严重等级']==level)
    limits=limits or {}
    allowed={'现金预算','最低完成数','必修任务','最大总成本'}
    if set(limits)-allowed: raise ValueError('未知业务约束，需扩展编译器')
    if '现金预算' in limits: m.Add(cash<=round(limits['现金预算']*MONEY))
    if '最大总成本' in limits: m.Add(cost<=round(limits['最大总成本']*MONEY))
    if '最低完成数' in limits: m.Add(count>=int(limits['最低完成数']))
    for tid in limits.get('必修任务',[]):
        if tid not in task_vars: raise ValueError('必修任务不存在于当前告警快照')
        m.Add(sum(x for c,x in task_vars[tid])==1)
    # DSL 附加约束：调用方给的闭包，在 choices/metrics 构建后执行。
    # 签名 (m, choices, task_vars, metrics) -> 错误列表；默认 None 时行为不变。
    if extra_constraints is not None:
        hook_errors = extra_constraints(m, choices, task_vars, metrics)
        if hook_errors:
            raise ValueError('；'.join(str(error) for error in hook_errors))
    if custom_stages is not None:
        # DSL 自定义目标：闭包返回分层阶段列表 [(名称, 方向, 表达式), ...]，绕过四模板。
        stages = list(custom_stages(m, choices, metrics))
    else:
        registry=json.loads((Path(__file__).resolve().parents[1]/'references/四种目标模板.json').read_text(encoding='utf-8'))
        template=next(t for t in registry['模板'] if t['模板编号']==strategy)
        declared=[]
        if partial:
            # 允许少修时，"先多修几个"本身要排在最前面，否则部分排程会挑最便宜的几项。
            declared.extend([(level+'严重完成数','maximize',metrics[level+'严重完成数']) for level in ('高','中','低')])
        if mode=='分层':
            # 按模板声明的优先级逐条优化，一条不改、一条不删。
            # 曾经的写法是 `if partial or not 指标.endswith('完成数')`——本意是躲开
            # 上面那三段的重复，实际效果是：非部分排程时把"高严重完成数"这类
            # 最核心的阶段整段删掉，高风险优先抢修退化成"高危早完工优先"，
            # 而模板里明明写着先修高危。去重必须按指标名做，不能按名字后缀猜。
            declared.extend((s['指标'],s['方向'],metrics[s['指标']]) for s in template['分层目标'])
        elif mode=='加权':
            weights=weights if weights is not None else template['加权默认值']
            if not weights or set(weights)-set(metrics) or any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in weights.values()) or not any(weights.values()):
                raise ValueError('权重需非负有限、至少一项为正，并使用已登记指标')
            terms=[]
            # Bound-based normalization coefficients, rounded with explicitly recorded precision.
            ranges={'总经济成本':max(1,abs(constant)+sum(abs(round(data.revenue(c['开始'],c['完成'])*MONEY)) for c,x in choices)+data.end//1440*line_count*round(data.params['船租（元每艘每天）']*MONEY)),
                    '现金支出':max(1,data.end//1440*line_count*round(data.params['船租（元每艘每天）']*MONEY)+sum(data.parts[t['部件编号']]['维修包成本（元每套）']*MONEY for t in data.tasks)),
                    '完成任务数':max(1,len(tasks)),'最后完工时间':data.end,'完工时间和':max(1,len(tasks))*data.end}
            for level in ('高','中','低'):
                ranges[level+'严重完成数']=max(1,sum(t['严重等级']==level for t in data.tasks))
                ranges[level+'严重完工时间和']=ranges[level+'严重完成数']*data.end
            # 小一修三个新口径的归一化分母。不补这三个键，加权模式一旦带上
            # 人工/备件/租船天权重就会 KeyError——ranges 必须和 metrics 同keyspace。
            ranges['人工费']=ranges['现金支出']
            ranges['备件费']=max(1,sum(data.parts[t['部件编号']]['维修包成本（元每套）']*MONEY
                                       for t in data.tasks))
            ranges['船舶艘天']=max(1,data.end//1440*line_count)
            for name,w in weights.items():
                coeff=round(1000000000000000*w/ranges[name])
                if w and coeff==0: raise ValueError('权重经整数缩放后为零，请提高加权精度或调整权重')
                terms.append((-1 if '完成数' in name or name=='完成任务数' else 1)*coeff*metrics[name])
            declared.append(('标准化加权目标','minimize',sum(terms)))
        else: raise ValueError('目标模式只能为分层或加权')
        # 同一指标只优化一次：先出现的那条优先（partial 前置的三段排在模板之前）。
        stages=[];seen=set()
        for item in declared:
            if item[0] in seen: continue
            seen.add(item[0]);stages.append(item)
    return m,choices,stages,cost


def solve(data, options, output, seconds=None, strategy='成本最低优先', mode='加权', weights=None, limits=None, partial=False, hint=None, on_solver=None, cancelled=None, model_fingerprint=None, extra_constraints=None, custom_stages=None, gap_limit=None):
    # 时限与容差只有一个来源，见 solve_budget：参数总表 → 调用方显式 → 环境上限。
    # 以前这里还藏着一次 min(seconds, MWA_SOLVE_SECONDS)，公告却按 300 秒说，
    # 于是在界面上同时存在 300 / 60 / 30 三个数。
    budget = resolve_budget(data.params, requested_seconds=seconds, requested_gap=gap_limit)
    seconds = budget['seconds']; gap_limit = budget['gapLimit']
    model,choices,stages,cost=build_model(data,options,partial,strategy,mode,weights,limits,extra_constraints,custom_stages)
    if hint:
        keys={(c['任务编号'],c['开始'],c['作业线']) for c in hint}
        for c,x in choices: model.AddHint(x,int((c['任务编号'],c['开始'],c['作业线']) in keys))
    error=model.Validate()
    if error: raise ValueError('模型校验失败：'+error)
    prototype = model.Proto()
    serializer = getattr(prototype, 'SerializeToString', None)
    if callable(serializer):
        try:
            prototype_bytes = serializer(deterministic=True)
        except TypeError:
            prototype_bytes = serializer()
    else:
        prototype_bytes = str(prototype).encode('utf-8')
    artifact_fingerprint = model_fingerprint or hashlib.sha256(prototype_bytes).hexdigest()
    solver_parameters = {'num_search_workers':WORKERS,'random_seed':17,
                         'max_time_in_seconds':seconds,'relative_gap_limit':gap_limit,
                         'gap_scope':'仅成本阶段，其余阶段要求证明最优'}
    artifact = build_model_artifact(
        data=data,
        options=options,
        model=model,
        choices=choices,
        stages=stages,
        strategy=strategy,
        mode=mode,
        weights=weights,
        limits=limits,
        partial=partial,
        solver_parameters=solver_parameters,
        model_fingerprint=artifact_fingerprint,
    )
    artifact = write_model_artifact(artifact, output, model)
    solver=cp_model.CpSolver();solver.parameters.num_search_workers=WORKERS;solver.parameters.random_seed=17
    if on_solver: on_solver(solver)
    # 上一版排程只做搜索起点（build_model 之后已用 AddHint 喂过），不做结果。
    #
    # 这里曾经写成 selected=fallback：约束把模型逼到不可行时，旧方案会被原样
    # 当成"有解"返回，解状态 FEASIBLE、来源 HEURISTIC_FALLBACK。上层的降级
    # 判断是 chosen is None，于是永远降不了级——"只修前 8 个"这类约束被吞掉，
    # 用户看到 11/13 却以为约束生效了。兜底改到 recommend 末尾，且必须标注。
    started=time.monotonic();logs=[];selected=None
    last_cost=None
    termination_reason='COMPLETED';business_complete=False;tie_complete=False;last_status=None

    def _rehint():
        """把当前解交给下一阶段当起点。

        分层求解每阶段会加一条“上一目标 == 最优值”。这条约束把可行域压成
        一层薄壳，下一阶段若从零开始搜，等于重新解一遍原问题——实测 12 项
        任务第一阶段 1.2 秒最优，第二阶段却烧掉 299 秒毫无进展。
        当前解本来就满足新加的那条等式，直接喂回去即可。
        """
        if selected is None:
            return
        keys={(c['任务编号'],c['开始'],c['作业线']) for c in selected}
        model.ClearHints()
        for c,x in choices:
            model.AddHint(x,int((c['任务编号'],c['开始'],c['作业线']) in keys))

    # 阶段计划可见化：跑了几段、跳了几段、为什么跳。
    # 只做"可证明"的跳过——没有依据就不跳，宁可多算一段也不许悄悄换目标。
    stage_upper={'完成任务数':len(data.tasks)}
    for level in ('高','中','低'):
        stage_upper[level+'严重完成数']=sum(t['严重等级']==level for t in data.tasks)
    plan=[];done_metrics=set();skipped=0;business_logs=[]
    executed=0;degraded=[];tolerant=[];max_gap=0.0
    total_stages=len(stages)
    for index,(name,sense,expression) in enumerate(stages):
        if cancelled and cancelled():
            termination_reason='USER_CANCELLED';break
        skip=None
        if name in done_metrics:
            skip='同一指标已在前面优化过，重复跑不会改变结果'
        elif name in stage_upper and stage_upper[name]==0:
            skip='当前没有该类别的任务，该阶段没有可优化内容'
        if skip:
            skipped+=1;plan.append({'目标':name,'结果':'跳过','原因':skip});continue
        done_metrics.add(name)
        remaining=seconds-(time.monotonic()-started)
        if remaining<=0:
            plan.append({'目标':name,'结果':'未执行','原因':'总时限已用完'})
            termination_reason='TIME_LIMIT';break
        # 剩余预算按"还没跑的段数"均分，第一段不许把后面几段的钱一次花光。
        # 以前每段都拿 remaining，第一段一超时，后面 3 段全部连跑都没跑——
        # 界面上写着"分层求解"，实际只优化了成本一项。
        stages_left=max(1,total_stages-index-skipped)
        stage_limit=max(0.5,min(remaining,remaining/stages_left))
        if executed==0:
            # 第一段是主目标（成本），给它一个下限：总预算的 40% 或均分值，
            # 取大的。主目标没证明最优，后面几段优化得再好也撑不起结论。
            stage_limit=max(stage_limit,min(remaining,max(0.5,seconds*0.4)))
        plan.append({'目标':name,'结果':'执行','分配秒':round(stage_limit,2)})
        solver.parameters.max_time_in_seconds=stage_limit
        # 容差只给成本阶段：钱是连续量，1% 以内的差距落在建模误差里；
        # 完成数是整数，必须证明最优才能锁成下一段的等式，否则"分层"名不副实。
        solver.parameters.relative_gap_limit=gap_limit if name=='总经济成本' else 0.0
        if sense=='maximize': model.Maximize(expression)
        else: model.Minimize(expression)
        stage_model_name = ('diagnostic' if partial else 'solver') + f'_model_stage_{len(logs)+1}.pbtxt'
        export_model_file(model, output/stage_model_name)
        status=solver.Solve(model);last_status=status
        log={'目标':name,'方向':sense,'状态':solver.StatusName(status),'耗时秒':solver.WallTime(),
             '分配秒':round(stage_limit,2),'模型文件':stage_model_name}
        logs.append(log);business_logs.append(log)
        if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):
            if status==cp_model.UNKNOWN: termination_reason='TIME_LIMIT'
            break
        executed+=1
        selected=[c for c,x in choices if solver.Value(x)]
        last_cost=solver.Value(cost)/MONEY
        val=solver.ObjectiveValue();bound=solver.BestObjectiveBound()
        # 状态已是 OPTIMAL 时差距按定义为 0；不去用浮点界反推，
        # 否则会出现"已证明最优、相对差距 0.94%"这种自相矛盾的行。
        stage_gap=0.0 if status==cp_model.OPTIMAL else abs(val-bound)/max(1,abs(val))
        max_gap=max(max_gap,stage_gap)
        # 容差内停止 = 成本阶段差距不超过参数总表写的 1%，可以当作"够好了"往下走；
        # 其余情况（含其他阶段的 FEASIBLE）都记为未证明，最终结论一并如实交代。
        tolerated = status!=cp_model.OPTIMAL and name=='总经济成本' and stage_gap<=gap_limit+1e-9
        quality = '已证明最优' if status==cp_model.OPTIMAL else ('容差内最优' if tolerated else '未证明最优')
        log.update({'目标值':val/MONEY if name=='总经济成本' else val,'界':bound/MONEY if name=='总经济成本' else bound,
                    '相对差距':round(stage_gap,6),'最优性':quality})
        if tolerated: tolerant.append(name)
        elif status!=cp_model.OPTIMAL: degraded.append(name)
        model.Add(expression==round(val))
        _rehint()
    planned=len(stages)-skipped
    # 分层"跑完"分两档：严格全部证明最优，和成本阶段在容差内收敛。
    business_complete=(len(business_logs)==planned
                       and all(r['状态']=='OPTIMAL' or r.get('最优性')=='容差内最优' for r in business_logs))
    strictly_optimal=(len(business_logs)==planned and all(r['状态']=='OPTIMAL' for r in business_logs))
    if business_complete:
        ordered=sorted(choices,key=lambda item:(item[0]['任务编号'],item[0]['开始'],item[0]['作业线']))
        chunks=[ordered[index:index+60] for index in range(0,len(ordered),60)]
        for index,chunk in enumerate(chunks,1):
            if cancelled and cancelled():
                termination_reason='USER_CANCELLED';break
            remaining=seconds-(time.monotonic()-started)
            if remaining<=0:
                termination_reason='TIME_LIMIT';break
            expression=sum((1 << offset)*variable for offset,(_,variable) in enumerate(reversed(chunk)))
            model.Minimize(expression)
            tie_limit=max(0.5,min(remaining,remaining/max(1,len(chunks)-index+1)))
            solver.parameters.max_time_in_seconds=tie_limit
            solver.parameters.relative_gap_limit=0.0
            status=solver.Solve(model);last_status=status
            stage_model_name = ('diagnostic' if partial else 'solver') + f'_model_tie_{index}.pbtxt'
            export_model_file(model, output/stage_model_name)
            log={'目标':f'确定性决胜{index}','方向':'minimize','状态':solver.StatusName(status),'耗时秒':solver.WallTime(),
                 '分配秒':round(tie_limit,2),'模型文件':stage_model_name}
            logs.append(log)
            # 决胜段没证到最优 ≠ 整场时间用尽。决胜段是按"剩余段数均分"给时限的，
            # 某一段跑满自己那 7 秒很常见，整场 180 秒才用 14 秒时说"时限用尽"
            # 就是假的。所以只看整场还剩多少，不看这一段分到的小份。
            overall_left=seconds-(time.monotonic()-started)
            tie_exhausted=overall_left<=max(1.0,seconds*0.02)
            if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):
                if status==cp_model.UNKNOWN:
                    termination_reason='TIME_LIMIT' if tie_exhausted else 'TIE_NOT_PROVEN'
                break
            selected=[c for c,x in choices if solver.Value(x)]
            last_cost=solver.Value(cost)/MONEY
            value=round(solver.ObjectiveValue());bound=solver.BestObjectiveBound()
            log.update({'目标值':value,'界':bound,
                        '相对差距':0.0 if status==cp_model.OPTIMAL else round(abs(value-bound)/max(1,abs(value)),6)})
            if status!=cp_model.OPTIMAL:
                termination_reason='TIME_LIMIT' if tie_exhausted else 'TIE_NOT_PROVEN'
                break
            model.Add(expression==value)
            _rehint()
        else:
            tie_complete=True
    if cancelled and cancelled(): termination_reason='USER_CANCELLED'
    # 有阶段在分到的时限里没证明最优：不是整场超时，也不能叫 COMPLETED。
    # 单独给一个原因，版本对比和模型问答读到它就知道该说"未证明"。
    if termination_reason=='COMPLETED' and degraded:
        termination_reason='STAGE_TIME_LIMIT'
    if selected is None:
        solve_status='INFEASIBLE' if last_status==cp_model.INFEASIBLE else 'UNKNOWN'
    else:
        solve_status='OPTIMAL' if strictly_optimal and tie_complete and termination_reason=='COMPLETED' else 'FEASIBLE'
    # 最优性一说清楚，别让"解状态 FEASIBLE"背两种意思：
    #   已证明最优 / 容差内最优（成本阶段差距 ≤ 参数总表允许值）/ 未证明最优
    if solve_status=='OPTIMAL':
        quality='已证明最优'
    elif selected is not None and tolerant and not degraded and len(business_logs)==planned:
        quality=f'容差内最优（成本相对差距 {max_gap:.1%}）'
    else:
        quality='未证明最优'
    return selected, {'求解器':'OR-Tools CP-SAT','模型形式':'有界整数线性目标及可线性化调度约束',
        '求解器参数':solver_parameters,
        '有解':selected is not None,'解状态':solve_status,'终止原因':termination_reason,
        '目标模式':mode,'最优性':quality,
        # 预算与容差：公告、参数总表、版本对比都读这一份，不再各算各的。
        '预算':{'seconds':seconds,'gapLimit':gap_limit,
                'secondsSource':budget['secondsSource'],'gapSource':budget['gapSource'],
                'tableSeconds':budget['tableSeconds'],'envCap':budget['envCap'],
                'requestedSeconds':budget['requestedSeconds']},
        '分层进度':f'{len(business_logs)}/{planned}' if planned else '0/0',
        '计划阶段数':planned,'执行阶段数':len(business_logs),
        '容差内阶段':tolerant,'未达最优阶段':degraded,'最大相对差距':round(max_gap,6),
        # 来源只描述"这份排程是谁给的"。selected 为空就是没解出来，
        # 不再用旧方案的来源字段去冒充。
        '方案来源':'CP_SAT' if selected is not None else None,
        '分层全部最优':strictly_optimal and tie_complete and termination_reason=='COMPLETED',
        '确定性决胜完成':tie_complete,
        '阶段计划':plan,'跳过阶段数':skipped,
        '阶段':logs,'总耗时秒':time.monotonic()-started,'模型成本':last_cost,'诊断模型':partial,
        '模型产物文件':artifact['artifactFile'],'模型产物指纹':artifact_fingerprint}


def recommend(data, output, seconds=None, strategy='成本最低优先', mode='加权', weights=None, limits=None, on_solver=None, cancelled=None, model_fingerprint=None, extra_constraints=None, custom_stages=None, partial=False, dsl=None, gap_limit=None):
    if strategy not in STRATEGIES: raise ValueError('未知策略')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if (output/'结果.json').exists(): raise ValueError('输出目录已有结果，请使用新目录保留历史版本')
    # 已批准检修单锁定的任务：从候选池里精确取出那一行当固定项，
    # 既不重新选时间、也不重复选一次，但仍然占作业线和备件。
    already_started=[]
    for task in list(data.tasks):
        lock=data.locked.get(task['任务编号'])
        if not lock: continue
        if lock['开始']<data.now:
            already_started.append(task['任务编号']);data.tasks.remove(task);continue
        matches=[c for c in candidates(data,task)
                 if c['开始']==lock['开始'] and c['窗口编号']==lock['窗口编号']]
        if not matches:
            raise ValueError(
                f"任务{task['任务编号']}已在检修单里排定{data.stamp(lock['开始'])}，"
                "但这个时间已经不满足当前海况、窗口或工时条件；"
                "请先取消这张检修单，再重新规划。")
        data.fixed.append(dict(matches[0],作业线=lock['作业线']))
    if already_started:data.meta['已开工不重排']=already_started
    options={t['任务编号']:candidates(data,t) for t in data.tasks}
    baselines={name:heuristic(data,options,name) for name in ('严重性优先','低风速优先')}
    if not data.tasks:
        budget=resolve_budget(getattr(data,'params',None),requested_seconds=seconds,requested_gap=gap_limit)
        chosen=[];info={'有解':True,'状态':'无待修告警','解状态':'OPTIMAL','终止原因':'COMPLETED','阶段':[],
                        '最优性':'已证明最优',
                        '预算':{'seconds':budget['seconds'],'gapLimit':budget['gapLimit'],
                                'secondsSource':budget['secondsSource'],'gapSource':budget['gapSource'],
                                'tableSeconds':budget['tableSeconds'],'envCap':budget['envCap'],
                                'requestedSeconds':budget['requestedSeconds']},
                        '分层进度':'0/0','计划阶段数':0,'执行阶段数':0,'容差内阶段':[],'未达最优阶段':[],
                        '诊断模型':False,'模型成本':0,'分层全部最优':True,'确定性决胜完成':True,
                        '求解器参数':{'num_search_workers':WORKERS,'random_seed':17,
                                      'max_time_in_seconds':budget['seconds'],
                                      'relative_gap_limit':budget['gapLimit'],
                                      'gap_scope':'仅成本阶段，其余阶段要求证明最优'}}
    else:
        best=max(baselines.values(),key=len)
        def _run(allow_partial):
            return solve(data,options,output,seconds,strategy,mode,weights,limits,
                         partial=allow_partial,hint=best,on_solver=on_solver,cancelled=cancelled,
                         model_fingerprint=model_fingerprint,extra_constraints=extra_constraints,
                         custom_stages=custom_stages,gap_limit=gap_limit)
        if partial:
            # 调用方已判定"允许少修"（DSL 里出现 exclude 这类选择类约束）。
            # 完整模式把每个任务都强制排入，跟"排除某几个"直接矛盾，跑也白跑。
            chosen,info=_run(True)
        else:
            chosen,info=_run(False)
            # 完整排程做不出来时才放宽为部分排程。
            # 除了”已证明不可行”，还要收”时限内既没找到可行解也没排除可行”的情况：
            # 加了业务约束后，不可行性的证明本身会变慢，只等证明会把明明存在的部分方案也一起埋掉。
            if chosen is None and info['阶段'] and info['阶段'][-1]['状态'] in ('INFEASIBLE','UNKNOWN'):
                formal=info
                chosen,info=_run(True)
                info['正式模型']=formal
                if formal['阶段'] and formal['阶段'][-1]['状态']=='UNKNOWN':
                    info['部分排程原因']=(
                        '完整排程在时限内既没有找到可行解、也没有证明不可行，'
                        '已放宽为部分排程；这不代表无解。'
                    )
        if chosen is None:
            # 完整与部分都没解出来，才回退启发式基线。必须留痕：来源标
            # HEURISTIC_FALLBACK 并写明原因，不能让它看起来像求解结果。
            # 解状态不得写 FEASIBLE——FEASIBLE 意味着"CP-SAT 找到了可行解"，
            # 而这份基线不是 CP-SAT 解。写成 UNKNOWN，和来源字段各司其职。
            chosen=list(best)
            info['方案来源']='HEURISTIC_FALLBACK'
            info['解状态']='UNKNOWN'
            info['兜底原因']=(
                '完整排程与部分排程都没有在时限内找到可行解，'
                '下面是未考虑本次约束的启发式基线；不代表无解。'
            )
    report={'版本':'3.0','数据':data.meta,'策略':strategy,'目标模式':mode,'自定义权重':weights,
            '约束覆盖':limits or {},'求解':info,'窗口':[{**w,'开始日期':data.stamp(w['开始']),
            '结束时间不含':data.stamp(w['结束'])} for w in data.windows], '人工基准':{}}
    if chosen is not None:
        report['推荐']=account(data,chosen)
        if info['模型成本'] is not None and abs(info['模型成本']-report['推荐']['总成本'])>max(.1,len(data.tasks)*.1):
            raise ValueError('求解目标与独立费用复算不符')
        for row in report['推荐']['未安排']:
            row['原因']='无符合时间/海况/期限的候选' if not options[row['任务编号']] else '本次资源/目标竞争未选入；不是单项不可行证明'
        for name,schedule in baselines.items():
            b=account(data,schedule)
            comparable={s['任务编号'] for s in chosen}=={s['任务编号'] for s in schedule}
            delta=round(b['总成本']-report['推荐']['总成本'],2)
            b.update({'同任务可比':comparable,'费用差额':delta,'节省金额':delta if comparable else None,
                      '节省比例':delta/b['总成本'] if comparable and b['总成本'] else None})
            report['人工基准'][name]=b
    # 意图对账：甲方说的话，产物里必须找得到证据。
    #
    # 放在写盘之前，让它进 结果.json 和版本的 solver 摘要；否则"复核通过"
    # 只是数学自洽，跟"是不是你要的那份"无关（2026-10-30 事故）。
    # 没有方案时不谈"对账"：那是"没解出来"，不是"做错了"。
    if dsl and chosen is not None:
        from dsl_core.reconcile import reconcile
        report['求解']['意图对账']=reconcile(
            dsl,report,data=data,
            overrides=(data.meta or {}).get('参数覆盖') or {},
            context={'策略':strategy,'目标模式':mode,'自定义权重':weights},
        )
    from product_report import write_report
    write_report(data,report,output)
    return report
