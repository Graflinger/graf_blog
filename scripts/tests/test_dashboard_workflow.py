"""Offline contracts for the release-first dashboard workflows.

Run: python3 -B -m unittest discover -s scripts/tests -p 'test_dashboard_workflow.py' -v

These tests inspect the current, indentation-based workflow blocks using only the
standard library. This is deliberately not a general YAML/Actions interpreter;
Git behavior is covered by the publisher and sync integration tests. No workflow
commands, source requests, or Git operations are executed here.
"""

from pathlib import Path
import re
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
PUBLISH = "env.PUBLISH == 'true'"
CHANGED = "steps.prepare.outputs.changed == 'true'"
ELECTRICITY = "python -m src.data_pipelines.dashboards.german_electricity.refresh"
SCRIPT_TESTS = "python -B -m unittest discover -s scripts/tests -p 'test_*.py' -v"
FRONTEND_CHECKS = ["npm ci", "npm test -- --runInBand", "npm run lint", "npm run build"]


def workflow(name):
    text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    # Comments must never satisfy executable workflow contracts.
    return re.sub(r"(?m)^ *#.*\n?", "", text)


def field(block, key, default=None):
    """Read one top-level scalar or indented block; nested keys cannot match."""
    matches = list(re.finditer(r"(?m)^" + re.escape(key) + r":([^\n]*)$", block))
    if not matches:
        return default
    if len(matches) != 1:
        raise AssertionError(f"Expected exactly one {key!r} field")
    match = matches[0]
    value = match.group(1).strip()
    if value and value != "|":
        return value
    rest = block[match.end():].lstrip("\n")
    sibling = re.search(r"(?m)^\S", rest)
    return textwrap.dedent(rest[:sibling.start()] if sibling else rest).strip()


def steps(job):
    body = field(job, "steps")
    if body is None:
        raise AssertionError("Missing job steps")
    starts = list(re.finditer(r"(?m)^- ", body))
    if not starts:
        raise AssertionError("Expected block-style workflow steps")
    return [textwrap.dedent("  " + body[start.end():end]).strip()
            for start, end in zip(starts, [m.start() for m in starts[1:]] + [len(body)])]


class DashboardWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.refresh = workflow("dashboard-refresh.yml")
        cls.ci = workflow("frontend-ci.yml")
        cls.production = field(field(cls.refresh, "jobs"), "validate")
        cls.sync = field(field(cls.refresh, "jobs"), "sync-main")
        cls.production_steps = steps(cls.production)
        cls.sync_steps = steps(cls.sync)

    def one_step(self, job_steps, *, run=None, action=None, step_id=None):
        matches = [step for step in job_steps
                   if (run is None or run in field(step, "run", ""))
                   and (action is None or field(step, "uses", "").startswith(action + "@"))
                   and (step_id is None or field(step, "id") == step_id)]
        self.assertEqual(len(matches), 1, f"Expected one step: {run or action or step_id}")
        return matches[0]

    def assert_order(self, job_steps, *ordered):
        positions = [job_steps.index(step) for step in ordered]
        self.assertEqual(positions, sorted(set(positions)), "Workflow steps out of order")

    def assert_success_only(self, step, condition=None):
        # An absent if uses Actions' implicit success(); neither always() nor
        # continue-on-error may turn failed validation into publication.
        self.assertEqual(field(step, "if"), condition)
        self.assertIn(field(step, "continue-on-error"), (None, "false"))

    def test_production_budget_schedule_concurrency_and_manual_default(self):
        triggers = field(self.refresh, "on")
        self.assertIn("- cron: '0 6 * * *'", field(triggers, "schedule"))
        publish = field(field(field(triggers, "workflow_dispatch"), "inputs"), "publish")
        self.assertEqual(field(publish, "type"), "boolean")
        self.assertEqual(field(publish, "default"), "false")
        self.assertEqual(field(self.production, "timeout-minutes"), "10")
        concurrency = field(self.refresh, "concurrency")
        self.assertEqual(field(concurrency, "group"), "electricity-dashboard-publication")
        self.assertEqual(field(concurrency, "cancel-in-progress"), "false")
        self.assertEqual(field(field(self.refresh, "permissions"), "contents"), "write")

    def test_schedule_on_main_and_publishing_dispatch_checkout_release(self):
        self.assertEqual(field(field(self.production, "env"), "PUBLISH"),
                         "${{ github.event_name == 'schedule' || inputs.publish }}")
        checkout = self.one_step(self.production_steps, action="actions/checkout")
        # The schedule's event SHA/ref belongs to main. It must not determine
        # which source code executes in a production refresh.
        self.assertEqual(field(field(checkout, "with"), "ref"),
                         "${{ env.PUBLISH == 'true' && 'releases/cloudflare' || github.ref_name }}")
        self.assertEqual(field(field(checkout, "with"), "fetch-depth"), "0")
        self.assertEqual(field(field(checkout, "with"), "persist-credentials"), "true")
        self.assert_success_only(checkout)

    def test_manual_false_validates_selected_branch_without_publication(self):
        checkout = self.one_step(self.production_steps, action="actions/checkout")
        self.assertTrue(field(field(checkout, "with"), "ref").endswith("|| github.ref_name }}"))
        gated = [step for step in self.production_steps if field(step, "if") is not None]
        self.assertEqual(gated, [
            self.one_step(self.production_steps, run='test "$GITHUB_REF"'),
            self.one_step(self.production_steps, run="scripts/dashboard_publish.py check"),
            self.one_step(self.production_steps, run="scripts/dashboard_publish.py publish"),
            self.one_step(self.production_steps, step_id="verified"),
        ])
        for step in gated:
            self.assert_success_only(step, PUBLISH)
        self.assertIsNone(field(self.production, "if"))

    def test_main_event_guard_and_release_base_check_precede_work(self):
        guard = self.one_step(self.production_steps, run='test "$GITHUB_REF"')
        self.assertRegex(field(guard, "run"),
                         r'test "\$GITHUB_REF" = "refs/heads/main" \|\| \{[^\n]*exit 1; \}')
        self.assert_success_only(guard, PUBLISH)
        checkout = self.one_step(self.production_steps, action="actions/checkout")
        check = self.one_step(self.production_steps, run="scripts/dashboard_publish.py check")
        self.assertEqual(field(check, "id"), "release")
        self.assertIn('base=$(python3 scripts/dashboard_publish.py check)', field(check, "run"))
        self.assertIn('echo "base=$base" >> "$GITHUB_OUTPUT"', field(check, "run"))
        recent = next(s for s in self.production_steps if field(s, "run") == ELECTRICITY)
        self.assert_order(self.production_steps, guard, checkout, check,
                          self.one_step(self.production_steps, run="pip install"), recent)

    def test_recent_history_trade_frontend_publish_verify_order(self):
        refresh_steps = [s for s in self.production_steps
                         if field(s, "run", "").startswith(ELECTRICITY)]
        self.assertEqual([field(s, "run") for s in refresh_steps], [
            ELECTRICITY,
        ])
        for step in refresh_steps:
            self.assert_success_only(step)
            self.assertEqual(field(step, "working-directory"), "pipeline")
            self.assertEqual(field(field(step, "env"), "PYTHONPATH"), ".")
        frontend = self.one_step(self.production_steps, run="npm run build")
        self.assertEqual(field(frontend, "working-directory"), "frontend")
        self.assertEqual(field(frontend, "run").splitlines(), FRONTEND_CHECKS)
        self.assert_success_only(frontend)
        publish = self.one_step(self.production_steps, run="scripts/dashboard_publish.py publish")
        self.assertEqual(field(publish, "run"),
                         'python3 scripts/dashboard_publish.py publish --base "$RELEASE_BASE"')
        self.assertEqual(field(field(publish, "env"), "RELEASE_BASE"), "${{ steps.release.outputs.base }}")
        self.assert_success_only(publish, PUBLISH)
        self.assert_order(self.production_steps, *refresh_steps, frontend, publish,
                          self.one_step(self.production_steps, step_id="verified"))

    def test_production_offline_safeguards_run_before_refresh(self):
        pipeline = self.one_step(self.production_steps, run="test_german_electricity*.py")
        safeguards = self.one_step(self.production_steps, run=SCRIPT_TESTS)
        for step in (pipeline, safeguards):
            self.assert_success_only(step)
        self.assertEqual(field(pipeline, "working-directory"), "pipeline")
        recent = next(s for s in self.production_steps if field(s, "run") == ELECTRICITY)
        self.assertLess(self.production_steps.index(pipeline), self.production_steps.index(recent))
        self.assertLess(self.production_steps.index(safeguards), self.production_steps.index(recent))

    def test_publication_commands_use_checkout_root_and_fail_fast_shell(self):
        for job_steps in (self.production_steps, self.sync_steps):
            for step in job_steps:
                with self.subTest(step=field(step, "name", field(step, "uses"))):
                    self.assertIn(field(step, "continue-on-error"), (None, "false"))
                    # Default Linux run shell exits on command failure, including
                    # before a later echo can emit a trusted job/step output.
                    self.assertIsNone(field(step, "shell"))
        for command in ("scripts/dashboard_publish.py check", "scripts/dashboard_publish.py publish",
                        "scripts/verify_dashboard_deployment.py", SCRIPT_TESTS):
            step = self.one_step(self.production_steps, run=command)
            self.assertIsNone(field(step, "working-directory"))

    def test_verified_sha_is_emitted_only_after_public_verification_including_no_change(self):
        verify = self.one_step(self.production_steps, step_id="verified")
        self.assert_success_only(verify, PUBLISH)
        commands = field(verify, "run").splitlines()
        self.assertEqual(commands[:2], [
            "python3 scripts/verify_dashboard_deployment.py --timeout 240 --interval 10",
            'echo "release_sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"',
        ])
        self.assertEqual(field(field(self.production, "outputs"), "release_sha"),
                         "${{ steps.verified.outputs.release_sha }}")
        self.assertEqual(sum('echo "release_sha=' in field(s, "run", "")
                             for s in self.production_steps), 1)
        # There is no data-changed/publisher-output gate: unchanged production
        # must still be verified and retry a previously unsuccessful main sync.
        self.assertNotIn("changed", field(verify, "if"))

    def test_sync_is_a_separate_job_requiring_success_and_verified_sha(self):
        self.assertIsNone(field(self.production, "needs"))
        self.assertEqual(field(self.sync, "needs"), "validate")
        self.assertEqual(field(self.sync, "if"), "needs.validate.outputs.release_sha != ''")
        self.assertEqual(field(field(self.sync, "env"), "RELEASE_SHA"),
                         "${{ needs.validate.outputs.release_sha }}")
        self.assertEqual(field(self.sync, "timeout-minutes"), "10")
        for job in (self.production, self.sync):
            self.assertIn(field(job, "continue-on-error"), (None, "false"))

    def test_sync_uses_released_script_and_shared_external_state(self):
        checkout = self.one_step(self.sync_steps, action="actions/checkout")
        self.assertEqual(field(field(checkout, "with"), "ref"), "releases/cloudflare")
        self.assertEqual(field(field(checkout, "with"), "fetch-depth"), "0")
        self.assertEqual(field(field(checkout, "with"), "persist-credentials"), "true")
        prepare = self.one_step(self.sync_steps, step_id="prepare")
        commands = field(prepare, "run").splitlines()
        self.assertIn('test "$(git rev-parse HEAD)" = "$RELEASE_SHA"', commands[0])
        self.assertIn('exit 1;', commands[0])
        command = "\n".join(commands[1:]).replace("\\\n", " ")
        self.assertEqual(" ".join(command.split()),
                         'python3 scripts/dashboard_sync.py prepare --release "$RELEASE_SHA" '
                         '--worktree "$RUNNER_TEMP/dashboard-sync" '
                         '--state "$RUNNER_TEMP/dashboard-sync.json" >> "$GITHUB_OUTPUT"')
        publish = self.one_step(self.sync_steps, run="scripts/dashboard_sync.py publish")
        self.assertEqual(field(publish, "run"),
                         'python3 scripts/dashboard_sync.py publish --state "$RUNNER_TEMP/dashboard-sync.json"')
        for block in (self.refresh, self.sync, self.production):
            self.assertIsNone(field(block, "defaults"), "Publisher commands must run in checkout root")
        for step in (checkout, prepare, publish):
            self.assertIsNone(field(step, "working-directory"))
            self.assert_success_only(step)
        self.assert_order(self.sync_steps, checkout, prepare, publish)

    def test_candidate_validation_runs_in_worktree_before_main_push(self):
        install = self.one_step(self.sync_steps, run="pip install")
        pipeline = self.one_step(self.sync_steps, run="test_german_electricity*.py")
        node = self.one_step(self.sync_steps, action="actions/setup-node")
        frontend = self.one_step(self.sync_steps, run="npm run build")
        for step, suffix in ((install, "/pipeline"), (pipeline, ""), (frontend, "/frontend")):
            self.assertEqual(field(step, "working-directory"), "${{ runner.temp }}/dashboard-sync" + suffix)
        self.assertEqual(field(install, "run"), "python -m pip install -r requirements-dashboard.txt")
        self.assertEqual(field(pipeline, "run").splitlines(), [
            "(cd pipeline && PYTHONPATH=. python -B -m unittest discover -s tests -p 'test_german_electricity*.py' -v)",
            SCRIPT_TESTS,
        ])
        self.assertEqual(field(frontend, "run").splitlines(), FRONTEND_CHECKS)
        self.assertEqual(field(field(node, "with"), "node-version"), "'20'")
        for step in (install, pipeline, node, frontend):
            self.assert_success_only(step, CHANGED)
        self.assert_order(self.sync_steps, self.one_step(self.sync_steps, step_id="prepare"),
                          install, pipeline, node, frontend,
                          self.one_step(self.sync_steps, run="scripts/dashboard_sync.py publish"))

    def test_no_change_sync_still_checks_state_without_running_candidate_validators(self):
        conditional = [s for s in self.sync_steps if field(s, "if") == CHANGED]
        self.assertEqual(conditional, [
            self.one_step(self.sync_steps, run="pip install"),
            self.one_step(self.sync_steps, run="test_german_electricity*.py"),
            self.one_step(self.sync_steps, action="actions/setup-node"),
            self.one_step(self.sync_steps, run="npm run build"),
        ])
        self.assert_success_only(self.one_step(self.sync_steps, run="scripts/dashboard_sync.py publish"))

    def test_sync_has_no_live_refresh_or_production_publisher_commands(self):
        commands = "\n".join(field(s, "run", "") for s in self.sync_steps)
        self.assertNotIn("src.data_pipelines", commands)
        self.assertNotIn("dashboard_publish.py", commands)
        self.assertNotIn("verify_dashboard_deployment.py", commands)
        self.assertNotRegex(commands, r"\b(?:curl|wget|refresh|backfill|reconcile)\b")
        self.assertNotRegex(commands, r"\bgit\s+(?:push|merge|checkout|switch|reset)\b")
        self.assertNotIn("dashboard_sync.py", self.production)

    def test_sync_failure_reports_verified_production_is_unaffected(self):
        report = self.one_step(self.sync_steps, run="Main synchronization:")
        self.assertEqual(field(report, "if"), "always()")
        self.assertEqual(field(field(report, "env"), "SYNC_STATUS"), "${{ job.status }}")
        command = field(report, "run")
        self.assertIn('Verified production commit: $RELEASE_SHA.', command)
        self.assertIn('>> "$GITHUB_STEP_SUMMARY"', command)
        self.assertIn('if [ "$SYNC_STATUS" != "success" ]; then', command)
        self.assertIn("::error::Main synchronization failed; production was already verified and is unaffected.", command)
        self.assertIn("retries sync, even with unchanged data", command)
        self.assertEqual(self.sync_steps[-1], report)

    def test_main_only_sync_script_changes_trigger_ci_and_run_script_tests(self):
        triggers = field(self.ci, "on")
        self.assertEqual(field(field(triggers, "push"), "branches"), "['main']")
        for event in ("push", "pull_request"):
            with self.subTest(event=event):
                paths = field(field(triggers, event), "paths")
                for path in ("scripts/dashboard_sync.py", "scripts/dashboard_publish.py",
                             "scripts/verify_dashboard_deployment.py", "scripts/tests/**", ".github/workflows/**"):
                    self.assertIn("'" + path + "'", paths)
        job = field(field(self.ci, "jobs"), "pipeline")
        self.assertIsNone(field(job, "if"))
        self.assertIsNone(field(job, "defaults"))
        test = self.one_step(steps(job), run="unittest discover -s scripts/tests -p 'test_*.py' -v")
        self.assert_success_only(test)
        self.assertIsNone(field(test, "working-directory"))


if __name__ == "__main__":
    unittest.main()
