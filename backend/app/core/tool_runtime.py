"""工具运行时治理层。

把一次工具调用从“找到 handler 直接跑”升级成一条受控链路：

  1. Schema 校验 —— 执行前按 input_schema 校验必填、枚举和标量类型，
     非法参数直接返回 `invalid_arguments`，不进入 handler。
  2. 熔断 —— 连续失败达到阈值后打开熔断，半开后探测恢复。
  3. 重试 —— 只对只读工具重试一次；写侧（solve / ledger）绝不重试，避免重复副作用。
  4. 缓存 —— 只读工具按 (会话, 工具, 参数) 做 TTL 缓存；默认关闭，避免读到过期台账。
  5. 超时 —— 只读工具可配置超时，超时返回 `timeout`，不把卡死描述成无数据。
  6. 指标 —— 每次调用记录 success / latency / 连续失败 / 命中缓存 / 重试次数。

错误分类必须可机读。固定状态如下，模型只能基于这些状态说话：

  ok / not_found / not_available / invalid_arguments / timeout / internal
  needs_confirmation / clarify（由 handler 自己返回，不经运行时改写）

设计原则：默认行为与旧版完全一致（无超时线程、无缓存、无重试副作用），
所有治理能力按需开启，保证现有 111 个测试不回归。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

# 只读工具白名单：这些工具没有副作用，允许重试与缓存。
READ_ONLY_TOOLS = {
    "read_versions",
    "read_environment",
    "read_catalog",
    "read_alarms",
    "read_site",
}


class ToolRuntimeError(RuntimeError):
    """工具运行时产生的、需要转成信封的错误。status 与 message 一起携带。"""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class ToolOutcome:
    """一次工具调用的运行时结果，不含业务信封。"""

    data: Any
    cached: bool = False
    retried: bool = False
    latency_ms: float = 0.0


@dataclass
class ToolMetrics:
    """单工具运行时统计，供监控与评测读取。"""

    total: int = 0
    success: int = 0
    failed: int = 0
    total_latency_ms: float = 0.0
    consecutive_fails: int = 0
    cached_hits: int = 0
    retries: int = 0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total else 0.0

    def record_success(self, latency_ms: float) -> None:
        self.total += 1
        self.success += 1
        self.total_latency_ms += latency_ms
        self.consecutive_fails = 0

    def record_failure(self) -> None:
        self.total += 1
        self.failed += 1
        self.consecutive_fails += 1


class CircuitBreaker:
    """三态熔断器：CLOSED -> OPEN -> HALF_OPEN -> CLOSED。

    连续失败 failure_threshold 次后打开；打开 recovery_s 秒后进入 HALF_OPEN 探测；
    探测成功则关闭，失败则重新打开。
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, recovery_s: float = 60.0) -> None:
        self.threshold = failure_threshold
        self.recovery_s = recovery_s
        self.state = self.CLOSED
        self.fail_count = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.state == self.CLOSED:
            return True
        if self.state == self.OPEN:
            if time.monotonic() - (self.opened_at or 0.0) >= self.recovery_s:
                self.state = self.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN：放行一次探测

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = self.CLOSED

    def record_failure(self) -> None:
        self.fail_count += 1
        if self.state == self.HALF_OPEN or self.fail_count >= self.threshold:
            self.state = self.OPEN
            self.opened_at = time.monotonic()


