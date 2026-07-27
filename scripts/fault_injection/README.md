# Fault Injection Scripts

This directory contains scripts for generating faulty gate-level netlists.

## Current script

### `branch_fault.py`

Handles branch faults injected at standard-cell input pins.

The planned operations are:

1. Scan a synthesized gate-level netlist and identify eligible branch sites.
2. Generate a single SA0 or SA1 faulty netlist.
3. Save the complete faulty netlist and its modification metadata.
4. Select a generated faulty netlist as the current simulation input.

The script is intended to be reused across different designs. Design-specific
fault data is stored under:

```text
faults/<design>/<fault_type>/
for the current cv32e40p experiment: faults/cv32e40p/branch_faults/
Stem faults and other fault models will be implemented separately when needed
