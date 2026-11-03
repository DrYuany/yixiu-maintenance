#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Portable runtime bundled with the maintenance-window-forecast skill."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Sequence

try:
    from ortools.linear_solver import pywraplp
except ImportError:  # pragma: no cover
    pywraplp = None


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    duration_hours: int
    wind_speed_limit: float
    wave_height_limit: float
    severity: float
    can_night_work: bool = False


@dataclass(frozen=True)
class HourData:
    hour: int
    electricity_price: float
    wind_speed: float
    wave_height: float


@dataclass(frozen=True)
class ObjectiveWeights:
    loss: float = 1.0
    duration: float = 1.0
    severity: float = 1.0


@dataclass(frozen=True)
class ProblemConfig:
    num_turbines: int = 3
    time_horizon: int = 72
    max_parallel_tasks: int = 3
    turbine_ids: Sequence[int] = field(default_factory=tuple)
    component_names: Sequence[str] = field(
        default_factory=lambda: ["叶片", "轮毂", "主轴", "齿轮箱", "发电机", "偏航系统", "变桨系统", "变频器"]
    )
    weights: ObjectiveWeights = field(default_factory=ObjectiveWeights)
    rated_power_mw: float = 2.0
    cut_in_wind_speed: float = 3.0
    rated_wind_speed: float = 12.0
    night_work_start: int = 22
    night_work_end: int = 6
    labor_cost_per_hour: float = 80.0
    severity_reference_scale: float = 2.5
    unrepaired_risk_multiplier: float = 1.25
    delay_risk_multiplier: float = 0.6


PARAMETER_REGISTRY = {
    "weights": ["loss", "duration", "severity"],
    "constraints": [
        "time_horizon",
        "max_parallel_tasks",
        "rated_power_mw",
        "cut_in_wind_speed",
        "rated_wind_speed",
        "labor_cost_per_hour",
        "severity_reference_scale",
        "unrepaired_risk_multiplier",
        "delay_risk_multiplier",
    ],
    "objects": ["turbine_ids", "include_components", "exclude_components"],
    "component_params": [
        "duration_hours",
        "severity",
        "wind_speed_limit",
        "wave_height_limit",
        "can_night_work",
    ],
}


NAMED_STRATEGIES = {
    "economic_priority": {"weights": {"loss": 1.2, "duration": 0.8, "severity": 0.9}},
    "risk_priority": {"weights": {"loss": 0.9, "duration": 0.8, "severity": 1.3}},
    "fast_repair_priority": {
        "weights": {"loss": 0.9, "duration": 1.1, "severity": 0.9},
        "constraints": {"labor_cost_per_hour": 100.0},
    },
}


def default_component_library() -> Dict[str, ComponentSpec]:
    return {
        "叶片": ComponentSpec("叶片", 10, 12.0, 1.8, 7.0, False),
        "轮毂": ComponentSpec("轮毂", 8, 12.5, 2.0, 6.0, False),
        "主轴": ComponentSpec("主轴", 9, 12.0, 2.0, 8.0, False),
        "齿轮箱": ComponentSpec("齿轮箱", 12, 15.0, 2.5, 10.0, False),
        "发电机": ComponentSpec("发电机", 8, 15.0, 2.5, 9.0, False),
        "偏航系统": ComponentSpec("偏航系统", 6, 14.0, 2.2, 5.5, False),
        "变桨系统": ComponentSpec("变桨系统", 6, 14.0, 2.2, 6.5, False),
        "变频器": ComponentSpec("变频器", 5, 15.0, 2.5, 5.0, True),
    }


def default_problem_config() -> ProblemConfig:
    return ProblemConfig(turbine_ids=(1, 2, 3))


def generate_demo_hour_data(hours: int = 72) -> List[HourData]:
    base_prices = [300, 280, 260, 250, 280, 320, 380, 420, 450, 480, 460, 440, 420, 400, 380, 360, 380, 420, 480, 520, 480, 420, 360, 320]
    base_wind = [8.5, 9.0, 9.2, 8.8, 7.5, 6.8, 6.2, 5.8, 5.5, 6.0, 7.2, 8.5, 9.5, 10.2, 10.8, 11.5, 10.5, 9.8, 9.2, 8.5, 7.8, 7.2, 6.8, 6.5]
    base_wave = [1.5, 1.6, 1.7, 1.8, 1.7, 1.5, 1.3, 1.2, 1.1, 1.0, 1.2, 1.5, 1.8, 2.0, 2.2, 2.4, 2.2, 1.9, 1.7, 1.5, 1.4, 1.3, 1.2, 1.1]
    return [
        HourData(hour=i, electricity_price=base_prices[i % 24], wind_speed=base_wind[i % 24], wave_height=base_wave[i % 24])
        for i in range(hours)
    ]


