# Stage 6 Experiment Log

## Current Goal

Stage 6 evaluates whether an LLM can generate a workload-aware diagnostic assertion for a known structural fault using:

* static fault information;
* the fault site and its local receiver signals;
* observed fault-free Golden behavior.

The generated assertion must satisfy two conditions:

1. remain silent during the complete Golden CRC32 execution;
2. trigger during the target faulty execution.

The current pilot fault is:

```text
TF000516_SA0
```

The fault is located in:

```text
cv32e40p_load_store_unit_PULP_OBI0
```

and Stage 5 previously classified its natural faulty execution as:

```text
OUTPUT_MISMATCH
```

Therefore, this is not an inactive or architecturally irrelevant fault. The injected fault reproducibly changes the final workload result.

---

## Round 0: Golden-Behavior Baseline

The first Stage-6 prototype used only limited Golden activity information. This made it easy for the model to invent local relationships that were not actually valid during the Golden workload.

Stage 6 was therefore restarted with a stronger baseline.

Before assertion generation, a Golden behavioral profiler now observes exactly the local signals exposed to the LLM:

```text
site_i
recv_0_i
recv_1_i
```

The profiler records:

* observed same-cycle joint states;
* observed one-cycle transitions between `site_i` and each receiver.

The complete profiling result is retained in:

```text
golden_behavior.json
```

while the LLM receives a compact representation without occurrence counts or simulation metadata.

For `TF000516_SA0`, the observed Golden joint states were:

```text
000
001
010
101
110
111
```

and all 16 possible one-cycle pair transitions were observed for both:

```text
(site_i, recv_0_i)
(site_i, recv_1_i)
```

This indicates that the immediate local behavior around the fault site is relatively rich and does not provide many simple pairwise invariants.

---

## Round-0 Simulation Result

The Round-0 assertion successfully compiled and passed the complete Golden execution.

The final result was:

```text
Stage-6 verdict: TARGET_NOT_DETECTED

Fault ID        : TF000516_SA0
Stage-5 baseline: OUTPUT_MISMATCH
Compile         : COMPILE_PASS
Golden          : PASS
Faulty runner   : OUTPUT_MISMATCH
```

This result is important.

The assertion is:

* syntactically valid;
* successfully integrated into Xcelium;
* Golden-safe.

However, it does not detect the target fault.

At the same time, the faulty workload still reproduces the Stage-5 `OUTPUT_MISMATCH` result.

Therefore, the failure is not because the target fault disappeared or became inactive.

---

## Current Hypothesis

The current hypothesis is that the original observation window may be too close to the physical fault site.

A stuck-at fault forces the injected node to remain permanently at either `0` or `1`. However, the stuck value itself is not necessarily illegal during normal execution.

For example, for a stuck-at-0 fault:

```text
site_i = 0
```

can occur frequently during the Golden execution.

The diagnostic problem is therefore not simply:

```text
"Is the site equal to 0?"
```

but rather:

```text
"Is the site equal to 0 under a context in which this behavior becomes inconsistent?"
```

The immediate downstream receiver signals may not provide enough context to answer this question.

In particular, a fault can propagate through several levels of combinational logic before producing a behavior that is clearly distinguishable from the Golden workload.

Therefore, forcing the LLM to construct the assertion only from:

```text
fault site + direct receivers
```

may be unnecessarily restrictive.

This does **not** yet prove that no useful assertion exists at the original site. A more complex temporal property may still distinguish the fault.

The current experiment only suggests that the shallow local observation window does not expose an obvious discriminative behavior.

---

## Bounded Downstream Expansion

To test this hypothesis, a bounded downstream search was added to Stage 6.

The search starts from the injected fault site and follows the outputs of downstream standard cells.

The search is intentionally limited to three levels:

```text
Depth 0: injected fault site

Depth 1: direct receiver outputs

Depth 2: outputs downstream of Depth 1

Depth 3: outputs downstream of Depth 2
```

The purpose is not to search the entire processor for an arbitrary signal that happens to correlate with failure.

Instead, the goal is to determine whether a slightly deeper point in the fault-propagation cone provides a clearer diagnostic observation.

The current pilot produced:

```text
Depth 1           : 2 signals
Depth 2           : 9 signals
Depth 3           : 100 signals

Profiled new      : 109 signals
```

