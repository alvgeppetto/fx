from __future__ import annotations

import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bench.yml"
PROOFPACK_ACTION = (
    REPO_ROOT / ".github" / "actions" / "setup-proofpack-fx" / "action.yml"
)


class StartupWorkflowTests(unittest.TestCase):
    def test_workflow_builds_same_runner_control_and_retains_sealed_evidence(
        self,
    ) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn('test "$(git rev-parse HEAD)" = "$HEAD_SHA"', workflow)
        self.assertIn(
            'git worktree add --detach "$base_worktree" "$BASE_SHA"', workflow
        )
        self.assertIn("fx-startup-head-cache", workflow)
        self.assertIn("fx-startup-base-cache", workflow)
        self.assertIn('--control-binary "$RUNNER_TEMP/fx-startup-control"', workflow)
        self.assertIn("proofpack fx seal", workflow)
        self.assertIn("proofpack fx verify", workflow)
        self.assertIn("python3 -m benchmarks.startup_verify", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("include-hidden-files: true", workflow)
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            workflow,
        )

    def test_hyperfine_and_proofpack_sources_are_immutable(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        action = PROOFPACK_ACTION.read_text(encoding="utf-8")

        self.assertIn("hyperfine-v1.20.0-x86_64-unknown-linux-musl.tar.gz", workflow)
        self.assertIn(
            "77611a0a0843d210996227e0fd596a9474ef57dd44268aef9c193c7c830cc125",
            workflow,
        )
        self.assertNotIn("sudo dpkg", workflow)
        self.assertIn("968b8e13589930f9dde919d1334df356943ee509", action)
        self.assertIn("integrations/proofpack-fx/constraints.txt", action)
        self.assertIn("proofpack fx --help", action)


if __name__ == "__main__":
    unittest.main()
