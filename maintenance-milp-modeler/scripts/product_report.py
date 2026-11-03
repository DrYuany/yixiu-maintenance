"""Chinese summary plus auditable artifacts; every displayed number is computed."""
import csv
import html
import json
from collections import defaultdict


def write_report(data, report, out):
    (out/'结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'输入快照.json').write_text(json.dumps(data.__dict__,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    rows=[]
    for s in report.get('推荐',{}).get('排程',[]):
        rows.append({'任务编号':s['任务编号'],'风机编号':s['风机编号'],'部件':s['部件名称'],'严重等级':s['严重等级'],
            '窗口':s['窗口编号'],'作业线':s['作业线'],'开始':data.stamp(s['开始']),'完成':data.stamp(s['完成']),
            '维修人时':sum(b-a for a,b in s['作业段'])/60})
    with (out/'维修任务明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]) if rows else ['任务编号','窗口','开始','完成'])
        writer.writeheader();writer.writerows(rows)
    esc=lambda v:html.escape(str(v))
    title='检修窗口推荐'
    lines=[f'# {title}',f"规划时点：{data.meta['规划时点']}；数据：{data.meta['数据性质']}。",'']
    body=f'<h1>{title}</h1><p>规划时点 {esc(data.meta["规划时点"])} · {esc(data.meta["数据性质"])}数据</p>'
    if '推荐' not in report:
        lines.append('未取得可用解，不能生成检修单。')
        body+='<p>未取得可用解。请查看求解状态与数据审计。</p>'
    else:
        rec=report['推荐'];groups=defaultdict(list)
        for s in rec['排程']: groups[s['窗口编号']].append(s)
        if report['求解'].get('诊断模型'):
            warning='正式必修模型不可行；以下为部分任务诊断方案，须单独确认未完成任务。'
            lines.append(warning);body+=f'<p class="warning">{warning}</p>'
        stages=report['求解'].get('阶段',[])
        if stages and not report['求解']['分层全部最优']:
            body+='<p>当前为已验证可行解；尚未证明全部目标最优。</p>'
            lines.append('已验证可行解，尚未证明全部目标最优。')
        body+=f'<div class="card"><strong>预计总成本 ¥{rec["总成本"]:,.2f}</strong><p>安排 {rec["完成任务数"]} / {rec["待修总数"]} 项 · 租船 {rec["船舶艘天"]} 艘天</p></div>'
        lines.append(f'预计总成本：¥{rec["总成本"]:,.2f}；完成 {rec["完成任务数"]}/{rec["待修总数"]} 项。')
        body+='<table><tr><th>推荐窗口</th><th>实际检修起止</th><th>修哪些</th></tr>'
        for window,group in sorted(groups.items()):
            start=data.stamp(min(s['开始'] for s in group));end=data.stamp(max(s['完成'] for s in group))
            tasks='；'.join(s['风机编号']+' '+s['部件名称'] for s in group)
            body+=f'<tr><td>{esc(window)}</td><td>{esc(start)}<br>{esc(end)}</td><td>{esc(tasks)}</td></tr>'
            lines.append(f'- {window}：{start} 至 {end}，维修 {tasks}')
        body+='</table><h2>与人工排程比较</h2><table><tr><th>方法</th><th>完成</th><th>总成本</th><th>推荐方案节省</th></tr>'
        for name,b in report['人工基准'].items():
            savings=f'¥{b["节省金额"]:,.2f}（{b["节省比例"]:.1%}）' if b['节省比例'] is not None else '任务集合不同，不宣称同等服务节省'
            body+=f'<tr><td>{esc(name)}</td><td>{b["完成任务数"]}</td><td>¥{b["总成本"]:,.2f}</td><td>{esc(savings)}</td></tr>'
            lines.append(f'- 相对{name}：{savings}；基准完成 {b["完成任务数"]} 项。')
        body+='</table>'
        if rec['未安排']:
            body+=f'<p class="warning">{len(rec["未安排"])} 项未安排，详见未安排清单。</p>'
        for label,detail in [('费用明细',{k:rec[k] for k in ('船租','人工','备件','其他费用','发电损失','现金支出')}),
                             ('未安排任务',rec['未安排']),('求解与数据审计',{'数据':report['数据'],'求解':report['求解']})]:
            body+=f'<details><summary>{label}</summary><pre>{esc(json.dumps(detail,ensure_ascii=False,indent=2))}</pre></details>'
    body+='<p><a href="维修任务明细.csv">下载维修任务表</a> · <a href="结果.json">完整结果</a> · <a href="输入快照.json">输入快照</a></p>'
    page='<!doctype html><html lang="zh"><meta charset="utf-8"><title>'+title+'</title><style>body{font:16px system-ui;max-width:1100px;margin:36px auto;padding:0 24px;color:#173042}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{text-align:left;padding:12px;border-bottom:1px solid #d7e1e7}th{background:#eef4f7}.card{padding:20px;background:#edf8f2;border-radius:12px;font-size:20px}.warning{color:#9a5510}details{margin:16px 0}pre{white-space:pre-wrap;font-size:13px}a{color:#006b86}</style>'+body+'</html>'
    (out/'推荐报告.html').write_text(page,encoding='utf-8')
    (out/'推荐摘要.md').write_text('\n'.join(lines),encoding='utf-8')
