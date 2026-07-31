# Stage 5 Phase 2 G1 Checkpoint

Date: 2026-07-31  
Status: PASS  
Smoke fault: `TF000002_SA0`

## Gate objective

Phase2-G1 validates the compile/elaboration infrastructure required for the
three execution modes used in later Stage 5 diagnostic characterization:

- NATIVE
- OBSERVE
- QUARANTINE

This gate does not execute diagnostic runtime behavior and does not assign a
final fault-effect oracle.

## Completed work

All four required compile/elaboration cases passed:

- golden NATIVE: `COMPILE_PASS`
- fault NATIVE: `COMPILE_PASS`
- fault OBSERVE: `COMPILE_PASS`
- fault QUARANTINE: `COMPILE_PASS`

The G1 driver now attempts and records all four cases before producing the
final gate result. A failure in one case no longer prevents collection of the
remaining case results.

## Native execution contract

NATIVE uses the original CV32E40P testbench source:

`verification/shared/tb/mm_ram.sv`

The pre-existing `out_of_bounds_write` assertion and its original `$fatal`
action remain unchanged. No run-local diagnostic overlay is used by NATIVE.

This preserves the Phase 1 natural-execution baseline.

## Diagnostic execution contract

OBSERVE and QUARANTINE use a run-local diagnostic copy of `mm_ram.sv`.

The diagnostic overlay:

1. verifies that the original `out_of_bounds_write` assertion block occurs
   exactly once;
2. removes that original assertion block from the diagnostic-only copy;
3. does not generate a replacement SVA property;
4. evaluates the equivalent out-of-bounds-write predicate procedurally at the
   positive clock edge;
5. suppresses detection during reset;
6. records only the first matching detector event;
7. leaves the original CV32E40P source unmodified.

The persistent ownership audit reports:

- duplicate declarations: 0
- managed declaration initializers: 0
- configuration owners: 1
- sequential-state owners: 1
- predicate owners: 1
- event tasks: 1
- original target assertion blocks in diagnostic overlay: 0
- procedural first-event detectors: 1

The diagnostic detector is classified as:

`PROCEDURAL_FIRST_EVENT`

Its event policy is:

`FIRST_VIOLATION_ONLY`

## Mode behavior prepared for later runtime gates

OBSERVE will record the first equivalent detector event without modifying the
unsafe transaction.

QUARANTINE will record the same first detector event and acknowledge/drop the
unsafe write so diagnostic execution can continue.

Behavior after the first diagnostic detector boundary is counterfactual and
must not replace the NATIVE architectural outcome.

## Storage and provenance

The diagnostic overlay is a run-local generated artifact. Successful runs may
delete the complete `work/` directory according to the Stage 5 retention
policy.

The persistent evidence consists of:

- preparation report;
- ownership report;
- source and policy hashes;
- runner manifest;
- compile result;
- G1 validation report.

The absence of `work/mm_ram.stage5.sv` after a successful cleaned run is
therefore expected and does not invalidate G1.

## Explicitly not completed

Phase2-G1 does not validate:

- NATIVE runtime equivalence with Phase 1;
- OBSERVE runtime continuation;
- QUARANTINE runtime continuation;
- equality of first detector events across modes;
- pre-event trace equivalence;
- final fault-effect classification;
- AI-generated assertions.

## Next steps

1. Phase2-G2: rerun golden and fault NATIVE using the original `mm_ram.sv`.
2. Compare G2 results and compact traces against the frozen Phase 1 evidence.
3. Freeze G2 only after status, completion, workload outcome, architectural
   outcome, detector identity, detector timing, and trace equivalence pass.
4. Phase2-G3: execute OBSERVE and validate first-event equivalence and
   continuation beyond the NATIVE terminal boundary.
5. Phase2-G4: execute QUARANTINE and validate the same first event,
   intervention evidence, and continued execution.
6. Produce a cross-mode report before starting multidimensional oracle
   generation or batch execution.
