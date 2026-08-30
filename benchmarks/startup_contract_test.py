from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest

from benchmarks.startup_contract import (
    RUN_SCHEMA,
    SUBJECT_SCHEMA,
    StartupContractError,
    digest_bytes,
    finalize_bundle,
    load_plan,
    verify_bundle_report,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAN_PATH = REPO_ROOT / "benchmarks" / "startup_plan.json"


class StartupContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = load_plan(PLAN_PATH)

    def _write_export(
        self,
        path: pathlib.Path,
        labels: dict[str, list[float]],
        *,
        claimed_mean: float | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        results = []
        for label, samples in labels.items():
            results.append(
                {
                    "command": label,
                    "mean": claimed_mean
                    if claimed_mean is not None
                    else sum(samples) / len(samples),
                    "times": samples,
                    "exit_codes": [0] * len(samples),
                }
            )
        path.write_text(json.dumps({"results": results}) + "\n", encoding="utf-8")

    def _base_bundle(self, root: pathlib.Path, *, compared: bool) -> pathlib.Path:
        bundle = root / "bundle"
        bundle.mkdir()
        shutil.copyfile(PLAN_PATH, bundle / "context.json")
        (bundle / "subject.json").write_text(
            json.dumps(
                {
                    "schema_version": SUBJECT_SCHEMA,
                    "head": {
                        "source_sha": "a" * 40,
                        "source_dirty": False,
                        "sha256": "sha256:" + ("b" * 64),
                        "size_bytes": 123,
                    },
                    "control": (
                        {
                            "source_sha": "c" * 40,
                            "source_dirty": False,
                            "sha256": "sha256:" + ("d" * 64),
                            "size_bytes": 122,
                        }
                        if compared
                        else None
                    ),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (bundle / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": RUN_SCHEMA,
                    "plan_digest": self.plan.digest,
                    "mode": "quick",
                    "runs": self.plan.modes["quick"].runs,
                    "compared": compared,
                    "host": {"platform": "Linux"},
                    "tools": {
                        "hyperfine": {
                            "digest": "sha256:" + ("e" * 64),
                            "version": "hyperfine 1.20.0",
                        },
                        "zig_version": "0.16.0",
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        preflights = []
        for case in self.plan.cases:
            arms = (
                ("control", "head") if compared and case.role == "gating" else ("head",)
            )
            for arm in arms:
                stdout = b"" if case.stdout_policy == "empty" else b"{}"
                stderr = b""
                stdout_relative = f"preflight-output/{case.id}-{arm}.stdout"
                stderr_relative = f"preflight-output/{case.id}-{arm}.stderr"
                stdout_path = bundle / stdout_relative
                stderr_path = bundle / stderr_relative
                stdout_path.parent.mkdir(exist_ok=True)
                stdout_path.write_bytes(stdout)
                stderr_path.write_bytes(stderr)
                record = {
                    "schema_version": "fx-startup-preflight/v1",
                    "case_id": case.id,
                    "arm": arm,
                    "argv": [case.executable, *case.argv],
                    "exit_code": 0,
                    "stdout_policy": case.stdout_policy,
                    "stdout_artifact": stdout_relative,
                    "stdout_digest": digest_bytes(stdout),
                    "stdout_size_bytes": len(stdout),
                    "stderr_artifact": stderr_relative,
                    "stderr_digest": digest_bytes(stderr),
                    "stderr_size_bytes": len(stderr),
                    "passed": True,
                }
                preflights.append(record)
                detail = bundle / "preflight" / f"{case.id}-{arm}.json"
                detail.parent.mkdir(exist_ok=True)
                detail.write_text(json.dumps(record) + "\n", encoding="utf-8")
        (bundle / "preflight.json").write_text(
            json.dumps(preflights) + "\n",
            encoding="utf-8",
        )
        return bundle

    def _populate_single(
        self, bundle: pathlib.Path, *, slow_case: str | None = None
    ) -> None:
        count = self.plan.modes["quick"].runs
        for case in self.plan.cases:
            sample = 0.003 if case.id == slow_case else 0.001
            self._write_export(
                bundle / "raw" / f"{case.id}.json",
                {case.label: [sample] * count},
                claimed_mean=99.0,
            )

    def _populate_comparison(self, bundle: pathlib.Path) -> None:
        mode = self.plan.modes["quick"]
        block_size = mode.runs // mode.comparison_blocks
        for case in self.plan.cases:
            if case.role == "diagnostic":
                self._write_export(
                    bundle / "raw" / f"{case.id}.json",
                    {case.label: [0.0004] * mode.runs},
                )
                continue
            for block in range(mode.comparison_blocks):
                labels = (
                    {"control": [0.001] * block_size, "head": [0.00105] * block_size}
                    if block % 2 == 0
                    else {
                        "head": [0.00105] * block_size,
                        "control": [0.001] * block_size,
                    }
                )
                self._write_export(
                    bundle / "raw" / case.id / f"block-{block + 1:03d}.json",
                    labels,
                )

    def test_plan_has_one_explicit_policy_for_every_case(self) -> None:
        self.assertEqual("1.20.0", self.plan.tool_version)
        self.assertEqual(7, len(self.plan.cases))
        self.assertEqual(1, sum(case.role == "diagnostic" for case in self.plan.cases))
        self.assertTrue(
            all(
                case.linux_mean_ceiling_seconds == 0.002
                for case in self.plan.cases
                if case.role == "gating"
            )
        )

    def test_raw_samples_override_untrusted_hyperfine_aggregate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-contract-") as tmp:
            bundle = self._base_bundle(pathlib.Path(tmp), compared=False)
            self._populate_single(bundle)

            report = finalize_bundle(
                bundle,
                plan=self.plan,
                mode_name="quick",
                system_name="Linux",
                compared=False,
            )

            self.assertEqual("pass", report["status"])
            self.assertFalse((bundle / "proof.lock").exists())
            gating = [case for case in report["cases"] if case["role"] == "gating"]
            self.assertTrue(
                all(case["head"]["mean_seconds"] == 0.001 for case in gating)
            )
            self.assertEqual(report, verify_bundle_report(bundle))

    def test_missing_case_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-contract-") as tmp:
            bundle = self._base_bundle(pathlib.Path(tmp), compared=False)
            self._populate_single(bundle)
            (bundle / "raw" / "doctor.json").unlink()

            with self.assertRaisesRegex(StartupContractError, "doctor.json"):
                finalize_bundle(
                    bundle,
                    plan=self.plan,
                    mode_name="quick",
                    system_name="Linux",
                    compared=False,
                )

    def test_unknown_raw_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-contract-") as tmp:
            bundle = self._base_bundle(pathlib.Path(tmp), compared=False)
            self._populate_single(bundle)
            (bundle / "raw" / "stale.json").write_text(
                '{"results": []}\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(
                StartupContractError, "unexpected=.*stale.json"
            ):
                finalize_bundle(
                    bundle,
                    plan=self.plan,
                    mode_name="quick",
                    system_name="Linux",
                    compared=False,
                )

    def test_preflight_output_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-contract-") as tmp:
            bundle = self._base_bundle(pathlib.Path(tmp), compared=False)
            self._populate_single(bundle)
            (bundle / "preflight-output" / "help-head.stdout").write_bytes(b"changed")

            with self.assertRaisesRegex(StartupContractError, "does not match"):
                finalize_bundle(
                    bundle,
                    plan=self.plan,
                    mode_name="quick",
                    system_name="Linux",
                    compared=False,
                )

    def test_preflight_rejects_consistently_recorded_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-contract-") as tmp:
            bundle = self._base_bundle(pathlib.Path(tmp), compared=False)
            self._populate_single(bundle)
            content = b"not-json\n"
            output = bundle / "preflight-output" / "status-head.stdout"
            output.write_bytes(content)
            summary = json.loads((bundle / "preflight.json").read_text())
            record = next(item for item in summary if item["case_id"] == "status")
            record["stdout_digest"] = digest_bytes(content)
            record["stdout_size_bytes"] = len(content)
            (bundle / "preflight.json").write_text(
                json.dumps(summary) + "\n",
                encoding="utf-8",
            )
            (bundle / "preflight" / "status-head.json").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StartupContractError, "stdout JSON"):
                finalize_bundle(
                    bundle,
                    plan=self.plan,
                    mode_name="quick",
                    system_name="Linux",
                    compared=False,
                )

    def test_report_verification_rejects_post_capture_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-contract-") as tmp:
            bundle = self._base_bundle(pathlib.Path(tmp), compared=False)
            self._populate_single(bundle)
            finalize_bundle(
                bundle,
                plan=self.plan,
                mode_name="quick",
                system_name="Linux",
                compared=False,
            )
            report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
            report["status"] = "fail"
            (bundle / "report.json").write_text(
                json.dumps(report) + "\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                StartupContractError, "does not match recomputed"
            ):
                verify_bundle_report(bundle)

    def test_run_tool_identity_must_match_the_plan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-contract-") as tmp:
            bundle = self._base_bundle(pathlib.Path(tmp), compared=False)
            self._populate_single(bundle)
            run = json.loads((bundle / "run.json").read_text())
            run["tools"]["hyperfine"]["version"] = "hyperfine 1.19.0"
            (bundle / "run.json").write_text(
                json.dumps(run) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StartupContractError, "version"):
                finalize_bundle(
                    bundle,
                    plan=self.plan,
                    mode_name="quick",
                    system_name="Linux",
                    compared=False,
                )

    def test_linux_ceiling_failure_is_retained_in_a_valid_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-contract-") as tmp:
            bundle = self._base_bundle(pathlib.Path(tmp), compared=False)
            self._populate_single(bundle, slow_case="help")

            report = finalize_bundle(
                bundle,
                plan=self.plan,
                mode_name="quick",
                system_name="Linux",
                compared=False,
            )

            self.assertEqual("fail", report["status"])
            self.assertEqual(["help"], report["failed_cases"])
            self.assertTrue((bundle / "report.json").is_file())

    def test_comparison_uses_complete_alternating_blocks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-contract-") as tmp:
            bundle = self._base_bundle(pathlib.Path(tmp), compared=True)
            self._populate_comparison(bundle)

            report = finalize_bundle(
                bundle,
                plan=self.plan,
                mode_name="quick",
                system_name="Linux",
                compared=True,
            )

            help_case = next(case for case in report["cases"] if case["id"] == "help")
            relative = help_case["relative_comparison"]
            self.assertEqual(4, len(relative["blocks"]))
            self.assertEqual(3, relative["degrees_freedom"])
            self.assertEqual(
                "nominal_assuming_independent_blocks",
                relative["confidence_interpretation"],
            )
            self.assertIsNone(relative["lag_one_autocorrelation"])
            self.assertAlmostEqual(0.05, relative["point_change"], places=12)
            self.assertTrue(relative["within_registered_margin"])


if __name__ == "__main__":
    unittest.main()
