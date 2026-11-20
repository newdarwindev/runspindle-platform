"""One bounded HTTP/JSON trace submission for the disposable Collector probes."""

from __future__ import annotations

import json
import re
import sys
from email.message import Message
from http.client import HTTPException
from typing import IO
from urllib.error import HTTPError
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

MAX_RESPONSE = 16 * 1024
REJECTED_STATUS = frozenset({400, 401, 403, 404, 405, 413, 415})


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("ambiguous trace response")
    return result


def classify(body: bytes) -> str:
    if len(body) > MAX_RESPONSE:
        return "unknown"
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        return "unknown"
    if not isinstance(value, dict) or value.keys() - {"partialSuccess"}:
        return "unknown"
    partial = value.get("partialSuccess", {})
    if not isinstance(partial, dict) or partial.keys() - {
        "rejectedSpans",
        "errorMessage",
    }:
        return "unknown"
    rejected = partial.get("rejectedSpans", 0)
    if isinstance(rejected, str) and re.fullmatch(r"0|[1-9][0-9]{0,18}", rejected):
        rejected = int(rejected)
    message = partial.get("errorMessage", "")
    if (
        type(rejected) is not int
        or not 0 <= rejected < 2**63
        or not isinstance(message, str)
    ):
        return "unknown"
    if rejected:
        return "spans_rejected"
    return "accepted_with_warning" if message else "accepted"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


def post(url: str, payload: bytes) -> str:
    sender = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with sender.open(request, timeout=5) as response:
            if (
                response.status != 200
                or response.headers.get_content_type() != "application/json"
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                return "unknown"
            return classify(response.read(MAX_RESPONSE + 1))
    except HTTPError as error:
        with error:
            return "rejected" if error.code in REJECTED_STATUS else "unknown"
    except (OSError, HTTPException):
        return "unknown"


if __name__ == "__main__":
    host, body = sys.argv[1:]
    if host not in ("collector", "primary"):
        raise SystemExit("unsupported trace destination")
    print(post(f"http://{host}:4318/v1/traces", body.encode()))
