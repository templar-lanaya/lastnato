"""OpenTelemetry instrumentation for the Investor Services Copilot.

Rebuilt against the team's own observability.py (reference project) --
same shape, adapted only where our domain genuinely differs. See the
adaptation notes below each renamed/dropped piece; nothing here is a
silent substitution.

Two things live here, same split as the reference:
1. `configure_tracing()` -- builds ONE TracerProvider and attaches
   whichever exporters you ask for. Call it once, at process start.
2. `production_exporters()` -- Application Insights as primary,
   TreeSpanExporter always alongside it, so local visibility survives
   whether Azure Monitor is reachable at runtime OR even constructable
   (e.g. missing connection string).

NOTE: an earlier version of this file also included OTelCallbackHandler,
a LangChain callback-handler bridge. Removed -- every real agent file in
this project (account_agent.py, knowledge_agent.py, fulfillment_agent.py,
compliance_reviewer_agent.py, intake_router_agent.py, orchestrator.py,
tools_1.py) is built on agent_framework, not LangChain, and agent_framework
has no equivalent callback system for this class to hook into. It was only
ever relevant to investor_tools_v2.py / investor_tools_langchain_demo.py,
both superseded by tools_1.py. If a LangChain-based path returns to this
project later, that class's implementation is recoverable from version
history rather than needing to be rewritten from scratch.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Sequence
from uuid import UUID, uuid4

from dotenv import load_dotenv
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, Status, StatusCode

load_dotenv()

SERVICE_NAME = "investor-services-copilot"
TRACER_NAME = "investor-services-copilot.instrumentation"

# ADAPTATION: reference uses "tenant" (multi-tenant B2B support platform).
# We don't have tenants -- our natural correlation unit is one rep/investor
# interaction. Same mechanism (baggage), different key.
INTERACTION_BAGGAGE_KEY = "interaction_id"
AGENT_BAGGAGE_KEY = "agent"  # unchanged -- Knowledge/Account/Fulfillment Agent maps directly

# ADAPTATION: reference pulls this from a shared .config module we don't
# have. Sourced from an env var here instead, same purpose (which
# prompt/instruction version produced this trace).
INSTRUCTION_SET_VERSION = os.environ.get("INSTRUCTION_SET_VERSION", "v1")

CAPTURE_CONTENT = os.getenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false")


def set_capture_content(enabled: bool) -> None:
    global CAPTURE_CONTENT
    CAPTURE_CONTENT = enabled


def set_interaction(interaction_id: str):
    """Put the interaction id on the OTel context as baggage -- rides
    alongside the trace context so a span created deep inside an agent
    can ask "which interaction is this?" without threading a parameter
    through every signature. Returns a token for context.detach.
    """
    return context.attach(baggage.set_baggage(INTERACTION_BAGGAGE_KEY, interaction_id))


def current_interaction() -> str:
    return str(baggage.get_baggage(INTERACTION_BAGGAGE_KEY) or "unknown")


def set_agent(name: str):
    """Name the agent currently executing, for every span it produces."""
    return context.attach(baggage.set_baggage(AGENT_BAGGAGE_KEY, name))


def current_agent() -> str:
    return str(baggage.get_baggage(AGENT_BAGGAGE_KEY) or "")


# Which attributes the tree printer bothers to show. ADAPTATION: "ticket."
# swapped for "rule." (our evaluate_policy_rule outputs: rule_id, decision,
# source_doc) and "account." added.
#
# NAMING CONVENTION, worth getting right: attributes must use DOTTED
# namespacing to be printed -- "account.id", not "account_id". This isn't
# enforced anywhere; a span attribute using an underscore just silently
# won't show up in the tree (still present on the span object, still
# exported to Application Insights -- only the LOCAL tree printer skips
# it). Caught this exact trap testing this file: passing account_id="..."
# to traced_run()'s **attributes produced a span attribute that never
# printed, purely because of the underscore vs dot.
PRINTED_ATTRIBUTE_PREFIXES = ("gen_ai.", "account.", "rule.", "retrieval.",
                               "correlation_id", "instruction_set.", "error.")


class SpanCollector(SpanExporter):
    """In-memory exporter -- the same spans get read more than once: to
    draw a tree, to compute latency percentiles, to harvest an eval set.
    """

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        return None


class TreeSpanExporter(SpanCollector):
    """Dev-only exporter: buffers spans, prints them as an indented tree
    on shutdown(). A real backend (Application Insights) draws this
    waterfall for you; locally we do it ourselves so the shape of a
    trace is visible without leaving the terminal.
    """

    def shutdown(self) -> None:
        by_parent: dict[str | None, list[ReadableSpan]] = {}
        for span in self.spans:
            parent = format(span.parent.span_id, "016x") if span.parent else None
            by_parent.setdefault(parent, []).append(span)

        def emit(parent_id: str | None, depth: int) -> None:
            for span in sorted(by_parent.get(parent_id, []), key=lambda s: s.start_time):
                ms = (span.end_time - span.start_time) / 1_000_000
                mark = "!" if span.status.status_code is StatusCode.ERROR else " "
                print(f"{mark}{'  ' * depth}{span.name}  ({ms:.0f} ms)")
                operation = span.attributes.get("gen_ai.operation.name")
                for key in sorted(span.attributes):
                    if key == "gen_ai.agent.name" and not operation:
                        continue
                    if key.startswith(PRINTED_ATTRIBUTE_PREFIXES):
                        print(f" {'  ' * depth}    {key} = {span.attributes[key]}")
                emit(format(span.context.span_id, "016x"), depth + 1)

        print("\n--- trace ---")
        emit(None, 0)


def azure_monitor_exporter(connection_string: str | None = None) -> SpanExporter:
    """Application Insights exporter -- the brief's Section 4.3 target.
    Not in the reference file (it uses langfuse_exporter.py as a separate,
    pluggable module instead) -- same "one function builds one exporter"
    pattern, just our actual production target instead of Langfuse.

    NOT verified against a live resource -- no Azure credentials in this
    sandbox. Verified: the exporter object constructs against the real SDK,
    AND that a failing/unreachable endpoint at RUNTIME does not affect any
    other exporter running alongside it (OTel's SpanProcessor swallows a
    failed export() call and logs a warning rather than raising).

    Raises ValueError immediately if there's no connection string -- this
    is a CONSTRUCTION-time failure, different from a runtime send failure.
    See production_exporters() below if you want construction failures to
    fall back gracefully instead of raising.
    """
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

    conn_str = connection_string or os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn_str:
        raise ValueError(
            "No Application Insights connection string -- pass connection_string= "
            "or set APPLICATIONINSIGHTS_CONNECTION_STRING."
        )
    return AzureMonitorTraceExporter(connection_string=conn_str)


def production_exporters(connection_string: str | None = None) -> list[SpanExporter]:
    """Application Insights as primary, TreeSpanExporter always alongside
    it -- so local visibility survives regardless of whether Azure Monitor
    is reachable AT RUNTIME (already true of any two exporters run
    together; see azure_monitor_exporter()'s docstring) AND regardless of
    whether it can even be CONSTRUCTED (e.g. missing connection string) --
    which azure_monitor_exporter() alone does not protect against, since
    it raises immediately in that case.

    Use like: configure_tracing(exporters=production_exporters())
    """
    exporters: list[SpanExporter] = [TreeSpanExporter()]
    try:
        exporters.insert(0, azure_monitor_exporter(connection_string))
    except ValueError as e:
        print(f"[tracing_provider] Application Insights not configured, tree-only: {e}")
    return exporters


def configure_tracing(
    *,
    service_name: str = SERVICE_NAME,
    exporters: Sequence[SpanExporter] | None = None,
    batch: bool = False,
    set_global: bool = True,
) -> TracerProvider:
    """Build a TracerProvider and (by default) install it as the process
    global. batch=False (default) uses SimpleSpanProcessor -- spans export
    the instant they end, which is what a script that runs and exits
    needs (a BatchSpanProcessor's queue can still hold spans when the
    process ends). set_global=False returns a standalone provider without
    touching the global -- needed if one process wants two pipelines,
    since OTel's set_tracer_provider only takes effect once per process.
    """
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    for exporter in exporters or [TreeSpanExporter()]:
        processor = BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
        provider.add_span_processor(processor)
    if set_global:
        trace.set_tracer_provider(provider)
    return provider


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def _stamp_identity(span: trace.Span) -> None:
    span.set_attribute("interaction_id", current_interaction())
    if current_agent():
        span.set_attribute("agent", current_agent())


@contextmanager
def traced_run(name: str, correlation_id: str | None = None, **attributes: Any):
    """Open the ROOT span for one turn -- one rep/investor interaction.
    Everything created inside the `with` block hangs off this span.
    """
    with get_tracer().start_as_current_span(name) as span:
        span.set_attribute("correlation_id", correlation_id or uuid4().hex[:12])
        span.set_attribute("instruction_set.version", INSTRUCTION_SET_VERSION)
        span.set_attribute("interaction_id", current_interaction())
        for key, value in attributes.items():
            if value is not None:  # OTel rejects None attributes
                span.set_attribute(key, value)
        yield span


@contextmanager
def agent_span(name: str, deployment: str | None = None):
    """One agent invocation. Names every span produced inside it."""
    token = set_agent(name)
    try:
        with get_tracer().start_as_current_span(f"invoke_agent {name}") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", name)
            span.set_attribute("instruction_set.version", INSTRUCTION_SET_VERSION)
            if deployment:
                span.set_attribute("gen_ai.request.model", deployment)
            yield span
    finally:
        context.detach(token)


@contextmanager
def retrieval_span(query: str, *, top_k: int, capture_query: bool = False):
    """One retrieval call -- directly usable around search_knowledge_base.
    Call span.record_hits([(chunk_id, score), ...]) with what was
    actually returned; a retrieval span with no chunk ids can't answer
    "what did we give the model" when an answer turns out wrong.
    """
    with get_tracer().start_as_current_span("retrieve knowledge_base") as span:
        span.set_attribute("gen_ai.operation.name", "retrieve")
        span.set_attribute("retrieval.top_k", top_k)
        if capture_query:
            span.set_attribute("retrieval.query", query[:1000])

        def record_hits(hits: Sequence[tuple[str, float]]) -> None:
            span.set_attribute("retrieval.chunk_ids", [chunk_id for chunk_id, _ in hits])
            span.set_attribute("retrieval.scores", [round(score, 4) for _, score in hits])
            span.set_attribute("retrieval.hit_count", len(hits))

        span.record_hits = record_hits  # type: ignore[attr-defined]
        yield span


@contextmanager
def chat_span(model: str, *, step: int | None = None, messages: list[dict] | None = None):
    with get_tracer().start_as_current_span(f"chat {model}") as span:
        if step is not None:
            span.set_attribute("gen_ai.request.step", step)
        span.set_attribute("gen_ai.request.model", model)
        _stamp_identity(span)
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "azure.ai.openai")

        if CAPTURE_CONTENT and messages:
            span.set_attribute(
                "gen_ai.request.messages",
                json.dumps([{"role": i.get("role"), "content": str(i.get("content"))[:1000]}
                            for i in messages if isinstance(i, dict)]),
            )

        def record_response(response: Any) -> None:
            usage = getattr(response, "usage", None)
            if usage:
                span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
                details = getattr(usage, "completion_token_details", None)
                reasoning = getattr(details, "reasoning_tokens", 0) or 0
                if reasoning:
                    span.set_attribute("gen_ai.usage.reasoning_tokens", reasoning)
            span.set_attribute("gen_ai.response.model", response.model)
            span.set_attribute("gen_ai.response.id", response.id)
            choice = response.choices[0]
            span.set_attribute("gen_ai.response.finish_reasons", [choice.finish_reason])
            if choice.message.tool_calls:
                span.set_attribute(
                    "gen_ai.response.tool_calls",
                    json.dumps([{"name": tc.function.name, "arguments": tc.function.arguments}
                                for tc in choice.message.tool_calls]),
                )
            if CAPTURE_CONTENT and choice.message.content:
                span.set_attribute("gen_ai.output.messages", str(choice.message.content)[:1000])

        span.record_response = record_response  # type: ignore[attr-defined]
        yield span


@contextmanager
def tool_span(name: str, arguments: str | None = None, *, capture_arguments: bool = False):
    """Span for one tool call. record_result() parses the tool's JSON
    result and flags {"error": ...} as StatusCode.ERROR -- matches
    exactly how policy_engine.evaluate_policy_rule and every tool in
    investor_tools_v2.py already report failure, no changes needed on
    the tool side for this to work.
    """
    with get_tracer().start_as_current_span(f"execute_tool {name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", name)
        _stamp_identity(span)
        if (capture_arguments or CAPTURE_CONTENT) and arguments:
            span.set_attribute("gen_ai.tool.call.arguments", arguments[:1000])

        def record_result(result: str) -> None:
            span.set_attribute("gen_ai.tool.result.size", len(result))
            try:
                payload = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                payload = None
            if isinstance(payload, dict) and "error" in payload:
                span.set_attribute("error.type", "tool_error")
                # NOTE: the reference file writes this as
                # set_status(Status(StatusCode.ERROR), str(payload["error"]))
                # -- passing a Status object AND a separate description
                # together is invalid OTel usage; the description gets
                # silently dropped (confirmed via a runtime warning while
                # testing this). Correct form: description goes INSIDE
                # the Status() constructor, as below.
                span.set_status(Status(StatusCode.ERROR, str(payload["error"])))
            else:
                span.set_status(Status(StatusCode.OK))

        span.record_result = record_result  # type: ignore[attr-defined]
        yield span


