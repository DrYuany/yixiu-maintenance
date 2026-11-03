"""带角色权限的本地业务接口；/docs 仅用于开发联调。"""
import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from product_data import load_project
from product_planner import recommend
from product_store import Store


class PlanRequest(BaseModel):
    base: str = Field('基座01',description='预测基座编号，默认使用基座01')
    as_of: str | None = Field(None,description='规划时点，必须包含时区')
    input_file: str | None = Field(None,description='真实数据输入包路径')
    seconds: float = Field(default=60,gt=0,le=300,description='求解时间上限（秒）')
    strategy: str = Field('成本最低优先',description='高风险优先抢修、成本最低优先、最短完成优先、检修最多器件优先')
    mode: str = Field('分层',description='分层或加权')
    weights: dict | None = Field(None,description='加权模式的指标权重')
    limits: dict | None = Field(None,description='预算、最低完成量等约束')
    overrides: dict | None = Field(None,description='经过登记的运行参数覆盖')


class DraftRequest(BaseModel):
    run_id: str = Field('latest',description='推荐运行编号；填写 latest 自动使用最近一次成功推荐')
    windows: list[str] | None = Field(None,description='要生成检修单的窗口编号；为空表示全部推荐任务')
    accept_partial: bool = Field(False,description='是否接受部分诊断方案')
    key: str = Field(description='幂等操作编号，重试时保持不变')


class ApprovalRequest(BaseModel):
    reject: bool = Field(False,description='是否拒绝检修单')
    key: str = Field(description='幂等操作编号')


class FeedbackRequest(BaseModel):
    at: str = Field(description='实际发生时间，必须包含时区')
    key: str = Field(description='幂等操作编号')


class ExternalRequest(FeedbackRequest):
    run_id: str = Field(description='推荐运行编号')
    movement_id: str = Field(description='库存流水编号')


def create_app(project,db_path,users=None):
    project=Path(project).resolve();store=Store(db_path)
    users=users or json.loads(os.environ.get('MAINTENANCE_PRODUCT_USERS','{}'))
    if not users or any(not isinstance(v,dict) or v.get('role') not in ('planner','approver','executor','admin')
                        or not v.get('name') for v in users.values()):
        raise ValueError('MAINTENANCE_PRODUCT_USERS 需设置 token -> {name,role}，且不得为空')
    app=FastAPI(title='海上风电检修窗口推荐系统',version='一期',description='用于告警接收、窗口推荐、检修审批、出海清单和库存执行反馈。当前为本地验收版。')
    bearer=HTTPBearer(description='输入角色令牌，例如 planner、approver 或 executor')

    def identity(credentials: HTTPAuthorizationCredentials=Depends(bearer)):
        token=credentials.credentials
        if token not in users:raise HTTPException(401,'无效访问令牌')
        return users[token]

    def require(user,roles):
        if user['role'] not in roles and user['role']!='admin':raise HTTPException(403,'该角色无权执行此操作')

    def call(fn):
        try:return fn()
        except (ValueError,KeyError,FileNotFoundError) as exc:raise HTTPException(409,str(exc)) from exc

    def optional_text(value):
        # Swagger uses the literal placeholder "string" for optional text fields.
        return None if value in (None, '', 'string') else value

    def optional_dict(value):
        # Swagger represents an empty dict as {"additionalProp1": {}}.
        if value is None:
            return None
        if not isinstance(value, dict):
            return value
        cleaned={k:v for k,v in value.items() if k != 'additionalProp1'}
        return cleaned or None

    @app.post('/plans',summary='生成检修窗口推荐',description='读取当前告警、窗口、海况、电价、风速和库存，调用求解器生成推荐方案。')
    def plan(body:PlanRequest,user=Depends(identity)):
        require(user,('planner',))
        def work():
            input_file=optional_text(body.input_file)
            as_of=optional_text(body.as_of)
            if input_file and not Path(input_file).resolve().is_relative_to(project):
                raise ValueError('API 输入文件必须位于项目目录内')
            data=store.overlay(load_project(project,body.base,as_of,optional_dict(body.overrides),input_file))
            import uuid
            out=project/'product-output'/('run-'+uuid.uuid4().hex[:12])
            result=recommend(data,out,body.seconds,body.strategy,body.mode,
                             optional_dict(body.weights),optional_dict(body.limits))
            run_id=store.save_run(result,data,out)
            return {'运行编号':run_id,'报告路径':str(out/'推荐报告.html'),'结果':result}
        return call(work)

    @app.post('/orders',summary='生成待审批检修单',description='从推荐方案中选择窗口，生成待审批检修单。')
    def draft(body:DraftRequest,user=Depends(identity)):
        require(user,('planner',))
        run_id=body.run_id
        if run_id in ('', 'string', 'latest'): run_id=store.latest_run_id()
        return call(lambda:store.draft(run_id,body.windows,body.key,user['name'],body.accept_partial))

    @app.post('/orders/{oid}/approval',summary='审批或拒绝检修单',description='审批后生成出海清单和库存变化单，并预留库存。')
    def approval(oid:str,body:ApprovalRequest,user=Depends(identity)):
        require(user,('approver',));return call(lambda:store.approve(oid,project,body.key,user['name'],body.reject))

    @app.post('/orders/{oid}/issue',summary='登记实际领料',description='确认出海执行前的实际备件领用，扣减实物库存。')
    def issue(oid:str,body:FeedbackRequest,user=Depends(identity)):
        require(user,('executor',));return call(lambda:store.execute(oid,'领料',body.key,user['name'],body.at))

    @app.post('/orders/{oid}/complete',summary='登记检修完成',description='记录检修单实际完成。')
    def complete(oid:str,body:FeedbackRequest,user=Depends(identity)):
        require(user,('executor',));return call(lambda:store.execute(oid,'完成',body.key,user['name'],body.at))

    @app.post('/orders/{oid}/cancel',summary='取消检修单',description='取消尚未开始执行的检修单。')
    def cancel(oid:str,body:FeedbackRequest,user=Depends(identity)):
        require(user,('planner','approver'));return call(lambda:store.execute(oid,'取消',body.key,user['name'],body.at))

    @app.post('/inventory/movements',summary='确认实际库存流水',description='登记实际补库或出库流水。')
    def movement(body:ExternalRequest,user=Depends(identity)):
        require(user,('executor',));return call(lambda:store.post_external(body.run_id,body.movement_id,body.at,body.key,user['name']))

    @app.get('/orders',summary='查询检修单')
    def orders(user=Depends(identity)):return store.list_orders()

    @app.get('/orders/{oid}/documents',summary='查看检修单附件')
    def documents(oid:str,user=Depends(identity)):return store.documents(oid)

    @app.get('/audit',summary='检查审计链')
    def audit(user=Depends(identity)):return call(store.audit)

    return app