Golden and faulty simulations were executed for these bounded candidates.

No VCD or permanent faulty netlist was retained.

---

## Important Observation

For the original Stage-6 observation set:

```text
site_i
recv_0_i
recv_1_i
```

the downstream analysis found:

```text
Base fault-only states: NONE
```

This means that, under the current same-cycle representation, every local state observed during the faulty execution was also observed somewhere during the Golden execution.

This provides a plausible explanation for the Round-0 result.

Although Golden and faulty executions may diverge at the same simulation cycle, the faulty local pattern itself can still look locally legal because the same pattern occurs at another point in the Golden workload.

Therefore:

```text
time-aligned divergence
```

does not automatically imply:

```text
a locally discriminative assertion condition
```

---

## Selected Downstream Observation Point

The bounded search found a candidate at Depth 3:

```text
Selected alias  : down_0_i
Selected signal : n_192
Selected depth  : 3
```

The first aligned divergence involving this candidate occurs at:

```text
cycle 935243
```

More importantly, when this signal is added to the original local observation window, the faulty execution produces the joint state:

```text
0100
```

which was not observed in the corresponding Golden behavior.

The search therefore reported:

```text
Added fault-only state:
0100
```

No additional fault-only pairwise transition was found:

```text
Fault-only transition:
NONE
```

This makes `n_192` interesting primarily because it adds a discriminative **joint state**, rather than because it introduces a simple new one-cycle relationship with the original fault site.

---

## Interpretation

The current evidence supports the following working hypothesis:

```text
physical fault site
        ?
immediate local behavior is not sufficiently discriminative
        ?
fault propagates through downstream LSU logic
        ?
a deeper observation point exposes a Golden-unseen local state
```

If the next assertion generated using `down_0_i` becomes both:

```text
Golden-safe
```

and:

```text
target-detecting
```

then this would provide evidence that the Round-0 failure was at least partly caused by an insufficient observation window rather than only by LLM reasoning failure.

This distinction is important.

A failed assertion-generation attempt may result from either:

```text
1. insufficient or poorly selected observation signals;
```

or:

```text
2. failure of the model to synthesize a useful property from adequate signals.
```

The downstream experiment is intended to separate these two cases.

---

## Methodological Constraint

The downstream search uses both Golden and target faulty executions to identify a discriminative observation point.

Therefore, this information is considered:

```text
privileged Train-only feedback
```

It cannot be used directly during a final Test experiment in which per-target faulty-run feedback is prohibited.

The intended future methodology is:

```text
Round 0:
static fault context
+ Golden observed behavior
        ?
assertion generation

if TARGET_NOT_DETECTED during Train:
        ?
bounded downstream fault-propagation feedback
        ?
Round 1 assertion generation
```

For held-out Test faults, the target faulty execution must not be used to select the observation point.

A later research question is whether experience collected from Train faults can help predict useful downstream observation points for unseen Test faults without executing the Test fault first.

---

## Current Status

The current Stage-6 pilot has reached:

```text
Round 0:
    COMPILE_PASS
    GOLDEN_PASS
    TARGET_NOT_DETECTED

Downstream expansion:
    Depth 1 = 2
    Depth 2 = 9
    Depth 3 = 100

Selected feedback signal:
    down_0_i = n_192
    depth = 3

Discriminative faulty behavior:
    fault-only joint state = 0100
```

No Round-1 assertion has been evaluated yet.

---

## Next Step

The next experiment will expose the selected downstream observation point to the LLM as:

```text
down_0_i
```

The Round-1 prompt will contain:

* the original fault context;
* the original Round-0 result;
* the expanded Golden behavior including `down_0_i`;
* the fact that Round 0 was Golden-safe but did not detect the target;
* the Train-only downstream diagnostic feedback.

A new assertion will then be generated and evaluated using the same validation sequence:

```text
generate Round-1 property
        ?
compile/elaborate
        ?
Golden CRC32 execution
        ?
if Golden-safe
        ?
TF000516_SA0 faulty execution
        ?
TARGET_DETECTED
or
TARGET_NOT_DETECTED
```

No conclusion about the effectiveness of downstream expansion will be made until this Round-1 simulation is completed.

