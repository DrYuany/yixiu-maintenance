# -*- coding: utf-8 -*-
"""ledger 工具必须真能加载业务台账模块，不能退化成 status=internal。

回归背景：2026-11-10 全量线上链路评测（OD-48）录到真实工具信封
`cannot import name 'product_writeback' from 'app.core'`。原因是
`_ledger_module()` 前一行刚把 maintenance-milp-modeler/scripts 挂进 sys.path，
却用包路径去导一个不在包里的独立模块，导致下发检修单/审批/回填整条闭环不可用。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.agent_tools import AgentToolbox


def _toolbox() -> AgentToolbox:
    return AgentToolbox(
        conversation_id="conv_ledger_test",
        versions=SimpleNamespace(list_by_conversation=lambda cid: []),
        model_artifacts=None,
    )


def test_ledger_module_is_scripts_module_not_app_core() -> None:
    module = AgentToolbox._ledger_module()
    assert module.__name__ == "product_writeback"
    assert module.__file__.replace("\\", "/").endswith(
        "maintenance-milp-modeler/scripts/product_writeback.py"
    )
    for name in ("board", "dispatch", "approve", "record_feedback", "read_rows"):
        assert callable(getattr(module, name)), name


def test_ledger_board_returns_ok_not_internal() -> None:
    """board 是只读动作；只要模块能加载，就不该再返回 internal。"""
    result = _toolbox().execute("ledger", {"action": "board"})
    assert result["status"] != "internal", result
    assert result["status"] == "ok", result
    assert "data" in result
    assert (
        "cannot import name" not in str(result)
    ), "台账模块导入失败的旧错误又回来了"
