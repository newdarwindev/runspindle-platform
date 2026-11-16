# Offline demo enrollment proposal

`tools.runner_fleet.demo_readiness.prepare` produces paired candidate policy and configuration objects. It does not write either object, change the deployed controller, add a repository to the active fleet, enqueue work or enable any service. The active `model.config()` still requires the original exact repository tuple. Candidate validation requires the original tuple plus exactly `runspindle-demo-suite` in both objects, owner `newdarwindev`, and `activation_allowed: false`.

The candidate is preparation for the standalone fictional demo suite, whose executable dependencies must remain independent of upstream Runspindle components. Fleet hosting is infrastructure preparation; it does not add an upstream integration to the demo.

Before activation the owner must review the paired policy/config proposal, independently qualify the exact disposable Linux x64 guest image and dependency closure, and separately approve any required network/security settings. Prerequisites include Python3, Node/npm, Git, gh, ffmpeg/ffprobe, locked Playwright 1.56.1 and Chromium/headless-shell revision 1194 with sandboxing enabled. Boolean tool availability in this source-only planner is a readiness checklist, not byte, provenance, image or browser qualification.

Bounded guest qualification must record exact parent/submodule commits, image/source/controller/recipe/tool hashes, all contract tests, desktop/mobile branding, seven normal workflows, fault/recovery cases, evaluation rollback, media playback/link checks and ephemeral teardown. Short page captures remain fixture evidence, never complete product execution or upstream acceptance.

The owner must personally enable persistent demo fleet enrollment, services, jobs or agents and add demo-specific standing instructions to their main agent. This proposal neither performs nor grants authority for those steps. Pending required CI checks remain pending; source-only tests do not satisfy live fleet qualification.

Validation: `python3 -m unittest tests.test_demo_readiness -v` covers unchanged active policy/config, matching paired proposal, mismatches, unexpected repositories, missing tools/wrong browser, incorrect owner/architecture and the activation boundary. Run the existing fleet tests and `python3 tools/check_scaffold.py` as well.
