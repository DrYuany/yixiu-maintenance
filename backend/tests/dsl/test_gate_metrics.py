"""闸门分层归因计数：只验统计口径，不重跑任何判定逻辑。

对应 docs/2026-11-02-Shi双视图DSL逐条机制对照与借鉴.md 的 A3。
借的是 Shi et al. 的 Compiler-ER / Runtime-ER 分层思路，机制零新增。
"""
from __future__ import annotations

from dsl_core import gate_metrics as gm
from dsl_core.runner import DslError


# ───────────────────────── 分类：六类各自命中 ─────────────────────────


def test_intent_violation_is_classified_before_compile() -> None:
    """原话对账是最早的一道门，同时给信号时它先算。"""
    assert gm.classify(
        utterance_audit={"通过": False, "违例": ["..."]},
        compile_errors=["未知原语"],
    ) == gm.INTENT_VIOLATION


def test_compile_reject() -> None:
    assert gm.classify(compile_errors=["未知字段: foo"]) == gm.COMPILE_REJECT


def test_solve_incomplete_is_not_infeasible() -> None:
    assert gm.classify(diagnosis={"结论": "未算完"}) == gm.SOLVE_INCOMPLETE


def test_proven_infeasible() -> None:
    assert gm.classify(diagnosis={"结论": "不可行"}) == gm.PROVEN_INFEASIBLE


def test_unlocated_is_its_own_class() -> None:
    """diagnose 第三种结论必须单列，不能算进「真不可行」。"""
    assert gm.classify(diagnosis={"结论": "未能定位"}) == gm.UNLOCATED
    assert gm.UNLOCATED in gm.GATE_CLASSES


def test_artifact_violation() -> None:
    assert gm.classify(reconcile_audit={"通过": False, "违例": ["..."]}) == gm.ARTIFACT_VIOLATION


def test_audit_without_pass_field_falls_back_to_violation_list() -> None:
    assert gm.classify(reconcile_audit={"违例": ["..."]}) == gm.ARTIFACT_VIOLATION


def test_passed_case_is_not_blocked() -> None:
    assert gm.classify(passed=True) == gm.NOT_BLOCKED


def test_no_evidence_is_unclassified_not_guessed() -> None:
    assert gm.classify() == gm.UNCLASSIFIED


def test_unknown_diagnosis_verdict_is_unclassified() -> None:
    assert gm.classify(diagnosis={"结论": "某种新结论"}) == gm.UNCLASSIFIED


def test_unverified_audit_also_blocks() -> None:
    """未核对是证据空白、不是违例，但闸门照样拦；分层只回答落在哪道门。"""
    assert gm.classify(utterance_audit={"通过": False, "未核对": ["..."]}) == gm.INTENT_VIOLATION


# ───────────────────────── 从 DslError 反推 ─────────────────────────


def test_from_dsl_error_compile() -> None:
    assert gm.from_dsl_error(DslError(["未知原语 foo"])) == gm.COMPILE_REJECT


def test_from_dsl_error_diagnosis_wins_over_error_text() -> None:
    exc = DslError(["没找到解"], diagnosis={"结论": "未算完"})
    assert gm.from_dsl_error(exc) == gm.SOLVE_INCOMPLETE


def test_from_dsl_error_infeasible_with_proof() -> None:
    exc = DslError(["..."], diagnosis={"结论": "不可行"})
    assert gm.from_dsl_error(exc) == gm.PROVEN_INFEASIBLE


def test_from_dsl_error_unlocated() -> None:
    exc = DslError(["..."], diagnosis={"结论": "未能定位"})
    assert gm.from_dsl_error(exc) == gm.UNLOCATED


def test_from_dsl_error_reconcile_prefix() -> None:
    exc = DslError(["意图对账未通过：约束2 must 未排入"])
    assert gm.from_dsl_error(exc) == gm.ARTIFACT_VIOLATION


def test_from_dsl_error_independent_verify_prefix() -> None:
    exc = DslError(["求解结果未通过独立复核：库存不足"])
    assert gm.from_dsl_error(exc) == gm.ARTIFACT_VIOLATION


def test_from_dsl_error_empty_is_unclassified() -> None:
    assert gm.from_dsl_error(DslError([])) == gm.UNCLASSIFIED


def test_dsl_error_exposes_gate_class_property() -> None:
    assert DslError(["未知原语"]).gate_class == gm.COMPILE_REJECT
    assert DslError(["x"], diagnosis={"结论": "未算完"}).gate_class == gm.SOLVE_INCOMPLETE


# ───────────────────────── 计数与分布表 ─────────────────────────


def test_tally_counts_shares_and_buckets() -> None:
    labels = [
        gm.INTENT_VIOLATION, gm.COMPILE_REJECT,
        gm.SOLVE_INCOMPLETE, gm.PROVEN_INFEASIBLE, gm.UNLOCATED,
        gm.ARTIFACT_VIOLATION, gm.NOT_BLOCKED,
    ]
    report = gm.tally(labels)
    assert report["总数"] == 7
    assert report["被拦下"] == 6
    assert report["按类计数"][gm.COMPILE_REJECT] == 1
    assert report["按桶计数"][gm.COMPILER_BUCKET] == 2
    assert report["按桶计数"][gm.RUNTIME_BUCKET] == 4
    assert abs(sum(report["按类占比"].values()) - 1.0) < 1e-6


def test_tally_flags_unknown_labels_instead_of_dropping_them() -> None:
    report = gm.tally([gm.COMPILE_REJECT, "编译期拒 "])  # 尾部空格拼错
    assert report["未识别标签"] == ["编译期拒 "]
    assert report["按类计数"][gm.COMPILE_REJECT] == 1


def test_tally_empty() -> None:
    assert gm.tally([])["总数"] == 0


def test_format_tally_readable() -> None:
    text = gm.format_tally(gm.tally([gm.COMPILE_REJECT, gm.SOLVE_INCOMPLETE]))
    assert "闸门分布" in text
    assert "编译期拒：1 次" in text
    assert "分桶" in text


def test_format_tally_no_samples() -> None:
    assert "没有样本" in gm.format_tally(gm.tally([]))


def test_classify_records_accepts_both_labels_and_signals() -> None:
    records = [
        {"gate": gm.PROVEN_INFEASIBLE},
        {"compile_errors": ["x"]},
        {"diagnosis": {"结论": "未算完"}},
        {"passed": True},
    ]
    assert gm.classify_records(records) == [
        gm.PROVEN_INFEASIBLE, gm.COMPILE_REJECT, gm.SOLVE_INCOMPLETE, gm.NOT_BLOCKED,
    ]


def test_cli_outputs_a_distribution_table(tmp_path) -> None:
    """命令行能读 jsonl 出表：这是复盘/测评真正调用的入口。"""
    import json

    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in [
                {"id": "c1", "compile_errors": ["未知原语"]},
                {"id": "c2", "diagnosis": {"结论": "未算完"}},
                {"id": "c3", "diagnosis": {"结论": "不可行"}},
                {"id": "c4", "utterance_audit": {"通过": False, "违例": ["x"]}},
                {"id": "c5", "passed": True},
            ]
        ),
        encoding="utf-8",
    )

    assert gm.main([str(path), "--json"]) == 0


def test_cli_flags_unknown_label_with_nonzero_exit(tmp_path) -> None:
    import json

    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"gate": "打错的标签"}, ensure_ascii=False), encoding="utf-8")

    assert gm.main([str(path)]) == 1