def serialize_config(config: ProblemConfig) -> Dict[str, Any]:
    return {
        "num_turbines": config.num_turbines,
        "time_horizon": config.time_horizon,
        "max_parallel_tasks": config.max_parallel_tasks,
        "turbine_ids": list(config.turbine_ids) if config.turbine_ids else list(range(1, config.num_turbines + 1)),
        "component_names": list(config.component_names),
        "weights": asdict(config.weights),
        "rated_power_mw": config.rated_power_mw,
        "cut_in_wind_speed": config.cut_in_wind_speed,
        "rated_wind_speed": config.rated_wind_speed,
        "labor_cost_per_hour": config.labor_cost_per_hour,
        "severity_reference_scale": config.severity_reference_scale,
        "unrepaired_risk_multiplier": config.unrepaired_risk_multiplier,
        "delay_risk_multiplier": config.delay_risk_multiplier,
    }


def serialize_component_library(component_library: Dict[str, ComponentSpec]) -> Dict[str, Dict[str, Any]]:
    return {name: asdict(spec) for name, spec in component_library.items()}


def apply_problem_updates(
    updates: Dict[str, Any] | None = None,
    base_config: ProblemConfig | None = None,
    base_component_library: Dict[str, ComponentSpec] | None = None,
) -> tuple[ProblemConfig, Dict[str, ComponentSpec], Dict[str, Any]]:
    config = base_config or default_problem_config()
    component_library = dict(base_component_library or default_component_library())
    updates = updates or {}
    applied_updates: Dict[str, Any] = {}

    if "weights" in updates:
        data = updates["weights"]
        config = replace(
            config,
            weights=ObjectiveWeights(
                loss=data.get("loss", config.weights.loss),
                duration=data.get("duration", config.weights.duration),
                severity=data.get("severity", config.weights.severity),
            ),
        )
        applied_updates["weights"] = asdict(config.weights)

    if "constraints" in updates:
        for key in PARAMETER_REGISTRY["constraints"]:
            if key in updates["constraints"]:
                config = replace(config, **{key: updates["constraints"][key]})
        applied_updates["constraints"] = updates["constraints"]

    if "objects" in updates:
        component_names = list(config.component_names)
        objects = updates["objects"]
        if "include_components" in objects:
            component_names = [name for name in objects["include_components"] if name in component_library]
        if "exclude_components" in objects:
            excluded = set(objects["exclude_components"])
            component_names = [name for name in component_names if name not in excluded]
        turbine_ids = tuple(objects.get("turbine_ids", config.turbine_ids or tuple(range(1, config.num_turbines + 1))))
        config = replace(config, component_names=component_names, turbine_ids=turbine_ids, num_turbines=max(turbine_ids) if turbine_ids else config.num_turbines)
        applied_updates["objects"] = {"component_names": component_names, "turbine_ids": list(turbine_ids)}

    if "component_params" in updates:
        for name, params in updates["component_params"].items():
            if name in component_library:
                component_library[name] = replace(component_library[name], **params)
        applied_updates["component_params"] = updates["component_params"]

    return config, component_library, applied_updates


def _build_problem(config: ProblemConfig, component_library: Dict[str, ComponentSpec], hour_data: List[HourData]) -> Dict[str, Any]:
    turbine_ids = list(config.turbine_ids) if config.turbine_ids else list(range(1, config.num_turbines + 1))
    tasks = []
    for turbine_id in turbine_ids:
        for component_name in config.component_names:
            tasks.append({
                "task_id": f"T{turbine_id}-{component_name}",
                "turbine_id": turbine_id,
                "component_name": component_name,
                "component": component_library[component_name],
            })
    return {"config": config, "component_library": component_library, "hour_data": hour_data, "turbine_ids": turbine_ids, "tasks": tasks}


def _is_night(config: ProblemConfig, hour: int) -> bool:
    h = hour % 24
    return h >= config.night_work_start or h < config.night_work_end


def _hourly_loss(config: ProblemConfig, hour_data: HourData) -> float:
    wind = hour_data.wind_speed
    if wind < config.cut_in_wind_speed:
        power = 0.0
    elif wind < config.rated_wind_speed:
        power = config.rated_power_mw * ((wind - config.cut_in_wind_speed) / (config.rated_wind_speed - config.cut_in_wind_speed))
    else:
        power = config.rated_power_mw
    return power * hour_data.electricity_price


