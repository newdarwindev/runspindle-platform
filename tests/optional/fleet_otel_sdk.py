"""Explicit SDK test: python -m unittest tests.optional.fleet_otel_sdk.

Requires requirements-telemetry.txt. No exporter network or provider calls.
"""

import unittest
from importlib import import_module

from tools.runner_fleet.telemetry import Observation, Operation, Outcome, export

context = import_module("opentelemetry.context")
trace = import_module("opentelemetry.trace")
TracerProvider = import_module("opentelemetry.sdk.trace").TracerProvider
SimpleSpanProcessor = import_module(
    "opentelemetry.sdk.trace.export"
).SimpleSpanProcessor
InMemorySpanExporter = import_module(
    "opentelemetry.sdk.trace.export.in_memory_span_exporter"
).InMemorySpanExporter


class SDKTests(unittest.TestCase):
    def test_actual_sdk_root_span_allowlist(self) -> None:
        sink = InMemorySpanExporter()
        provider = TracerProvider()
        self.addCleanup(provider.shutdown)
        provider.add_span_processor(SimpleSpanProcessor(sink))
        tracer = provider.get_tracer("runspindle-platform")
        with tracer.start_as_current_span("PRIVATE_PARENT"):
            token = context.attach(context.set_value("SECRET_BAGGAGE", "PRIVATE"))
            try:
                export(tracer, Observation(Operation.CLEANUP, Outcome.UNKNOWN, 5, 3))
            finally:
                context.detach(token)
        child = sink.get_finished_spans()[0]
        self.assertIsNone(child.parent)
        self.assertEqual(child.name, "platform.fleet.cleanup")
        self.assertEqual(child.end_time, 5)
        self.assertEqual(
            dict(child.attributes or {}),
            {
                "runspindle.telemetry.version": "runspindle.telemetry.v1",
                "runspindle.component": "platform",
                "runspindle.outcome": "unknown",
            },
        )
        self.assertEqual(trace.get_current_span().get_span_context().trace_id, 0)