class ToolCache:
    """带 TTL 的进程内缓存。ttl<=0 表示关闭。"""

    def __init__(self, ttl_seconds: float = 0.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._store: dict[Any, tuple[float, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def get(self, key: Any) -> Any | None:
        if not self.enabled:
            return None
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: Any) -> None:
        if not self.enabled:
            return
        self._store[key] = (time.monotonic() + self.ttl_seconds, value)


class ToolRuntime:
    """围绕单个 handler 调用的治理层。

    read_only_tools：允许重试与缓存的白名单。
    timeout_seconds：只读工具的调用超时；None 表示不走线程，直接内联执行（默认）。
    """

    def __init__(
        self,
        *,
        read_only_tools: set[str] | frozenset[str] | None = None,
        timeout_seconds: float | None = None,
        max_retries: int = 1,
        cache_ttl_seconds: float = 0.0,
        cache_whitelist: set[str] | frozenset[str] | None = None,
        failure_threshold: int = 5,
        recovery_seconds: float = 60.0,
    ) -> None:
        self.read_only_tools = set(read_only_tools) if read_only_tools else set(READ_ONLY_TOOLS)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, int(max_retries))
        self.cache_whitelist = set(cache_whitelist) if cache_whitelist else set()
        self.cache = ToolCache(cache_ttl_seconds)
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._breakers: dict[str, CircuitBreaker] = {}
        self._metrics: dict[str, ToolMetrics] = {}
        self._executor: ThreadPoolExecutor | None = None

    # ── 校验 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def validate(arguments: Any, spec: dict[str, Any]) -> list[str]:
        """按 input_schema 校验参数，返回错误列表（空表示通过）。

        刻意保持宽松：只校验 required、enum 和标量类型，不校验 array/object 的
        内部结构与 unknown 字段（现有 schema 未设 additionalProperties:false），
        避免把 handler 本来就容忍的调用误判成非法。
        """
        if not isinstance(arguments, dict):
            return ["工具参数必须是 JSON 对象"]
        schema = spec.get("input_schema") or {}
        errors: list[str] = []
        for field_name in schema.get("required", []):
            if field_name not in arguments:
                errors.append(f"缺少必需参数: {field_name}")
        properties = schema.get("properties", {})
        type_map = {"string": str, "number": (int, float), "integer": int, "boolean": bool}
        for key, value in arguments.items():
            prop = properties.get(key)
            if not isinstance(prop, dict):
                continue
            expected_type = prop.get("type")
            enum = prop.get("enum")
            if enum and ToolRuntime._violates_enum(value, expected_type, enum):
                errors.append(f"参数 {key} 不在允许值内: {', '.join(map(str, enum))}")
                continue
            if expected_type in type_map and not ToolRuntime._matches_scalar(value, expected_type, type_map):
                errors.append(f"参数 {key} 类型错误，期望 {expected_type}")
        return errors

    @staticmethod
    def _matches_scalar(value: Any, expected_type: str, type_map: dict[str, Any]) -> bool:
        expected = type_map[expected_type]
        if isinstance(expected, tuple):
            return isinstance(value, expected) and not isinstance(value, bool)
        return isinstance(value, expected) and not (expected is int and isinstance(value, bool))

    @staticmethod
    def _violates_enum(value: Any, expected_type: str | None, enum: list[Any]) -> bool:
        if expected_type == "array":
            return isinstance(value, list) and any(item not in enum for item in value)
        if expected_type in {"string", "integer", "number"}:
            return value not in enum
        return False

    # ── 执行 ──────────────────────────────────────────────────────────────────

    def run(
        self,
        name: str,
        arguments: dict[str, Any],
        spec: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
    ) -> ToolOutcome:
        metrics = self._metrics.setdefault(name, ToolMetrics())
        breaker = self._breakers.setdefault(
            name, CircuitBreaker(self.failure_threshold, self.recovery_seconds)
        )

        errors = self.validate(arguments, spec)
        if errors:
            raise ToolRuntimeError("invalid_arguments", "；".join(errors))

        if not breaker.allow():
            raise ToolRuntimeError(
                "not_available",
                f"工具 {name} 连续失败已触发熔断，暂时不可用，请稍后重试。",
            )

        cache_key = self._cache_key(name, arguments)
        if self._cacheable(name) and (cached := self.cache.get(cache_key)) is not None:
            metrics.cached_hits += 1
            return ToolOutcome(cached, cached=True)

        attempt = 0
        last_error: Exception | None = None
        while True:
            try:
                t0 = time.monotonic()
                data = self._invoke(name, handler, arguments)
                latency_ms = (time.monotonic() - t0) * 1000
                metrics.record_success(latency_ms)
                breaker.record_success()
                if self._cacheable(name):
                    self.cache.set(cache_key, data)
                return ToolOutcome(data, latency_ms=latency_ms, retried=attempt > 0)
            except ToolRuntimeError:
                raise
            except Exception as exc:  # handler 自身异常
                last_error = exc
                attempt += 1
                metrics.record_failure()
                breaker.record_failure()
                if attempt <= self.max_retries and self._retryable(name):
                    metrics.retries += 1
                    continue
                break

        raise ToolRuntimeError("internal", str(last_error or "工具执行失败"))

    def _invoke(self, name: str, handler: Callable, arguments: dict[str, Any]) -> Any:
        if self.timeout_seconds is None or name not in self.read_only_tools:
            return handler(arguments)
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-runtime")
        future = self._executor.submit(handler, arguments)
        try:
            return future.result(timeout=self.timeout_seconds)
        except FutureTimeout:
            future.cancel()
            raise ToolRuntimeError(
                "timeout",
                f"工具 {name} 超过 {self.timeout_seconds:.0f}s 未返回，已放弃本次读取。",
            ) from None

    def _retryable(self, name: str) -> bool:
        return name in self.read_only_tools

    def _cacheable(self, name: str) -> bool:
        return self.cache.enabled and name in self.cache_whitelist

    @staticmethod
    def _cache_key(name: str, arguments: dict[str, Any]) -> tuple:
        try:
            args = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args = repr(arguments)
        return (name, args)

    # ── 统计 ──────────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "total": metrics.total,
                "success_rate": round(metrics.success_rate, 3),
                "avg_latency_ms": round(metrics.avg_latency_ms, 1),
                "consecutive_fails": metrics.consecutive_fails,
                "cached_hits": metrics.cached_hits,
                "retries": metrics.retries,
                "circuit_state": self._breakers.get(name, CircuitBreaker()).state,
            }
            for name, metrics in self._metrics.items()
        }
