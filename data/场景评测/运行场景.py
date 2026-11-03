"""真实数据子集的成本与约束场景评测；用户意图由本轮Codex人工解释，不是自动NL解析器。"""
import copy
import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
SKILL = PROJECT / 'maintenance-milp-modeler'
sys.path.insert(0, str(SKILL / 'scripts'))
from solve_model_spec import solve_model_spec
from validate_model_spec import validate_model_spec


def rental_days(start, end):
    return 0 if end <= start else (end - 1) // 1440 - start // 1440 + 1


def variable_cost(candidate, forecast, labor_price, price_multiplier):
    loss = 0
    for hour in range(candidate['开始'] // 60, (candidate['完成'] + 59) // 60):
        overlap = max(0, min(candidate['完成'], (hour + 1) * 60) - max(candidate['开始'], hour * 60))
        row = forecast[hour]
        loss += overlap / 60 * row['预测电价（元每兆瓦时）'] * price_multiplier * row['单台风机潜在功率（兆瓦）']
    labor = sum(b-a for a,b in candidate['作业段']) / 60 * labor_price
    return loss, labor


def load_audit():
    path = PROJECT / 'data/phase1-demo/校验排程.py'
    spec = importlib.util.spec_from_file_location('business_audit', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(scenario):
    audit = load_audit()
    audit.DATA = copy.deepcopy(audit.DATA)
    audit.PARTS = {p['部件编号']: p for p in audit.DATA['部件库']}
    audit.WEATHER = audit.DATA['每日海况预测']
    config = {r['参数名称']: r['参数值'] for r in audit.DATA['系统参数']}
    snapshots = {r['部件编号']: r for r in audit.DATA['库存快照']}
    for movement in audit.DATA['库存收发计划']:
        if movement['流水状态'] != '已确认计划':
            raise ValueError('当前评测仅支持明确确认且未计入快照的计划流水；其他状态需先对账')
        if movement['计划时间'] <= snapshots[movement['部件编号']]['库存统计时间']:
            raise ValueError('快照已包含或早于快照的流水不能重复累计')
    for key, value in scenario.get('作业覆盖', {}).items():
        config[key] = value
        next(r for r in audit.DATA['系统参数'] if r['参数名称'] == key)['参数值'] = value
    for movement in audit.DATA['库存收发计划']:
        if movement['收发方向'] == '补库':
            movement['计划时间'] = audit.stamp(audit.minute(movement['计划时间']) + scenario.get('到货延迟小时', 0)*60)
    known_ids = scenario.get('任务编号', ['任务001','任务002','任务005','任务006','任务007','任务008'])
    tasks = [t for t in audit.DATA['告警任务'] if t['任务编号'] in known_ids]
    now = audit.minute(scenario.get('规划时间', '2026-10-22T18:00:00+08:00'))
    assert len(tasks) == len(known_ids)
    assert all(audit.minute(t['首次规划时间']) <= now for t in tasks), '禁止使用未来告警'
    assert len({t['风机编号'] for t in tasks}) == len(tasks), '本评测模板暂不支持同机多故障'
    spec = {'schema_version':'1.0', 'status':'ready', 'problem_statement':scenario['用户提问'],
            'data':{}, 'variables':[], 'objectives':[], 'constraints':[], 'unresolved_data':[],
            'assumptions':['评测已收到真实演示任务子集，全部作为必修；不是全场最优结论',
                           '本模板不支持同机多故障、先后关系、自动采购和跨窗口维修'],
            'provenance':[{'source':'第五版合成演示数据；本次显式场景覆盖独立副本'}]}
    def var(name, kind='binary'):
        spec['variables'].append({'id':name,'type':kind,'lower_bound':0,'upper_bound':1 if kind=='binary' else 0,'unit':'1'})
        return name
    def row(name, terms, sense, rhs, meaning):
        spec['constraints'].append({'id':name,'kind':'linear_row','meaning':meaning,
                                    'lhs':{'constant':0,'terms':[{'variable':k,'coefficient':v} for k,v in terms if v]},
                                    'sense':sense,'rhs':rhs})
    choices = []
    lines = min(config['最多作业线数'], config['人员总上限'])
    costs, cash_terms = [], []
    for task in tasks:
        group = []
        for ci, candidate in enumerate(audit.make_candidates(task, now)):
            loss, labor = variable_cost(candidate, audit.DATA['每小时电价与发电预测'],
                                        scenario.get('人工单价',150), scenario.get('电价倍率',1))
            material = audit.PARTS[task['部件编号']]['维修包成本（元每套）']
            for line in range(lines):
                name = var(f"任务选择_{task['任务编号']}_{ci}_{line+1}")
                item = dict(candidate, 变量编号=name, 作业线=line+1, 发电损失=loss, 人工=labor, 材料=material)
                choices.append(item)
                group.append((name,1))
                costs.append((name,loss+labor+material))
                cash_terms.append((name,labor+material))
        row('必修_'+task['任务编号'],group,'==',1,'场景指定的六项任务全部必须维修，不能为省钱全不修')
    if not choices:
        var('无候选占位', 'continuous')
    events = sorted({c[k] for c in choices for k in ['开始','下项最早开始']})
    for line in range(1,lines+1):
        for at in events:
            terms = [(c['变量编号'],1) for c in choices if c['作业线']==line and c['开始']<=at<c['下项最早开始']]
            if terms:
                row(f'线占用_{line}_{at}',terms,'<=',1,'同线任务不能重叠，包含下一风机前的安全转移')
    moments = sorted({c['开始'] for c in choices} | {audit.minute(e['计划时间']) for e in audit.DATA['库存收发计划']})
    for stock in audit.DATA['库存快照']:
        part = stock['部件编号']
        for at in moments:
            capacity = stock['现有库存数量'] + sum(e['数量']*(1 if e['收发方向']=='补库' else -1)
                         for e in audit.DATA['库存收发计划'] if e['部件编号']==part and audit.minute(e['计划时间'])<=at)
            terms = [(c['变量编号'],1) for c in choices if c['部件编号']==part and c['开始']<=at]
            row(f'库存_{part}_{at}',terms,'<=',capacity-scenario.get('安全库存',0),'保留其他已批准出库与确认到货，并检查库存下限')
    # 用前缀/后缀OR和AND构件精确表达同窗口首末任务之间的连续租船日。
    rent_terms = []
    for window in audit.DATA['海事检修窗口']:
        lo,hi = audit.minute(window['开始时间']), audit.minute(window['结束时间（不含）'])
        for line in range(1,lines+1):
            group = [c for c in choices if c['窗口编号']==window['窗口编号'] and c['作业线']==line]
            if not group:
                continue
            for day in range(lo//1440,hi//1440):
                prefix = [(c['变量编号'],1) for c in group if c['开始']<(day+1)*1440]
                suffix = [(c['变量编号'],1) for c in group if c['完成']>day*1440]
                label = f"{window['窗口编号']}_{line}_{day}"
                a,b,y = [var(n+'_'+label) for n in ['首项已开始','末项未完成','租船日']]
                for flag,terms,key in [(a,prefix,'前缀'),(b,suffix,'后缀')]:
                    row(key+'有则真_'+label,terms+[(flag,-len(tasks))],'<=',0,'有限上界为必修任务总数，不使用任意大M')
                    row(key+'真则有_'+label,[(flag,1)]+[(k,-n) for k,n in terms],'<=',0,'无对应任务时标记必须为0')
                row('租船下界_'+label,[(a,1),(b,1),(y,-1)],'<=',1,'只有首项已开始且末项未完成才持续租船')
                row('租船前界_'+label,[(y,1),(a,-1)],'<=',0,'租船状态AND约束')
                row('租船后界_'+label,[(y,1),(b,-1)],'<=',0,'租船状态AND约束')
                rent_terms.append((y,scenario.get('船租',12000)))
    costs += rent_terms
    cash_terms += rent_terms
    if '现金预算' in scenario:
        row('现金预算',cash_terms,'<=',scenario['现金预算'],'现金支出预算不包含发电机会损失')
    spec['objectives']=[{'id':'总经济成本','priority':1,'sense':'minimize',
                         'expression':{'constant':0,'terms':[{'variable':k,'coefficient':v} for k,v in costs]}}]
    return audit,tasks,choices,spec


def check_and_measure(audit,tasks,choices,result,scenario):
    chosen = [c for c in choices if result['变量值'].get(c['变量编号'],0)>.5]
    audit.verify(copy.deepcopy(chosen),tasks)
    assert len(chosen)==len(tasks)
    for window in audit.DATA['海事检修窗口']:
        for line in {c['作业线'] for c in chosen}:
            seq=sorted([c for c in chosen if c['窗口编号']==window['窗口编号'] and c['作业线']==line],key=lambda c:c['开始'])
            for before,after in zip(seq,seq[1:]):
                assert before['下项最早开始']<=after['开始']
    # 不读取MILP租船标记，按选中的真正首末任务独立重算船租。
    days=0
    for key in {(c['窗口编号'],c['作业线']) for c in chosen}:
        group=[c for c in chosen if (c['窗口编号'],c['作业线'])==key]
        days+=rental_days(min(c['开始'] for c in group),max(c['完成'] for c in group))
    # 从原预测、原组件库与最终工作段重新计算，不复用候选费用缓存。
    loss,labor,material=0,0,0
    for candidate in chosen:
        own_loss=0
        for index, forecast in enumerate(audit.DATA['每小时电价与发电预测']):
            hours=max(0,min((index+1)*60,candidate['完成'])-max(index*60,candidate['开始']))/60
            own_loss+=hours*forecast['单台风机潜在功率（兆瓦）']*forecast['预测电价（元每兆瓦时）']*scenario.get('电价倍率',1)
        own_labor=sum(hi-lo for lo,hi in candidate['作业段'])/60*scenario.get('人工单价',150)
        own_material=audit.PARTS[candidate['部件编号']]['维修包成本（元每套）']
        assert abs(own_loss-candidate['发电损失'])<1e-6
        assert abs(own_labor-candidate['人工'])<1e-6
        assert own_material==candidate['材料']
        loss+=own_loss;labor+=own_labor;material+=own_material
    for stock in audit.DATA['库存快照']:
        events=[(audit.minute(e['计划时间']),0 if e['收发方向']=='补库' else 1,
                 e['数量']*(1 if e['收发方向']=='补库' else -1))
                for e in audit.DATA['库存收发计划'] if e['部件编号']==stock['部件编号']]
        events += [(c['开始'],1,-1) for c in chosen if c['部件编号']==stock['部件编号']]
        balance=stock['现有库存数量']
        for _,_,delta in sorted(events):
            balance+=delta
            assert balance>=scenario.get('安全库存',0)
    rent=days*scenario.get('船租',12000)
    total=loss+labor+material+rent
    assert abs(result['各级目标'][-1]['目标值']-total)<1e-3
    if '现金预算' in scenario:
        assert labor+material+rent<=scenario['现金预算']+1e-6
    return chosen,{'已安排任务数':len(chosen),'船舶租赁艘天':days,'发电损失元':round(loss,2),
                   '船租元':rent,'人工元':round(labor,2),'维修包元':material,
                   '现金支出元':round(labor+material+rent,2),'总经济成本元':round(total,2),'业务独立复核':'通过'}


def main():
    cases=[
        {'场景编号':'计算01','用户提问':'先排风机001、002、005、006、007、008的六项期初高告警，按总成本最低。'},
        {'场景编号':'计算02','用户提问':'同样六项，船租上涨20%。','船租':14400},
        {'场景编号':'计算03','用户提问':'同样六项，最多只用两条作业线。','作业覆盖':{'最多作业线数':2}},
        {'场景编号':'计算04','用户提问':'同样六项，每天08至16点维修，最多八小时。','作业覆盖':{'每天维修结束时间':'16:00','每天最多维修小时数':8}},
        {'场景编号':'计算05','用户提问':'同样六项，换风机四小时。','作业覆盖':{'风机间转移小时数':4}},
        {'场景编号':'计算06','用户提问':'同样六项，电价上涨30%。','电价倍率':1.3},
        {'场景编号':'计算07','用户提问':'同样六项，现金预算不超过30万元。','现金预算':300000},
        {'场景编号':'计算08','用户提问':'同样六项，现金预算不超过3万元。','现金预算':30000,'预期状态':'不可行'},
        {'场景编号':'计算09','用户提问':'同样六项，各部件的确认补库都晚到两天。','到货延迟小时':48},
        {'场景编号':'计算10','用户提问':'同样六项，其他已审批出库照常，每种部件任意时点至少留一套。','安全库存':1},
        {'场景编号':'计算11','用户提问':'11月10日18点新收到任务036，必须在原高严重期限前完成。','任务编号':['任务036'],'规划时间':'2026-11-10T18:00:00+08:00','预期状态':'不可行'},
        {'场景编号':'计算12','用户提问':'同样六项，维修人员单价调整为200元每人时。','人工单价':200},
    ]
    summary=[]
    for scenario in cases:
        directory=ROOT/scenario['场景编号']
        directory.mkdir(exist_ok=True)
        audit,tasks,choices,spec=build(scenario)
        errors=validate_model_spec(spec)
        assert not errors,errors
        (directory/'模型.json').write_text(json.dumps(spec,ensure_ascii=False),encoding='utf-8')
        result=solve_model_spec(spec,time_limit_seconds=8)
        (directory/'求解结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        row={'场景编号':scenario['场景编号'],'用户提问':scenario['用户提问'],'意图来源':'本轮Codex解释后显式记录，不是关键词解析器',
             '求解状态':result['状态'],'变量数':len(spec['variables']),'约束数':len(spec['constraints']),
             '耗时秒':result['总耗时秒'],'复核结果':'无解，不生成排程'}
        if scenario.get('预期状态'):
            assert result['状态']==scenario['预期状态'],row
        elif result['变量值']:
            assert result['独立复核通过']
            chosen,metrics=check_and_measure(audit,tasks,choices,result,scenario)
            row.update(metrics)
            details=[{'任务编号':c['任务编号'],'窗口编号':c['窗口编号'],'作业线':c['作业线'],
                      '开工时间':audit.stamp(c['开始']),'完工时间':audit.stamp(c['完成']),
                      '有效工时':sum(b-a for a,b in c['作业段'])/60} for c in chosen]
            with (directory/'排程.csv').open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(details[0]));writer.writeheader();writer.writerows(details)
            row['复核结果']='结构与业务费用复核通过'
        else:
            raise RuntimeError(f'本场景未取得计算结果：{row}')
        summary.append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    headers=list(dict.fromkeys(key for row in summary for key in row))
    with (ROOT/'多场景计算汇总.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=headers);writer.writeheader();writer.writerows(summary)
    (ROOT/'多场景计算汇总.json').write_text(json.dumps({'场景':summary,'原数据哈希':hashlib.sha256((PROJECT/'data/phase1-demo/初版完整数据.json').read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
