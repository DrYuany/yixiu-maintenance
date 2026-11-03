"""First-stage maintenance product command line. All writes are explicit transactions."""
import argparse
import json
import os
import sys
from pathlib import Path

from product_data import load_project
from product_planner import recommend
from product_store import Store


def main(argv=None):
    p=argparse.ArgumentParser(description='海上风电检修窗口推荐与审批')
    skill_root=Path(__file__).resolve().parents[1]
    pointer=skill_root/'project-path.txt'
    default_project=os.environ.get('MAINTENANCE_PRODUCT_PROJECT') or (pointer.read_text(encoding='utf-8-sig').strip() if pointer.exists() else str(skill_root.parent))
    p.add_argument('--project',default=default_project)
    p.add_argument('--db',default=None)
    p.add_argument('--actor',default=None)
    sub=p.add_subparsers(dest='command',required=True)
    plan=sub.add_parser('plan',help='求解、复核、生成报告并保存运行')
    plan.add_argument('--base',default='基座01');plan.add_argument('--as-of')
    plan.add_argument('--input');plan.add_argument('--output');plan.add_argument('--seconds',type=float,default=60)
    plan.add_argument('--strategy',default='成本最低优先');plan.add_argument('--mode',default='分层')
    plan.add_argument('--weights');plan.add_argument('--limits');plan.add_argument('--overrides')
    draft=sub.add_parser('draft',help='根据推荐生成待审批检修单')
    draft.add_argument('run_id');draft.add_argument('--windows',nargs='*');draft.add_argument('--accept-partial',action='store_true');draft.add_argument('--key')
    approval=sub.add_parser('approve',help='审批或拒绝检修单')
    approval.add_argument('order_id');approval.add_argument('--reject',action='store_true');approval.add_argument('--input');approval.add_argument('--key')
    external=sub.add_parser('post-external',help='确认预测中的实际入出库')
    external.add_argument('run_id');external.add_argument('movement_id');external.add_argument('--at',required=True);external.add_argument('--key',required=True)
    for name in ('issue','complete','cancel'):
        cmd=sub.add_parser(name);cmd.add_argument('order_id');cmd.add_argument('--at',required=True);cmd.add_argument('--key',required=True)
    sub.add_parser('orders');doc=sub.add_parser('documents');doc.add_argument('order_id')
    sub.add_parser('audit')
    serve=sub.add_parser('serve',help='启动带角色令牌的本地 API 与交互文档')
    serve.add_argument('--host',default='127.0.0.1');serve.add_argument('--port',type=int,default=8765)
    args=p.parse_args(argv);project=Path(args.project).resolve()
    if args.command=='serve':
        import uvicorn
        from product_service import create_app
        uvicorn.run(create_app(project,args.db or project/'product-output/product-state.sqlite3'),
                    host=args.host,port=args.port)
        return
    store=Store(args.db or project/'product-output/product-state.sqlite3')
    def decoded(value): return json.loads(value) if value else None
    if args.command=='plan':
        data=store.overlay(load_project(project,args.base,args.as_of,decoded(args.overrides),args.input))
        output=Path(args.output or project/'product-output'/('run-'+data.meta['规划时点'].replace(':','-'))).resolve()
        report=recommend(data,output,args.seconds,args.strategy,args.mode,decoded(args.weights),decoded(args.limits))
        run_id=store.save_run(report,data,output)
        result={'运行编号':run_id,'报告':str(output/'推荐报告.html'),'结果':str(output/'结果.json'),
                '求解':report['求解'],'推荐':{k:report['推荐'][k] for k in ('总成本','完成任务数','待修总数','复核通过')}
                if '推荐' in report else None}
    elif args.command=='draft': result=store.draft(args.run_id,args.windows,args.key,args.actor,args.accept_partial)
    elif args.command=='approve': result=store.approve(args.order_id,project,args.key,args.actor,args.reject,args.input)
    elif args.command=='post-external': result=store.post_external(args.run_id,args.movement_id,args.at,args.key,args.actor)
    elif args.command in ('issue','complete','cancel'):
        result=store.execute(args.order_id,{'issue':'领料','complete':'完成','cancel':'取消'}[args.command],args.key,args.actor,args.at)
    elif args.command=='orders':result=store.list_orders()
    elif args.command=='documents':result=store.documents(args.order_id)
    else:result=store.audit()
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,FileNotFoundError) as exc:
        print(json.dumps({'错误':str(exc)},ensure_ascii=False),file=sys.stderr);sys.exit(2)
