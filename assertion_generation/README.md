# Stage 6: AI-Assisted Assertion Generation

This directory stores tracked experiment definitions for the
Fault2Assertion assertion-generation stage.

It does not store OpenAI API credentials, Xcelium work directories,
temporary fault netlists, raw waveforms, or other large runtime artifacts.

## Directory ownership

Tracked experiment definitions are stored under:

    assertion_generation/

Stage-6 implementation scripts are stored under:

    scripts/assertion_generation/

Runtime artifacts are stored under:

    runs/stage6/

The complete runs directory is ignored by Git.

## Planned runtime layout

The initial runtime layout is:

    runs/stage6/
    +-- smoke/
    ¦   +-- api/
    ¦   +-- extraction/
    ¦   +-- xcelium/
    +-- split_001/
        +-- train/
        ¦   +-- <site_id>/
        ¦       +-- <fault_id>/
        ¦           +-- attempt_001/
        +-- test/
            +-- <site_id>/
                +-- <fault_id>/
                    +-- attempt_001/

Smoke-test artifacts are infrastructure-validation artifacts. They are not
members of the train set, test set, scientific dataset, or future RAG corpus.

## Identifier semantics

The Stage-6 identifiers have the following meanings.

- split:
  One complete train/test partition of a frozen Stage-5 fault campaign.

- partition:
  Either the train partition or test partition inside one split.

- site:
  One physical fault site.

- fault:
  One specific fault instance belonging to a site, such as SA0 or SA1.

- attempt:
  One independent assertion-generation attempt for one fault.

- round:
  One feedback or repair iteration inside one attempt.

- execution:
  One deterministic tool run, such as compile, golden simulation, or
  faulty simulation.

A split identifier must not be used as a generation-round identifier.
A round identifier must not be used as a train/test split identifier.

## Train/test partition rule

Train/test partitioning must be performed at the physical-site level.

All fault instances associated with the same physical site must remain in
the same partition. Paired SA0 and SA1 faults from one site must never be
split between train and test.

A formal split must not be created until the source Stage-5 campaign and
the usable fault population have been frozen.

## Current implementation scope

The current implementation contains only the API connectivity smoke test.

Its data flow is:

    fixed message
        -> OpenAI Responses API
        -> complete SDK response
        -> plain-text response
        -> structured PASS or FAIL result

The API smoke test validates only:

- Python environment availability;
- OpenAI Python SDK availability;
- API credential availability;
- network and API connectivity;
- model access;
- non-empty text extraction;
- local artifact creation.

It does not yet perform:

- SVA generation;
- assertion extraction;
- SystemVerilog wrapping;
- Xcelium compilation;
- golden simulation;
- faulty simulation;
- train/test splitting;
- feedback repair;
- RAG retrieval;
- tree search.

## API credential policy

A real OpenAI API key must not be stored inside this repository.

The default local credential file is:

    ~/.config/fault2assertion/openai.env

The tracked file:

    .env.stage6.example

is only a template.

The API runner loads the local credential file in a child process. Users
should not source the credential file in a shell that will later execute
Xcelium, because simulator environment-history files may record inherited
environment variables.

## Result retention policy

For each API smoke run, retain:

- request.json;
- runtime.json;
- response.json;
- response.txt;
- result.json.

No API key may appear in any retained artifact.

Later Xcelium stages will retain compact verdicts and diagnostic evidence
while allowing large work directories, temporary netlists, and waveforms
to be removed after a strict valid result has been produced.
