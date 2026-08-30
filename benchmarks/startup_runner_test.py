from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock

from benchmarks import startup_runner


class StartupRunnerTests(unittest.TestCase):
    def _executable(self, path: pathlib.Path, content: str) -> pathlib.Path:
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_runner_captures_complete_neutral_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-runner-") as temporary:
            root = pathlib.Path(temporary)
            fake_fx = self._executable(
                root / "fx",
                """
                #!/usr/bin/env python3
                import os
                if os.environ.get("FX_BENCH") != "1":
                    print("{}")
                """,
            )
            fake_hyperfine = self._executable(
                root / "hyperfine",
                """
                #!/usr/bin/env python3
                import json
                import pathlib
                import sys

                if sys.argv[1:] == ["--version"]:
                    print("hyperfine 1.20.0")
                    raise SystemExit(0)
                export = pathlib.Path(sys.argv[sys.argv.index("--export-json") + 1])
                runs = int(sys.argv[sys.argv.index("--runs") + 1])
                labels = [
                    sys.argv[index + 1]
                    for index, value in enumerate(sys.argv)
                    if value == "--command-name"
                ]
                export.parent.mkdir(parents=True, exist_ok=True)
                export.write_text(json.dumps({"results": [
                    {
                        "command": label,
                        "mean": 42.0,
                        "times": [0.001] * runs,
                        "exit_codes": [0] * runs,
                    }
                    for label in labels
                ]}) + "\\n", encoding="utf-8")
                """,
            )
            output = root / "evidence"
            output.mkdir()
            (output / startup_runner.OUTPUT_MARKER).write_text(
                "fx-startup-evidence/v1\n",
                encoding="utf-8",
            )
            (output / "stale.json").write_text("{}\n", encoding="utf-8")
            arguments = startup_runner._parse_args(
                (
                    "--quick",
                    "--skip-build",
                    "--hyperfine",
                    str(fake_hyperfine),
                    "--output",
                    str(output),
                )
            )
            with (
                mock.patch.object(startup_runner, "FX_BINARY", fake_fx),
                mock.patch.object(
                    startup_runner,
                    "_source_identity",
                    return_value=("a" * 40, False),
                ),
                mock.patch.dict(
                    os.environ,
                    {"FX_UNREGISTERED_TEST": "must-not-reach-child"},
                ),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = startup_runner.run(arguments)

            self.assertEqual(0, exit_code)
            self.assertFalse((output / "stale.json").exists())
            self.assertTrue((output / "context.json").is_file())
            self.assertTrue((output / "subject.json").is_file())
            self.assertFalse((output / "proof.lock").exists())
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(7, len(report["cases"]))
            self.assertEqual(startup_runner.platform.system(), report["platform"])
            self.assertTrue(
                all(case["head"]["mean_seconds"] == 0.001 for case in report["cases"])
            )
            run = json.loads((output / "run.json").read_text(encoding="utf-8"))
            omitted = run["environment"]["omitted_ambient_fx_variables"]
            self.assertIn("FX_UNREGISTERED_TEST", omitted)

    def test_runner_refuses_to_replace_unmarked_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fx-startup-runner-") as temporary:
            root = pathlib.Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            output = root / "evidence"
            output.mkdir()
            important = output / "important.txt"
            important.write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(
                startup_runner.StartupRunnerError,
                "regular marker",
            ):
                startup_runner._publish(staging, output)

            self.assertEqual("keep\n", important.read_text(encoding="utf-8"))

    def test_control_arguments_are_atomic(self) -> None:
        arguments = startup_runner._parse_args(("--ci", "--control-sha", "a" * 40))
        with self.assertRaisesRegex(
            startup_runner.StartupRunnerError, "supplied together"
        ):
            startup_runner.run(arguments)


if __name__ == "__main__":
    unittest.main()
