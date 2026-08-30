from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import typer
from proofpack.bundle import verify_bundle
from proofpack.models import ArtifactLock
from proofpack_fx.cli import attach
from typer.testing import CliRunner

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    attach(app)
    return app


def _bundle(root: Path) -> Path:
    bundle = root / "evidence"
    bundle.mkdir()
    (bundle / "subject.json").write_text(
        json.dumps({"schema_version": "fx-subject/v1", "binary": "sha256:" + "1" * 64})
        + "\n",
        encoding="utf-8",
    )
    (bundle / "context.json").write_text(
        json.dumps({"schema_version": "fx-plan/v1", "budget_seconds": 0.002}) + "\n",
        encoding="utf-8",
    )
    (bundle / "report.json").write_text(
        json.dumps({"schema_version": "fx-report/v1", "status": "pass"}) + "\n",
        encoding="utf-8",
    )
    return bundle


class ProofPackFxPluginTests(unittest.TestCase):
    def test_plugin_seals_and_rederives_semantic_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proofpack-fx-") as temporary:
            bundle = _bundle(Path(temporary))

            sealed = runner.invoke(_app(), ["fx", "seal", str(bundle), "--json"])
            self.assertEqual(0, sealed.exit_code, sealed.output)
            producer = json.loads(
                (bundle / "proofpack-producer.json").read_text(encoding="utf-8")
            )
            self.assertEqual("0.1.0", producer["plugin_version"])
            self.assertEqual("0.1.0", producer["proofpack_core_version"])
            lock = verify_bundle(bundle)
            self.assertIsInstance(lock, ArtifactLock)
            assert isinstance(lock, ArtifactLock)

            verified = runner.invoke(_app(), ["fx", "verify", str(bundle), "--json"])
            self.assertEqual(0, verified.exit_code, verified.output)
            self.assertEqual(
                json.loads(verified.stdout)["root_digest"], lock.root_digest
            )

    def test_plugin_refuses_tampering_and_semantic_root_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proofpack-fx-") as temporary:
            bundle = _bundle(Path(temporary))
            self.assertEqual(
                0,
                runner.invoke(_app(), ["fx", "seal", str(bundle)]).exit_code,
            )
            (bundle / "report.json").write_text('{"status":"fail"}\n', encoding="utf-8")

            result = runner.invoke(_app(), ["fx", "verify", str(bundle)])

            self.assertEqual(3, result.exit_code)
            self.assertIn("artifact digest mismatch", result.output)

    def test_plugin_refuses_subject_outside_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proofpack-fx-") as temporary:
            bundle = _bundle(Path(temporary))

            result = runner.invoke(
                _app(),
                ["fx", "seal", str(bundle), "--subject-document", "../subject.json"],
            )

            self.assertEqual(3, result.exit_code)
            self.assertIn("must be confined", result.output)


if __name__ == "__main__":
    unittest.main()
