# ProofPack fx plugin

This optional package attaches `proofpack fx` to an installed ProofPack CLI. fx writes neutral,
typed measurement documents; the plugin asks ProofPack Core to seal and verify their exact artifact
inventory. ProofPack remains the only implementation of the lock and evidence-chain format.

CI installs the plugin with `constraints.txt`, which freezes the currently reviewed transitive
dependency versions. Before sealing, the plugin records its Python, ProofPack source, and installed
distribution inventory in `proofpack-producer.json`; ProofPack then includes that document in the
exact artifact lock.

The bundle must contain `subject.json` and `context.json`. Their canonical semantic digests become
the ProofPack subject and context roots. Both documents are also part of the exact locked inventory.

```bash
proofpack fx seal benchmarks/results/startup-evidence
proofpack fx verify benchmarks/results/startup-evidence
```
