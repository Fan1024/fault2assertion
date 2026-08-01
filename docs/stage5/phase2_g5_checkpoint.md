# Stage5 Phase2 G5 Single-Fault Checkpoint

This checkpoint freezes the completed `TF000002_SA0` Stage5 smoke flow.
Runtime evidence remains under `runs/stage5_dev/phase2_v1` and is not
intended for Git tracking. This document and its JSON manifest preserve
the validated conclusions and SHA-256 references to the evidence.

## Gate status

| Gate | Status | Meaning |
|---|---:|---|
| G1 | PASS | Native/OBSERVE/DIAGNOSTIC_QUARANTINE infrastructure compiles. |
| G2 | PASS | Minimal Native sanity check. |
| G3 | PASS | OBSERVE executed; result was DIAGNOSTIC_TIMEOUT. |
| G4 | PASS | DIAGNOSTIC_QUARANTINE executed; result was DIAGNOSTIC_TIMEOUT. |
| G5 | PASS | Multidimensional oracle built and independently validated. |

## Frozen scientific result

- Fault: `TF000002_SA0`
- Injection module: `cv32e40p_core_COREV_PULP0_COREV_CLUSTER0_FPU0_FPU_ADDMUL_LAT0_FPU_OTHERS_LAT0_ZFINX0_NUM_MHPMCOUNTERS1`
- Injection signal: `branch_decision`
- Natural architectural outcome: `CENSORED`
- OBSERVE result: `DIAGNOSTIC_TIMEOUT`
- DIAGNOSTIC_QUARANTINE result: `DIAGNOSTIC_TIMEOUT`
- Validated capability: `NON_CONTINUABLE`
- Capability scope: `CURRENT_REGISTERED_QUARANTINE_POLICY`
- First observable detector boundary: cycle `58`, time `780ns`
- Earliest local divergence: `NOT_COMPUTED_IN_MINIMAL_G5`
- SVA generated: `false`

## Interpretation

- Native execution defines the natural outcome.
- OBSERVE and DIAGNOSTIC_QUARANTINE are counterfactual after the first
  detector event and cannot replace the Native architectural outcome.
- `NON_CONTINUABLE` is scoped to the current registered quarantine policy.
- The detector boundary is not claimed to be the earliest local divergence.

## Reproducibility

- Source/evidence hashes: `docs/stage5/phase2_g5_checkpoint_manifest.json`
- Repository HEAD before this checkpoint commit: `a4b4d2b22a31763784e0d853b6cd833892947479`

