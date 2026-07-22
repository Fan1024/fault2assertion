# Platform Adapters

This directory contains design-specific software adapters required to run
the shared workloads on different processor implementations.

A platform adapter may define:

- startup and reset behavior;
- linker and memory layout;
- compiler ISA flags;
- workload output reporting;
- testbench pass/fail signaling;
- simulation termination.

The workload algorithms and input datasets remain under workloads/.