def _average_hourly_loss(problem: Dict[str, Any]) -> float:
    config = problem["config"]
    if config.time_horizon <= 0:
        return 0.0
    return sum(_hourly_loss(config, item) for item in problem["hour_data"]) / config.time_horizon


def _task_reference_risk_cost(problem: Dict[str, Any], task: Dict[str, Any]) -> float:
    config = problem["config"]
    component = task["component"]
    severity_ratio = max(0.1, component.severity / 10.0)
    base_cost = _average_hourly_loss(problem) * component.duration_hours
    return base_cost * (1.0 + config.severity_reference_scale * severity_ratio)


def _unrepaired_risk_cost(problem: Dict[str, Any], task: Dict[str, Any]) -> float:
    return _task_reference_risk_cost(problem, task) * problem["config"].unrepaired_risk_multiplier


def _delayed_risk_cost(problem: Dict[str, Any], task: Dict[str, Any], start: int) -> float:
    config = problem["config"]
    return _task_reference_risk_cost(problem, task) * config.delay_risk_multiplier * (start / config.time_horizon)


def _objective_from_costs(config: ProblemConfig, loss: float, direct_cost: float, risk_cost: float) -> float:
    return config.weights.loss * loss + config.weights.duration * direct_cost + config.weights.severity * risk_cost


def _feasible_starts(problem: Dict[str, Any], task: Dict[str, Any]) -> List[int]:
    config = problem["config"]
    component = task["component"]
    feasible = []
    for start in range(config.time_horizon - component.duration_hours + 1):
        ok = True
        for hour in range(start, start + component.duration_hours):
            hour_data = problem["hour_data"][hour]
            if hour_data.wind_speed > component.wind_speed_limit or hour_data.wave_height > component.wave_height_limit:
                ok = False
                break
            if not component.can_night_work and _is_night(config, hour):
                ok = False
                break
        if ok:
            feasible.append(start)
    return feasible


def _evaluate(problem: Dict[str, Any], assignments: Dict[str, int]) -> Dict[str, Any]:
    config = problem["config"]
    active_counts = [0] * config.time_horizon
    turbine_down = {tid: [False] * config.time_horizon for tid in problem["turbine_ids"]}
    completion = {tid: 0 for tid in problem["turbine_ids"]}
    total_duration = 0
    risk_cost = 0.0
    for task in problem["tasks"]:
        start = assignments.get(task["task_id"])
        component = task["component"]
        if start is None:
            risk_cost += _unrepaired_risk_cost(problem, task)
            continue
        total_duration += component.duration_hours
        risk_cost += _delayed_risk_cost(problem, task, start)
        end = start + component.duration_hours
        completion[task["turbine_id"]] = max(completion[task["turbine_id"]], end)
        for hour in range(start, end):
            active_counts[hour] += 1
            turbine_down[task["turbine_id"]][hour] = True
    for hour, count in enumerate(active_counts):
        if count > config.max_parallel_tasks:
            raise ValueError(f"Parallel limit exceeded at hour {hour}")
    for prev, nxt in zip(problem["turbine_ids"], problem["turbine_ids"][1:]):
        next_starts = [assignments[t["task_id"]] for t in problem["tasks"] if t["turbine_id"] == nxt and t["task_id"] in assignments]
        if next_starts and min(next_starts) < completion[prev]:
            raise ValueError("Turbine sequencing constraint violated")
    total_loss = 0.0
    makespan = 0
    for tid in problem["turbine_ids"]:
        for hour, down in enumerate(turbine_down[tid]):
            if down:
                total_loss += _hourly_loss(config, problem["hour_data"][hour])
                makespan = max(makespan, hour + 1)
    direct_cost = total_duration * config.labor_cost_per_hour
    return {
        "status": "OPTIMAL",
        "total_loss": total_loss,
        "total_duration": total_duration,
        "direct_cost": direct_cost,
        "risk_cost": risk_cost,
        "objective_value": _objective_from_costs(config, total_loss, direct_cost, risk_cost),
        "makespan": makespan,
        "repaired_task_count": len(assignments),
    }


