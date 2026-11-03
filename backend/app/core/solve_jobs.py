from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.db.database import Database, dumps, loads


JobStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]
SolverStatus = Literal["OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE"]


# 求解器会在预算处自己收工，之后还有独立复核。给一小段宽限，避免"刚好卡在
# 预算上"和弹出询问撞在一起；只有超预算仍在跑才真的需要问用户要不要继续等。
DECISION_GRACE_SECONDS = 30


def _effective_budget(request: dict[str, Any] | None) -> int:
    """本次真实时限：budgetSeconds 优先，老任务退回 seconds，最后才兜底。

    必须和 _with_elapsed 用同一个口径，否则"进度说 180 秒、170 秒就弹询问"。
    """
    request = request or {}
    try:
        return max(1, int(request.get("budgetSeconds") or request.get("seconds") or 60))
    except (TypeError, ValueError):
        return 60


class SolveJob(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    conversationId: str = Field(validation_alias="conversation_id")
    status: JobStatus
    solverStatus: SolverStatus = "UNKNOWN"
    submittedAt: str
    startedAt: str | None = None
    finishedAt: str | None = None
    elapsedSeconds: int = 0
    needsUserDecision: bool = False
    nextDecisionAt: int = 180
    backgrounded: bool = False
    hasIncumbent: bool = False
    modelFingerprint: str = ""
    phase: str = "queued"
    progressPercent: int = 5
    statusMessage: str = "正在锁定数据快照"
    snapshot: dict[str, Any] | None = None
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class SolveJobRepository:
    def __init__(self, database: Database, soft_limit_seconds: int = 180) -> None:
        self.database = database
        self.soft_limit_seconds = soft_limit_seconds

    def create(
        self,
        conversation_id: str,
        request_payload: dict[str, Any],
        model_fingerprint: str,
        snapshot: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> SolveJob:
        submitted = now or datetime.now(timezone.utc)
        job = SolveJob(
            id=f"job_{uuid4().hex[:12]}",
            conversationId=conversation_id,
            status="QUEUED",
            submittedAt=submitted.isoformat(),
            modelFingerprint=model_fingerprint,
            snapshot=snapshot,
            request=request_payload,
            # 询问阈值跟着本次预算走，别再钉死在 180：预算给到 180 秒时，
            # 正好撞上旧阈值会让用户在求解器即将收工时被无故打断。
            nextDecisionAt=_effective_budget(request_payload) + DECISION_GRACE_SECONDS,
        )
        self._save(job)
        return job

    def get(self, job_id: str, now: datetime | None = None) -> SolveJob | None:
        row = self.database.fetch_one("SELECT payload FROM solve_jobs WHERE id = ?", (job_id,))
        if not row:
            return None
        job = SolveJob(**loads(row["payload"]))
        return self._with_elapsed(job, now or datetime.now(timezone.utc))

    def list_by_conversation(self, conversation_id: str) -> list[SolveJob]:
        rows = self.database.fetch_all(
            "SELECT payload FROM solve_jobs WHERE conversation_id = ? ORDER BY submitted_at DESC",
            (conversation_id,),
        )
        now = datetime.now(timezone.utc)
        return [self._with_elapsed(SolveJob(**loads(row["payload"])), now) for row in rows]

    def latest_for_conversation(self, conversation_id: str) -> SolveJob | None:
        row = self.database.fetch_one(
            "SELECT payload FROM solve_jobs WHERE conversation_id = ? ORDER BY submitted_at DESC LIMIT 1",
            (conversation_id,),
        )
        if not row:
            return None
        return self._with_elapsed(SolveJob(**loads(row["payload"])), datetime.now(timezone.utc))

    def recover_interrupted_jobs(self) -> int:
        rows = self.database.fetch_all("SELECT payload FROM solve_jobs")
        recovered = 0
        for row in rows:
            job = SolveJob(**loads(row["payload"]))
            if job.status not in {"QUEUED", "RUNNING"}:
                continue
            self.mark_failed(job.id, "服务重启导致本次求解中断，请重新发起；这不代表模型无解。")
            recovered += 1
        return recovered

    def mark_running(self, job_id: str, now: datetime | None = None) -> SolveJob:
        job = self._require(job_id)
        updated = job.model_copy(
            update={"status": "RUNNING", "startedAt": (now or datetime.now(timezone.utc)).isoformat(),
                    "phase": "solving", "progressPercent": 35, "statusMessage": "正在构建并求解单窗口模型"}
        )
        self._save(updated)
        return updated

    def mark_completed(
        self,
        job_id: str,
        result: dict[str, Any],
        solver_status: SolverStatus,
        has_incumbent: bool | None = None,
        now: datetime | None = None,
        status_message: str | None = None,
    ) -> SolveJob:
        job = self._require(job_id)
        finished = now or datetime.now(timezone.utc)
        elapsed = self._elapsed(job, finished)
        incumbent = (
            solver_status in {"OPTIMAL", "FEASIBLE"}
            if has_incumbent is None
            else has_incumbent
        )
        if status_message:
            message = status_message
        elif not incumbent and solver_status == "INFEASIBLE":
            # 没产出方案却写"排程与独立复核已完成"，是另一种"说一套做一套"：
            # 任务状态说完成了，界面上什么方案都没有。
            message = "已证明在给定约束下无解，未产出方案"
        elif not incumbent:
            message = "达到时限，未找到可行方案（未证明无解）"
        else:
            message = "排程与独立复核已完成"
        updated = job.model_copy(
            update={
                "status": "COMPLETED",
                "solverStatus": solver_status,
                "hasIncumbent": incumbent,
                "finishedAt": finished.isoformat(),
                "elapsedSeconds": elapsed,
                "needsUserDecision": False,
                "result": result,
                "phase": "completed",
                "progressPercent": 100,
                "statusMessage": message,
            }
        )
        self._save(updated)
        return updated

    def mark_failed(self, job_id: str, error: str, now: datetime | None = None) -> SolveJob:
        job = self._require(job_id)
        finished = now or datetime.now(timezone.utc)
        updated = job.model_copy(
            update={
                "status": "FAILED",
                "solverStatus": "UNKNOWN",
                "finishedAt": finished.isoformat(),
                "elapsedSeconds": self._elapsed(job, finished),
                "needsUserDecision": False,
                "error": error,
                "phase": "failed",
                "statusMessage": "执行失败，未将其标记为无解",
            }
        )
        self._save(updated)
        return updated

    def set_backgrounded(self, job_id: str, backgrounded: bool = True) -> SolveJob:
        job = self._require(job_id)
        updated = job.model_copy(update={"backgrounded": backgrounded, "needsUserDecision": False})
        self._save(updated)
        return updated

    def mark_reviewing(self, job_id: str) -> SolveJob:
        job = self._require(job_id)
        updated = job.model_copy(update={"phase": "reviewing", "progressPercent": 85, "statusMessage": "正在独立复核费用与约束"})
        self._save(updated)
        return updated

    def defer_decision(self, job_id: str, seconds: int | None = None, now: datetime | None = None) -> SolveJob:
        job = self.get(job_id, now=now)
        if not job:
            raise ValueError("Solve job not found")
        delay = seconds or self.soft_limit_seconds
        updated = job.model_copy(
            update={
                "backgrounded": False,
                "needsUserDecision": False,
                "nextDecisionAt": job.elapsedSeconds + delay,
            }
        )
        self._save(updated)
        return updated

    def mark_cancelled(self, job_id: str) -> SolveJob:
        job = self._require(job_id)
        now = datetime.now(timezone.utc)
        updated = job.model_copy(
            update={
                "status": "CANCELLED",
                "solverStatus": "UNKNOWN",
                "finishedAt": now.isoformat(),
                "elapsedSeconds": self._elapsed(job, now),
                "needsUserDecision": False,
            }
        )
        self._save(updated)
        return updated

    def _require(self, job_id: str) -> SolveJob:
        job = self.get(job_id)
        if not job:
            raise ValueError("Solve job not found")
        return job

    def _save(self, job: SolveJob) -> None:
        self.database.execute(
            """
            INSERT INTO solve_jobs (id, conversation_id, submitted_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
            """,
            (job.id, job.conversationId, job.submittedAt, dumps(job.model_dump())),
        )

    def _with_elapsed(self, job: SolveJob, now: datetime) -> SolveJob:
        """RUNNING 时按已用时长推算进度，避免长时间停在 35%。

        CP-SAT 求解期间没有可靠的细粒度进度，用「已用时长 / 时限」在 35%→80% 区间
        线性推进，配合真实秒数；求解结束由 mark_reviewing / mark_completed 接管。
        """
        if job.status != "RUNNING":
            return job
        elapsed = self._elapsed(job, now)
        # budgetSeconds 是 prepare 时按参数总表算好的真实时限；老任务没有这个
        # 字段才退回 seconds，最后才用 60 兜底（曾经写 300，和实际跑的 30 对不上）。
        max_seconds = _effective_budget(job.request)
        ratio = min(1.0, elapsed / max_seconds)
        progress = min(80, 35 + int(45 * ratio))
        if elapsed <= max_seconds:
            message = f"正在求解（已运行 {elapsed} 秒，时限 {max_seconds} 秒）"
        else:
            message = f"已超时限 {max_seconds} 秒，仍在求解（已运行 {elapsed} 秒）"
        return job.model_copy(
            update={
                "elapsedSeconds": elapsed,
                # 预算之内绝不打扰；只有真的超时且仍没算完才请用户决定。
                "needsUserDecision": (
                    elapsed > max_seconds
                    and elapsed >= job.nextDecisionAt
                    and not job.backgrounded
                ),
                "progressPercent": progress,
                "statusMessage": message,
            }
        )

    @staticmethod
    def _elapsed(job: SolveJob, now: datetime) -> int:
        anchor = job.startedAt or job.submittedAt
        started = datetime.fromisoformat(anchor)
        return max(0, int((now - started).total_seconds()))

