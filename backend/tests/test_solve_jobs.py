from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.solve_jobs import SolveJobRepository
from app.db.database import Database


def test_solve_job_is_persisted_and_soft_prompt_does_not_relabel_unknown(temp_db_path) -> None:
    repository = SolveJobRepository(Database(temp_db_path))
    submitted = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    job = repository.create(
        conversation_id="conv-1",
        request_payload={"strategy": "高风险优先抢修"},
        model_fingerprint="model-1",
        now=submitted,
    )

    repository.mark_running(job.id, now=submitted + timedelta(seconds=1))
    persisted = repository.get(job.id, now=submitted + timedelta(seconds=181))

    assert persisted is not None
    assert persisted.status == "RUNNING"
    assert persisted.solverStatus == "UNKNOWN"
    assert persisted.needsUserDecision is True
    assert persisted.elapsedSeconds == 180

    deferred = repository.defer_decision(job.id, seconds=180, now=submitted + timedelta(seconds=181))
    assert deferred.needsUserDecision is False
    assert repository.get(job.id, now=submitted + timedelta(seconds=200)).needsUserDecision is False
    assert repository.get(job.id, now=submitted + timedelta(seconds=362)).needsUserDecision is True


def test_completed_job_survives_repository_recreation(temp_db_path) -> None:
    database = Database(temp_db_path)
    first = SolveJobRepository(database)
    job = first.create("conv-1", {"strategy": "成本最低优先"}, "model-1")
    first.mark_completed(job.id, {"summary": "ok"}, solver_status="FEASIBLE")

    restored = SolveJobRepository(Database(temp_db_path)).get(job.id)

    assert restored is not None
    assert restored.status == "COMPLETED"
    assert restored.solverStatus == "FEASIBLE"
    assert restored.result == {"summary": "ok"}


def test_interrupted_running_jobs_are_recovered_as_unknown_failures(temp_db_path) -> None:
    repository = SolveJobRepository(Database(temp_db_path))
    job = repository.create("conv-1", {"strategy": "成本最低优先"}, "model-1")
    repository.mark_running(job.id)

    recovered = SolveJobRepository(Database(temp_db_path)).recover_interrupted_jobs()
    restored = repository.get(job.id)

    assert recovered == 1
    assert restored.status == "FAILED"
    assert restored.solverStatus == "UNKNOWN"
    assert "服务重启" in restored.error


def test_completed_without_solution_does_not_claim_finished(temp_db_path) -> None:
    """没产出方案却写"已完成"，用户看到的是"成功了但什么也没有"。

    这跟兜底冒充结果是同一类：状态字段说的话，跟产物里的事实对不上。
    """
    repository = SolveJobRepository(Database(temp_db_path))
    job = repository.create("conv-1", {"strategy": "成本最低优先"}, "model-1")

    infeasible = repository.mark_completed(
        job.id, {"summary": "不可行"}, solver_status="INFEASIBLE", has_incumbent=False
    )
    assert "无解" in infeasible.statusMessage
    assert "已完成" not in infeasible.statusMessage

    timeout = repository.mark_completed(
        job.id, {"summary": "超时"}, solver_status="UNKNOWN", has_incumbent=False
    )
    assert "未证明无解" in timeout.statusMessage


def test_completed_with_solution_keeps_the_normal_message(temp_db_path) -> None:
    repository = SolveJobRepository(Database(temp_db_path))
    job = repository.create("conv-1", {"strategy": "成本最低优先"}, "model-1")

    done = repository.mark_completed(job.id, {"summary": "ok"}, solver_status="OPTIMAL")

    assert done.statusMessage == "排程与独立复核已完成"


def test_running_job_progress_advances_with_elapsed_time(temp_db_path) -> None:
    """求解期间进度按「已用时长/时限」在 35→80 之间推进，不再长期停在 35。"""
    repository = SolveJobRepository(Database(temp_db_path))
    submitted = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    job = repository.create(
        conversation_id="conv-progress",
        request_payload={"strategy": "成本最低优先", "seconds": 100},
        model_fingerprint="model-1",
        now=submitted,
    )
    repository.mark_running(job.id, now=submitted)

    early = repository.get(job.id, now=submitted + timedelta(seconds=5))
    halfway = repository.get(job.id, now=submitted + timedelta(seconds=50))
    late = repository.get(job.id, now=submitted + timedelta(seconds=95))

    assert 35 <= early.progressPercent < halfway.progressPercent < late.progressPercent <= 80
    assert "已运行" in halfway.statusMessage
    assert halfway.elapsedSeconds == 50


def test_decision_prompt_follows_the_budget_not_a_fixed_180(temp_db_path) -> None:
    """预算拉到 180 秒后，不能在 180 秒就弹「是否继续」打断求解。

    以前 nextDecisionAt 钉死 180，预算一拉长正好撞上；现在阈值跟着预算走，
    且预算内一律不打扰，只有超预算仍在跑才问。
    """
    from app.core.solve_jobs import DECISION_GRACE_SECONDS

    repository = SolveJobRepository(Database(temp_db_path))
    submitted = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    job = repository.create(
        conversation_id="conv-budget",
        request_payload={"strategy": "成本最低优先", "budgetSeconds": 180},
        model_fingerprint="model-1",
        now=submitted,
    )
    assert job.nextDecisionAt == 180 + DECISION_GRACE_SECONDS
    repository.mark_running(job.id, now=submitted)

    at_limit = repository.get(job.id, now=submitted + timedelta(seconds=180))
    just_after = repository.get(job.id, now=submitted + timedelta(seconds=181))
    assert at_limit.needsUserDecision is False
    assert just_after.needsUserDecision is False
    assert "时限 180 秒" in just_after.statusMessage

    over_grace = repository.get(job.id, now=submitted + timedelta(seconds=211))
    assert over_grace.needsUserDecision is True
    assert "已超时限" in over_grace.statusMessage


def test_longer_user_budget_does_not_prompt_at_old_180(temp_db_path) -> None:
    """用户点名 300 秒时，别在旧的 180 秒阈值上提前打断。"""
    repository = SolveJobRepository(Database(temp_db_path))
    submitted = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    job = repository.create(
        conversation_id="conv-long",
        request_payload={"strategy": "成本最低优先", "seconds": 300, "budgetSeconds": 300},
        model_fingerprint="model-1",
        now=submitted,
    )
    repository.mark_running(job.id, now=submitted)

    assert repository.get(job.id, now=submitted + timedelta(seconds=200)).needsUserDecision is False
    assert repository.get(job.id, now=submitted + timedelta(seconds=331)).needsUserDecision is True
