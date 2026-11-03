from __future__ import annotations

import time

import pytest

from app.core.tool_runtime import CircuitBreaker, ToolRuntime, ToolRuntimeError


def _spec(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "name": "t",
        "input_schema": {"type": "object", "properties": properties or {}, "required": required or []},
    }


def _ok(args: dict) -> dict:
    return {"status": "ok", "data": args}


def test_validate_reports_missing_required() -> None:
    spec = _spec(properties={"instruction": {"type": "string"}}, required=["instruction"])
    assert ToolRuntime.validate({}, spec) == ["缺少必需参数: instruction"]


def test_validate_reports_bad_enum() -> None:
    spec = _spec(properties={"scope": {"type": "string", "enum": ["fleet", "inventory"]}})
    errors = ToolRuntime.validate({"scope": "windows"}, spec)
    assert any("scope" in error for error in errors)


def test_validate_reports_bad_scalar_type() -> None:
    spec = _spec(properties={"days": {"type": "integer"}})
    assert any("days" in error for error in ToolRuntime.validate({"days": "seven"}, spec))


def test_validate_accepts_valid_args() -> None:
    spec = _spec(
        properties={
            "days": {"type": "integer"},
            "severity": {"type": "string", "enum": ["高", "中", "低"]},
        },
        required=["days"],
    )
    assert ToolRuntime.validate({"days": 7, "severity": "高"}, spec) == []


def test_invalid_arguments_raises_before_handler() -> None:
    runtime = ToolRuntime(read_only_tools=set())
    spec = _spec(properties={"days": {"type": "integer"}}, required=["days"])
    called = False

    def handler(_args):
        nonlocal called
        called = True
        return _ok(_args)

    with pytest.raises(ToolRuntimeError) as exc:
        runtime.run("t", {}, spec, handler)
    assert exc.value.status == "invalid_arguments"
    assert called is False


def test_circuit_breaker_opens_after_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_s=10.0)
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.allow() is True  # 还没到阈值
    breaker.record_failure()
    assert breaker.allow() is False  # 已打开


def test_circuit_breaker_open_blocks_until_recovery() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_s=60.0)
    breaker.record_failure()
    assert breaker.allow() is False  # 未到恢复时间，拒绝


def test_circuit_breaker_half_open_probe() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_s=0.0)
    breaker.record_failure()
    assert breaker.state == CircuitBreaker.OPEN
    assert breaker.allow() is True  # 半开放行一次探测
    assert breaker.state == CircuitBreaker.HALF_OPEN
    breaker.record_success()
    assert breaker.state == CircuitBreaker.CLOSED


def test_runtime_opens_circuit_on_repeated_failure() -> None:
    runtime = ToolRuntime(read_only_tools=set(), failure_threshold=2, recovery_seconds=30.0)
    spec = _spec()

    def handler(_args):
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(ToolRuntimeError) as exc:
            runtime.run("t", {}, spec, handler)
        assert exc.value.status == "internal"
    with pytest.raises(ToolRuntimeError) as exc:
        runtime.run("t", {}, spec, handler)
    assert exc.value.status == "not_available"


def test_retry_only_read_only_tools() -> None:
    calls = {"n": 0}

    def handler(_args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return _ok({})

    # 只读工具：第一次失败后重试成功
    runtime = ToolRuntime(read_only_tools={"read_x"}, max_retries=1)
    outcome = runtime.run("read_x", {}, _spec(), handler)
    assert outcome.retried is True
    assert calls["n"] == 2

    # 写工具：不重试
    calls["n"] = 0
    runtime = ToolRuntime(read_only_tools=set(), max_retries=1)
    with pytest.raises(ToolRuntimeError):
        runtime.run("write_x", {}, _spec(), handler)
    assert calls["n"] == 1


def test_cache_hit_only_when_whitelisted() -> None:
    calls = {"n": 0}

    def handler(args):
        calls["n"] += 1
        return _ok(args)

    runtime = ToolRuntime(read_only_tools={"read_catalog"}, cache_ttl_seconds=10.0, cache_whitelist={"read_catalog"})
    spec = _spec()
    first = runtime.run("read_catalog", {"a": 1}, spec, handler)
    second = runtime.run("read_catalog", {"a": 1}, spec, handler)
    assert first.cached is False
    assert second.cached is True
    assert calls["n"] == 1
    assert runtime.stats()["read_catalog"]["cached_hits"] == 1


def test_timeout_read_only_tool() -> None:
    runtime = ToolRuntime(read_only_tools={"slow"}, timeout_seconds=0.05)

    def handler(_args):
        time.sleep(0.3)
        return _ok({})

    with pytest.raises(ToolRuntimeError) as exc:
        runtime.run("slow", {}, _spec(), handler)
    assert exc.value.status == "timeout"


def test_metrics_track_success_and_failure() -> None:
    runtime = ToolRuntime(read_only_tools=set())
    spec = _spec()

    def ok_handler(_args):
        return _ok({})

    def bad_handler(_args):
        raise RuntimeError("x")

    runtime.run("a", {}, spec, ok_handler)
    with pytest.raises(ToolRuntimeError):
        runtime.run("b", {}, spec, bad_handler)

    stats = runtime.stats()
    assert stats["a"]["success_rate"] == 1.0
    assert stats["b"]["success_rate"] == 0.0
    assert stats["b"]["consecutive_fails"] == 1
