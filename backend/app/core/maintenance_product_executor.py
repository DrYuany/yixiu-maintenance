from __future__ import annotations

import json
import shutil
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.core.planning_intent import PlanningRequest


class MaintenanceProductExecutionError(RuntimeError):
    pass


class MaintenanceProductExecutor:
    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = (project_root or Path(__file__).resolve().parents[3]).resolve()
        self.scripts_dir = self.project_root / "maintenance-milp-modeler" / "scripts"
        if str(self.scripts_dir) not in sys.path:
            sys.path.insert(0, str(self.scripts_dir))

        self._cancel_events: dict[str, threading.Event] = {}
        self._solvers: dict[str, Any] = {}
        self._dir_locks: dict[str, threading.Lock] = {}

    def prepare(self, request: PlanningRequest) -> tuple[PlanningRequest, dict[str, Any]]:
        from product_data import load_project

        base_data = load_project(self.project_root, request.base)
        overrides = dict(request.overrides)
        for name, multiplier in request.relative_overrides.items():
            if name not in base_data.params:
                raise ValueError(f"无法对未知参数执行相对调整：{name}")
            overrides[name] = float(base_data.params[name]) * multiplier
        prepared = replace(request, overrides=overrides, relative_overrides={})
        data = load_project(
            self.project_root,
            prepared.base,
            overrides=prepared.overrides,
            data_patches=prepared.data_patches,
        )
        # 预算只在这里算一次：参数总表 → 用户显式 → 部署上限。
        # 公告、job 进度、模型产物参数表、版本对比都读这个结果，
        # 不再各写一个数（曾经同时存在 300 / 60 / 30）。
        from solve_budget import resolve as resolve_budget

        snapshot = {
            "input_hash": data.meta.get("输入哈希"),
            "run_hash": data.meta.get("运行哈希"),
            "as_of": data.meta.get("规划时点"),
            "business_version": data.meta.get("业务版本"),
            "budget": resolve_budget(data.params, requested_seconds=prepared.seconds),
        }
        return prepared, snapshot

    def execute(
        self,
        request: PlanningRequest,
        job_id: str,
        model_fingerprint: str | None = None,
        dsl: dict[str, Any] | None = None,
        utterance: str | None = None,
    ) -> dict[str, Any]:
        """dsl：受控 DSL（约束/自定义目标），校验后编译成注入闭包传给求解。

        utterance：用户这一轮的原话。它跟 DSL 对不上就不出方案——预览阶段已经拦过
        一次，这里是第二道：pending 可能来自上一轮、也可能被人改过，只靠前一道门
        等于把"用户批准的东西"这一点押在一条路径上。
        """
        cancel_event: threading.Event | None = None
        try:
            from product_data import load_project
            from product_planner import recommend

            if request.relative_overrides:
                request, _ = self.prepare(request)
            data = load_project(
                self.project_root,
                request.base,
                overrides=request.overrides,
                data_patches=request.data_patches,
            )
            extra_constraints = None
            custom_stages = None
            allow_partial = False
            utterance_audit: dict[str, Any] | None = None
            if dsl:
                from dsl_core.validator import validate_dsl

                dsl_errors = validate_dsl(dsl, data)
                if dsl_errors:
                    raise MaintenanceProductExecutionError(
                        "DSL 校验失败：" + "；".join(dsl_errors)
                    )
                # 原话 ↔ DSL 的第二道门（第一道在 solve 生成预览时）。这里拒绝，
                # 是因为"用户批准的那句话"和"要执行的那份 DSL"必须始终是同一件事；
                # 只靠预览阶段拦一次，等于把这一点押在一条路径上。
                if utterance:
                    from dsl_core.utterance import audit_utterance, world_from_data
                    from dsl_core.utterance import describe as describe_utterance

                    utterance_audit = audit_utterance(utterance, dsl, world_from_data(data))
                    if utterance_audit.get("违例"):
                        raise MaintenanceProductExecutionError(
                            "原话核对未通过，这份变更不是你要的那件事："
                            + describe_utterance(utterance_audit)
                        )
                # 第三期：DSL 全段编译——参数/数据/目标并入 PlanningRequest，
                # 只剩 PlanningRequest 表达不了的约束/目标走注入闭包。
                from dsl_core.compiler import constraints_builder, stages_builder
                from dsl_core.planning_bridge import custom_objectives, dsl_to_request

                request, residual, timeline = dsl_to_request(dsl, request)
                if request.relative_overrides:
                    request, _ = self.prepare(request)
                data = load_project(
                    self.project_root,
                    request.base,
                    overrides=request.overrides,
                    data_patches=request.data_patches,
                    timeline_patches=timeline or None,
                )
                if residual:
                    extra_constraints = constraints_builder(residual, data)
                # exclude / at_most_one / 完成数上限都是"允许少修"的明确信号：
                # 完整模式给每个任务加 sum(x)==1（必须排入），跟"少修"直接矛盾，
                # 必然判不可行。判定收在 compiler.needs_partial，和 runner 同一份。
                from dsl_core.compiler import needs_partial

                allow_partial = needs_partial(dsl)
                customs = custom_objectives(dsl)
                if customs:
                    custom_stages = stages_builder(customs, data)
            suffix = model_fingerprint or job_id
            base_output = self.project_root / "product-output" / f"agent-model-{suffix[:16]}"
            output = base_output
            result_path = output / "结果.json"
            if result_path.exists():
                cached_report = json.loads(result_path.read_text(encoding="utf-8"))
                has_cached_solution = bool((cached_report.get("求解") or {}).get("有解"))
                has_cached_artifact = (
                    (output / "model_artifact.json").exists()
                    or (output / "diagnostic_model_artifact.json").exists()
                )
                cached_audit = (cached_report.get("求解") or {}).get("意图对账") or {}
                cached_clean = not (
                    cached_audit.get("违例") or cached_audit.get("未核对")
                )
                if not has_cached_solution or not has_cached_artifact:
                    output = base_output.with_name(f"{base_output.name}-{job_id[-8:]}")
                    result_path = output / "结果.json"
                # 意图对账没过（违例或未核对）的结果不能当缓存复用：代码改了、
                # 约束补了，还拿旧结论顶上去，等于把上次的错再确认一遍。
                elif not cached_clean:
                    output = base_output.with_name(f"{base_output.name}-{job_id[-8:]}")
                    result_path = output / "结果.json"
            # 并发求解：按输出目录加锁——指纹不同则目录不同、可并发互不覆盖；
            # 指纹相同（同一请求重复提交）则串行复用缓存，避免后一个撞已存在结果。
            dir_lock = self._dir_locks.setdefault(str(output), threading.Lock())
            with dir_lock:
                if result_path.exists():
                    report = json.loads(result_path.read_text(encoding="utf-8"))
                else:
                    cancel_event = threading.Event()
                    self._cancel_events[job_id] = cancel_event
                    def register_solver(solver):
                        self._solvers[job_id] = solver
                    report = recommend(
                        data,
                        output,
                        seconds=request.seconds,
                        strategy=request.strategy,
                        mode=request.mode,
                        weights=request.weights,
                        limits=request.limits or None,
                        on_solver=register_solver,
                        cancelled=cancel_event.is_set,
                        model_fingerprint=model_fingerprint,
                        extra_constraints=extra_constraints,
                        custom_stages=custom_stages,
                        partial=allow_partial,
                        dsl=dsl,
                    )
                    if cancel_event.is_set():
                        shutil.rmtree(output, ignore_errors=True)
                        raise MaintenanceProductExecutionError("求解已由用户停止")
            # 意图对账有违例 = 这份排程跟用户的要求对不上，不能沉淀成方案。
            # 未核对同理：没有证据证明对得上，就不是"通过"，不能出方案。
            # 启发式兜底另外处理：它本来就不是按约束求出来的，界面已有专门标注
            # （"启发式兜底方案 · 未证明最优"），这里不重复拦截。
            audit = (report.get("求解") or {}).get("意图对账") or {}
            if (audit.get("违例") or audit.get("未核对")) and str(
                (report.get("求解") or {}).get("方案来源")
            ) != "HEURISTIC_FALLBACK":
                from dsl_core.reconcile import describe

                raise MaintenanceProductExecutionError(
                    "意图对账未通过，这份排程与实际要求不符：" + describe(audit)
                )
            # 没产出方案时，把"为什么"写进报告。这份诊断只由代码算出来：
            # 直接矛盾、时间窗为空、任务无候选、配额上限不够，每条带证据；
            # 算不出来就标"未能定位"，绝不让模型事后编一个理由。
            if dsl and not report.get("推荐"):
                try:
                    from product_data import candidates

                    from dsl_core.diagnose import diagnose

                    options = {t["任务编号"]: candidates(data, t) for t in data.tasks}
                    report.setdefault("求解", {})["失败诊断"] = diagnose(
                        dsl=dsl,
                        data=data,
                        options=options,
                        info=report.get("求解") or {},
                        partial=allow_partial,
                    )
                except Exception:  # noqa: BLE001 - 诊断失败不能掩盖原始失败
                    report.setdefault("求解", {})["失败诊断"] = {
                        "结论": "未能定位",
                        "原因": [],
                        "建议": ["诊断过程本身出错，没有给出原因"],
                    }
            report["Agent任务"] = {
                "任务编号": job_id,
                "输出目录": str(output),
                "模型输入": request.to_payload(),
            }
            if utterance_audit:
                report.setdefault("求解", {})["原话对账"] = utterance_audit
            if dsl:
                report["Agent任务"]["DSL"] = dsl
            artifact_path = output / "model_artifact.json"
            if not artifact_path.exists():
                artifact_path = output / "diagnostic_model_artifact.json"
            if artifact_path.exists():
                report["模型产物"] = json.loads(artifact_path.read_text(encoding="utf-8"))
                report["Agent任务"]["模型产物文件"] = str(artifact_path)
            return report
        except Exception as exc:
            raise MaintenanceProductExecutionError(str(exc)) from exc
        finally:
            self._cancel_events.pop(job_id, None)
            self._solvers.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        event = self._cancel_events.get(job_id)
        solver = self._solvers.get(job_id)
        if event:
            event.set()
        if solver:
            solver.StopSearch()
        return bool(event or solver)


def solver_status(report: dict[str, Any]) -> tuple[str, bool]:
    solving = report.get("求解", {})
    has_incumbent = bool(solving.get("有解")) and bool(report.get("推荐"))
    explicit_status = solving.get("解状态")
    if explicit_status in {"OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE"}:
        return explicit_status, has_incumbent
    stages = solving.get("阶段", [])
    statuses = [stage.get("状态") for stage in stages]
    if has_incumbent and solving.get("分层全部最优"):
        return "OPTIMAL", True
    if has_incumbent:
        return "FEASIBLE", True
    if statuses and statuses[-1] == "INFEASIBLE":
        return "INFEASIBLE", False
    return "UNKNOWN", False

