# Fault Injection Scripts

This directory contains the fault-population, selection, metadata, and run-local
netlist tools used by `fault2assertion`.

## Current fault model

The current campaign implements **branch stuck-at faults** for the CV32E40P
post-synthesis gate-level netlist.

A branch site is one specific connection:

```text
source net -> sink standard-cell instance.pin
```

A source net must have local fanout of at least two. Each selected branch
location is paired into SA0 and SA1 faults.

Clock, reset, and scan pins are excluded:

```text
CK RN SN SE SI
```

Constants, open connections, complex expressions, and connections with multiple
local drivers are also excluded.

## Functional regions

Eligible sites are classified with signal-name rules first and module-name
rules second:

```text
csr_debug
irq_control
lsu_mem
if_prefetch
id_control_regfile
execute_wb
core_glue_sleep
```

A site that remains unclassified stops campaign generation. It is not silently
placed into a generic region.

## Sampling policy

The current selection uses stratified random sampling without replacement.
The stratum is:

```text
functional_region | source_class | fanout_bucket
```

Source classes:

```text
sequential_output
combinational_output
hierarchy_boundary
```

Fanout buckets:

```text
2
3_4
5_8
gt_8
```

The sample size uses 95% confidence, a +/-5% margin of error, conservative
`p = 0.5`, and finite-population correction. Every non-empty stratum receives a
minimum quota before proportional allocation. The current reproducible random
seed is `20260724`.

For the current CV32E40P netlist, campaign generation produced:

```text
eligible unique branch locations : 30910
selected unique locations        : 380
paired SA0+SA1 faults             : 760
```

## Persistent campaign layout

Campaign preparation intentionally creates only:

```text
faults/cv32e40p/branchfault/
+-- population.json
+-- selection.json
```

A selected fault is materialized only when requested:

```text
faults/cv32e40p/branchfault/BF0001_SA0/
+-- fault.json
+-- fault.patch
```

A complete faulty netlist is **not** stored in the fault directory.

## `branch_fault.py` commands

### Prepare population and selection

`all` means `scan + select` only. It does not materialize all selected faults.

```bash
FAULT_ROOT="$PWD/faults/cv32e40p/branchfault"
NETLIST="/raid/spring2026/fwu44/research/cv32e40p/syn/runs/run_003/results/cv32e40p.SYN/cv32e40p.mapped.v"

python3 scripts/fault_injection/branch_fault.py all \
  --netlist "$NETLIST" \
  --design cv32e40p \
  --output-root "$FAULT_ROOT" \
  --seed 20260724 \
  --force
```

Equivalent separate commands:

```bash
python3 scripts/fault_injection/branch_fault.py scan \
  --netlist "$NETLIST" \
  --design cv32e40p \
  --output-root "$FAULT_ROOT" \
  --force

python3 scripts/fault_injection/branch_fault.py select \
  --output-root "$FAULT_ROOT" \
  --seed 20260724 \
  --force
```

### Materialize one selected fault

```bash
python3 scripts/fault_injection/branch_fault.py materialize \
  --output-root faults/cv32e40p/branchfault \
  --fault-id BF0001_SA0
```

This creates only `fault.json` and `fault.patch`.

### Generate a run-local fault netlist manually

Normally `scripts/run_xrun_fault.sh` performs this step automatically.

```bash
python3 scripts/fault_injection/branch_fault.py apply \
  --fault-json faults/cv32e40p/branchfault/BF0001_SA0/fault.json \
  --output-netlist /tmp/BF0001_SA0/fault_netlist.v
```

The command checks the immutable golden-netlist SHA-256 before applying the
fault and refuses to overwrite the golden netlist.

## Run one fault simulation

The fault runner materializes the requested fault on demand, generates the
faulty netlist under the run-local `work/` directory, and invokes the shared
Xcelium flow.

```bash
VCD=1 KEEP_NETLIST=0 KEEP_WORK=1 MAXCYCLES=5000000 \
  ./scripts/run_xrun_fault.sh \
  cv32e40p crc32 branchfault BF0001_SA0 run_bf0001_sa0_vcd
```

Run output:

```text
faults/cv32e40p/branchfault/BF0001_SA0/results/crc32/<run_name>/
+-- xrun.log
+-- result.txt
+-- result.env
+-- fault.json
+-- fault.patch
+-- manifest.txt
+-- command.txt
+-- work/
    +-- riscy_tb.vcd          # when VCD=1
```

The runner removes these complete run-local netlists when it exits, including
when Xcelium fails:

```text
work/fault_netlist.v
work/cv32e40p.mapped.sim.v
```

Use `KEEP_NETLIST=1` only for debugging netlist generation.

## VCD feature extraction

The repository keeps the original filename:

```text
scripts/analyze_vcd.py
```

Generate both JSON and text features:

```bash
RUN_DIR="$PWD/faults/cv32e40p/branchfault/BF0001_SA0/results/crc32/run_bf0001_sa0_vcd"

python3 scripts/analyze_vcd.py \
  "$RUN_DIR/work/riscy_tb.vcd" \
  --json-output "$RUN_DIR/vcd_features.json" \
  --text-output "$RUN_DIR/vcd_features.txt"
```

The JSON contains source SHA-256, VCD metadata, activity totals, unknown-value
statistics, and per-signal features. The default signal set includes clocks,
reset, fetch/instruction/data interfaces, and IF/ID/EX/WB program counters.

After checking the first experiment, the VCD can be deleted safely during
feature extraction:

```bash
python3 scripts/analyze_vcd.py \
  "$RUN_DIR/work/riscy_tb.vcd" \
  --json-output "$RUN_DIR/vcd_features.json" \
  --text-output "$RUN_DIR/vcd_features.txt" \
  --delete-vcd
```

`--delete-vcd` deletes the VCD only after parsing and all requested output writes
succeed. The current analyzer extracts one VCD independently; golden-versus-
fault divergence comparison will be added as a separate later step.

## Current scope

Only branch stuck-at faults are implemented in this script. Stem faults,
register-output faults, delay faults, and other models should use separate fault
model implementations and separate campaign metadata.
