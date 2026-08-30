from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import pathlib
import platform
import re
import statistics
from collections.abc import Mapping, Sequence

PLAN_SCHEMA = "fx-startup-benchmark-plan/v1"
REPORT_SCHEMA = "fx-startup-benchmark-report/v1"
SUBJECT_SCHEMA = "fx-startup-benchmark-subject/v1"
RUN_SCHEMA = "fx-startup-benchmark-run/v1"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
IDENTIFIER_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class StartupContractError(RuntimeError):
    pass


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise StartupContractError(f"value is not canonical JSON: {error}") from error


def digest_value(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def digest_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise StartupContractError(f"cannot read artifact {path}: {error}") from error
    return f"sha256:{digest.hexdigest()}"


@dataclasses.dataclass(frozen=True)
class RunMode:
    runs: int
    warmup_runs: int
    comparison_blocks: int
    comparison_warmup_runs_per_block: int


@dataclasses.dataclass(frozen=True)
class BenchmarkCase:
    id: str
    label: str
    executable: str
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    fixture: str
    working_directory: str
    stdout_policy: str
    role: str
    linux_mean_ceiling_seconds: float | None


@dataclasses.dataclass(frozen=True)
class RelativePolicy:
    decision: str
    maximum_regression: float
    confidence_level: float
    confidence_assumption: str
    method: str
    order: str


@dataclasses.dataclass(frozen=True)
class StartupPlan:
    path: pathlib.Path
    raw: Mapping[str, object]
    digest: str
    tool_version: str
    modes: Mapping[str, RunMode]
    relative_policy: RelativePolicy
    inherited_environment: tuple[str, ...]
    fixed_environment: Mapping[str, str]
    cases: tuple[BenchmarkCase, ...]


def _strict_json_text(value: str, label: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise StartupContractError(f"{label} contains duplicate key {key!r}")
            result[key] = item
        return result

    def reject_constant(value: str) -> object:
        raise StartupContractError(f"{label} contains non-finite number {value}")

    try:
        return json.loads(
            value,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise StartupContractError(f"cannot parse {label}: {error}") from error


def _load_json(path: pathlib.Path, label: str) -> object:
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise StartupContractError(f"cannot read {label} at {path}: {error}") from error
    return _strict_json_text(value, label)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise StartupContractError(f"{label} must be an object")
    return value


def _exact_keys(
    value: object,
    expected: Sequence[str],
    label: str,
) -> Mapping[str, object]:
    mapping = _mapping(value, label)
    expected_set = set(expected)
    actual = set(mapping)
    if actual != expected_set:
        missing = sorted(expected_set - actual)
        unexpected = sorted(actual - expected_set)
        raise StartupContractError(
            f"{label} fields mismatch: missing={missing}, unexpected={unexpected}"
        )
    return mapping


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StartupContractError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StartupContractError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StartupContractError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise StartupContractError(f"{label} must be finite")
    return result


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise StartupContractError(f"{label} must be an array")
    result = tuple(_text(item, f"{label}[{index}]") for index, item in enumerate(value))
    if len(result) != len(set(result)):
        raise StartupContractError(f"{label} must not contain duplicates")
    return result


def load_plan(path: pathlib.Path) -> StartupPlan:
    value = _load_json(path, "startup benchmark plan")
    raw = _exact_keys(
        value,
        (
            "schema_version",
            "measurement",
            "platform_policy",
            "environment",
            "cases",
        ),
        "startup benchmark plan",
    )
    if raw["schema_version"] != PLAN_SCHEMA:
        raise StartupContractError(
            f"unsupported startup plan schema: {raw['schema_version']!r}"
        )

    platform_policy = _exact_keys(
        raw["platform_policy"],
        ("Linux", "other"),
        "platform_policy",
    )
    if platform_policy["Linux"] != "gate_raw_mean":
        raise StartupContractError("Linux platform policy must gate the raw mean")
    if platform_policy["other"] != "informational_raw_mean":
        raise StartupContractError("non-Linux platform policy must be informational")

    environment = _exact_keys(
        raw["environment"],
        ("fixed", "inherited"),
        "environment",
    )
    inherited = _string_tuple(environment["inherited"], "environment.inherited")
    if tuple(sorted(inherited)) != inherited:
        raise StartupContractError("environment.inherited must be sorted")
    fixed_raw = _mapping(environment["fixed"], "environment.fixed")
    fixed = {
        _text(key, "environment.fixed key"): _text(value, f"environment.fixed.{key}")
        for key, value in fixed_raw.items()
    }
    if tuple(sorted(fixed)) != tuple(fixed):
        raise StartupContractError("environment.fixed keys must be sorted")

    measurement = _exact_keys(
        raw["measurement"],
        (
            "metric",
            "modes",
            "process_baseline_policy",
            "relative_comparison",
            "shell",
            "tool",
        ),
        "measurement",
    )
    if measurement["metric"] != "raw_wall_clock_seconds":
        raise StartupContractError("startup metric must be raw wall-clock seconds")
    if measurement["process_baseline_policy"] != "diagnostic_only_no_subtraction":
        raise StartupContractError("the process baseline must remain diagnostic only")
    if measurement["shell"] != "none":
        raise StartupContractError("startup measurements must disable the shell")
    tool = _exact_keys(measurement["tool"], ("name", "version"), "measurement.tool")
    if tool["name"] != "hyperfine":
        raise StartupContractError("startup measurement tool must be hyperfine")
    tool_version = _text(tool["version"], "measurement.tool.version")

    modes_raw = _exact_keys(
        measurement["modes"],
        ("ci", "default", "quick"),
        "measurement.modes",
    )
    modes: dict[str, RunMode] = {}
    for name, mode_value in modes_raw.items():
        mode = _exact_keys(
            mode_value,
            (
                "runs",
                "warmup_runs",
                "comparison_blocks",
                "comparison_warmup_runs_per_block",
            ),
            f"measurement.modes.{name}",
        )
        parsed = RunMode(
            runs=_integer(mode["runs"], f"measurement.modes.{name}.runs"),
            warmup_runs=_integer(
                mode["warmup_runs"],
                f"measurement.modes.{name}.warmup_runs",
                minimum=0,
            ),
            comparison_blocks=_integer(
                mode["comparison_blocks"],
                f"measurement.modes.{name}.comparison_blocks",
                minimum=4,
            ),
            comparison_warmup_runs_per_block=_integer(
                mode["comparison_warmup_runs_per_block"],
                f"measurement.modes.{name}.comparison_warmup_runs_per_block",
                minimum=0,
            ),
        )
        if parsed.runs % parsed.comparison_blocks != 0:
            raise StartupContractError(
                f"measurement.modes.{name}.runs must divide evenly into comparison blocks"
            )
        modes[name] = parsed

    relative_raw = _exact_keys(
        measurement["relative_comparison"],
        (
            "confidence_assumption",
            "confidence_level",
            "decision",
            "maximum_regression",
            "method",
            "order",
        ),
        "measurement.relative_comparison",
    )
    relative = RelativePolicy(
        decision=_text(relative_raw["decision"], "relative comparison decision"),
        maximum_regression=_number(
            relative_raw["maximum_regression"],
            "relative comparison maximum_regression",
        ),
        confidence_level=_number(
            relative_raw["confidence_level"],
            "relative comparison confidence_level",
        ),
        confidence_assumption=_text(
            relative_raw["confidence_assumption"],
            "relative comparison confidence_assumption",
        ),
        method=_text(relative_raw["method"], "relative comparison method"),
        order=_text(relative_raw["order"], "relative comparison order"),
    )
    if relative.decision != "informational":
        raise StartupContractError("relative comparison is informational in plan v1")
    if not 0 <= relative.maximum_regression < 1:
        raise StartupContractError("maximum_regression must satisfy 0 <= value < 1")
    if relative.confidence_level != 0.95:
        raise StartupContractError("plan v1 supports a 0.95 confidence level")
    if relative.confidence_assumption != "independent_block_log_ratios":
        raise StartupContractError(
            "unsupported relative comparison confidence assumption"
        )
    if relative.method != "one_sided_paired_t_on_block_log_ratios":
        raise StartupContractError("unsupported relative comparison method")
    if relative.order != "alternating_control_head":
        raise StartupContractError("relative comparison order must alternate")

    cases_value = raw["cases"]
    if not isinstance(cases_value, list) or not cases_value:
        raise StartupContractError("cases must be a non-empty array")
    cases: list[BenchmarkCase] = []
    for index, case_value in enumerate(cases_value):
        case = _exact_keys(
            case_value,
            (
                "id",
                "label",
                "executable",
                "argv",
                "environment",
                "fixture",
                "working_directory",
                "stdout_policy",
                "role",
                "linux_mean_ceiling_seconds",
            ),
            f"cases[{index}]",
        )
        case_id = _text(case["id"], f"cases[{index}].id")
        if IDENTIFIER_PATTERN.fullmatch(case_id) is None:
            raise StartupContractError(f"invalid case id: {case_id!r}")
        executable = _text(case["executable"], f"cases[{index}].executable")
        if executable not in ("fx", "process_baseline"):
            raise StartupContractError(f"unsupported executable role: {executable!r}")
        role = _text(case["role"], f"cases[{index}].role")
        ceiling_value = case["linux_mean_ceiling_seconds"]
        if role == "gating":
            if ceiling_value is None:
                raise StartupContractError(
                    f"gating case {case_id} is missing its Linux ceiling"
                )
            ceiling = _number(
                ceiling_value, f"cases[{index}].linux_mean_ceiling_seconds"
            )
            if ceiling <= 0:
                raise StartupContractError(
                    f"gating case {case_id} ceiling must be positive"
                )
        elif role == "diagnostic":
            if ceiling_value is not None:
                raise StartupContractError(
                    f"diagnostic case {case_id} cannot have a ceiling"
                )
            ceiling = None
        else:
            raise StartupContractError(f"unsupported case role: {role!r}")
        argv = _string_tuple(case["argv"], f"cases[{index}].argv")
        case_environment_raw = _mapping(
            case["environment"], f"cases[{index}].environment"
        )
        case_environment = {
            _text(key, f"cases[{index}].environment key"): _text(
                item,
                f"cases[{index}].environment.{key}",
            )
            for key, item in case_environment_raw.items()
        }
        fixture = _text(case["fixture"], f"cases[{index}].fixture")
        if fixture not in ("general", "sessions"):
            raise StartupContractError(f"unsupported fixture role: {fixture!r}")
        working_directory = _text(
            case["working_directory"],
            f"cases[{index}].working_directory",
        )
        if working_directory not in ("repository", "session_workspace"):
            raise StartupContractError(
                f"unsupported working directory role: {working_directory!r}"
            )
        stdout_policy = _text(case["stdout_policy"], f"cases[{index}].stdout_policy")
        if stdout_policy not in ("empty", "json", "nonempty"):
            raise StartupContractError(f"unsupported stdout policy: {stdout_policy!r}")
        cases.append(
            BenchmarkCase(
                id=case_id,
                label=_text(case["label"], f"cases[{index}].label"),
                executable=executable,
                argv=argv,
                environment=case_environment,
                fixture=fixture,
                working_directory=working_directory,
                stdout_policy=stdout_policy,
                role=role,
                linux_mean_ceiling_seconds=ceiling,
            )
        )

    for attribute in ("id", "label"):
        values = [getattr(case, attribute) for case in cases]
        if len(values) != len(set(values)):
            raise StartupContractError(f"case {attribute}s must be unique")
    diagnostics = [case for case in cases if case.role == "diagnostic"]
    if len(diagnostics) != 1 or diagnostics[0].executable != "process_baseline":
        raise StartupContractError(
            "the plan must contain one process baseline diagnostic"
        )
    if not any(case.role == "gating" for case in cases):
        raise StartupContractError("the plan has no gating cases")

    return StartupPlan(
        path=path,
        raw=raw,
        digest=digest_value(raw),
        tool_version=tool_version,
        modes=modes,
        relative_policy=relative,
        inherited_environment=inherited,
        fixed_environment=fixed,
        cases=tuple(cases),
    )


def artifact_identity(
    path: pathlib.Path,
    *,
    source_sha: str,
    dirty: bool,
) -> dict[str, object]:
    if SHA_PATTERN.fullmatch(source_sha) is None:
        raise StartupContractError("source SHA must be lowercase 40-character hex")
    try:
        size_bytes = path.stat().st_size
    except OSError as error:
        raise StartupContractError(
            f"cannot stat benchmark artifact {path}: {error}"
        ) from error
    if not path.is_file() or size_bytes <= 0:
        raise StartupContractError(f"benchmark artifact is missing or empty: {path}")
    return {
        "source_sha": source_sha,
        "source_dirty": dirty,
        "sha256": digest_file(path),
        "size_bytes": size_bytes,
    }


def build_subject(
    head: Mapping[str, object],
    control: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        "schema_version": SUBJECT_SCHEMA,
        "head": dict(head),
        "control": None if control is None else dict(control),
    }


def _read_hyperfine(
    path: pathlib.Path,
    *,
    expected_labels: Sequence[str],
    expected_samples: int,
) -> dict[str, tuple[float, ...]]:
    value = _load_json(path, f"Hyperfine export {path.name}")
    payload = _mapping(value, f"Hyperfine export {path.name}")
    results = payload.get("results")
    if not isinstance(results, list):
        raise StartupContractError(
            f"Hyperfine export {path} is missing its results list"
        )
    parsed: dict[str, tuple[float, ...]] = {}
    expected = set(expected_labels)
    for index, entry_value in enumerate(results):
        entry = _mapping(entry_value, f"Hyperfine result {index}")
        label = entry.get("command")
        if not isinstance(label, str) or label not in expected:
            raise StartupContractError(
                f"Hyperfine export {path} has unexpected label {label!r}"
            )
        if label in parsed:
            raise StartupContractError(
                f"Hyperfine export {path} duplicates label {label!r}"
            )
        times = entry.get("times")
        if not isinstance(times, list) or len(times) != expected_samples:
            observed = len(times) if isinstance(times, list) else "missing"
            raise StartupContractError(
                f"Hyperfine {label} returned {observed} samples; expected {expected_samples}"
            )
        samples: list[float] = []
        for sample_index, sample in enumerate(times):
            measured = _number(sample, f"Hyperfine {label} sample {sample_index}")
            if measured <= 0:
                raise StartupContractError(
                    f"Hyperfine {label} sample {sample_index} must be positive"
                )
            samples.append(measured)
        exit_codes = entry.get("exit_codes")
        if exit_codes is not None and (
            not isinstance(exit_codes, list)
            or len(exit_codes) != expected_samples
            or any(code != 0 for code in exit_codes)
        ):
            raise StartupContractError(f"Hyperfine {label} contains a failed command")
        parsed[label] = tuple(samples)
    missing = expected - set(parsed)
    if missing:
        raise StartupContractError(
            f"Hyperfine export {path} is missing labels: {sorted(missing)}"
        )
    return parsed


def _percentile(samples: Sequence[float], fraction: float) -> float:
    if not samples:
        raise StartupContractError("percentile requires samples")
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def sample_statistics(samples: Sequence[float]) -> dict[str, object]:
    if not samples:
        raise StartupContractError("sample statistics require samples")
    if any(not math.isfinite(sample) or sample <= 0 for sample in samples):
        raise StartupContractError("sample statistics require finite positive samples")
    return {
        "count": len(samples),
        "mean_seconds": statistics.fmean(samples),
        "stddev_seconds": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "median_seconds": statistics.median(samples),
        "p95_seconds": _percentile(samples, 0.95),
        "min_seconds": min(samples),
        "max_seconds": max(samples),
    }


_ONE_SIDED_T_95 = (
    0.0,
    6.314,
    2.920,
    2.353,
    2.132,
    2.015,
    1.943,
    1.895,
    1.860,
    1.833,
    1.812,
    1.796,
    1.782,
    1.771,
    1.761,
    1.753,
    1.746,
    1.740,
    1.734,
    1.729,
    1.725,
    1.721,
    1.717,
    1.714,
    1.711,
    1.708,
    1.706,
    1.703,
    1.701,
    1.699,
    1.697,
)


def paired_relative_statistics(
    control_blocks: Sequence[Sequence[float]],
    head_blocks: Sequence[Sequence[float]],
    *,
    maximum_regression: float,
) -> dict[str, object]:
    if len(control_blocks) != len(head_blocks) or len(control_blocks) < 2:
        raise StartupContractError(
            "paired comparison requires at least two complete blocks"
        )
    log_ratios: list[float] = []
    block_rows: list[dict[str, object]] = []
    for index, (control, head) in enumerate(
        zip(control_blocks, head_blocks, strict=True)
    ):
        if not control or not head:
            raise StartupContractError("paired comparison contains an empty block")
        control_mean = statistics.fmean(control)
        head_mean = statistics.fmean(head)
        if control_mean <= 0 or head_mean <= 0:
            raise StartupContractError("paired block means must be positive")
        log_ratio = math.log(head_mean / control_mean)
        log_ratios.append(log_ratio)
        block_rows.append(
            {
                "block": index + 1,
                "control_mean_seconds": control_mean,
                "head_mean_seconds": head_mean,
                "head_change": math.exp(log_ratio) - 1.0,
            }
        )
    mean_log_ratio = statistics.fmean(log_ratios)
    standard_error = statistics.stdev(log_ratios) / math.sqrt(len(log_ratios))
    degrees_freedom = len(log_ratios) - 1
    critical = (
        _ONE_SIDED_T_95[degrees_freedom]
        if degrees_freedom < len(_ONE_SIDED_T_95)
        else 1.645
    )
    upper_change = math.exp(mean_log_ratio + critical * standard_error) - 1.0
    point_change = math.exp(mean_log_ratio) - 1.0
    centered = [value - mean_log_ratio for value in log_ratios]
    squared_total = sum(value * value for value in centered)
    lag_one_autocorrelation = (
        None
        if squared_total == 0
        else sum(
            centered[index - 1] * centered[index] for index in range(1, len(centered))
        )
        / squared_total
    )
    return {
        "method": "one_sided_paired_t_on_block_log_ratios",
        "decision": "informational",
        "confidence_level": 0.95,
        "confidence_interpretation": "nominal_assuming_independent_blocks",
        "degrees_freedom": degrees_freedom,
        "lag_one_autocorrelation": lag_one_autocorrelation,
        "maximum_regression": maximum_regression,
        "point_change": point_change,
        "upper_confidence_change": upper_change,
        "within_registered_margin": upper_change <= maximum_regression,
        "blocks": block_rows,
    }


def _expected_preflights(
    plan: StartupPlan,
    *,
    compared: bool,
) -> tuple[tuple[BenchmarkCase, str], ...]:
    expected: list[tuple[BenchmarkCase, str]] = []
    for case in plan.cases:
        if compared and case.role == "gating":
            expected.extend(((case, "control"), (case, "head")))
        else:
            expected.append((case, "head"))
    return tuple(expected)


def _validate_preflights(
    bundle: pathlib.Path,
    *,
    plan: StartupPlan,
    compared: bool,
) -> None:
    summary = _load_json(bundle / "preflight.json", "benchmark preflight summary")
    if not isinstance(summary, list):
        raise StartupContractError("benchmark preflight summary must be an array")
    expected = _expected_preflights(plan, compared=compared)
    expected_keys = {(case.id, arm) for case, arm in expected}
    expected_paths = {f"{case.id}-{arm}.json" for case, arm in expected}
    preflight_root = bundle / "preflight"
    try:
        discovered = tuple(preflight_root.rglob("*"))
    except OSError as error:
        raise StartupContractError(
            f"cannot inspect benchmark preflights: {error}"
        ) from error
    if any(path.is_symlink() for path in discovered):
        raise StartupContractError("benchmark preflight directory contains a symlink")
    actual_paths = {
        path.relative_to(preflight_root).as_posix()
        for path in discovered
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise StartupContractError(
            "benchmark preflight inventory mismatch: "
            f"missing={sorted(expected_paths - actual_paths)}, "
            f"unexpected={sorted(actual_paths - expected_paths)}"
        )
    output_root = bundle / "preflight-output"
    expected_outputs = {
        f"{case.id}-{arm}.{stream}"
        for case, arm in expected
        for stream in ("stdout", "stderr")
    }
    try:
        output_files = tuple(output_root.rglob("*"))
    except OSError as error:
        raise StartupContractError(
            f"cannot inspect benchmark preflight output: {error}"
        ) from error
    if any(path.is_symlink() for path in output_files):
        raise StartupContractError("benchmark preflight output contains a symlink")
    actual_outputs = {
        path.relative_to(output_root).as_posix()
        for path in output_files
        if path.is_file()
    }
    if actual_outputs != expected_outputs:
        raise StartupContractError(
            "benchmark preflight output inventory mismatch: "
            f"missing={sorted(expected_outputs - actual_outputs)}, "
            f"unexpected={sorted(actual_outputs - expected_outputs)}"
        )
    if len(summary) != len(expected):
        raise StartupContractError(
            f"benchmark preflight summary has {len(summary)} records; expected {len(expected)}"
        )
    observed: set[tuple[str, str]] = set()
    cases = {case.id: case for case in plan.cases}
    for index, raw_record in enumerate(summary):
        record = _exact_keys(
            raw_record,
            (
                "schema_version",
                "case_id",
                "arm",
                "argv",
                "exit_code",
                "stdout_policy",
                "stdout_artifact",
                "stdout_digest",
                "stdout_size_bytes",
                "stderr_artifact",
                "stderr_digest",
                "stderr_size_bytes",
                "passed",
            ),
            f"preflight[{index}]",
        )
        if record["schema_version"] != "fx-startup-preflight/v1":
            raise StartupContractError(
                f"preflight[{index}] has the wrong schema_version"
            )
        case_id = _text(record["case_id"], f"preflight[{index}].case_id")
        arm = _text(record["arm"], f"preflight[{index}].arm")
        key = (case_id, arm)
        if key not in expected_keys or key in observed:
            raise StartupContractError(f"unexpected or duplicate preflight: {key}")
        observed.add(key)
        case = cases[case_id]
        if record["argv"] != [case.executable, *case.argv]:
            raise StartupContractError(f"preflight {key} argv does not match the plan")
        if record["exit_code"] != 0 or record["passed"] is not True:
            raise StartupContractError(f"preflight {key} did not pass")
        if record["stdout_policy"] != case.stdout_policy:
            raise StartupContractError(
                f"preflight {key} stdout policy does not match the plan"
            )
        stdout_size = _integer(
            record["stdout_size_bytes"],
            f"preflight {key} stdout size",
            minimum=0,
        )
        stderr_size = _integer(
            record["stderr_size_bytes"],
            f"preflight {key} stderr size",
            minimum=0,
        )
        if (case.stdout_policy == "empty") != (stdout_size == 0):
            raise StartupContractError(f"preflight {key} violates its stdout policy")
        if stderr_size != 0:
            raise StartupContractError(f"preflight {key} wrote stderr")
        for digest_key in ("stdout_digest", "stderr_digest"):
            digest = record[digest_key]
            if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
                raise StartupContractError(
                    f"preflight {key} has an invalid {digest_key}"
                )
        for stream, size in (("stdout", stdout_size), ("stderr", stderr_size)):
            artifact_key = f"{stream}_artifact"
            expected_artifact = f"preflight-output/{case_id}-{arm}.{stream}"
            if record[artifact_key] != expected_artifact:
                raise StartupContractError(
                    f"preflight {key} has an invalid {artifact_key}"
                )
            artifact = bundle / expected_artifact
            try:
                content = artifact.read_bytes()
            except OSError as error:
                raise StartupContractError(
                    f"cannot read preflight {key} {stream}: {error}"
                ) from error
            if (
                len(content) != size
                or digest_bytes(content) != record[f"{stream}_digest"]
            ):
                raise StartupContractError(
                    f"preflight {key} {stream} does not match its recorded evidence"
                )
            if stream == "stdout" and case.stdout_policy == "json":
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise StartupContractError(
                        f"preflight {key} stdout is not UTF-8 JSON"
                    ) from error
                _strict_json_text(text, f"preflight {key} stdout JSON")
        detail = _load_json(
            preflight_root / f"{case_id}-{arm}.json",
            f"preflight detail {case_id}-{arm}",
        )
        if detail != record:
            raise StartupContractError(
                f"preflight detail {key} differs from its summary"
            )


def _validate_raw_inventory(
    bundle: pathlib.Path,
    *,
    plan: StartupPlan,
    mode: RunMode,
    compared: bool,
) -> None:
    expected: set[str] = set()
    for case in plan.cases:
        if compared and case.role == "gating":
            expected.update(
                f"{case.id}/block-{index + 1:03d}.json"
                for index in range(mode.comparison_blocks)
            )
        else:
            expected.add(f"{case.id}.json")
    raw_root = bundle / "raw"
    try:
        discovered = tuple(raw_root.rglob("*"))
    except OSError as error:
        raise StartupContractError(
            f"cannot inspect raw benchmark evidence: {error}"
        ) from error
    if any(path.is_symlink() for path in discovered):
        raise StartupContractError("raw benchmark evidence contains a symlink")
    actual = {
        path.relative_to(raw_root).as_posix() for path in discovered if path.is_file()
    }
    if actual != expected:
        raise StartupContractError(
            "raw benchmark inventory mismatch: "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
        )


def _validate_artifact_identity(value: object, label: str) -> None:
    identity = _exact_keys(
        value,
        ("source_sha", "source_dirty", "sha256", "size_bytes"),
        label,
    )
    if (
        not isinstance(identity["source_sha"], str)
        or SHA_PATTERN.fullmatch(identity["source_sha"]) is None
    ):
        raise StartupContractError(f"{label}.source_sha is invalid")
    if not isinstance(identity["source_dirty"], bool):
        raise StartupContractError(f"{label}.source_dirty must be boolean")
    if (
        not isinstance(identity["sha256"], str)
        or DIGEST_PATTERN.fullmatch(identity["sha256"]) is None
    ):
        raise StartupContractError(f"{label}.sha256 is invalid")
    _integer(identity["size_bytes"], f"{label}.size_bytes")


def analyze_bundle(
    bundle: pathlib.Path,
    *,
    plan: StartupPlan,
    mode_name: str,
    system_name: str,
    compared: bool,
) -> dict[str, object]:
    if mode_name not in plan.modes:
        raise StartupContractError(f"unknown startup benchmark mode: {mode_name}")
    mode = plan.modes[mode_name]
    context = _load_json(bundle / "context.json", "startup benchmark context")
    if digest_value(context) != plan.digest:
        raise StartupContractError(
            "context.json does not match the registered startup plan"
        )
    _validate_preflights(bundle, plan=plan, compared=compared)
    _validate_raw_inventory(bundle, plan=plan, mode=mode, compared=compared)
    case_reports: list[dict[str, object]] = []
    failures: list[str] = []
    for case in plan.cases:
        if compared and case.role == "gating":
            block_size = mode.runs // mode.comparison_blocks
            control_blocks: list[tuple[float, ...]] = []
            head_blocks: list[tuple[float, ...]] = []
            raw_paths: list[str] = []
            for index in range(mode.comparison_blocks):
                relative = pathlib.Path("raw") / case.id / f"block-{index + 1:03d}.json"
                raw_paths.append(relative.as_posix())
                values = _read_hyperfine(
                    bundle / relative,
                    expected_labels=("control", "head"),
                    expected_samples=block_size,
                )
                control_blocks.append(values["control"])
                head_blocks.append(values["head"])
            control_samples = tuple(
                sample for block in control_blocks for sample in block
            )
            head_samples = tuple(sample for block in head_blocks for sample in block)
            control_stats: dict[str, object] | None = sample_statistics(control_samples)
            relative_stats: dict[str, object] | None = paired_relative_statistics(
                control_blocks,
                head_blocks,
                maximum_regression=plan.relative_policy.maximum_regression,
            )
        else:
            relative = pathlib.Path("raw") / f"{case.id}.json"
            raw_paths = [relative.as_posix()]
            values = _read_hyperfine(
                bundle / relative,
                expected_labels=(case.label,),
                expected_samples=mode.runs,
            )
            head_samples = values[case.label]
            control_stats = None
            relative_stats = None

        head_stats = sample_statistics(head_samples)
        mean = float(head_stats["mean_seconds"])
        if case.role == "diagnostic":
            absolute_status = "diagnostic"
        elif system_name == "Linux":
            ceiling = case.linux_mean_ceiling_seconds
            assert ceiling is not None
            absolute_status = "pass" if mean <= ceiling else "fail"
            if absolute_status == "fail":
                failures.append(case.id)
        else:
            absolute_status = "informational"
        case_reports.append(
            {
                "id": case.id,
                "label": case.label,
                "role": case.role,
                "argv": list(case.argv),
                "raw_artifacts": raw_paths,
                "head": head_stats,
                "control": control_stats,
                "absolute_decision": {
                    "status": absolute_status,
                    "metric": "raw_mean_seconds",
                    "ceiling_seconds": case.linux_mean_ceiling_seconds,
                    "baseline_subtracted": False,
                },
                "relative_comparison": relative_stats,
            }
        )

    if failures:
        status = "fail"
    elif system_name == "Linux":
        status = "pass"
    else:
        status = "informational"
    subject = _exact_keys(
        _load_json(bundle / "subject.json", "benchmark subject"),
        ("schema_version", "head", "control"),
        "subject",
    )
    run = _mapping(_load_json(bundle / "run.json", "benchmark run"), "run")
    if subject.get("schema_version") != SUBJECT_SCHEMA:
        raise StartupContractError("benchmark subject has the wrong schema_version")
    _validate_artifact_identity(subject["head"], "subject.head")
    if compared:
        _validate_artifact_identity(subject["control"], "subject.control")
    elif subject["control"] is not None:
        raise StartupContractError(
            "benchmark subject has an unexpected control artifact"
        )
    if run.get("schema_version") != RUN_SCHEMA:
        raise StartupContractError("benchmark run has the wrong schema_version")
    host = _mapping(run.get("host"), "benchmark run host")
    if host.get("platform") != system_name:
        raise StartupContractError(
            "benchmark run host does not match the report platform"
        )
    tools = _exact_keys(
        run.get("tools"),
        ("hyperfine", "zig_version"),
        "benchmark run tools",
    )
    hyperfine = _exact_keys(
        tools["hyperfine"],
        ("digest", "version"),
        "benchmark run Hyperfine",
    )
    if hyperfine["version"] != f"hyperfine {plan.tool_version}":
        raise StartupContractError(
            "benchmark run Hyperfine version does not match the plan"
        )
    if (
        not isinstance(hyperfine["digest"], str)
        or DIGEST_PATTERN.fullmatch(hyperfine["digest"]) is None
    ):
        raise StartupContractError("benchmark run Hyperfine digest is invalid")
    zig_version = tools["zig_version"]
    if zig_version is not None and (
        not isinstance(zig_version, str) or not zig_version
    ):
        raise StartupContractError("benchmark run Zig version is invalid")
    if run.get("plan_digest") != plan.digest:
        raise StartupContractError(
            "benchmark run plan digest does not match context.json"
        )
    if run.get("mode") != mode_name:
        raise StartupContractError(
            "benchmark run mode does not match the requested mode"
        )
    if run.get("runs") != mode.runs:
        raise StartupContractError("benchmark run sample count does not match the plan")
    if run.get("compared") is not compared:
        raise StartupContractError(
            "benchmark run comparison mode does not match raw evidence"
        )
    return {
        "schema_version": REPORT_SCHEMA,
        "status": status,
        "failed_cases": failures,
        "platform": system_name,
        "plan_digest": plan.digest,
        "subject_digest": digest_value(subject),
        "mode": mode_name,
        "sample_count_per_arm": mode.runs,
        "comparison_blocks": mode.comparison_blocks if compared else None,
        "cases": case_reports,
    }


def benchmark_action_summary(report: Mapping[str, object]) -> list[dict[str, object]]:
    cases = report.get("cases")
    if not isinstance(cases, list):
        raise StartupContractError("report is missing cases")
    entries: list[dict[str, object]] = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("head"), dict):
            raise StartupContractError("report case is malformed")
        head = case["head"]
        assert isinstance(head, dict)
        entries.append(
            {
                "name": case["label"],
                "unit": "s",
                "value": round(float(head["mean_seconds"]), 9),
                "range": f"± {float(head['stddev_seconds']):.9f}",
                "extra": (
                    f"min={float(head['min_seconds']):.9f}s "
                    f"max={float(head['max_seconds']):.9f}s "
                    f"median={float(head['median_seconds']):.9f}s "
                    f"p95={float(head['p95_seconds']):.9f}s "
                    f"runs={int(head['count'])}"
                ),
            }
        )
    return entries


def verify_bundle_report(bundle: pathlib.Path) -> Mapping[str, object]:
    """Recompute an existing report from its sealed-compatible neutral evidence."""
    plan = load_plan(bundle / "context.json")
    run = _mapping(_load_json(bundle / "run.json", "benchmark run"), "benchmark run")
    mode_name = _text(run.get("mode"), "benchmark run mode")
    compared = run.get("compared")
    if not isinstance(compared, bool):
        raise StartupContractError("benchmark run compared must be boolean")
    host = _mapping(run.get("host"), "benchmark run host")
    system_name = _text(host.get("platform"), "benchmark run host platform")
    observed = _mapping(
        _load_json(bundle / "report.json", "startup benchmark report"),
        "startup benchmark report",
    )
    expected = analyze_bundle(
        bundle,
        plan=plan,
        mode_name=mode_name,
        system_name=system_name,
        compared=compared,
    )
    if observed != expected:
        raise StartupContractError(
            "startup report does not match recomputed raw evidence"
        )
    summary = _load_json(bundle / "summary.json", "startup benchmark summary")
    if summary != benchmark_action_summary(expected):
        raise StartupContractError(
            "startup summary does not match recomputed raw evidence"
        )
    try:
        markdown = (bundle / "report.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise StartupContractError(
            f"cannot read startup Markdown report: {error}"
        ) from error
    if markdown != render_report(expected):
        raise StartupContractError(
            "startup Markdown report does not match raw evidence"
        )
    return expected


def render_report(report: Mapping[str, object]) -> str:
    cases = report.get("cases")
    if not isinstance(cases, list):
        raise StartupContractError("report is missing cases")
    lines = [
        "# Startup latency evidence",
        "",
        f"Status: `{report['status']}`",
        "",
        f"Platform policy: `{report['platform']}`",
        "",
        f"Samples per measured arm: `{report['sample_count_per_arm']}`",
        "",
        (
            "The process baseline is diagnostic and is never subtracted. Relative base-to-head "
            "bounds are nominal one-sided 95% intervals that assume independent blocks and are "
            "informational in plan v1; the Linux raw mean ceiling remains the product gate."
        ),
        "",
        "| Command | Head mean | Head p95 | Linux ceiling | Absolute status | Nominal relative upper | Block lag-1 |",
        "| --- | ---: | ---: | ---: | --- | ---: | ---: |",
    ]
    for case in cases:
        if not isinstance(case, dict):
            raise StartupContractError("report case is malformed")
        head = case["head"]
        decision = case["absolute_decision"]
        relative = case["relative_comparison"]
        assert isinstance(head, dict) and isinstance(decision, dict)
        ceiling = decision["ceiling_seconds"]
        ceiling_text = "n/a" if ceiling is None else f"{float(ceiling) * 1000:.3f} ms"
        relative_text = (
            "n/a"
            if relative is None
            else f"{float(relative['upper_confidence_change']) * 100:+.2f}%"
        )
        autocorrelation = (
            None if relative is None else relative["lag_one_autocorrelation"]
        )
        autocorrelation_text = (
            "n/a" if autocorrelation is None else f"{float(autocorrelation):+.3f}"
        )
        lines.append(
            f"| `{case['label']}` | {float(head['mean_seconds']) * 1000:.3f} ms | "
            f"{float(head['p95_seconds']) * 1000:.3f} ms | {ceiling_text} | "
            f"`{decision['status']}` | {relative_text} | {autocorrelation_text} |"
        )
    return "\n".join(lines) + "\n"


def finalize_bundle(
    bundle: pathlib.Path,
    *,
    plan: StartupPlan,
    mode_name: str,
    system_name: str | None = None,
    compared: bool,
) -> dict[str, object]:
    resolved_system = platform.system() if system_name is None else system_name
    report = analyze_bundle(
        bundle,
        plan=plan,
        mode_name=mode_name,
        system_name=resolved_system,
        compared=compared,
    )
    try:
        (bundle / "report.json").write_text(
            json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (bundle / "report.md").write_text(render_report(report), encoding="utf-8")
        (bundle / "summary.json").write_text(
            json.dumps(
                benchmark_action_summary(report),
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise StartupContractError(f"cannot write startup report: {error}") from error
    return report


def print_report(report: Mapping[str, object]) -> None:
    cases = report.get("cases")
    if not isinstance(cases, list):
        raise StartupContractError("report is missing cases")
    print(
        f"{'COMMAND':<25} {'MEAN':>10} {'P95':>10} {'STATUS':>14} {'NOMINAL UPPER':>16}"
    )
    print(
        f"{'-------':<25} {'----':>10} {'---':>10} {'------':>14} {'--------------':>16}"
    )
    for case in cases:
        assert isinstance(case, dict)
        head = case["head"]
        decision = case["absolute_decision"]
        relative = case["relative_comparison"]
        assert isinstance(head, dict) and isinstance(decision, dict)
        relative_text = (
            "n/a"
            if relative is None
            else f"{float(relative['upper_confidence_change']) * 100:+.2f}%"
        )
        print(
            f"{case['label']!s:<25} "
            f"{float(head['mean_seconds']) * 1000:>9.3f}ms "
            f"{float(head['p95_seconds']) * 1000:>9.3f}ms "
            f"{decision['status']!s:>14} "
            f"{relative_text:>16}"
        )
    print()
    print(f"Startup benchmark status: {report['status']}")
