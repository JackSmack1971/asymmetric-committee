# Experimental Validity Audit Rubric

Use this reference to classify findings, determine readiness, and structure the final audit. Apply only controls relevant to the claim rung under review.

## 1. Claim ladder and default interpretation

| Claim | Question | Minimum comparison |
|---|---|---|
| Predictive | Does the output predict a future target out of sample? | predeclared naive/domain baseline |
| Incremental | Does it add predictive value over comparator C? | strong comparator under equivalent information |
| Mechanism | Is the gain attributable to asymmetry itself? | matched-resource symmetric/ablation controls |
| Economic | Does the edge survive realistic implementation? | preregistered costs/latency/constraints |
| Causal | Does changing asymmetry cause the outcome difference? | intervention/identification supporting causal inference |

A committee-versus-single-model backtest can support at most the claim identified by its actual resource and information controls.

## 2. Severity

- **BLOCKER** — prevents identification, falsification, clean confirmatory evaluation, or prospective integrity.
- **MAJOR** — materially threatens magnitude/uncertainty but has a bounded pre-start correction.
- **MINOR** — clarification/freeze issue unlikely by itself to reverse the central inference.
- **NOTE** — transparency/reporting improvement.

Do not inflate every issue into a blocker. Severity follows the claim and risk mechanism.

## 3. Protocol freeze matrix

Before confirmatory evaluation, establish a disposition for every material item:

| Family | Examples |
|---|---|
| Hypothesis/estimand | null/alternative, claim rung, falsification condition, comparator |
| Unit | prediction/event/asset-time unit; clustering/effective inference unit |
| Universe | eligibility, delistings, survivorship, constituent timing |
| Target | label, horizon, overlap, timestamp availability, revisions |
| Inputs | source, point-in-time cutoff, retrieval corpus/index version |
| LLM state | provider/model identifier, prompts, sampling/request parameters, memory/context |
| Committee | members, roles, topology, deliberation, aggregation, retries |
| Budget | tokens/samples/tool calls/latency/wall-clock for treatment and controls |
| Calibration | sample, objective, tunables, stopping/recalibration rule |
| Baselines | naive/domain, strongest single, matched-resource, symmetric/ablation |
| Metrics | primary endpoint/score, secondary/exploratory endpoints |
| Statistics | dependence treatment, comparison test/CI, selection/multiplicity family |
| Economics | fees, spread, slippage, latency, turnover, capacity/constraints |
| Splits | train/tune/calibrate/confirm/forward boundaries |
| Adaptation | prompt/model/data/policy update governance |
| Stopping | sample/duration, peeking, early-stop/restart rule |
| Reporting | publication/reporting regardless of sign |

Allowed dispositions: `FROZEN`, `RANDOMIZED`, `GOVERNED`, `OPEN`. A confirmatory result cannot be cleanly interpreted while an outcome-reactive load-bearing item remains `OPEN`.

## 4. Information-boundary audit

For each prediction timestamp `t`, verify:

- raw-event time **and** vendor/ingestion availability time;
- publication time versus later revision/edit time;
- constituent membership and delisting status known at `t`;
- retrieval/index contents as of `t`;
- normalization/features fitted only on admissible data;
- labels do not leak across split boundaries;
- committee memory/context excludes later outcomes;
- evaluator/human actions cannot feed test outcomes back into future confirmatory decisions unless governed.

If point-in-time availability cannot be demonstrated, mark the datum unavailable for a clean confirmatory test until evidence closes the gap.

## 5. Mechanism-identification checks

For an asymmetry claim, determine whether treatment and controls differ on any of:

- model capability or provider;
- number of agents/samples;
- tokens/context;
- tools/retrieval/data;
- retry/fallback policy;
- latency/wall-clock budget;
- prompt/tuning effort;
- aggregator/decision layer;
- calibration budget;
- human intervention.

If yes, either match/govern the resource or narrow the estimand to the full-system difference actually tested.

### Minimal ablation logic

Choose only ablations that distinguish plausible explanations:

1. strongest single member;
2. single/ensemble baseline matched on total inference budget where scientifically meaningful;
3. symmetric committee with comparable membership/information/budget;
4. targeted ablation that removes the claimed asymmetry while preserving unrelated resources;
5. randomized/permuted aggregation only when it answers a real mechanism question.

Do not require a long ladder when a smaller controlled contrast identifies the claim.

## 6. LLM-specific reproducibility

Record, when material:

- provider and exact model identifier/snapshot if available;
- date/time of requests;
- system/developer/user prompts or durable hashes plus stored source;
- sampling and generation parameters;
- tools, retrieval sources, corpus/index identity, and tool permissions;
- committee topology, message-passing order, and aggregation;
- retry/fallback/error policy;
- request/response IDs or durable logs where available.

