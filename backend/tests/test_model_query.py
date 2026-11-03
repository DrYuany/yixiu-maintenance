from __future__ import annotations

from app.core.model_query import detect_model_question, render_status_answer
from app.schemas.agent import WindowVersion


def test_model_question_routing_distinguishes_read_only_queries_from_changes() -> None:
    assert detect_model_question("把这次求解对应的数学模型写出来") == "formula"
    assert detect_model_question("让我看看模型") == "structure"
    assert detect_model_question("方案02的数学模型呢") == "formula"
    assert detect_model_question("船租这个参数怎么来的") == "parameter"
    assert detect_model_question("这是最优吗") == "status"
    assert detect_model_question("任务018为什么没排") == "solution"
    assert detect_model_question("结果呢") == "solution"
    assert detect_model_question("船租上涨 20%，其他不变") is None


def test_capability_questions_do_not_need_a_solution() -> None:
    assert detect_model_question("制定排程时，我有哪些目标函数可以设置权重") == "capability"
    assert detect_model_question("你支持哪些策略") == "capability"
    assert detect_model_question("权重怎么设置") == "capability"
    # 带具体版本的权重是事实问题，交给能读那一版的链路，不能当成能力目录回答。
    assert detect_model_question("这版用了多少权重") != "capability"


def test_environment_questions_route_to_the_environment_scope() -> None:
    assert detect_model_question("接下来一周的风速和电价可视化一下") == "environment"
    assert detect_model_question("明天风大吗") == "environment"
    assert detect_model_question("下周海况怎么样") == "environment"
    # 带“重排”的是变更请求，不该被当成查天气。
    assert detect_model_question("明天风变大，重排一版") is None


def test_capability_answer_comes_from_the_objective_template() -> None:
    from app.core.model_query import render_capability_answer

    answer = render_capability_answer()
    # 指标表在前，「能改的数据」「能加的限制」在后面。
    weight_section = answer.split("**能改的数据**")[0]

    assert "高风险优先抢修" in answer
    assert "成本最低优先" in answer
    assert "总经济成本" in answer
    # 现金支出是「能加的限制」，不是「可加权的目标」，只能出现在限制段。
    assert "现金支出" not in weight_section
    assert "现金支出不超过500万" in answer


def test_status_answer_explains_unknown_stage_in_business_language() -> None:
    version = WindowVersion(
        id="ver_status01",
        conversationId="conv_test",
        generatedAt="2026-10-27T10:00:00+08:00",
        userPrompt="测试",
        focusTag="成本最低优先",
        intentType="generate_version",
        parameterSnapshot={},
        resultSummary={
            "solver": {
                "解状态": "FEASIBLE",
                "终止原因": "TIME_LIMIT",
                "总耗时秒": 300.5,
                "阶段": [
                    {"目标": "总经济成本", "状态": "UNKNOWN", "耗时秒": 299.9},
                ],
            }
        },
    )

    answer = render_status_answer(version)

    assert "FEASIBLE" in answer
    assert "UNKNOWN" not in answer
    assert "在时限内未证明最优" in answer
    assert "总用时：300.5 秒" in answer


def test_status_answer_names_heuristic_fallback_instead_of_claiming_a_feasible_solve() -> None:
    version = WindowVersion(
        id="ver_status02",
        conversationId="conv_test",
        generatedAt="2026-10-29T15:35:00+08:00",
        userPrompt="测试",
        focusTag="成本最低优先",
        intentType="generate_version",
        parameterSnapshot={},
        resultSummary={
            "scheduledTaskCount": 15,
            "totalTaskCount": 28,
            "solver": {
                "有解": True,
                "解状态": "FEASIBLE",
                "方案来源": "HEURISTIC_FALLBACK",
                "终止原因": "TIME_LIMIT",
                "阶段": [
                    {"目标": "总经济成本", "状态": "UNKNOWN", "耗时秒": 299.9},
                ],
            },
        },
    )

    answer = render_status_answer(version)

    assert "启发式" in answer
    assert "15/28" in answer
    assert "已复核可行方案" not in answer
