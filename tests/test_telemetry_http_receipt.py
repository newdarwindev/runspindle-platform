"""Exercise the trace sender against a bounded loopback HTTP fixture."""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from tools import collector_trace_http as trace_http
from tools.collector_privacy_check import CANARY, require_trace_acceptance


class TraceReceiptTests(unittest.TestCase):
    def test_acceptance_and_rejection_counts(self) -> None:
        cases = (
            (b"{}", "accepted"),
            (b'{"partialSuccess":{}}', "accepted"),
            (b'{"partialSuccess":{"rejectedSpans":"1"}}', "spans_rejected"),
            (b'{"partialSuccess":{"rejectedSpans":2}}', "spans_rejected"),
            (b'{"partialSuccess":{"errorMessage":"warning"}}', "accepted_with_warning"),
            (
                b'{"partialSuccess":{"rejectedSpans":"0","errorMessage":"warning"}}',
                "accepted_with_warning",
            ),
        )
        for response, expected in cases:
            with self.subTest(response=response):
                self.assertEqual(trace_http.classify(response), expected)

    def test_ambiguous_bodies_never_confer_acceptance(self) -> None:
        for body in (
            b"",
            b"null",
            b"[]",
            b"{",
            b"\xff",
            b'{"partialSuccess":null}',
            b'{"partialSuccess":{},"partialSuccess":{}}',
            b'{"partialSuccess":{"rejectedSpans":1,"rejectedSpans":0}}',
            b'{"partialSuccess":{"rejectedSpans":true}}',
            b'{"partialSuccess":{"rejectedSpans":0.0}}',
            b'{"partialSuccess":{"rejectedSpans":-1}}',
            b'{"partialSuccess":{"rejectedSpans":"9223372036854775808"}}',
            b'{"partialSuccess":{"rejectedSpans":" 0"}}',
            b'{"partialSuccess":{"errorMessage":false}}',
            b'{"partialSuccess":{"rejectedDataPoints":1}}',
            b"{}" + b" " * trace_http.MAX_RESPONSE,
        ):
            with self.subTest(length=len(body), prefix=body[:80]):
                self.assertEqual(trace_http.classify(body), "unknown")

    def test_http_path_uses_body_and_does_not_retry_or_follow_redirects(self) -> None:
        responses = iter(
            (
                (200, "application/json", b'{"partialSuccess":{"rejectedSpans":"1"}}'),
                (200, "application/json", b"{}"),
                (200, "text/html", b"{}"),
                (302, "application/json", b"{}"),
                (400, "application/json", CANARY.encode()),
                (503, "application/json", CANARY.encode()),
                (200, "application/json", b"{}" + b" " * trace_http.MAX_RESPONSE),
            )
        )
        observed: list[tuple[str, str, bytes]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                request_size = int(self.headers["Content-Length"])
                observed.append(("POST", self.path, self.rfile.read(request_size)))
                status, content_type, body = next(responses)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Location", "/forbidden")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                observed.append(("GET", self.path, b""))
                self.send_error(404)

            def log_message(self, format: str, *args: object) -> None:
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
                expected = [
                    "spans_rejected",
                    "accepted",
                    "unknown",
                    "unknown",
                    "rejected",
                    "unknown",
                    "unknown",
                ]
                with patch.dict(
                    "os.environ", {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}
                ):
                    self.assertEqual(
                        [trace_http.post(endpoint, b"{}") for _ in expected], expected
                    )
                self.assertEqual(
                    observed, [("POST", "/v1/traces", b"{}")] * len(expected)
                )
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_probe_requires_full_acceptance_and_reports_only_closed_states(
        self,
    ) -> None:
        script = Path(trace_http.__file__).read_text()
        for host in ("primary", "collector"):
            for receipt, state in (
                ("spans_rejected", "spans_rejected"),
                ("rejected", "rejected"),
                ("200", "unknown"),
                (CANARY, "unknown"),
            ):
                with self.assertRaisesRegex(RuntimeError, f"\\({state}\\)$"):
                    require_trace_acceptance(lambda *_: receipt, host, "{}")
            for receipt in ("accepted", "accepted_with_warning"):
                calls: list[tuple[str, str, str]] = []

                def client(code: str, alias: str, payload: str) -> str:
                    calls.append((code, alias, payload))
                    return receipt

                require_trace_acceptance(client, host, "{}")
                self.assertEqual(calls, [(script, host, "{}")])


if __name__ == "__main__":
    unittest.main()
