# CV32E40P Platform

This directory will contain the CV32E40P-specific adapter required to
compile and run the shared workloads.

The implementation will be created after examining and reusing the
existing CV32E40P hello_world build and simulation flow.

Expected responsibilities include:

- selecting the existing startup code and linker script;
- defining the compiler ISA and ABI options;
- loading the workload into the existing testbench memory;
- reporting the workload signature;
- reporting pass or fail;
- terminating the simulation.

No CRC32 algorithm source or input dataset should be placed here.
