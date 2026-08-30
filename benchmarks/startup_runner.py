"""Capture startup latency evidence under the registered fx benchmark plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence

from benchmarks.session_list_fixture import generate as generate_session_fixture
from benchmarks.startup_contract import (
    RUN_SCHEMA,
    StartupContractError,
    StartupPlan,
    artifact_identity,
    build_subject,
    digest_file,
    finalize_bundle,
    load_plan,
    print_report,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAN_PATH = REPO_ROOT / "benchmarks" / "startup_plan.json"
DEFAULT_OUTPUT = REPO_ROOT / "benchmarks" / "results" / "startup-evidence"
FX_BINARY = REPO_ROOT / "zig-out" / "bin" / "fx"
GENERAL_SETTINGS = {
    "effort": "high",
    "fast_mode": False,
    "model": "openai/gpt-5.4",
    "permission": {"bash": {"git status *": "allow"}},
    "prompt_history": {"enabled": True},
    "startup_scrollback": True,
    "statusLine": {"context": True, "sandbox": True},
}
SESSION_COUNT = 8
SESSION_LOG_SIZE = 256 * 1024 * 1024
OUTPUT_MARKER = ".fx-startup-evidence"


class StartupRunnerError(RuntimeError):
    pass


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(_json_bytes(value))
    except OSError as error:
        raise StartupRunnerError(f"cannot write {path}: {error}") from error


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _run(
    argv: Sequence[str],
    *,
    cwd: pathlib.Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            tuple(argv),
            cwd=cwd,
            env=dict(environment),
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StartupRunnerError(
            f"command failed to run: {shlex.join(argv)}: {error}"
        ) from error


def _git_output(argv: Sequence[str]) -> str:
    result = _run(
        ("git", *argv),
        cwd=REPO_ROOT,
        environment=os.environ,
        timeout_seconds=30,
    )
    if result.returncode != 0 or result.stderr:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise StartupRunnerError(f"git {' '.join(argv)} failed: {detail}")
    return result.stdout.decode("utf-8", errors="strict").strip()


def _source_identity() -> tuple[str, bool]:
    source_sha = _git_output(("rev-parse", "HEAD"))
    dirty = bool(_git_output(("status", "--porcelain", "--untracked-files=all")))
    return source_sha, dirty


def _resolve_executable(value: str, label: str) -> pathlib.Path:
    candidate = pathlib.Path(value)
    if candidate.parent == pathlib.Path("."):
        discovered = shutil.which(value)
        if discovered is None:
            raise StartupRunnerError(f"{label} is not installed: {value}")
        candidate = pathlib.Path(discovered)
    try:
        resolved = candidate.expanduser().resolve(strict=True)
    except OSError as error:
        raise StartupRunnerError(
            f"cannot resolve {label} at {candidate}: {error}"
        ) from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise StartupRunnerError(f"{label} is not executable: {resolved}")
    return resolved


def _check_hyperfine(binary: pathlib.Path, plan: StartupPlan) -> str:
    result = _run(
        (str(binary), "--version"),
        cwd=REPO_ROOT,
        environment=os.environ,
        timeout_seconds=30,
    )
    observed = result.stdout.decode("utf-8", errors="replace").strip()
    expected = f"hyperfine {plan.tool_version}"
    if result.returncode != 0 or result.stderr or observed != expected:
        raise StartupRunnerError(
            f"startup plan requires {expected!r}; observed stdout={observed!r}, "
            f"stderr={result.stderr.decode('utf-8', errors='replace').strip()!r}"
        )
    return observed


def _build_fx() -> str:
    zig = _resolve_executable("zig", "Zig")
    version = _run(
        (str(zig), "version"),
        cwd=REPO_ROOT,
        environment=os.environ,
        timeout_seconds=30,
    )
    if version.returncode != 0 or version.stderr:
        raise StartupRunnerError("cannot determine the Zig version")
    print("Building fx (ReleaseSafe)...", flush=True)
    built = subprocess.run(
        (str(zig), "build", "-Doptimize=ReleaseSafe"),
        cwd=REPO_ROOT,
        env=os.environ,
        check=False,
    )
    if built.returncode != 0:
        raise StartupRunnerError("ReleaseSafe build failed")
    return version.stdout.decode("utf-8", errors="strict").strip()


def _optional_zig_version() -> str | None:
    discovered = shutil.which("zig")
    if discovered is None:
        return None
    result = _run(
        (str(pathlib.Path(discovered).resolve()), "version"),
        cwd=REPO_ROOT,
        environment=os.environ,
        timeout_seconds=30,
    )
    if result.returncode != 0 or result.stderr:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip() or None


def _true_binary() -> pathlib.Path:
    for candidate in (pathlib.Path("/usr/bin/true"), pathlib.Path("/bin/true")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return _resolve_executable("true", "process baseline")


def _private_json(path: pathlib.Path, value: object) -> None:
    _write_json(path, value)
    path.chmod(0o600)


def _fixture_file(path: pathlib.Path) -> dict[str, object]:
    info = path.stat()
    return {
        "digest": digest_file(path),
        "mode": format(stat.S_IMODE(info.st_mode), "04o"),
        "size_bytes": info.st_size,
    }


def _prepare_fixtures(
    root: pathlib.Path,
) -> tuple[dict[str, pathlib.Path], dict[str, object]]:
    general_home = root / "general-home"
    session_home = root / "session-home"
    session_workspace = root / "session-workspace"
    settings_dir = general_home / ".fx"
    settings_dir.mkdir(parents=True, mode=0o700)
    settings_dir.chmod(0o700)
    settings_path = settings_dir / "settings.json"
    _private_json(settings_path, GENERAL_SETTINGS)
    session_home.mkdir()
    session_workspace.mkdir()
    generate_session_fixture(
        session_home,
        session_workspace,
        SESSION_COUNT,
        SESSION_LOG_SIZE,
        False,
    )
    generator = REPO_ROOT / "benchmarks" / "session_list_fixture.py"
    fixtures = {
        "general_home": general_home,
        "session_home": session_home,
        "session_workspace": session_workspace,
    }
    manifest = {
        "schema_version": "fx-startup-fixtures/v1",
        "general": {
            "settings": GENERAL_SETTINGS,
            "settings_file": _fixture_file(settings_path),
        },
        "sessions": {
            "count": SESSION_COUNT,
            "event_log_size_bytes_each": SESSION_LOG_SIZE,
            "generator_digest": digest_file(generator),
            "generator_relative_path": "benchmarks/session_list_fixture.py",
            "projected_schema_version": 3,
            "sparse_event_logs": True,
        },
    }
    return fixtures, manifest


def _sanitized_environment(
    plan: StartupPlan,
    *,
    home: pathlib.Path,
    additions: Mapping[str, str],
) -> dict[str, str]:
    result = {
        key: os.environ[key] for key in plan.inherited_environment if key in os.environ
    }
    result.update(plan.fixed_environment)
    result.update(additions)
    result["HOME"] = str(home)
    return result


def _recorded_environment(plan: StartupPlan) -> dict[str, object]:
    inherited = []
    for key in plan.inherited_environment:
        if key in os.environ:
            inherited.append(
                {
                    "name": key,
                    "value_digest": _sha256_bytes(os.environ[key].encode("utf-8")),
                }
            )
    return {
        "fixed": dict(plan.fixed_environment),
        "inherited": inherited,
        "omitted_ambient_fx_variables": sorted(
            key
            for key in os.environ
            if key.startswith("FX_") and key not in plan.fixed_environment
        ),
    }


def _case_command(
    executable: str,
    argv: Sequence[str],
    *,
    fx_binary: pathlib.Path,
    baseline_binary: pathlib.Path,
) -> tuple[str, ...]:
    binary = fx_binary if executable == "fx" else baseline_binary
    return (str(binary), *argv)


def _case_cwd(
    working_directory: str, fixtures: Mapping[str, pathlib.Path]
) -> pathlib.Path:
    if working_directory == "repository":
        return REPO_ROOT
    return fixtures["session_workspace"]


def _case_home(fixture: str, fixtures: Mapping[str, pathlib.Path]) -> pathlib.Path:
    return (
        fixtures["general_home"] if fixture == "general" else fixtures["session_home"]
    )


def _stdout_matches(policy: str, content: bytes) -> bool:
    if policy == "empty":
        return not content
    if policy == "nonempty":
        return bool(content)
    try:
        json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def _preflight(
    *,
    bundle: pathlib.Path,
    plan: StartupPlan,
    head_binary: pathlib.Path,
    control_binary: pathlib.Path | None,
    baseline_binary: pathlib.Path,
    fixtures: Mapping[str, pathlib.Path],
) -> list[dict[str, object]]:
    summary: list[dict[str, object]] = []
    for case in plan.cases:
        arms: tuple[tuple[str, pathlib.Path], ...]
        if case.executable == "process_baseline" or control_binary is None:
            arms = (("head", head_binary),)
        else:
            arms = (("control", control_binary), ("head", head_binary))
        for arm, fx_binary in arms:
            command = _case_command(
                case.executable,
                case.argv,
                fx_binary=fx_binary,
                baseline_binary=baseline_binary,
            )
            environment = _sanitized_environment(
                plan,
                home=_case_home(case.fixture, fixtures),
                additions=case.environment,
            )
            result = _run(
                command,
                cwd=_case_cwd(case.working_directory, fixtures),
                environment=environment,
                timeout_seconds=30,
            )
            stdout_ok = _stdout_matches(case.stdout_policy, result.stdout)
            passed = result.returncode == 0 and not result.stderr and stdout_ok
            stdout_relative = (
                pathlib.Path("preflight-output") / f"{case.id}-{arm}.stdout"
            )
            stderr_relative = (
                pathlib.Path("preflight-output") / f"{case.id}-{arm}.stderr"
            )
            stdout_path = bundle / stdout_relative
            stderr_path = bundle / stderr_relative
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_bytes(result.stdout)
            stderr_path.write_bytes(result.stderr)
            record = {
                "schema_version": "fx-startup-preflight/v1",
                "case_id": case.id,
                "arm": arm,
                "argv": [case.executable, *case.argv],
                "exit_code": result.returncode,
                "stdout_policy": case.stdout_policy,
                "stdout_artifact": stdout_relative.as_posix(),
                "stdout_digest": _sha256_bytes(result.stdout),
                "stdout_size_bytes": len(result.stdout),
                "stderr_artifact": stderr_relative.as_posix(),
                "stderr_digest": _sha256_bytes(result.stderr),
                "stderr_size_bytes": len(result.stderr),
                "passed": passed,
            }
            _write_json(bundle / "preflight" / f"{case.id}-{arm}.json", record)
            summary.append(record)
            if not passed:
                raise StartupRunnerError(
                    f"preflight failed for {case.id} ({arm}): exit={result.returncode}, "
                    f"stdout_bytes={len(result.stdout)}, stderr_bytes={len(result.stderr)}"
                )
    _write_json(bundle / "preflight.json", summary)
    return summary


def _driver_record(
    *,
    path: pathlib.Path,
    logical_argv: Sequence[str],
    result: subprocess.CompletedProcess[bytes],
) -> None:
    _write_json(
        path,
        {
            "schema_version": "fx-startup-driver/v1",
            "argv": list(logical_argv),
            "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace"),
        },
    )


def _hyperfine(
    *,
    binary: pathlib.Path,
    export: pathlib.Path,
    commands: Sequence[tuple[str, Sequence[str]]],
    runs: int,
    warmups: int,
    cwd: pathlib.Path,
    environment: Mapping[str, str],
    driver_log: pathlib.Path,
) -> None:
    export.parent.mkdir(parents=True, exist_ok=True)
    argv: list[str] = [
        str(binary),
        "--shell=none",
        "--style",
        "none",
        "--warmup",
        str(warmups),
        "--runs",
        str(runs),
        "--export-json",
        str(export),
    ]
    logical_argv = list(argv)
    logical_argv[logical_argv.index(str(export))] = export.relative_to(
        export.parents[1]
    ).as_posix()
    for label, command in commands:
        joined = shlex.join(command)
        argv.extend(("--command-name", label, joined))
        logical_command = [pathlib.Path(command[0]).name, *command[1:]]
        logical_argv.extend(("--command-name", label, shlex.join(logical_command)))
    result = _run(
        argv,
        cwd=cwd,
        environment=environment,
        timeout_seconds=600,
    )
    _driver_record(path=driver_log, logical_argv=logical_argv, result=result)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise StartupRunnerError(f"Hyperfine failed for {export.name}: {detail}")
    if not export.is_file():
        raise StartupRunnerError(f"Hyperfine did not write {export}")


def _measure(
    *,
    bundle: pathlib.Path,
    plan: StartupPlan,
    mode_name: str,
    hyperfine: pathlib.Path,
    head_binary: pathlib.Path,
    control_binary: pathlib.Path | None,
    baseline_binary: pathlib.Path,
    fixtures: Mapping[str, pathlib.Path],
) -> None:
    mode = plan.modes[mode_name]
    for case in plan.cases:
        cwd = _case_cwd(case.working_directory, fixtures)
        environment = _sanitized_environment(
            plan,
            home=_case_home(case.fixture, fixtures),
            additions=case.environment,
        )
        head_command = _case_command(
            case.executable,
            case.argv,
            fx_binary=head_binary,
            baseline_binary=baseline_binary,
        )
        if control_binary is None or case.role == "diagnostic":
            _hyperfine(
                binary=hyperfine,
                export=bundle / "raw" / f"{case.id}.json",
                commands=((case.label, head_command),),
                runs=mode.runs,
                warmups=mode.warmup_runs,
                cwd=cwd,
                environment=environment,
                driver_log=bundle / "driver" / f"{case.id}.json",
            )
            continue
        control_command = _case_command(
            case.executable,
            case.argv,
            fx_binary=control_binary,
            baseline_binary=baseline_binary,
        )
        samples_per_block = mode.runs // mode.comparison_blocks
        for block_index in range(mode.comparison_blocks):
            commands = (
                (("control", control_command), ("head", head_command))
                if block_index % 2 == 0
                else (("head", head_command), ("control", control_command))
            )
            filename = f"block-{block_index + 1:03d}.json"
            _hyperfine(
                binary=hyperfine,
                export=bundle / "raw" / case.id / filename,
                commands=commands,
                runs=samples_per_block,
                warmups=mode.comparison_warmup_runs_per_block,
                cwd=cwd,
                environment=environment,
                driver_log=bundle / "driver" / case.id / filename,
            )


def _host_metadata() -> dict[str, object]:
    uname = platform.uname()
    return {
        "machine": uname.machine,
        "platform": platform.system(),
        "platform_release": uname.release,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
    }


def _publish(staging: pathlib.Path, output: pathlib.Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            raise StartupRunnerError(
                f"refusing to replace non-directory output: {output}"
            )
        if any(output.iterdir()):
            marker = output / OUTPUT_MARKER
            if marker.is_symlink() or not marker.is_file():
                raise StartupRunnerError(
                    f"refusing to replace output without a regular marker: {output}"
                )
            try:
                marker_value = marker.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise StartupRunnerError(
                    f"refusing to replace unmarked output directory: {output}"
                ) from error
            if marker_value != "fx-startup-evidence/v1\n":
                raise StartupRunnerError(
                    f"refusing to replace output with an invalid marker: {output}"
                )
        shutil.rmtree(output)
    shutil.move(str(staging), str(output))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--quick", action="store_true", help="Use the registered quick plan."
    )
    modes.add_argument(
        "--ci", action="store_true", help="Use the registered CI plan and skip build."
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--hyperfine", default="hyperfine")
    parser.add_argument("--control-binary", type=pathlib.Path)
    parser.add_argument("--control-sha")
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def run(arguments: argparse.Namespace) -> int:
    plan = load_plan(PLAN_PATH)
    mode_name = "ci" if arguments.ci else "quick" if arguments.quick else "default"
    if (arguments.control_binary is None) != (arguments.control_sha is None):
        raise StartupRunnerError(
            "--control-binary and --control-sha must be supplied together"
        )
    if not arguments.skip_build and not arguments.ci:
        zig_version = _build_fx()
    else:
        zig_version = _optional_zig_version()

    head_binary = _resolve_executable(str(FX_BINARY), "freshly built fx")
    hyperfine = _resolve_executable(arguments.hyperfine, "Hyperfine")
    hyperfine_version = _check_hyperfine(hyperfine, plan)
    baseline_binary = _true_binary()
    source_sha, source_dirty = _source_identity()
    control_binary = (
        None
        if arguments.control_binary is None
        else _resolve_executable(str(arguments.control_binary), "control fx")
    )
    head_identity = artifact_identity(
        head_binary,
        source_sha=source_sha,
        dirty=source_dirty,
    )
    control_identity = (
        None
        if control_binary is None
        else artifact_identity(
            control_binary,
            source_sha=arguments.control_sha,
            dirty=False,
        )
    )

    output = arguments.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(
        tempfile.mkdtemp(prefix=".startup-evidence-", dir=output.parent)
    )
    started_ns = time.time_ns()
    try:
        (staging / OUTPUT_MARKER).write_text(
            "fx-startup-evidence/v1\n",
            encoding="utf-8",
        )
        shutil.copyfile(PLAN_PATH, staging / "context.json")
        _write_json(
            staging / "subject.json", build_subject(head_identity, control_identity)
        )
        with tempfile.TemporaryDirectory(prefix="fx-startup-fixtures-") as fixture_temp:
            fixtures, fixture_manifest = _prepare_fixtures(pathlib.Path(fixture_temp))
            _write_json(staging / "fixtures.json", fixture_manifest)
            run_document = {
                "schema_version": RUN_SCHEMA,
                "plan_digest": plan.digest,
                "mode": mode_name,
                "runs": plan.modes[mode_name].runs,
                "compared": control_binary is not None,
                "host": _host_metadata(),
                "environment": _recorded_environment(plan),
                "tools": {
                    "hyperfine": {
                        "digest": digest_file(hyperfine),
                        "version": hyperfine_version,
                    },
                    "zig_version": zig_version,
                },
                "producer_source": [
                    {
                        "path": source.relative_to(REPO_ROOT).as_posix(),
                        "digest": digest_file(source),
                    }
                    for source in (
                        REPO_ROOT / "benchmarks" / "session_list_fixture.py",
                        REPO_ROOT / "benchmarks" / "startup_contract.py",
                        REPO_ROOT / "benchmarks" / "startup_runner.py",
                    )
                ],
                "started_unix_ns": started_ns,
            }
            _write_json(staging / "run.json", run_document)
            _preflight(
                bundle=staging,
                plan=plan,
                head_binary=head_binary,
                control_binary=control_binary,
                baseline_binary=baseline_binary,
                fixtures=fixtures,
            )
            _measure(
                bundle=staging,
                plan=plan,
                mode_name=mode_name,
                hyperfine=hyperfine,
                head_binary=head_binary,
                control_binary=control_binary,
                baseline_binary=baseline_binary,
                fixtures=fixtures,
            )
        report = finalize_bundle(
            staging,
            plan=plan,
            mode_name=mode_name,
            compared=control_binary is not None,
        )
        print_report(report)
        _publish(staging, output)
        print(f"Evidence written to {output}")
        return 1 if report["status"] == "fail" else 0
    except Exception as error:
        _write_json(
            staging / "failure.json",
            {
                "schema_version": "fx-startup-failure/v1",
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
        _publish(staging, output)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(_parse_args(argv))
    except (StartupContractError, StartupRunnerError, OSError, ValueError) as error:
        print(f"startup benchmark failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