def _solve_with_heuristic(problem: Dict[str, Any]) -> Dict[str, Any]:
    config = problem["config"]
    assignments: Dict[str, int] = {}
    release_hour = 0
    for turbine_id in problem["turbine_ids"]:
        turbine_tasks = [task for task in problem["tasks"] if task["turbine_id"] == turbine_id]
        active = [0] * config.time_horizon
        coverage: Dict[int, int] = {}
        while True:
            best_choice = None
            for task in turbine_tasks:
                if task["task_id"] in assignments:
                    continue
                for start in _feasible_starts(problem, task):
                    if start < release_hour:
                        continue
                    end = start + task["component"].duration_hours
                    if any(active[h] >= config.max_parallel_tasks for h in range(start, end)):
                        continue
                    uncovered = [hour for hour in range(start, end) if coverage.get(hour, 0) == 0]
                    delta_loss = sum(_hourly_loss(config, problem["hour_data"][hour]) for hour in uncovered)
                    delta_direct = task["component"].duration_hours * config.labor_cost_per_hour
                    delta_risk = _delayed_risk_cost(problem, task, start) - _unrepaired_risk_cost(problem, task)
                    delta = _objective_from_costs(config, delta_loss, delta_direct, delta_risk)
                    if best_choice is None or delta < best_choice[0]:
                        best_choice = (delta, start, task)
            if best_choice is None or best_choice[0] >= 0:
                break
            _, start, task = best_choice
            assignments[task["task_id"]] = start
            for hour in range(start, start + task["component"].duration_hours):
                active[hour] += 1
                coverage[hour] = coverage.get(hour, 0) + 1
        turbine_assigned = [assignments[t["task_id"]] + t["component"].duration_hours for t in turbine_tasks if t["task_id"] in assignments]
        if turbine_assigned:
            release_hour = max(turbine_assigned)
    metrics = _evaluate(problem, assignments)
    return {"solver_name": "Heuristic", "assignments": [{"task_id": k, "start_hour": v} for k, v in sorted(assignments.items(), key=lambda item: (item[1], item[0]))], "metrics": metrics}


def _solve_with_ortools(problem: Dict[str, Any]) -> Dict[str, Any]:
    if pywraplp is None:
        return _solve_with_heuristic(problem)
    config = problem["config"]
    solver = pywraplp.Solver.CreateSolver("CBC")
    solver.SetTimeLimit(30000)
    tasks = problem["tasks"]
    feasible = {i: _feasible_starts(problem, task) for i, task in enumerate(tasks)}
    start_vars: Dict[tuple[int, int], Any] = {}
    select_vars: Dict[int, Any] = {}
    start_time_vars: Dict[int, Any] = {}
    end_time_vars: Dict[int, Any] = {}
    completion_vars: Dict[int, Any] = {}
    down_vars: Dict[tuple[int, int], Any] = {}

    for i, task in enumerate(tasks):
        select_vars[i] = solver.BoolVar(f"select_{i}")
        start_time_vars[i] = solver.IntVar(0, config.time_horizon, f"start_time_{i}")
        end_time_vars[i] = solver.IntVar(0, config.time_horizon, f"end_time_{i}")
        for start in feasible[i]:
            start_vars[(i, start)] = solver.BoolVar(f"start_{i}_{start}")
        solver.Add(solver.Sum(start_vars[(i, start)] for start in feasible[i]) == select_vars[i])
        solver.Add(start_time_vars[i] == solver.Sum(start * start_vars[(i, start)] for start in feasible[i]))
        solver.Add(end_time_vars[i] == start_time_vars[i] + task["component"].duration_hours * select_vars[i])

    for turbine_id in problem["turbine_ids"]:
        completion_vars[turbine_id] = solver.IntVar(0, config.time_horizon, f"completion_{turbine_id}")
        turbine_task_indices = [i for i, task in enumerate(tasks) if task["turbine_id"] == turbine_id]
        for i in turbine_task_indices:
            solver.Add(completion_vars[turbine_id] >= end_time_vars[i])

    for prev, nxt in zip(problem["turbine_ids"], problem["turbine_ids"][1:]):
        for i, task in enumerate(tasks):
            if task["turbine_id"] == nxt:
                solver.Add(start_time_vars[i] >= completion_vars[prev] - config.time_horizon * (1 - select_vars[i]))

    for hour in range(config.time_horizon):
        active_terms = []
        for i, task in enumerate(tasks):
            for start in feasible[i]:
                if start <= hour < start + task["component"].duration_hours:
                    active_terms.append(start_vars[(i, start)])
        solver.Add(solver.Sum(active_terms) <= config.max_parallel_tasks)

    for turbine_id in problem["turbine_ids"]:
        turbine_task_indices = [i for i, task in enumerate(tasks) if task["turbine_id"] == turbine_id]
        for hour in range(config.time_horizon):
            down_var = solver.BoolVar(f"down_{turbine_id}_{hour}")
            down_vars[(turbine_id, hour)] = down_var
            hour_terms = [
                start_vars[(i, start)]
                for i in turbine_task_indices
                for start in feasible[i]
                if start <= hour < start + tasks[i]["component"].duration_hours
            ]
            if hour_terms:
                solver.Add(down_var <= solver.Sum(hour_terms))
                for term in hour_terms:
                    solver.Add(down_var >= term)
            else:
                solver.Add(down_var == 0)

    loss_expr = solver.Sum(
        _hourly_loss(config, problem["hour_data"][hour]) * down_vars[(turbine_id, hour)]
        for turbine_id in problem["turbine_ids"]
        for hour in range(config.time_horizon)
    )
    direct_expr = solver.Sum(tasks[i]["component"].duration_hours * config.labor_cost_per_hour * select_vars[i] for i in range(len(tasks)))
    risk_expr = solver.Sum(
        _unrepaired_risk_cost(problem, tasks[i]) * (1 - select_vars[i])
        + solver.Sum(_delayed_risk_cost(problem, tasks[i], start) * start_vars[(i, start)] for start in feasible[i])
        for i in range(len(tasks))
    )

    solver.Minimize(_objective_from_costs(config, loss_expr, direct_expr, risk_expr))
    status_code = solver.Solve()
    status_lookup = {
        pywraplp.Solver.OPTIMAL: "OPTIMAL",
        pywraplp.Solver.FEASIBLE: "FEASIBLE",
        pywraplp.Solver.INFEASIBLE: "INFEASIBLE",
        pywraplp.Solver.UNBOUNDED: "UNBOUNDED",
        pywraplp.Solver.ABNORMAL: "ABNORMAL",
        pywraplp.Solver.NOT_SOLVED: "NOT_SOLVED",
    }
    assignments = [
        {"task_id": tasks[i]["task_id"], "start_hour": start}
        for (i, start), variable in start_vars.items()
        if variable.solution_value() > 0.5
    ]
    metrics = _evaluate(problem, {item["task_id"]: item["start_hour"] for item in assignments})
    metrics["status"] = status_lookup.get(status_code, "UNKNOWN")
    return {"solver_name": "OR-Tools-CBC", "assignments": sorted(assignments, key=lambda item: (item["start_hour"], item["task_id"])), "metrics": metrics}


