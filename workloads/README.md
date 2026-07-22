# Workloads

This directory contains the working copies of the C workloads used by
fault2assertion.

The original upstream sources remain available in the separate local
MiBench repository. Files under this directory may later receive the
minimum modifications required for bare-metal RISC-V execution.

Current workloads:

- crc32
- bitcount
- aes
- qsort
- dijkstra

Design-specific startup code, linker scripts, memory maps, and simulation
interfaces do not belong in this directory. They belong under platform/.
