import hashlib
import fcntl
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from tools import strix_pilot
from tools.runner_fleet import direct_routing
from tools.runner_fleet.github_jobs import LAB, NEXT, Attempt, Route, RoutingError


class DirectRoutingTest(unittest.TestCase):
    def setUp(self):
        self.quota_delay = strix_pilot.github_rate_limit_delay
        quota = patch.object(strix_pilot, "github_rate_limit_delay", return_value=0)
        quota.start()
        self.addCleanup(quota.stop)
        self.route = Route("newdarwindev/runspindle-host", 100, 200, "a" * 40)
        self.attempt = Attempt(self.route, 300, 1, "b" * 40, self.route.workflow_blob)
        repository = {"id": 100, "full_name": self.route.repository, "private": False}
        self.run = {
            "id": 300, "workflow_id": 200, "event": "push", "status": "queued",
            "head_branch": "ci/pilot", "head_sha": self.attempt.sha, "run_attempt": 1,
            "path": ".github/workflows/validation.yml", "repository": repository,
            "head_repository": repository, "actor": {"id": 42}, "triggering_actor": {"id": 42},
        }
        self.job = {
            "id": 401, "name": "validation / component", "status": "queued", "runner_id": 0,
            "run_id": 300, "run_attempt": 1, "head_sha": self.attempt.sha,
            "labels": ["runspindle-strix-direct-300-1-component"],
        }

    def read(self, method, path):
        self.assertEqual(method, "GET")
        if "/attempts/1/jobs?" in path:
            return {"total_count": 1, "jobs": [self.job]}
        if "/contents/" in path:
            self.assertIn(self.route.workflow_path, path)
            return {"type": "file", "sha": self.route.workflow_blob}
        return self.run

    def test_queue_discovery_preserves_low_quota(self):
        core = {"remaining": 320, "reset": int(time.time()) + 120}
        with (
            patch.object(strix_pilot, "github_rate_limit_delay", self.quota_delay),
            patch.object(strix_pilot, "api", return_value={"resources": {"core": core}}) as api,
            patch.object(strix_pilot, "queue_listing_api") as listing,
        ):
            self.assertIsNone(strix_pilot.next_queued_job([self.route], 0))
            api.assert_called_once_with("GET", "/rate_limit")
            listing.assert_not_called()
            core["remaining"] = 321
            self.assertEqual(self.quota_delay(0, reserve=320), 0)

    def test_only_approved_push_actor_and_exact_single_label(self):
        observed = direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})
        self.assertEqual(observed, (self.attempt, [self.job]))
        with self.assertRaisesRegex(RoutingError, "actor"):
            direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={43})
        self.job["labels"].append("self-hosted")
        with self.assertRaisesRegex(RoutingError, "identity or label"):
            direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})

    def test_partial_rerun_accepts_inherited_completed_siblings_only(self):
        attempt = Attempt(self.route, 300, 2, self.attempt.sha, self.route.workflow_blob)
        queued = self.job | {"run_attempt": 2, "labels": ["runspindle-strix-direct-300-2-component"]}
        sibling = queued | {
            "id": 402, "name": "validation / quality", "status": "completed",
            "labels": ["runspindle-strix-direct-300-1-quality"],
        }
        with (
            patch.object(direct_routing, "observe_push", return_value=attempt),
            patch.object(direct_routing, "inventory", return_value=[queued, sibling]),
        ):
            self.assertEqual(
                direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42}),
                (attempt, [queued]),
            )
            sibling["status"] = "queued"
            with self.assertRaisesRegex(RoutingError, "identity or label"):
                direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})

    def test_attempt_race_is_rechecked_at_admission(self):
        changed = Attempt(self.route, 300, 2, self.attempt.sha, self.route.workflow_blob)
        with (
            patch.object(direct_routing, "observe_push", side_effect=[self.attempt, changed]),
            patch.object(direct_routing, "inventory", return_value=[self.job]),
            self.assertRaisesRegex(RoutingError, "attempt changed during job admission"),
        ):
            direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})
        with (
            patch.object(direct_routing, "observe_push", return_value=self.attempt) as observe,
            patch.object(direct_routing, "inventory", return_value=[self.job]),
        ):
            self.assertEqual(
                direct_routing.queued_jobs(
                    self.read, self.route, 300, actor_ids={42}, confirm_attempt=False,
                ),
                (self.attempt, [self.job]),
            )
            observe.assert_called_once()

    def test_rate_limit_does_not_end_registered_job_observation(self):
        terminal = self.job | {
            "status": "completed", "conclusion": "success",
            "runner_id": 52, "runner_name": "runner",
        }
        with (
            patch.object(strix_pilot, "api", side_effect=[strix_pilot.GitHubRateLimited("limited"), (terminal, None)]),
            patch.object(strix_pilot.time, "sleep") as pause,
        ):
            result = strix_pilot.observe_result(self.route.repository, 300, self.job, 52, "runner", 120)
        self.assertEqual(result["conclusion"], "success")
        pause.assert_called_once_with(60)

    def test_registered_job_observation_revalidates_unchanged_status(self):
        running = self.job | {"status": "in_progress", "runner_id": 52}
        terminal = running | {"status": "completed", "conclusion": "success", "runner_name": "runner"}
        with (
            patch.object(strix_pilot, "api", side_effect=[
                (running, '"job-v1"'), (None, None), (terminal, '"job-v2"'),
            ]) as github,
            patch.object(strix_pilot.time, "sleep"),
        ):
            result = strix_pilot.observe_result(self.route.repository, 300, self.job, 52, "runner", 120)
        self.assertEqual(result["conclusion"], "success")
        self.assertEqual([call.kwargs["etag"] for call in github.call_args_list], [None, '"job-v1"', '"job-v1"'])

    def test_listener_stays_offline_if_identity_cannot_be_saved(self):
        record = Mock(side_effect=OSError("ledger unavailable"))
        with (
            patch.object(direct_routing, "configure", return_value=52),
            patch.object(direct_routing, "start_listener") as listen,
        ):
            with self.assertRaises(OSError):
                direct_routing.enroll_job(
                    self.read, Mock(), Mock(), self.route, 300, 401,
                    "runspindle-fleet-" + "f" * 32, actor_ids={42},
                    verify_guest=Mock(), record_runner=record,
                )
            record.assert_called_once_with(52)
            listen.assert_not_called()

    def test_additional_workflow_revision_is_bound_to_attempt(self):
        route = Route(self.route.repository, 100, 200, "a" * 40, ["b" * 40])

        def read(method, path):
            if "/contents/" in path:
                return {"type": "file", "sha": "b" * 40}
            return self.read(method, path)

        attempt, jobs = direct_routing.queued_jobs(read, route, 300, actor_ids={42})
        self.assertEqual(attempt.workflow_blob, "b" * 40)
        self.assertEqual(jobs, [self.job])
        with self.assertRaisesRegex(RoutingError, "approved revision"):
            direct_routing.queued_jobs(read, self.route, 300, actor_ids={42})

    def test_watch_after_restart_resumes_after_last_reconciled_repository(self):
        path = Path("routes.json")
        routes = [self.route, Route(LAB, 101, 201, "c" * 40)]
        stopping = Event()

        def observe(approved, cursor):
            self.assertEqual(approved, routes)
            self.assertEqual(cursor, 1)
            stopping.set()
            return None

        with (
            patch.object(strix_pilot, "run"),
            patch.object(strix_pilot, "load_routes", return_value=routes),
            patch.object(strix_pilot, "last_direct_repository", return_value=self.route.repository),
            patch.object(strix_pilot, "next_queued_job", side_effect=observe) as next_job,
        ):
            self.assertEqual(strix_pilot.watch(path, stopping), 0)
            next_job.assert_called_once()

    def test_watch_scans_immediately_after_completed_and_deferred_jobs(self):
        stopping = Mock(wraps=Event())
        first = (0, 700, 900)

        def next_job(*_):
            if dispatch.called:
                stopping.set()
                return None
            return first

        with (
            patch.object(strix_pilot, "run", return_value=set()),
            patch.object(strix_pilot, "load_routes", return_value=[self.route]),
            patch.object(strix_pilot, "last_direct_repository", return_value=None),
            patch.object(strix_pilot, "next_queued_job", side_effect=next_job),
            patch.object(strix_pilot, "run_parallel", return_value=(
                {0, strix_pilot.CAPACITY_DEFERRED}, self.route.repository,
            )) as dispatch,
        ):
            self.assertEqual(strix_pilot.watch(Path("routes.json"), stopping), 0)
            dispatch.assert_called_once()
            stopping.wait.assert_not_called()

    def test_pending_run_admits_only_materialized_queued_jobs(self):
        self.run["status"] = "pending"
        observed = direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})
        self.assertEqual(observed, (self.attempt, [self.job]))
        self.job["status"] = "pending"
        self.assertEqual(
            direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42}),
            (self.attempt, []),
        )

    def test_lab_and_next_admit_only_their_verify_job_and_visibility(self):
        for repository, private in ((LAB, False), (NEXT, True)):
            with self.subTest(repository=repository):
                self.route = Route(repository, 100, 200, "a" * 40)
                attempt = Attempt(self.route, 300, 1, "b" * 40, self.route.workflow_blob)
                source = {"id": 100, "full_name": repository, "private": private}
                self.run.update(
                    path=self.route.workflow_path, repository=source, head_repository=source,
                )
                self.job.update(name="verify", labels=["runspindle-strix-direct-300-1-verify"])
                self.assertEqual(
                    direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42}),
                    (attempt, [self.job]),
                )
                self.run["head_repository"] = source | {"private": not private}
                with self.assertRaisesRegex(RoutingError, "source repository identity"):
                    direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})

    def test_cleanup_waits_for_busy_runner_then_proves_exact_removal(self):
        repo = self.route.repository
        name = "runspindle-fleet-" + "f" * 32
        label, = self.job["labels"]
        runner = {
            "id": 52, "name": name, "status": "offline", "busy": True,
            "labels": [{"name": label, "type": "custom"}],
        }
        inventory = {"total_count": 1, "runners": [runner]}
        with patch.object(strix_pilot, "api", return_value=inventory) as github:
            with self.assertRaises(strix_pilot.RunnerCleanupPending):
                strix_pilot.remove_owned_runner(repo, name, label, 52)
            self.assertTrue(all(call.args[0] == "GET" for call in github.call_args_list))
        runner["busy"] = False
        with patch.object(strix_pilot, "api", side_effect=[
            inventory, None, {"total_count": 0, "runners": []},
        ]) as github:
            strix_pilot.remove_owned_runner(repo, name, label, 52)
            self.assertEqual(github.call_args_list[1].args, (
                "DELETE", f"/repos/{repo}/actions/runners/52", 204,
            ))

    def test_queue_rotates_repositories_and_picks_oldest_eligible_run(self):
        routes = [self.route, Route(LAB, 101, 201, "c" * 40)]
        with (
            patch.object(strix_pilot, "inventory", return_value=[{"id": 800}, {"id": 700}]),
            patch.object(strix_pilot, "queued_jobs", return_value=(
                self.attempt, [{"id": 901}, {"id": 900}],
            )) as jobs,
        ):
            self.assertEqual(strix_pilot.next_queued_job(routes, 1), (1, 700, 900))
            jobs.assert_called_once_with(
                strix_pilot.discovery_api, routes[1], 700, actor_ids={strix_pilot.OWNER},
                confirm_attempt=False,
            )

    def test_discovery_caches_workflow_identity_and_refreshes_run_state(self):
        path = f"/repos/{self.route.repository}/contents/{self.route.workflow_path}"
        pinned = f"{path}?ref={'a' * 40}"
        run = f"/repos/{self.route.repository}/actions/runs/700"
        with patch.object(strix_pilot, "api", side_effect=[
            {"type": "file", "sha": "b" * 40},
            ({"status": "queued", "actor": {"id": 42, "login": "owner"}}, '"run-v1"'),
            (None, None),
            ({"status": "in_progress"}, None),
        ]) as github:
            self.assertEqual(strix_pilot.discovery_api("GET", pinned)["sha"], "b" * 40)
            self.assertEqual(strix_pilot.discovery_api("GET", run)["status"], "queued")
            self.assertEqual(strix_pilot.discovery_api("GET", pinned)["sha"], "b" * 40)
            self.assertEqual(strix_pilot.discovery_api("GET", run)["actor"], {"id": 42})
            self.assertEqual(strix_pilot.discovery_api("GET", run)["status"], "in_progress")
            self.assertEqual([call.args[1] for call in github.call_args_list], [pinned, run, run, run])
            self.assertEqual(github.call_args_list[2].kwargs["etag"], '"run-v1"')


    def test_discovery_revalidates_attempt_jobs_and_keeps_routing_fields(self):
        path = f"/repos/{self.route.repository}/actions/runs/700/attempts/1/jobs?per_page=100&page=1"
        job = dict.fromkeys(strix_pilot._DISCOVERY_JOB_FIELDS)
        job.update(id=900, labels=["dedicated"], status="queued")
        payload = {"total_count": 1, "jobs": [dict(job, steps=[{"name": "checkout"}])]}
        with (
            patch.object(strix_pilot, "_queue_listing_cache", strix_pilot.OrderedDict()),
            patch.object(strix_pilot, "api", side_effect=[
                (payload, '"jobs-v1"'), (None, None),
            ]) as github,
        ):
            self.assertEqual(strix_pilot.discovery_api("GET", path), {
                "total_count": 1, "jobs": [job],
            })
            self.assertEqual(strix_pilot.discovery_api("GET", path)["jobs"], [job])
            self.assertEqual(github.call_args.kwargs["etag"], '"jobs-v1"')

    def test_queue_finds_recent_push_after_incomplete_status_index(self):
        with (
            patch.object(strix_pilot, "inventory", side_effect=RoutingError("incomplete")),
            patch.object(strix_pilot, "queue_listing_api", return_value={"workflow_runs": [
                {"id": 700, "event": "push", "status": "queued"},
            ]}),
            patch.object(strix_pilot, "queued_jobs", return_value=(self.attempt, [{"id": 900}])),
        ):
            self.assertEqual(strix_pilot.next_queued_job([self.route], 0), (0, 700, 900))

    def test_queue_finds_recent_push_missing_from_status_index(self):
        with (
            patch.object(strix_pilot, "inventory", return_value=[{"id": 698}]),
            patch.object(strix_pilot, "queue_listing_api", return_value={"workflow_runs": [
                {"id": 698, "event": "push", "status": "in_progress"},
                {"id": 699, "event": "pull_request", "status": "queued"},
                {"id": 700, "event": "push", "status": "queued"},
                {"id": 701, "event": "push", "status": "completed"},
            ]}),
            patch.object(strix_pilot, "queued_jobs", side_effect=[
                (self.attempt, []), (self.attempt, [{"id": 900}]),
            ]) as jobs,
        ):
            self.assertEqual(strix_pilot.next_queued_job([self.route], 0), (0, 700, 900))
            self.assertEqual([args.args[2] for args in jobs.call_args_list], [698, 700])

    def test_second_candidate_excludes_first_job_in_same_repository(self):
        with (
            patch.object(strix_pilot, "inventory", return_value=[{"id": 700}]),
            patch.object(strix_pilot, "queued_jobs", return_value=(
                self.attempt, [{"id": 901}, {"id": 900}],
            )),
        ):
            first = strix_pilot.next_queued_job([self.route], 0)
            self.assertEqual(first, (0, 700, 900))
            self.assertEqual(
                strix_pilot.next_queued_job([self.route], 0, exclude={first}),
                (0, 700, 901),
            )


    def test_parallel_light_scan_uses_oldest_candidate_despite_response_order(self):
        route = Route(strix_pilot.NEXT, 101, 201, strix_pilot.LIGHT_WORKFLOWS[strix_pilot.NEXT])
        newer_evaluated = Event()

        def jobs(_api, _route, run_id, **kwargs):
            if run_id == 700:
                self.assertTrue(newer_evaluated.wait(2))
            else:
                newer_evaluated.set()
            return self.attempt, [{"id": run_id + 200, "name": "verify"}]

        with (
            patch.object(strix_pilot, "inventory", return_value=[{"id": 700}, {"id": 701}]),
            patch.object(strix_pilot, "queued_jobs", side_effect=jobs),
        ):
            self.assertEqual(strix_pilot.next_queued_job([route], 0, light_only=True), (0, 700, 900))

    def test_active_single_job_run_does_not_repeat_discovery(self):
        route = Route(strix_pilot.NEXT, 101, 201, strix_pilot.LIGHT_WORKFLOWS[strix_pilot.NEXT])
        with (
            patch.object(strix_pilot, "inventory", return_value=[{"id": 700}, {"id": 701}]),
            patch.object(strix_pilot, "queued_jobs", return_value=(
                self.attempt, [{"id": 901, "name": "verify"}],
            )) as jobs,
        ):
            self.assertEqual(
                strix_pilot.next_queued_job([route], 0, active_single_runs={(0, 700)}),
                (0, 701, 901),
            )
            self.assertEqual([call.args[2] for call in jobs.call_args_list], [701])

    def test_third_worker_only_polls_the_qualified_verify_route(self):
        light = Route(
            strix_pilot.NEXT, 101, 201, strix_pilot.LIGHT_WORKFLOWS[strix_pilot.NEXT],
        )
        wrong_blob = Route(strix_pilot.NEXT, 101, 201, "d" * 40)
        with (
            patch.object(strix_pilot, "inventory", return_value=[{"id": 700}]) as inventory,
            patch.object(strix_pilot, "queued_jobs", return_value=(self.attempt, [
                {"id": 899, "name": "other"}, {"id": 900, "name": "verify"},
            ])),
        ):
            self.assertIsNone(strix_pilot.next_queued_job([self.route, wrong_blob], 0, light_only=True))
            inventory.assert_not_called()
            self.assertEqual(
                strix_pilot.next_queued_job([self.route, light], 0, light_only=True),
                (1, 700, 900),
            )

    def test_prepared_demo_image_requires_flag_repository_job_and_exact_workflow(self):
        eligible = strix_pilot.prepared_demo_job
        verify = {"name": "verify"}
        demo, workflow = strix_pilot.DEMO, strix_pilot.DEMO_LAYER_WORKFLOW
        self.assertTrue(eligible(True, demo, verify, workflow))
        self.assertFalse(eligible(False, demo, verify, workflow))
        self.assertFalse(eligible(True, strix_pilot.LAB, verify, workflow))
        self.assertFalse(eligible(True, demo, {"name": "publish-presentation"}, workflow))
        self.assertFalse(eligible(True, demo, verify, "f" * 40))
        cached_workflow = strix_pilot.DEMO_CHECKOUT_WORKFLOW
        self.assertFalse(eligible(True, demo, verify, cached_workflow))
        self.assertTrue(eligible(
            True, demo, verify | {"head_sha": strix_pilot.DEMO_CHECKOUT_HEAD},
            cached_workflow,
        ))
        self.assertTrue(eligible(
            True, demo, verify | {"head_sha": strix_pilot.DEMO_CHECKOUT_MAIN_HEAD},
            cached_workflow,
        ))

    def test_tether_node_cache_uses_compact_vm_for_measured_revision(self):
        eligible = strix_pilot.compact_profile_job
        repository = strix_pilot.TETHER
        blob = "ad80e49b7eb14a02eb57bfc1e554b92f58c0203a"
        quality = {"head_sha": "cb4d52bf0714cd065b439d515591a2a568b76544", "name": "validation / quality"}
        self.assertTrue(eligible(repository, quality, blob))
        self.assertTrue(eligible(repository, quality | {"name": "validation / component"}, blob))
        self.assertFalse(eligible(repository, quality | {"head_sha": "a" * 40}, blob))
        self.assertFalse(eligible(repository, quality, "b" * 40))

    def test_measured_tether_alternate_aggregate_uses_light_slot(self):
        primary = strix_pilot.LIGHT_AGGREGATE_WORKFLOWS[strix_pilot.TETHER]
        alternate = "d86b2440139eea7bc522eb4dfc3920cd759bec7e"
        route = Route(strix_pilot.TETHER, 100, 200, primary, additional_workflow_blobs=(alternate,))
        job = {"head_sha": "60e758b5b8ac682d7fb2c0763aac889cd8080edd", "name": "validation / strix-linux-x64"}
        self.assertTrue(strix_pilot.light_profile_job(route, job, alternate))
        self.assertFalse(strix_pilot.light_profile_job(route, job | {"head_sha": "a" * 40}, alternate))
        self.assertFalse(strix_pilot.light_profile_job(route, job | {"name": "validation / component"}, alternate))
        node_blob = "ad80e49b7eb14a02eb57bfc1e554b92f58c0203a"
        node_route = Route(strix_pilot.TETHER, 100, 200, primary, additional_workflow_blobs=(node_blob,))
        node_job = job | {"head_sha": "cb4d52bf0714cd065b439d515591a2a568b76544"}
        self.assertTrue(strix_pilot.light_profile_job(node_route, node_job, node_blob))
        self.assertFalse(strix_pilot.light_profile_job(node_route, job, node_blob))

    def test_measured_runspindle_revision_routes_only_approved_jobs_to_slot_three(self):
        repository = "newdarwindev/runspindle"
        blob = strix_pilot.LIGHT_AGGREGATE_WORKFLOWS[repository]
        route = Route(repository, 100, 200, blob)
        head = "1880adfa713a6eeacbc4887722a5f11d9bea263b"
        mutation = {"head_sha": head, "name": "validation / mutation_3"}
        component = mutation | {"name": "validation / component"}
        self.assertTrue(strix_pilot.light_profile_job(route, mutation, blob))
        self.assertFalse(strix_pilot.light_profile_job(route, mutation | {"head_sha": "a" * 40}, blob))
        self.assertFalse(strix_pilot.light_profile_job(route, mutation, "b" * 40))
        self.assertFalse(strix_pilot.light_profile_job(route, component, blob))
        self.assertTrue(strix_pilot.compact_profile_job(repository, component, blob))
        measured = component | {"head_sha": "93009f2709518a893a3894e908441fc84fab6a9f"}
        self.assertTrue(strix_pilot.compact_profile_job(repository, measured, blob))
        self.assertTrue(strix_pilot.compact_profile_job(repository, measured | {"name": "validation / rust"}, blob))
        self.assertFalse(strix_pilot.compact_profile_job(repository, measured | {"name": "validation / mutation_3"}, blob))

    def test_compact_profile_accepts_only_qualified_workflows_and_jobs(self):
        verify = {"name": "validation / quality"}
        blob = "ef7080f2f8d334f5e9353e06fe187afd8e1065b1"
        eligible = strix_pilot.compact_profile_job
        self.assertTrue(eligible(strix_pilot.BROWSER, verify, blob))
        self.assertFalse(eligible(strix_pilot.BROWSER, verify, "a" * 40))
        self.assertFalse(eligible(strix_pilot.BROWSER, {"name": "validation / rust"}, blob))
        self.assertFalse(eligible(strix_pilot.DEMO, verify, blob))
        component = {"name": "validation / component"}
        self.assertFalse(eligible(strix_pilot.BROWSER, component, blob))
        self.assertTrue(eligible(strix_pilot.BROWSER, component | {
            "head_sha": "e47fb7b7ab7e2efe9bc563fd127467bc588d8acc",
        }, blob))

    def test_compact_profile_accepts_measured_host_and_browser_jobs(self):
        eligible = strix_pilot.compact_profile_job
        host_blob = "1224fd80383058a9c4eafad9bfdab8410cd56933"
        host = {
            "name": "validation / mutation_3",
            "head_sha": "559270de822e89e5ee7306e222a9aa9a317086c4",
        }
        self.assertTrue(eligible(strix_pilot.HOST, host, host_blob))
        rust = host | {"name": "validation / rust"}
        self.assertTrue(eligible(strix_pilot.HOST, rust, host_blob))
        self.assertFalse(eligible(strix_pilot.HOST, rust | {"head_sha": "a" * 40}, host_blob))
        self.assertFalse(eligible(strix_pilot.HOST, rust, "b" * 40))
        self.assertFalse(eligible(strix_pilot.HOST, host | {"name": "validation / mutation_1"}, host_blob))
        browser = {
            "name": "validation / component",
            "head_sha": "122a02fa948eed8c98332c38eb62ffc021afeeca",
        }
        self.assertTrue(eligible(
            strix_pilot.BROWSER, browser, "ef7080f2f8d334f5e9353e06fe187afd8e1065b1",
        ))

    def test_console_compact_trial_requires_the_baselined_revision(self):
        eligible = strix_pilot.compact_profile_job
        blob = "dcd890f5ade00e4c9a77cf511d6cedac38303597"
        job = {
            "name": "validation / quality",
            "head_sha": "ca89e2b3c1394c3d2c0941a5d27ec1f64c4876ce",
        }
        self.assertTrue(eligible(strix_pilot.CONSOLE, job, blob))
        measured_component = {
            "name": "validation / component",
            "head_sha": "d076276e632b0d76aedb21717bc4d02899a410f4",
        }
        self.assertTrue(eligible(strix_pilot.CONSOLE, measured_component, blob))
        self.assertTrue(eligible(strix_pilot.CONSOLE, measured_component | {
            "head_sha": "487a16b2e1bf4d5305bcee551a0aec995d1b3159",
        }, blob))
        self.assertTrue(eligible(
            strix_pilot.CONSOLE, job | {"name": "validation / mutation_1"}, blob,
        ))
        self.assertFalse(eligible(
            strix_pilot.CONSOLE, job | {"name": "validation / mutation_2"}, blob,
        ))
        self.assertFalse(eligible(strix_pilot.CONSOLE, job | {"head_sha": "a" * 40}, blob))
        self.assertFalse(eligible(strix_pilot.CONSOLE, job, "a" * 40))

    def test_compact_profile_requires_the_effective_cgroup_limit(self):
        compact = strix_pilot.COMPACT_MEMORY_LIMIT
        with patch.object(Path, "read_text", return_value=str(compact)):
            self.assertEqual(strix_pilot.parallel_memory_group(0, compact).name, "runspindle-vm-0.service")
            with self.assertRaises(strix_pilot.CapacityUnavailable):
                strix_pilot.parallel_memory_group(0)
            with self.assertRaises(strix_pilot.CapacityUnavailable):
                strix_pilot.parallel_memory_group(2, compact)

    def test_cleanup_can_defer_only_a_verified_remote_runner(self):
        state = Mock()
        state.active.return_value = [{"repository": "runspindle-core"}]
        with patch.object(
            strix_pilot, "reconcile_stopped_resource",
            side_effect=strix_pilot.RunnerCleanupPending("runner still busy"),
        ):
            self.assertEqual(
                strix_pilot.reconcile_stopped_direct(state, Mock(), Mock(), allow_pending=True),
                {"newdarwindev/runspindle-core"},
            )
            with self.assertRaises(strix_pilot.RunnerCleanupPending):
                strix_pilot.reconcile_stopped_direct(state, Mock(), Mock())
        with patch.object(strix_pilot, "reconcile_stopped_resource", side_effect=RuntimeError("VM remains")):
            with self.assertRaisesRegex(RuntimeError, "VM remains"):
                strix_pilot.reconcile_stopped_direct(state, Mock(), Mock(), allow_pending=True)

    def test_uncertain_resource_requires_a_stopped_vm_and_originals(self):
        resource = {
            "id": "core-guest", "slot": 1, "repository": "runspindle-core",
            "phase": "uncertain", "runner_name": "runspindle-fleet-core-guest",
            "details": {"isolated_direct_pilot": True, "image_sha256": strix_pilot.IMAGE_SHA},
        }
        guest = {"resource_id": "core-guest", "ActiveState": "inactive", "MainPID": 0}
        with self.assertRaisesRegex(RuntimeError, "occupied or unresolved"):
            strix_pilot.verified_stopped_resource(Mock(), Mock(), Mock(return_value=guest), resource)
        resource["details"]["original_retention_complete"] = True
        guest["ActiveState"] = "active"
        with self.assertRaisesRegex(RuntimeError, "occupied or unresolved"):
            strix_pilot.verified_stopped_resource(Mock(), Mock(), Mock(return_value=guest), resource)

    def test_stopped_peer_must_match_ownership_and_have_no_remaining_cgroup(self):
        peer = {"id": "current", "slot": 2}
        stopped = {"resource_id": "current", "ActiveState": "inactive", "MainPID": "0"}
        with patch.object(Path, "exists", return_value=False):
            self.assertTrue(strix_pilot.peer_has_stopped(peer, stopped))
            self.assertFalse(strix_pilot.peer_has_stopped(
                peer, stopped | {"resource_id": "different"},
            ))
            self.assertFalse(strix_pilot.peer_has_stopped(peer, stopped | {"MainPID": "123"}))
        with patch.object(Path, "exists", return_value=True):
            self.assertFalse(strix_pilot.peer_has_stopped(peer, stopped))

    def test_isolated_peer_allows_other_repository_preparation_only(self):
        peer = {"details": {"direct_batch": "current"}, "phase": "isolated", "repository": "core"}
        ready = strix_pilot.peer_ready_for_admission
        self.assertTrue(ready(peer, "current", "host", True))
        self.assertFalse(ready(peer, "current", "core", True))
        self.assertFalse(ready(peer, "earlier", "host", True))
        self.assertFalse(ready(peer, "current", "host", False))
        self.assertFalse(ready(peer | {"phase": "starting"}, "current", "host", True))
        self.assertTrue(ready(peer | {"phase": "registered"}, "current", "core", True))

    def test_lab_light_profile_requires_its_qualified_workflow(self):
        workflow = "fb048fa31ecec128f2518f847eb67d04307e3705"
        self.assertTrue(strix_pilot.light_workflow(Route(strix_pilot.LAB, 101, 201, workflow)))
        self.assertFalse(strix_pilot.light_workflow(Route(strix_pilot.LAB, 101, 201, "f" * 40)))
        self.assertFalse(strix_pilot.light_workflow(Route(strix_pilot.NEXT, 101, 201, workflow)))

    def test_third_worker_can_start_while_two_general_workers_are_live(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            routes = [self.route, Route(
                strix_pilot.NEXT, 101, 201, strix_pilot.LIGHT_WORKFLOWS[strix_pilot.NEXT],
            )]
            first, second, third = (0, 700, 900), (0, 700, 901), (1, 701, 902)

            def queued(approved, cursor, exclude, *, light_only=False, active_single_runs=()):
                if second not in exclude:
                    self.assertFalse(light_only)
                    return second
                if third not in exclude:
                    self.assertTrue(light_only)
                    return third
                return None

            def worker(args):
                (root / str(args.job_id)).touch()
                deadline = time.monotonic() + 6
                while not all((root / str(job[2])).exists() for job in (first, second, third)):
                    if time.monotonic() >= deadline:
                        return 2
                    time.sleep(0.02)
                return 0

            with (
                patch.object(strix_pilot, "FLEET_STATE", root),
                patch.object(strix_pilot, "light_slot_enabled", return_value=True),
                patch.object(strix_pilot, "run", side_effect=worker),
                patch.object(strix_pilot, "next_queued_job", side_effect=queued),
            ):
                statuses, _ = strix_pilot.run_parallel(
                    root / "routes.json", routes, first, Event(),
                )
            self.assertEqual(statuses, {0})

    def test_deferred_general_job_rechecks_light_queue_without_waiting(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            light = Route(LAB, 101, 201, strix_pilot.LIGHT_WORKFLOWS[LAB])
            first, general, verify = (0, 700, 900), (0, 701, 901), (1, 702, 902)
            scans = []

            def queued(routes, cursor, exclude, *, light_only=False, active_single_runs=()):
                scans.append(light_only)
                if len(scans) == 1:
                    return general
                if len(scans) == 2:
                    return None
                return verify if verify not in exclude else None

            def worker(args):
                if args.job_id == general[2]:
                    time.sleep(2.5)
                    raise strix_pilot.CapacityUnavailable("test headroom")
                marker = root / "light-started"
                if args.job_id == verify[2]:
                    marker.touch()
                    return 0
                deadline = time.monotonic() + 8
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                return 0 if marker.exists() else 2

            with (
                patch.object(strix_pilot, "FLEET_STATE", root),
                patch.object(strix_pilot, "light_slot_enabled", return_value=True),
                patch.object(strix_pilot, "run", side_effect=worker),
                patch.object(strix_pilot, "next_queued_job", side_effect=queued),
            ):
                statuses, _ = strix_pilot.run_parallel(
                    root / "routes.json", [self.route, light], first, Event(),
                )
            self.assertEqual(scans[:3], [False, True, True])
            self.assertEqual(statuses, {0, strix_pilot.CAPACITY_DEFERRED})

    def test_failed_job_frees_slot_without_waiting_for_next_poll(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, replacement = [(0, 700, job) for job in (900, 901, 902)]
            peer_running = root / "peer-running"
            replacement_running = root / "replacement-running"

            def worker(args):
                if args.job_id == replacement[2]:
                    replacement_running.touch()
                    return 0
                if args.job_id == second[2]:
                    peer_running.touch()
                    marker = replacement_running
                    status = 0
                else:
                    marker = peer_running
                    status = 1
                deadline = time.monotonic() + 7
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                return status if marker.exists() else 2

            def queued(routes, cursor, exclude):
                return next((job for job in (second, replacement) if job not in exclude), None)

            with (
                patch.object(strix_pilot, "FLEET_STATE", root),
                patch.object(strix_pilot, "light_slot_enabled", return_value=False),
                patch.object(strix_pilot, "run", side_effect=worker),
                patch.object(strix_pilot, "next_queued_job", side_effect=queued),
            ):
                statuses, _ = strix_pilot.run_parallel(
                    root / "routes.json", [self.route], first, Event(),
                )
            self.assertEqual(statuses, {0, 1})

    def test_light_slot_uses_two_guest_reserve_until_admitting_a_third(self):
        reserve = strix_pilot.reserve_guest_disk
        two = strix_pilot.PARALLEL_DISK_RESERVE
        three = strix_pilot.THREE_SLOT_DISK_RESERVE
        self.assertEqual(reserve({2}, two, 0), two)
        self.assertEqual(reserve({0}, three - 1, 0), two)
        self.assertEqual(reserve({0, 1}, three, 0), three)
        with self.assertRaises(strix_pilot.CapacityUnavailable):
            reserve({0, 2}, three - 1, 0)
        with self.assertRaises(strix_pilot.CapacityUnavailable):
            reserve({2}, two, strix_pilot.CORE_SEED_DISK_EXTRA)

    def test_third_slot_tries_alternative_after_ram_deferral(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, deferred, alternative = [(0, 700, number) for number in range(900, 904)]
            alternative_started = root / "alternative-started"

            def worker(args):
                if args.job_id == deferred[2]:
                    raise strix_pilot.InsufficientMemory("test headroom")
                if args.job_id == alternative[2]:
                    alternative_started.touch()
                    return 0
                deadline = time.monotonic() + 8
                while not alternative_started.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                return 0 if alternative_started.exists() else 2

            def queued(routes, cursor, exclude, **options):
                return next((job for job in (second, deferred, alternative) if job not in exclude), None)

            with (
                patch.object(strix_pilot, "FLEET_STATE", root),
                patch.object(strix_pilot, "light_slot_enabled", return_value=True),
                patch.object(strix_pilot, "light_slot_reserved", return_value=False),
                patch.object(strix_pilot, "run", side_effect=worker),
                patch.object(strix_pilot, "next_queued_job", side_effect=queued),
            ):
                statuses, _ = strix_pilot.run_parallel(
                    root / "routes.json", [self.route], first, Event(),
                )
            self.assertTrue(alternative_started.exists())
            self.assertEqual(statuses, {0, strix_pilot.CAPACITY_DEFERRED})

    def test_capacity_deferred_job_is_retried_while_peer_remains_live(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = (0, 700, 900), (0, 700, 901)

            def job(args):
                deferred = root / "deferred"
                finished = root / "finished"
                if args.job_id == second[2]:
                    if not deferred.exists():
                        deferred.touch()
                        raise strix_pilot.CapacityUnavailable("test headroom")
                    finished.touch()
                    return 0
                deadline = time.monotonic() + 7
                while not finished.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                return 0 if finished.exists() else 2

            def queued(routes, cursor, exclude):
                return second if second not in exclude else None

            with (
                patch.object(strix_pilot, "FLEET_STATE", root),
                patch.object(strix_pilot, "light_slot_enabled", return_value=False),
                patch.object(strix_pilot, "VACANT_SLOT_POLL_SECONDS", 0),
                patch.object(strix_pilot, "run", side_effect=job),
                patch.object(strix_pilot, "next_queued_job", side_effect=queued),
            ):
                statuses, _ = strix_pilot.run_parallel(
                    root / "routes.json", [self.route], first, Event(),
                )
            self.assertEqual(statuses, {0, strix_pilot.CAPACITY_DEFERRED})

    def test_small_ram_shortfall_gets_early_retry_while_peer_remains_live(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = (0, 700, 900), (0, 700, 901)

            def job(args):
                deferred = root / "deferred"
                finished = root / "finished"
                if args.job_id == second[2]:
                    if not deferred.exists():
                        deferred.touch()
                        raise strix_pilot.NearMemoryLimit("test headroom")
                    finished.touch()
                    return 0
                deadline = time.monotonic() + 7
                while not finished.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                return 0 if finished.exists() else 2

            def queued(routes, cursor, exclude):
                return second if second not in exclude else None

            with (
                patch.object(strix_pilot, "FLEET_STATE", root),
                patch.object(strix_pilot, "light_slot_enabled", return_value=False),
                patch.object(strix_pilot, "CAPACITY_RECHECK_SECONDS", 0),
                patch.object(strix_pilot, "run", side_effect=job),
                patch.object(strix_pilot, "next_queued_job", side_effect=queued),
            ):
                statuses, _ = strix_pilot.run_parallel(
                    root / "routes.json", [self.route], first, Event(),
                )
            self.assertEqual(statuses, {0, strix_pilot.CAPACITY_DEFERRED})

    def test_retention_reuses_verified_overlay_digest_and_checks_published_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            slot = root / "slot"
            slot.mkdir()
            disk = slot / "disk.qcow2"
            disk.write_bytes(b"overlay")
            serial = slot / "serial.log"
            serial.write_bytes(b"entire serial log")
            state = Mock()
            backend = Mock(ROOT=root)
            backend.digest.side_effect = lambda file: hashlib.sha256(file.read_bytes()).hexdigest()
            expected = hashlib.sha256(b"overlay").hexdigest()
            inspect = Mock(return_value={"overlay_sha256": expected})
            resource = {"id": "example"}
            (root / "retained/example").mkdir(parents=True)
            strix_pilot.retain_stopped(state, resource, slot, backend, inspect)

            archive = root / "retained/example/slot-originals"
            self.assertEqual((archive / disk.name).read_bytes(), b"overlay")
            self.assertEqual((archive / serial.name).read_bytes(), b"entire serial log")
            inspect.assert_called_once_with(
                disk, strix_pilot.IMAGE, strix_pilot.IMAGE_SHA, backend.execute,
            )
            self.assertNotIn(disk, [call.args[0] for call in backend.digest.call_args_list])
            backend.digest.assert_any_call(archive / disk.name)
            manifest = state.update.call_args_list[0].kwargs["original_retention"]
            self.assertEqual(manifest["files"][0]["sha256"], expected)
            self.assertEqual(manifest["backing"]["overlay_sha256"], expected)

    def test_parallel_watcher_refills_a_slot_while_the_first_job_runs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, third = [(0, 700, job_id) for job_id in (900, 901, 902)]

            def worker(args):
                with (root / "manager.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        return 2
                (root / str(args.job_id)).touch()
                if args.job_id == first[2]:
                    deadline = time.monotonic() + 6
                    while not (root / str(third[2])).exists():
                        if time.monotonic() >= deadline:
                            return 2
                        time.sleep(0.02)
                return 0

            def queued(routes, cursor, exclude):
                return next((candidate for candidate in (second, third)
                             if candidate not in exclude), None)

            with (
                patch.object(strix_pilot, "FLEET_STATE", root),
                patch.object(strix_pilot, "light_slot_enabled", return_value=False),
                patch.object(strix_pilot, "run", side_effect=worker),
                patch.object(strix_pilot, "next_queued_job", side_effect=queued),
            ):
                statuses, repository = strix_pilot.run_parallel(
                    root / "routes.json", [self.route], first, Event(),
                )
            self.assertEqual(statuses, {0})
            self.assertEqual(repository, self.route.repository)


    def test_incomplete_workflow_inventory_does_not_block_other_repositories(self):
        other = Route("newdarwindev/runspindle-core", 101, 201, "b" * 40)
        def inventory(_api, path, _field, filters):
            if self.route.repository in path:
                raise RoutingError("GitHub inventory pagination was incomplete")
            return [{"id": 700}] if filters["status"] == "queued" else []

        with (
            patch.object(strix_pilot, "inventory", side_effect=inventory),
            patch.object(strix_pilot, "queue_listing_api", side_effect=RoutingError("invalid recent listing")),
            patch.object(strix_pilot, "queued_jobs", return_value=(None, [{"id": 900}])),
        ):
            self.assertEqual(strix_pilot.next_queued_job([self.route, other], 0), (1, 700, 900))

    def test_light_only_search_preserves_general_queue_position(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            routes = [
                Route(f"newdarwindev/{name}", 100 + i, 200 + i, "a" * 40)
                for i, name in enumerate(("runspindle-host", "runspindle-core", "runspindle-next-experiment", "runspindle-demo-suite"))
            ]
            first, second, light, fourth = [(i, 700, 900 + i) for i in range(4)]
            cursors = []
            light_cursors = []

            def worker(args):
                (root / str(args.job_id)).touch()
                waiting_for = {900: 903, 901: 902, 902: 903}.get(args.job_id)
                if waiting_for is None:
                    return 0
                deadline = time.monotonic() + 12
                while not (root / str(waiting_for)).exists():
                    if time.monotonic() >= deadline:
                        return 2
                    time.sleep(0.02)
                return 0

            def queued(routes, cursor, exclude, *, light_only=False, active_single_runs=()):
                if light_only:
                    light_cursors.append(cursor)
                    return light if light not in exclude else None
                cursors.append(cursor)
                if second not in exclude:
                    return second
                return fourth if fourth not in exclude else None

            with (
                patch.object(strix_pilot, "FLEET_STATE", root),
                patch.object(strix_pilot, "light_slot_enabled", return_value=True),
                patch.object(strix_pilot, "light_workflow", side_effect=lambda route: route == routes[2]),
                patch.object(strix_pilot, "light_slot_reserved", return_value=True),
                patch.object(strix_pilot, "run", side_effect=worker),
                patch.object(strix_pilot, "next_queued_job", side_effect=queued),
            ):
                statuses, repository = strix_pilot.run_parallel(
                    root / "routes.json", routes, first, Event(),
                )
            self.assertEqual(cursors[:2], [1, 2])
            self.assertEqual(light_cursors[:1], [2])
            self.assertEqual(statuses, {0})
            self.assertEqual(repository, routes[3].repository)


if __name__ == "__main__":
    unittest.main()
