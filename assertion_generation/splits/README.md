# Train/Test Split Definitions

This directory will contain immutable train/test split definitions for
Stage 6.

Do not create a formal split until:

- the source Stage-5 campaign has been frozen;
- the usable Stage-5 fault population has been frozen;
- all non-scientific blocked or invalid faults have been identified;
- the Stage-6 single-fault engineering closure has succeeded.

## Planned split layout

Each split will use a flat definition directory:

    assertion_generation/splits/
    +-- split_001/
        +-- split_manifest.json

The train and test runtime results do not belong in this tracked directory.
They will be written under:

    runs/stage6/split_001/train/
    runs/stage6/split_001/test/

## Required split-manifest content

A split manifest must record at least:

- schema version;
- split identifier;
- creation timestamp;
- source Stage-5 campaign identifier;
- source Stage-5 campaign path;
- source campaign digest;
- split seed;
- split algorithm;
- train/test ratio;
- site-grouping policy;
- optional stratification policy;
- total eligible site count;
- total eligible fault count;
- train site count;
- train fault count;
- complete train site identifiers;
- complete train fault identifiers;
- test site count;
- test fault count;
- complete test site identifiers;
- complete test fault identifiers.

## Site-level grouping rule

Partitioning must be performed by physical fault site, not by individual
fault instance.

For example, the following is forbidden:

    TF000010_SA0 -> train
    TF000010_SA1 -> test

The following is required:

    TS000010
    +-- TF000010_SA0
    +-- TF000010_SA1

The complete site and all of its fault instances must be assigned to one
partition.

## Split immutability

After split_001 has been used for an experiment, its manifest must not be
edited in place.

If the source campaign, eligible-fault population, seed, ratio, or
partition algorithm changes, create a new split identifier such as:

    split_002

## Smoke-test exclusion

The following artifacts never belong to a train/test split:

- API Hello smoke tests;
- response-format extraction tests;
- synthetic assertion tests;
- toy-design Xcelium tests;
- manually written assertion-integration tests;
- infrastructure-debugging runs.

These artifacts must remain under:

    runs/stage6/smoke/