def _format_summary(problem: Dict[str, Any], result: Dict[str, Any]) -> str:
    config = problem["config"]
    metrics = result["metrics"]
    return (
        f"Solver: {result['solver_name']}\n"
        f"Status: {metrics['status']}\n"
        f"Downtime Loss: {metrics['total_loss']:.2f}\n"
        f"Direct Cost: {metrics['direct_cost']:.2f}\n"
        f"Risk Cost: {metrics['risk_cost']:.2f}\n"
        f"Total Duration Hours: {metrics['total_duration']}\n"
        f"Weighted Objective: {metrics['objective_value']:.2f}\n"
        f"Config: horizon={config.time_horizon}, parallel={config.max_parallel_tasks}, labor_cost_per_hour={config.labor_cost_per_hour}"
    )


def execute_parameter_updates(
    updates: Dict[str, Any] | None = None,
    solver_name: str = "ortools",
) -> Dict[str, Any]:
    config, component_library, applied_updates = apply_problem_updates(updates=updates)
    problem = _build_problem(config, component_library, generate_demo_hour_data(config.time_horizon))
    result = _solve_with_ortools(problem) if solver_name == "ortools" else _solve_with_heuristic(problem)
    return {
        "solver_name": result["solver_name"],
        "parameter_registry": PARAMETER_REGISTRY,
        "applied_updates": applied_updates,
        "applied_config": serialize_config(config),
        "component_library": serialize_component_library(component_library),
        "metrics": result["metrics"],
        "assignments": result["assignments"],
        "summary": _format_summary(problem, result),
    }


def execute_named_strategy_comparison(
    strategy_names: Sequence[str],
    solver_name: str = "ortools",
) -> Dict[str, Any]:
    comparisons = []
    for strategy_name in strategy_names:
        payload = execute_parameter_updates(NAMED_STRATEGIES[strategy_name], solver_name=solver_name)
        comparisons.append({
            "label": strategy_name,
            "updates": NAMED_STRATEGIES[strategy_name],
            "metrics": payload["metrics"],
            "component_library": payload["component_library"],
            "assignments": payload["assignments"],
            "summary": payload["summary"],
        })
    return {"solver_name": solver_name, "comparisons": comparisons}


def main() -> None:
    demo = execute_parameter_updates({"weights": {"loss": 1.1}}, solver_name="ortools")
    print(demo["summary"])


if __name__ == "__main__":
    main()
