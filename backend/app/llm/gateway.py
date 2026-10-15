"""LLM gateway: the ONLY place BriefDesk talks to a model.

Every agent call goes through the same guards, cheapest first:

    1. run budget       a research run may make at most RUN_MAX_LLM_CALLS calls (no runaway agent loops)
    2. cache            identical request (model + prompt + input) -> stored answer, 0 calls
    3. circuit breaker  per model: after N consecutive provider failures, stop calling it for a cool-down
    4. token pacer      per model sliding 60 s window of tokens: WAIT (and tell the user) instead of
                        hitting Groq's free-tier 429s
    5. bounded retry    transient errors only (timeout, 5xx, 429 with a short Retry-After), backoff + jitter;
                        a malformed structured answer is retried once; bad key / unknown model never

Agents run in background jobs, so pacing is fine: a report that waits 10 s is better than one that fails.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections import OrderedDict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel

from ..core.config import get_settings

log = logging.getLogger(__name__)

Tier = Literal["fast", "large", "router"]
PROMPT_VERSION = "v1"  # bump when prompts change so cached answers are not reused


class LLMUnavailable(Exception):
    """The model could not be used (outage, budget, misconfiguration). Agents degrade gracefully."""

    def __init__(self, reason: str, kind: str = "failed"):
        super().__init__(reason)
        self.reason, self.kind = reason, kind


class BudgetExceeded(LLMUnavailable):
    def __init__(self, reason: str):
        super().__init__(reason, "budget")


# ---------------------------------------------------------------- per-run context
@dataclass
class RunContext:
    """Set for the duration of one agent run (thread-local via ContextVar)."""
    max_calls: int
    calls: int = 0
    tokens: int = 0
    notify: Callable[[str], None] | None = None     # "pacing for the Groq free tier ..." -> chat trace

    def charge(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"run budget of {self.max_calls} LLM calls used up")
        self.calls += 1


run_context: ContextVar[RunContext | None] = ContextVar("run_context", default=None)


# ---------------------------------------------------------------- guards
class CircuitBreaker:
    """closed -> (N failures) -> open -> (cool-down) -> half_open -> 1 probe -> closed | open"""

    def __init__(self, failure_threshold: int, reset_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.failure_threshold, self.reset_seconds, self._clock = failure_threshold, reset_seconds, clock
        self._failures = 0
        self._opened_until = 0.0
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        if self._opened_until == 0.0:
            return "closed"
        return "open" if self._clock() < self._opened_until else "half_open"

    def allow(self) -> bool:
        with self._lock:
            state = self.state
            if state == "closed":
                return True
            if state == "half_open" and not self._probe_in_flight:
                self._probe_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures, self._opened_until, self._probe_in_flight = 0, 0.0, False

    def record_failure(self, open_for: float | None = None) -> None:
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if open_for is not None or self._failures >= self.failure_threshold or self._opened_until:
                duration = open_for if open_for is not None else self.reset_seconds
                self._opened_until = self._clock() + duration
                log.warning("LLM circuit OPEN for %.0fs after %d failure(s)", duration, self._failures)


class TokenPacer:
    """Sliding 60-second token window per model. reserve() blocks until the request fits."""

    def __init__(self, tokens_per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.capacity, self._clock, self._sleep = tokens_per_minute, clock, sleep
        self._window: deque[list] = deque()          # [timestamp, tokens]
        self._lock = threading.Lock()

    def _used(self, now: float) -> int:
        while self._window and now - self._window[0][0] >= 60:
            self._window.popleft()
        return sum(t for _, t in self._window)

    def reserve(self, estimate: int, max_wait: float, on_wait: Callable[[float], None] | None = None) -> list:
        """Returns a reservation (adjust it later with the real usage). Raises LLMUnavailable on timeout."""
        estimate = min(estimate, self.capacity)
        waited, told = 0.0, False
        while True:
            with self._lock:
                now = self._clock()
                used = self._used(now)
                if used + estimate <= self.capacity or not self._window:
                    entry = [now, estimate]
                    self._window.append(entry)
                    return entry
                # wait until enough of the oldest entries leave the window
                free_needed, delay = used + estimate - self.capacity, 0.0
                for ts, tok in self._window:
                    free_needed -= tok
                    delay = 60 - (now - ts)
                    if free_needed <= 0:
                        break
            delay = max(0.2, min(delay + 0.1, 10))
            if waited + delay > max_wait:
                raise LLMUnavailable("model is at its free-tier token limit; try again in a minute", "rate_limit")
            if on_wait and not told:
                on_wait(delay)
                told = True
            self._sleep(delay)
            waited += delay

    def settle(self, entry: list, actual: int) -> None:
        with self._lock:
            entry[1] = actual


class TTLCache:
    def __init__(self, ttl_seconds: float, max_items: int = 500, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.max_items, self._clock = ttl_seconds, max_items, clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            if self._clock() > item[0]:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return item[1]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (self._clock() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)


# ---------------------------------------------------------------- error policy
def _is_transient(exc: Exception) -> bool:
    try:
        import groq
        if isinstance(exc, (groq.APIConnectionError, groq.InternalServerError, groq.RateLimitError)):
            return True
    except ImportError:  # pragma: no cover
        pass
    return isinstance(exc, (TimeoutError, ConnectionError))


def _is_config_error(exc: Exception) -> bool:
    try:
        import groq
        return isinstance(exc, (groq.AuthenticationError, groq.PermissionDeniedError, groq.NotFoundError))
    except ImportError:  # pragma: no cover
        return False


def _is_bad_output(exc: Exception) -> bool:
    """The model produced invalid JSON / tool call: worth exactly one more try, never an outage."""
    text = str(exc)
    return "tool_use_failed" in text or "json_validate_failed" in text or "ValidationError" in type(exc).__name__ \
        or "OutputParserException" in type(exc).__name__


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    value = getattr(response, "headers", {}).get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def estimate_tokens(messages: list, max_out: int) -> int:
    chars = sum(len(m.content if hasattr(m, "content") and isinstance(m.content, str) else str(m)) for m in messages)
    return int(chars / 3.6) + int(max_out * 0.6) + 60


# ---------------------------------------------------------------- providers
class Provider:
    """What the gateway needs from a model backend. Each call returns (result, tokens_used)."""

    def model_name(self, tier: Tier) -> str: ...
    def structured(self, tier: Tier, kind: str, messages: list, schema: type[BaseModel]) -> tuple[BaseModel, int]: ...
    def text(self, tier: Tier, kind: str, messages: list, on_token, max_tokens: int) -> tuple[str, int]: ...
    def tools(self, tier: Tier, kind: str, messages: list, tools: list[dict]) -> tuple[Any, int]: ...


class GroqProvider(Provider):
    def __init__(self, api_key: str, models: dict[str, str], timeout: float):
        from langchain_groq import ChatGroq

        # max_retries=0: the gateway owns retries, so one call never fans out into hidden extra calls.
        # Reasoning tokens count against the per-minute budget: gpt-oss reasons briefly ("low"), other
        # reasoning models (qwen) keep their reasoning hidden and short.
        self._models = models

        def make(name: str):
            extra = {"reasoning_effort": "low"} if "gpt-oss" in name else {"reasoning_format": "hidden"}
            return ChatGroq(model=name, api_key=api_key, temperature=0.2, max_retries=0, timeout=timeout,
                            max_tokens=2500, **extra)
        self._llm = {tier: make(name) for tier, name in models.items()}

    def model_name(self, tier: Tier) -> str:
        return self._models[tier]

    @staticmethod
    def _usage(msg) -> int:
        usage = getattr(msg, "usage_metadata", None) or {}
        return int(usage.get("total_tokens") or 0)

    def structured(self, tier, kind, messages, schema):
        out = self._llm[tier].with_structured_output(schema, method="json_schema", include_raw=True).invoke(messages)
        if out.get("parsing_error") or out.get("parsed") is None:
            raise ValueError(f"OutputParserException: {out.get('parsing_error')}")
        parsed = out["parsed"]
        return (parsed if isinstance(parsed, schema) else schema.model_validate(parsed)), self._usage(out["raw"])

    def text(self, tier, kind, messages, on_token, max_tokens):
        llm = self._llm[tier].bind(max_tokens=max_tokens)
        if on_token is None:
            msg = llm.invoke(messages)
            return str(msg.content), self._usage(msg)
        parts, tokens = [], 0
        for chunk in llm.stream(messages):
            if chunk.content:
                parts.append(chunk.content)
                on_token(chunk.content)
            tokens = max(tokens, self._usage(chunk))
        return "".join(parts), tokens

    def tools(self, tier, kind, messages, tools):
        msg = self._llm[tier].bind_tools(tools).invoke(messages)
        return msg, self._usage(msg)


# ---------------------------------------------------------------- gateway
@dataclass
class LLMGateway:
    provider: Provider
    max_attempts: int = 3
    max_wait: float = 45
    tokens_per_minute: int = 8000
    breaker_failures: int = 4
    breaker_reset: float = 60
    cache: TTLCache = field(default_factory=lambda: TTLCache(3600))
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._pacers: dict[str, TokenPacer] = {}
        self._lock = threading.Lock()
        self.stats: dict[str, Any] = {"requests": 0, "llm_calls": 0, "cache_hits": 0, "retries": 0, "waits": 0,
                                      "failures": 0, "tokens": 0, "by_kind": {}}

    # -------------------------------------------------------------- public API
    def structured(self, kind: str, messages: list, schema: type[BaseModel], tier: Tier = "router",
                   use_cache: bool = True) -> BaseModel:
        key = self._key(kind, tier, messages, schema.__name__) if use_cache else None
        return self._run(kind, tier, messages, 900, key,
                         lambda: self.provider.structured(tier, kind, messages, schema))

    def text(self, kind: str, messages: list, tier: Tier = "large", on_token: Callable[[str], None] | None = None,
             max_tokens: int = 2200, use_cache: bool = True) -> str:
        key = self._key(kind, tier, messages, str(max_tokens)) if use_cache and on_token is None else None
        return self._run(kind, tier, messages, max_tokens, key,
                         lambda: self.provider.text(tier, kind, messages, on_token, max_tokens))

    def tools(self, kind: str, messages: list, tools: list[dict], tier: Tier = "fast"):
        return self._run(kind, tier, messages, 600, None, lambda: self.provider.tools(tier, kind, messages, tools))

    def health(self) -> dict:
        with self._lock:
            return {**{k: v for k, v in self.stats.items() if k != "by_kind"},
                    "by_kind": dict(self.stats["by_kind"]),
                    "models": {t: self.provider.model_name(t) for t in ("router", "fast", "large")},
                    "breakers": {m: b.state for m, b in self._breakers.items()}}

    # -------------------------------------------------------------- core
    def _bump(self, key: str, n: int = 1, kind: str | None = None) -> None:
        """Global counter; llm_calls / tokens are also tallied per agent kind (admin dashboard)."""
        with self._lock:
            self.stats[key] += n
            if kind:
                per_kind = self.stats["by_kind"].setdefault(kind, {"llm_calls": 0, "tokens": 0})
                per_kind[key] += n

    def _guards(self, model: str) -> tuple[CircuitBreaker, TokenPacer]:
        with self._lock:
            if model not in self._breakers:
                self._breakers[model] = CircuitBreaker(self.breaker_failures, self.breaker_reset)
                self._pacers[model] = TokenPacer(self.tokens_per_minute, sleep=self.sleep)
            return self._breakers[model], self._pacers[model]

    def _key(self, kind: str, tier: str, messages: list, extra: str) -> str:
        body = json.dumps([getattr(m, "type", "?") + ":" + str(getattr(m, "content", m)) for m in messages])
        raw = "|".join([self.provider.model_name(tier), PROMPT_VERSION, kind, extra, body])
        return hashlib.sha256(raw.encode()).hexdigest()

    def _run(self, kind: str, tier: Tier, messages: list, max_out: int, cache_key: str | None, call):
        self._bump("requests")
        ctx = run_context.get()
        if cache_key and (hit := self.cache.get(cache_key)) is not None:
            self._bump("cache_hits")
            return hit
        model = self.provider.model_name(tier)
        breaker, pacer = self._guards(model)
        if not breaker.allow():
            self._bump("failures")
            raise LLMUnavailable(f"{model} is temporarily unavailable (circuit open)", "circuit_open")

        def on_wait(delay: float) -> None:
            self._bump("waits")
            if ctx and ctx.notify:
                ctx.notify(f"Pacing for the Groq free-tier token limit ({delay:.0f}s)…")

        attempt, bad_output_retried = 0, False
        while True:
            attempt += 1
            if ctx:
                ctx.charge()
            reservation = pacer.reserve(estimate_tokens(messages, max_out), self.max_wait, on_wait)
            self._bump("llm_calls", kind=kind)
            start = time.perf_counter()
            try:
                result, tokens = call()
            except Exception as exc:  # noqa: BLE001 - classified below
                pacer.settle(reservation, reservation[1] // 3)   # a failed call still used some budget
                retry_after = _retry_after(exc)
                if _is_bad_output(exc) and not bad_output_retried:
                    bad_output_retried = True
                    self._bump("retries")
                    log.warning("llm %s returned malformed output, retrying once", kind)
                    continue
                if _is_transient(exc) and attempt < self.max_attempts \
                        and (retry_after is None or retry_after <= self.max_wait):
                    self._bump("retries")
                    delay = retry_after if retry_after is not None else \
                        min(8.0, 0.75 * 2 ** (attempt - 1)) + random.uniform(0, 0.3)
                    if retry_after and retry_after > 2:
                        on_wait(retry_after)
                    log.warning("llm transient error, retrying", extra={
                        "kind": kind, "error": type(exc).__name__, "attempt": attempt, "delay_s": round(delay, 2)})
                    self.sleep(delay)
                    continue
                self._bump("failures")
                if _is_config_error(exc):
                    breaker.record_failure(open_for=600)
                    log.error("llm configuration error (check GROQ_API_KEY / model names): %s", type(exc).__name__)
                    raise LLMUnavailable("the AI provider rejected the configuration (API key or model)",
                                         "config") from exc
                if _is_transient(exc):
                    breaker.record_failure(open_for=retry_after if retry_after and retry_after > 0 else None)
                log.warning("llm %s failed", kind, extra={"error": type(exc).__name__, "attempt": attempt,
                                                          "breaker": breaker.state})
                raise LLMUnavailable(f"the AI model failed ({type(exc).__name__})") from exc

            pacer.settle(reservation, tokens or reservation[1])
            breaker.record_success()
            self._bump("tokens", tokens, kind=kind)
            if ctx:
                ctx.tokens += tokens
            log.info("llm %s", kind, extra={"model": model, "attempt": attempt, "tokens": tokens,
                                            "latency_ms": round((time.perf_counter() - start) * 1000)})
            if cache_key:
                self.cache.put(cache_key, result)
            return result


_gateway: LLMGateway | None = None
_gateway_lock = threading.Lock()


def get_gateway() -> LLMGateway:
    """The process-wide gateway. Raises LLMUnavailable when no key is configured (UI shows how to fix)."""
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            s = get_settings()
            if s.llm_mode == "fake":
                from .fake import FakeProvider
                provider: Provider = FakeProvider()
            else:
                if not s.groq_api_key:
                    raise LLMUnavailable("GROQ_API_KEY is not set in backend/.env", "config")
                provider = GroqProvider(s.groq_api_key, {"router": s.model_router, "fast": s.model_fast,
                                                         "large": s.model_large}, s.llm_timeout_seconds)
            tpm = 10**9 if s.llm_mode == "fake" else s.model_tokens_per_minute
            _gateway = LLMGateway(provider, max_attempts=s.llm_max_attempts, max_wait=s.llm_max_wait_seconds,
                                  tokens_per_minute=tpm, breaker_failures=s.llm_breaker_failures,
                                  breaker_reset=s.llm_breaker_reset_seconds, cache=TTLCache(s.llm_cache_ttl_seconds))
        return _gateway


def set_gateway(gateway: LLMGateway | None) -> None:
    """Tests inject a gateway with a scripted provider."""
    global _gateway
    with _gateway_lock:
        _gateway = gateway


def llm_status() -> dict:
    s = get_settings()
    try:
        gw = get_gateway()
    except LLMUnavailable as e:
        return {"mode": s.llm_mode, "ready": False, "problem": e.reason}
    return {"mode": s.llm_mode, "ready": True, **gw.health()}
