"""Render the public reporter or the private Host validation workflow."""

import argparse
from pathlib import Path
from textwrap import indent

from tools.runner_fleet.private_relay import JOBS, PARTITIONS


def public_workflow():
    reporter = Path(__file__).with_name("runner_fleet") / "public_reporter.py"
    header = """name: validation
on:
  push:
    branches: [main, 'ci/*']
permissions:
  checks: read
concurrency:
  group: strix-relay-v1-${{ github.ref }}
  cancel-in-progress: false
jobs:
  strix-linux-x64:
    name: validation / strix-linux-x64
    runs-on: ubuntu-latest
    timeout-minutes: 150
    steps:
      - name: Report the Strix validation result
        shell: bash
        env:
          REPORTER_TOKEN: ${{ github.token }}
          STRIX_APP_ID: ${{ vars.RUNSPINDLE_STRIX_APP_ID }}
        run: |
          python3 -IB - <<'PY'
"""
    return header + indent(reporter.read_text(), "          ") + "          PY\n"


def private_workflow():
    header = """name: Strix validation
run-name: ${{ inputs.request }}
on:
  workflow_dispatch:
    inputs:
      request:
        required: true
        type: string
      repository:
        required: true
        type: string
      sha:
        required: true
        type: string
permissions: {}
env:
  SOURCE_REPOSITORY: ${{ inputs.repository }}
  SOURCE_SHA: ${{ inputs.sha }}
jobs:
"""
    checkout = """          set -euo pipefail
          test "$SOURCE_REPOSITORY" = newdarwindev/runspindle-host
          [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]
          test "$RUNNER_ENVIRONMENT/$RUNNER_OS/$RUNNER_ARCH" = self-hosted/Linux/X64
          export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
          work="$(mktemp -d "$RUNNER_TEMP/strix-source-XXXXXXXX")"
          cd "$work"
          git init --quiet --template=
          timeout 90 git -c credential.helper= -c http.followRedirects=false fetch --quiet \\
            --depth=1 --no-tags --no-recurse-submodules \\
            https://github.com/newdarwindev/runspindle-host.git "$SOURCE_SHA"
          git -c core.hooksPath=/dev/null checkout --quiet --detach FETCH_HEAD
          test "$(git rev-parse HEAD)" = "$SOURCE_SHA"
          test -z "$(git status --porcelain --untracked-files=all)"
          export GITHUB_SHA="$SOURCE_SHA"
"""
    sections = [header]
    for job in JOBS:
        sections.append(f"""  {job}:
    name: {job}
    runs-on:
      - ${{{{ format('runspindle-strix-{{0}}-{{1}}-{job}', github.run_id, github.run_attempt) }}}}
    timeout-minutes: {5 if job == 'aggregate' else 15}
""")
        if job == "aggregate":
            sections.append("    if: ${{ always() }}\n")
            sections.append(f"    needs: [{', '.join(PARTITIONS)}]\n")
            sections.append("    env:\n")
            for partition in PARTITIONS:
                sections.append(
                    f"      RECEIPT_{partition.upper()}: ${{{{ needs.{partition}.outputs.receipt }}}}\n"
                    f"      RESULT_{partition.upper()}: ${{{{ needs.{partition}.result }}}}\n"
                )
        else:
            sections.append("    outputs:\n      receipt: ${{ steps.validation.outputs.receipt }}\n")
        sections.append(
            "    steps:\n      - id: validation\n        shell: bash\n        run: |\n" + checkout
        )
        command = f"python3 tools/partition_validation.py {job}"
        if job != "aggregate":
            command += ' --output "$GITHUB_OUTPUT"'
        sections.append(f"          {command}\n")
    return "".join(sections)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", choices=("public", "private-host"))
    args = parser.parse_args()
    print(public_workflow() if args.workflow == "public" else private_workflow(), end="")
