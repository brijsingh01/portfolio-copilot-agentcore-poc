"""OpenTelemetry (ADOT) setup and W3C trace-context propagation helpers.

On AgentCore Runtime the container is launched with `opentelemetry-instrument` (ADOT),
which installs the global TracerProvider and exports to CloudWatch/X-Ray. Locally or in
tests, `configure_tracing` installs an SDK provider (OTLP/HTTP or a supplied exporter).

Carriers covered:
  * HTTP headers  (Orchestrator -> Gateway, MCP `POST /mcp`, A2A `POST /`)
  * JSON-RPC 2.0  (`params.metadata.traceparent` on A2A messages, survives proxies)
  * Lambda        (Gateway client_context.custom, event `_meta`, or `_X_AMZN_TRACE_ID`)
"""
from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import baggage, context as otel_context, propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import Getter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"
BAGGAGE = "baggage"
_PROPAGATION_KEYS = (TRACEPARENT, TRACESTATE, BAGGAGE)

class _CaseInsensitiveGetter(Getter[Mapping[str, str]]):
    """Header lookup that tolerates `Traceparent`, `X-Amzn-Trace-Id`, etc."""

    def get(self, carrier: Mapping[str, str], key: str) -> list[str] | None:
        value = carrier.get(key.lower())
        return [value] if value else None

    def keys(self, carrier: Mapping[str, str]) -> list[str]:
        return list(carrier.keys())


_GETTER = _CaseInsensitiveGetter()


def _build_propagator() -> CompositePropagator:
    propagators = [TraceContextTextMapPropagator(), W3CBaggagePropagator()]
    try:  # X-Ray header interop for Lambda / Gateway when ADOT is installed
        from opentelemetry.propagators.aws import AwsXRayPropagator

        propagators.append(AwsXRayPropagator())
    except ImportError:
        pass
    return CompositePropagator(propagators)


def configure_tracing(service_name: str, exporter: SpanExporter | None = None) -> trace.Tracer:
    """Configure propagation + provider (reusing ADOT's if present) and return a tracer."""
    propagate.set_global_textmap(_build_propagator())
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if exporter is None and (
            os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        ):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    return trace.get_tracer(service_name)


def get_tracer(name: str = "portfolio-copilot") -> trace.Tracer:
    return trace.get_tracer(name)


# --------------------------------------------------------------------------- HTTP headers
def inject_headers(headers: MutableMapping[str, str] | None = None, ctx: otel_context.Context | None = None) -> dict[str, str]:
    """Return `headers` with traceparent/tracestate/baggage for the active (or given) context."""
    carrier: dict[str, str] = dict(headers or {})
    propagate.inject(carrier, context=ctx)
    return carrier


def extract_context(carrier: Mapping[str, Any] | None) -> otel_context.Context:
    """Extract a context from a case-insensitive header mapping."""
    normalized = {str(k).lower(): str(v) for k, v in (carrier or {}).items() if v is not None}
    return propagate.extract(normalized, getter=_GETTER)


def current_traceparent() -> str | None:
    return inject_headers().get(TRACEPARENT)


def current_trace_id() -> str | None:
    span_ctx = trace.get_current_span().get_span_context()
    return format(span_ctx.trace_id, "032x") if span_ctx.is_valid else None


# --------------------------------------------------------------------------- JSON-RPC 2.0
def inject_jsonrpc(request: dict[str, Any]) -> dict[str, Any]:
    """Copy trace context into `params.metadata` of a JSON-RPC request (in place)."""
    metadata = request.setdefault("params", {}).setdefault("metadata", {})
    metadata.update({k: v for k, v in inject_headers().items() if k in _PROPAGATION_KEYS})
    return request


def extract_jsonrpc(request: Mapping[str, Any], headers: Mapping[str, Any] | None = None) -> otel_context.Context:
    """HTTP headers win; fall back to `params.metadata` / `params._meta` for hops that strip headers."""
    header_ctx = extract_context(headers)
    if trace.get_current_span(header_ctx).get_span_context().is_valid:
        return header_ctx
    params = request.get("params") or {}
    if not isinstance(params, Mapping):
        return header_ctx
    # A2A carries it in `params.metadata`, MCP in `params._meta`.
    for key in ("metadata", "_meta"):
        if isinstance(params.get(key), Mapping):
            ctx = extract_context(params[key])
            if trace.get_current_span(ctx).get_span_context().is_valid:
                return ctx
    return header_ctx


# --------------------------------------------------------------------------- Lambda
def extract_lambda(event: Mapping[str, Any], lambda_context: Any = None) -> otel_context.Context:
    """Resolve parent context for a Gateway-invoked Lambda target."""
    candidates: list[Mapping[str, Any]] = []
    custom = getattr(getattr(lambda_context, "client_context", None), "custom", None)
    if isinstance(custom, Mapping):
        candidates.append(custom)
    for key in ("_meta", "headers"):
        if isinstance(event.get(key), Mapping):
            candidates.append(event[key])
    candidates.append(event)
    if xray := os.getenv("_X_AMZN_TRACE_ID"):
        candidates.append({"x-amzn-trace-id": xray})
    for carrier in candidates:
        ctx = extract_context(carrier)
        if trace.get_current_span(ctx).get_span_context().is_valid:
            return ctx
    return otel_context.get_current()


# --------------------------------------------------------------------------- spans
def _record_error(span: Span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))


@contextmanager
def server_span(name: str, parent: otel_context.Context, attributes: Mapping[str, Any] | None = None,
                tracer: trace.Tracer | None = None) -> Iterator[Span]:
    """Start a SERVER span continuing an extracted remote parent."""
    token = otel_context.attach(parent)
    try:
        with (tracer or get_tracer()).start_as_current_span(
            name, kind=SpanKind.SERVER, attributes=dict(attributes or {}), record_exception=False
        ) as span:
            try:
                yield span
            except BaseException as exc:
                _record_error(span, exc)
                raise
    finally:
        otel_context.detach(token)


@contextmanager
def client_span(name: str, attributes: Mapping[str, Any] | None = None,
                tracer: trace.Tracer | None = None) -> Iterator[tuple[Span, dict[str, str]]]:
    """Start a CLIENT span and yield `(span, headers)` carrying its traceparent."""
    with (tracer or get_tracer()).start_as_current_span(
        name, kind=SpanKind.CLIENT, attributes=dict(attributes or {}), record_exception=False
    ) as span:
        try:
            yield span, inject_headers()
        except BaseException as exc:
            _record_error(span, exc)
            raise


@contextmanager
def internal_span(name: str, attributes: Mapping[str, Any] | None = None,
                  tracer: trace.Tracer | None = None) -> Iterator[Span]:
    with (tracer or get_tracer()).start_as_current_span(
        name, kind=SpanKind.INTERNAL, attributes=dict(attributes or {}), record_exception=False
    ) as span:
        try:
            yield span
        except BaseException as exc:
            _record_error(span, exc)
            raise


@contextmanager
def with_baggage(**entries: str) -> Iterator[None]:
    """Attach non-sensitive correlation IDs (e.g. advisor/client IDs) as W3C baggage.

    Baggage is for observability correlation only; authorization always re-validates the JWT.
    """
    ctx = otel_context.get_current()
    for key, value in entries.items():
        ctx = baggage.set_baggage(key, value, context=ctx)
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)
