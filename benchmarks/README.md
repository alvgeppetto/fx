# Benchmark evidence

fx has three measurement lanes with different decision owners:

| Lane | Product decision | Evidence owner |
| --- | --- | --- |
| Linux startup | Raw command mean at or below 2 ms | `startup_plan.json` and `startup_contract.py` |
| Pull request binary size | Informational warning at 52,429 added bytes | `scripts/binary_size.py` |
| macOS arm64 PGSO | Release eligibility, size, behavior, startup, and heavy workload gates | `scripts/pgso/` |

The standalone UI activity run in the startup workflow is a smoke gate. The PGSO UI activity
comparison is the authoritative retained performance measurement for that workload.

## Measurement contract

`startup_plan.json` is the registered plan. It has no policy defaults: every case declares its
command, fixture, environment additions, output expectation, role, and Linux ceiling. Each mode
declares its sample count and warmups before capture begins.

The runner:

- builds or receives an exact ReleaseSafe subject binary;
- admits only a version-pinned Hyperfine executable;
- constructs private deterministic homes and session fixtures;
- removes ambient `FX_*` variables unless the plan explicitly sets them;
- preflights every measured arm for exit status, empty stderr, and valid JSON where promised;
- uses Hyperfine without an intermediate shell;
- recomputes every statistic from raw `times` samples instead of trusting Hyperfine aggregates;
- rejects missing, duplicate, stale, or unknown raw and preflight artifacts;
- keeps the process launch baseline diagnostic and never subtracts it.

On pull requests, control and head run in ten same-runner blocks for the default and CI modes. The
order alternates between blocks. The report includes a nominal one-sided 95% upper confidence bound
on the paired block log ratios and a lag-one autocorrelation diagnostic. The bound assumes the block
ratios are independent; the diagnostic does not prove that assumption. This comparison is
informational in plan v1, while the registered Linux raw mean ceiling remains the product gate.
This avoids silently changing the established 2 ms contract while adding a noise-aware diagnostic.

These choices apply Assay's useful measurement discipline: freeze the plan first, distinguish
evidence from decisions, use the independent block as the comparison unit, require complete rows,
and retain failed decisions. Assay's current experiment certificate is designed for binary,
clustered outcomes, so fx does not misrepresent continuous latency samples as that schema.

## ProofPack boundary

fx emits ordinary JSON and raw artifacts. It does not implement ProofPack locks.

The optional package under `integrations/proofpack-fx/` registers the `proofpack.cli` entry point.
Its `proofpack fx seal` command derives semantic roots from `subject.json` and `context.json`, then
delegates the artifact inventory, evidence chain, lock, and verification to ProofPack Core. The
workflows install ProofPack from an exact external source commit and exercise the plugin boundary.

To test the sibling checkout locally:

```bash
python3.12 -m pip install \
  --constraint integrations/proofpack-fx/constraints.txt \
  ../proofpack/packages/core ./integrations/proofpack-fx
proofpack fx --help
proofpack fx seal benchmarks/results/startup-evidence
proofpack fx verify benchmarks/results/startup-evidence
python3 -m benchmarks.startup_verify benchmarks/results/startup-evidence
```

Sealing is intentionally separate from capture. A neutral bundle can be inspected without
ProofPack, while a sealed CI artifact can be verified without trusting the workflow log.

## Running startup measurements

Hyperfine 1.20.0 is required.

```bash
./benchmarks/startup.sh
./benchmarks/startup.sh --quick
```

The full and CI modes collect 100 samples per measured arm; quick mode collects 20. A pull request
comparison can also be reproduced with an externally built control:

```bash
./benchmarks/startup.sh --quick \
  --control-binary /absolute/path/to/base/fx \
  --control-sha 0123456789abcdef0123456789abcdef01234567
```

The fresh head binary is always `./zig-out/bin/fx`. Output is published after capture completes at
`benchmarks/results/startup-evidence/` and includes the registered context, subject identities,
fixture recipe, host and tool metadata, preflight records, raw exports, report, and benchmark-action
summary. Preflight stdout and stderr bytes are retained so their hashes and output-policy decisions
can be independently checked. CI adds `proofpack-producer.json`, `proof.lock`, and `evidence.jsonl`
through the plugin, recomputes the report once more from the sealed-compatible evidence, and then
uploads the bundle.

## Review findings and remaining work

The PGSO lane already has strong identity, completeness, same-runner, artifact-linkage, and failure
retention checks. Its aggregate is now ProofPack-sealed. Its p50 and p95 10% decisions remain point
estimate gates; changing a production release gate to a confidence-bound rule needs a separately
registered migration and historical calibration.

Binary-size reports already compare exact same-runner base and head artifacts on four native
targets. They now emit a portable neutral evidence directory and seal the retained report, section
tables, and architecture output. The binaries themselves remain separately retained only when the
warning fires, while their exact hashes and sizes are bound into the evidence subject and context.

The next useful extensions are structured output for the standalone UI smoke run and a historical
trend consumer. Neither should become a new authority until its plan, retention, and failure rules
are registered explicitly.
