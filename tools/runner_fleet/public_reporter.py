"""Stand-alone code embedded in the public workflow without a checkout."""

import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def result(fetch, repository, sha, app_id, key):
    matches = []
    seen = set()
    for page in range(1, 11):
        data = fetch(repository, sha, app_id, page)
        rows = data.get("check_runs")
        count = data.get("total_count")
        if type(rows) is not list or type(count) is not int or not 0 <= count <= 1000:
            raise ValueError("invalid check inventory")
        for check in rows:
            check_id = check.get("id")
            if type(check_id) is not int or check_id <= 0 or check_id in seen:
                raise ValueError("inconsistent check inventory")
            seen.add(check_id)
            if (
                check.get("external_id") == key
                and check.get("name") == "Runspindle Strix execution"
                and check.get("head_sha") == sha
                and (check.get("app") or {}).get("id") == app_id
            ):
                matches.append(check)
        if len(seen) == count:
            break
        if len(seen) > count or len(rows) != 100:
            raise ValueError("incomplete check inventory")
    else:
        raise ValueError("check inventory exceeded page limit")
    if len(matches) > 1:
        raise ValueError("conflicting Strix results")
    if not matches or matches[0].get("status") != "completed":
        return None
    return matches[0].get("conclusion") == "success"


def main():
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")
    identifiers = [
        os.environ.get(name, "")
        for name in ("GITHUB_REPOSITORY_ID", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "STRIX_APP_ID")
    ]
    token = os.environ.get("REPORTER_TOKEN")
    if (
        not re.fullmatch(r"newdarwindev/runspindle(?:-[a-z]+)?", repository)
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
        or any(not re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in identifiers)
        or os.environ.get("GITHUB_EVENT_NAME") != "push"
        or not token
    ):
        print("Missing or invalid Strix reporting configuration.")
        return 2
    repo_id, run_id, attempt, app_id = identifiers
    key = f"strix-v1:{repo_id}:{run_id}:{attempt}:{sha}"
    opener = build_opener(RejectRedirect)

    def fetch(repository, sha, app, page):
        query = urlencode({
            "app_id": app, "check_name": "Runspindle Strix execution",
            "filter": "all", "per_page": 100, "page": page,
        })
        request = Request(
            f"https://api.github.com/repos/{repository}/commits/{sha}/check-runs?{query}",
            headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )
        with opener.open(request, timeout=10) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("check response exceeded limit")
        return json.loads(raw)

    deadline = time.monotonic() + 145 * 60
    while time.monotonic() < deadline:
        try:
            outcome = result(fetch, repository, sha, int(app_id), key)
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504):
                print(f"GitHub check lookup failed with HTTP {error.code}.")
                return 2
            outcome = None
        except (TimeoutError, URLError):
            outcome = None
        except (AttributeError, ValueError):
            print("GitHub returned invalid or conflicting Strix evidence.")
            return 2
        if outcome is not None and time.monotonic() < deadline:
            print("Strix validation passed." if outcome else "Strix validation failed.")
            return 0 if outcome else 1
        time.sleep(min(20, max(0, deadline - time.monotonic())))
    print("Strix validation did not report a terminal result before the deadline.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
