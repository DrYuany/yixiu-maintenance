"""read_jobs：让模型看得见自己派出去的活。

这个工具不改变任何状态，只是把 SolveJobRepository 里本来就有的东西
递给模型。以前那套接口只给服务端和前端用，模型够不着——于是用户问
「算完了吗」，它只能让人自己去方案页面看。
"""

from __future__ import annotations

from pathlib import Path

from app.core.agent_tools import AgentToolbox
from app.core.solve_jobs import SolveJobRepository
from app.db.database import Database
from app.db.models import ModelArtifactRepository, VersionRepository


def _toolbox(temp_db_path: Path, *, with_jobs: bool = True) -> AgentToolbox:
    db = Database(temp_db_path)
    return AgentToolbox(
        conversation_id="conv_jobs",
        versions=VersionRepository(db),
        model_artifacts=ModelArtifactRepository(db),
        jobs=SolveJobRepository(db) if with_jobs else None,
    )


def _submit(repo: SolveJobRepository, conversation_id: str = "conv_jobs") -> str:
    job = repo.create(conversation_id, {"strategy": "成本最低优先"}, "fp_test")
    return job.id


def test_without_repository_it_says_so_instead_of_pretending(temp_db_path: Path) -> None:
    """没接任务仓库就明说做不到，不能返回一个空列表让模型以为「没派过活」。"""
    result = _toolbox(temp_db_path, with_jobs=False).execute("read_jobs", {})

    assert result["status"] == "unavailable"
    assert "查不到求解进展" in result["data"]["message"]


def test_empty_conversation_is_ok_but_says_nothing_submitted(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_jobs", {})

    assert result["status"] == "ok"
    assert result["data"]["count"] == 0
    assert any("还没派出过求解" in item for item in result["nextActions"])


def test_lists_only_this_conversation(temp_db_path: Path) -> None:
    """别的对话派的活不能串到这儿来——串了就会答错「你的方案算完了」。"""
    db = Database(temp_db_path)
    repo = SolveJobRepository(db)
    mine = _submit(repo, "conv_jobs")
    _submit(repo, "conv_other")

    toolbox = _toolbox(temp_db_path)
    result = toolbox.execute("read_jobs", {})

    ids = [item["jobId"] for item in result["data"]["jobs"]]
    assert ids == [mine]


def test_status_is_translated_for_the_model(temp_db_path: Path) -> None:
    """机器码 RUNNING 模型编不出中文，翻成「正在算」它才会说人话。"""
    db = Database(temp_db_path)
    repo = SolveJobRepository(db)
    job_id = _submit(repo)
    repo.mark_running(job_id)

    result = _toolbox(temp_db_path).execute("read_jobs", {"jobId": job_id})

    facts = result["data"]["jobs"][0]
    assert facts["status"] == "RUNNING"
    assert facts["statusZh"] == "正在算"


def test_completed_job_exposes_version_and_summary(temp_db_path: Path) -> None:
    db = Database(temp_db_path)
    repo = SolveJobRepository(db)
    job_id = _submit(repo)
    repo.mark_running(job_id)
    repo.mark_completed(
        job_id,
        {"versionId": "ver_00000009", "summary": "最晚完工 2026-11-18，比上一版早两天"},
        "FEASIBLE",
    )

    result = _toolbox(temp_db_path).execute("read_jobs", {"jobId": job_id})

    facts = result["data"]["jobs"][0]
    assert facts["statusZh"] == "算完了"
    assert facts["versionId"] == "ver_00000009"
    assert "早两天" in facts["summary"]
    assert facts["summary"] != ""


def test_unknown_job_id_is_not_found_with_a_way_out(temp_db_path: Path) -> None:
    result = _toolbox(temp_db_path).execute("read_jobs", {"jobId": "job_不存在的"})

    assert result["status"] == "not_found"
    assert any("不传 jobId" in item for item in result["nextActions"])


def test_job_from_another_conversation_is_not_readable_by_id(temp_db_path: Path) -> None:
    db = Database(temp_db_path)
    repo = SolveJobRepository(db)
    other = _submit(repo, "conv_other")

    result = _toolbox(temp_db_path).execute("read_jobs", {"jobId": other})

    assert result["status"] == "not_found"
    assert "不属于当前对话" in result["data"]["message"]


def test_limit_is_clamped_instead_of_crashing(temp_db_path: Path) -> None:
    """limit=999 不该炸，也不该把 20 条以外的东西吐出来；负数也不该返回空。"""
    db = Database(temp_db_path)
    repo = SolveJobRepository(db)
    for _ in range(5):
        _submit(repo)

    toolbox = _toolbox(temp_db_path)
    assert len(toolbox.execute("read_jobs", {"limit": 999})["data"]["jobs"]) == 5
    assert len(toolbox.execute("read_jobs", {"limit": -3})["data"]["jobs"]) == 1


def test_wrong_parameter_type_comes_back_as_a_reason_not_an_exception(temp_db_path: Path) -> None:
    """模型猜参数类型是常态。类型不对要回一句它能读懂的话让它自己改，
    而不是抛异常把整轮对话打断。"""
    result = _toolbox(temp_db_path).execute("read_jobs", {"limit": "很多"})

    assert result["status"] == "invalid_arguments"
    assert "integer" in result["data"]["message"]