A seed does not guarantee reproducibility for hosted nondeterministic services. If immutable snapshots are unavailable, preserve the strongest available provenance and bound the claim to that operational environment/version period.

## 7. Statistical validity

Use `statistical-decision-guide.md`; do not select methods from names alone.

At minimum determine:

- loss/score and whether it matches the forecast object;
- serial/cross-sectional/cluster/overlap dependence;
- effective inference unit;
- model-selection/search process;
- multiplicity family;
- whether the reported model was selected on the same sample;
- effect size and uncertainty;
- minimum relevant effect/precision target;
- peeking/stopping policy;
- economic significance where claimed.

A fresh untouched confirmation sample is the cleanest remedy when the discovery/search family cannot be reconstructed credibly.

## 8. Calibration

Calibration/tuning must not consume the final confirmatory sample.

Possible calibrated quantities include probability mapping, confidence, committee weights, abstention gates, trade thresholds, and cost-sensitive cutoffs. Recalibration during a forward test is a protocol change unless a predeclared rule governs when/how it occurs.

For probabilistic predictions, prefer a preregistered strictly proper score for primary comparison; report calibration diagnostics separately rather than optimizing directly on the final test.

## 9. Economic claim checks

A predictive edge is not automatically tradable. For an economic claim, predeclare relevant:

- fees and commissions;
- bid/ask spread and slippage;
- execution latency and signal decay;
- turnover;
- position/risk limits;
- capacity/liquidity assumptions;
- financing/borrow constraints where relevant;
- cost sensitivity across a plausible range.

Do not use optimistic treatment-only execution assumptions.

## 10. Prospective forward-test contract

Before the first eligible outcome, freeze/govern:

- protocol version and identity;
- start rule and minimum duration/effective sample;
- prediction schedule and eligible universe;
- point-in-time input rules;
- model/prompt/tool/committee state;
- aggregation and decision rule;
- primary endpoint/score and inference method;
- cost/execution assumptions;
- missing-data/outage policy;
- failed-request handling;
- restart rule;
- update/recalibration rule;
- reporting/publication rule.

Persist predictions before outcome availability and retain failed/abstained observations according to the predeclared policy.

## 11. Falsification standard

At least one reasonable negative result must force rejection or material downgrading. Examples:

- primary effect fails the preregistered comparison;
- sign is opposite the directional claim;
- gain disappears against matched-resource controls;
- result survives only in post-hoc subgroups;
- discovery advantage disappears after valid multiplicity/selection adjustment;
- probabilistic forecasts fail the preregistered proper-score comparison or materially miscalibrate out of sample;
- economic value vanishes under preregistered costs;
- prospective result misses the preregistered threshold/precision criterion.

## 12. Finding evidence contract

For each load-bearing finding, record:

- `ID` — stable identifier, e.g. `ID-01`;
- `Status` — `DOCUMENTED`, `INFERRED`, or `MISSING`;
- `Severity` — `BLOCKER`, `MAJOR`, `MINOR`, or `NOTE`;
- `Evidence` — path plus line/section/record/command-output locator when available;
- `Observed fact` — what the artifact actually establishes;
- `Risk mechanism` — how this can bias or invalidate the claim;
- `Required action` — bounded pre-start correction;
- `Closure evidence` — what would prove the issue closed.

Do not cite generic best practice as if it were project evidence.

## 13. Verdict precedence

1. `NOT IDENTIFIABLE` if the asserted incremental/mechanism/causal estimand cannot be isolated even with the documented protocol.
2. `NOT FALSIFIABLE` if no credible negative result can defeat the central claim.
3. `BLOCKED BY MISSING EVIDENCE` if a load-bearing fact cannot be determined from available artifacts.
4. `CONDITIONALLY READY` if only bounded pre-start corrections remain.
5. `READY TO PREREGISTER` only when blocker-level fields are complete enough to freeze.

Never call the experiment “validated” before prospective evidence exists.

## 14. Report contract

Return these headings in this order:

1. `Claim Under Review`
2. `Protocol Reconstruction`
3. `Claim-Ladder Verdict`
4. `Information Boundary`
5. `Mechanism Identification and Controls`
6. `Statistical Validity`
7. `Benchmark and Economic Integrity`
8. `Contamination and Provenance`
9. `Calibration and Adaptation`
10. `Forward-Test Readiness`
11. `Degrees-of-Freedom Freeze Matrix`
12. `Findings Register`
13. `Required Changes Before Start`
14. `Falsification Conditions`
15. `Final Verdict`

`Final Verdict` must contain exactly one allowed verdict token and a one-sentence reason.
