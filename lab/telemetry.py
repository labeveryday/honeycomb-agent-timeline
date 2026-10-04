"""PydanticAI's native GenAI spans, plus its conversation ID and agent name on our own spans."""
import json
import os
from pathlib import Path

from opentelemetry import baggage, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings


class CopyBaggage(SpanProcessor):
    # PydanticAI keeps the conversation ID and the current agent's name in OTel baggage.
    # Copying them onto every span puts our HTTP spans in the conversation, and credits an
    # agent-to-agent invoke_agent span to the calling agent, as Honeycomb's guide asks.
    def on_start(self, span, parent_context=None):
        for key in ("gen_ai.conversation.id", "gen_ai.agent.name"):
            if value := baggage.get_baggage(key, parent_context):
                span.set_attribute(key, value)


class JsonlExporter(SpanExporter):
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w")

    def export(self, spans):
        for span in spans:
            self.file.write(json.dumps(json.loads(span.to_json())) + "\n")
        self.file.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.file.close()


def configure(path=Path("artifacts/spans.jsonl"), exporter=None):
    provider = TracerProvider(sampler=ALWAYS_ON, resource=Resource.create({"service.name": "labeveryday-agent-lab"}))
    provider.add_span_processor(CopyBaggage())
    provider.add_span_processor(SimpleSpanProcessor(exporter or JsonlExporter(path)))
    if key := os.getenv("HONEYCOMB_API_KEY"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://api.honeycomb.io/v1/traces"),
            headers={"x-honeycomb-team": key}, timeout=10,
        )))
    trace.set_tracer_provider(provider)
    Agent.instrument_all(InstrumentationSettings(
        tracer_provider=provider,
        include_content=os.getenv("CAPTURE_CONTENT", "true").lower() == "true",
    ))
    return provider
