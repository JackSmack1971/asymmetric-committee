# Asymmetric Predictive Validity Audit

Audit date: 2026-09-25. Scope: experimental validity of the protocol described in `docs/asymmetric-committee-blueprint.md`; not a code-quality review. Evidence labels apply to current repository artifacts, not intentions.

## Claim Under Review

The blueprint asserts a predictive and economic system claim, and a mechanism thesis that asymmetric partitioning/roles add value. It states that primary evidence will come from forward paper trading (§12.1), with backtests used for debugging and ablations. A causal claim is not documented. The intended incremental comparator is not singular: §12.3 lists several benchmarks, while §12.5 proposes ablations.

## Protocol Reconstruction

**DOCUMENTED design:** weekly decisions use Friday-close `as_of`; the gate passes K=15; five fast-tier agents, a strong-tier red team and CIO are described; the aggregator pools agent probabilities, then a deterministic risk engine sizes the book. The stated target choices are 21 and 63 trading days, with the primary horizon undecided. Proposed metrics include portfolio returns/risk, signal IC, agent Brier/calibration, block-bootstrap CIs and Deflated Sharpe Ratio (blueprint §§6–12).

**Current state:** `docs/PROGRESS.md` marks P0–P2 done and P3–P8 not started. The P2 plan explicitly excludes LLMs, execution, costs and outcome scoring beyond a gate fixture diagnostic. `config/models.yaml` still has TODO model slugs and metadata. No preregistration, confirmatory results, forward log, statistical analysis plan, or implemented evaluation/committee exists in the listed project artifacts.

## Claim-Ladder Verdict

- **Predictive:** planned, not evidenced; primary target/score and confirmatory procedure are not fully frozen.
- **Incremental:** planned versus benchmarks, but no single primary comparator/estimand is frozen.
- **Mechanism:** not identified by the documented controls; see ID-01.
- **Economic:** intended paper-trading claim, but costs and paper execution are only partly specified and no prospective evidence exists.
- **Causal:** not asserted and unsupported.

## Information Boundary

**DOCUMENTED:** Friday-close `as_of`, point-in-time storage reads, label-end-before-as-of gate training, as-filed fundamentals, decision commitments before scoring, and anonymization/memorization-probe design (blueprint §§4, 12; progress P1/P2 notes). These are design statements; the main LLM data path is not implemented yet.

**MISSING:** validated real-source timestamps and revisions, immutable retrieval/index identity, exact served model snapshots/request records, prompt/tool state, complete point-in-time corporate actions, and proof that forward predictions are persisted before labels become available. Progress explicitly reports unverified news timestamps, synthetic-only fixtures, no point-in-time corporate-action feed, and survivorship in initial security seeding. These prevent a clean confirmatory information boundary until resolved.

## Mechanism Identification and Controls

The treatment mixes information partitioning, distinct roles, model tiers, and a special red-team/CIO path. The “symmetric info” ablation gives every agent all partitions, but the blueprint does not specify matched tokens, calls, latency, retries, model capability, tuning/calibration effort, or a control that removes only the targeted asymmetry while holding the rest fixed. “Strong model for all” changes capability and budget together. Shuffling verdicts tests name assignment/architecture, not asymmetry itself. Thus a favorable result could be caused by resources, model strength, role/prompt quality, or aggregation. A broad system-versus-baseline result could support system value at that operating budget, but not the stated asymmetry mechanism.

## Statistical Validity

Block-bootstrap CIs and a Deflated Sharpe Ratio are named, but the blueprint does not define the bootstrap sampling unit/block length, treatment of serial and cross-sectional dependence, primary forecast loss, primary comparison, inferential unit, confirmatory multiplicity family, minimum relevant effect/power or precision target, or fixed stopping/peeking rule. Multiple horizons, benchmarks, metrics and ablations are planned; “log every config” does not itself specify how the actual search family enters inference. Brier is named for agent diagnostics but no primary proper-score test or separate calibration/confirmation samples are specified. Overlapping 21/63-day labels and weekly portfolio returns require explicit dependence handling. No method is yet implemented (P6 not started).

## Benchmark and Economic Integrity

Exposure-matched SPY, equal-weight universe, sector ETF, quant baseline and random committee are documented as planned per-run benchmarks. The quant baseline shares the risk path per P2 plan, which is useful, but information and compute matching to the LLM treatment are not established. Paper orders use modeled spread/impact terms and log slippage in the blueprint; capacity, realistic latency/signal decay, sensitivity ranges, treatment/control parity, and final cost endpoint are not fully specified. Paper trading can establish performance only under its documented execution assumptions, not live tradability.

## Contamination and Provenance

The design addresses entity masking, model training cutoffs plus buffer, memorization probes and committed decisions. It acknowledges probe-based exclusions and hosted-model routing/fallbacks. Yet TODO model identities/cutoffs, no immutable hosted snapshot, current-name limitations, uncertain vendor revisions, no real ingestion recordings, and no durable confirmatory protocol identity prevent verification. The 6-month forward clock is described as starting at P8, but restart, failed-run inclusion, missingness, outage, reporting and protocol-version rules are absent.

## Calibration and Adaptation

**DOCUMENTED:** gate and committee skill weights update walk-forward using prior data; prompts are versioned and prompt changes reset score windows; agent weights cold-start equally until 100 verdicts (blueprint §§7, 10, 12). **OPEN/MISSING:** calibration objective and data boundary for all pooled probabilities/thresholds, gate/top-K and risk parameter freeze, whether six months permits recalibration, and how an update affects the confirmatory estimand. Calibration must be separate from the final confirmation sample or governed online without outcome-reactive discretion.

## Forward-Test Readiness

Not ready to start. P8 is not started. A six-month duration is mentioned, but the minimum effective sample, start rule, prediction schedule/eligible universe, fixed primary endpoint, inference method, missing/outage and failed-request handling, restart rule, update policy, and mandatory reporting rule are not specified. The protocol needs a versioned freeze before the first eligible outcome.

## Degrees-of-Freedom Freeze Matrix

| Family | Disposition | Evidence / issue |
|---|---|---|
| Claim, primary estimand/comparator | OPEN | No singular primary comparison; blueprint §§12.3–12.5 |
| Universe/sector set | OPEN | Sector set explicitly deferred to before P8; blueprint §18 |
| Target horizon | OPEN | 21 vs 63 days unresolved; blueprint §18 |
| News source and point-in-time semantics | OPEN | Progress open issues; real timestamps/revisions unverified |
| Model/provider identity and request state | OPEN | `config/models.yaml` TODO entries; hosted snapshots not established |
| Prompts/topology/aggregation/budget | OPEN | Design only; committee P4 and agents P3 not started; matching absent |
| Gate/agent calibration and thresholds | OPEN | Update concept documented, confirmation boundary incomplete |
| Primary score, metrics, tests, multiplicity | OPEN | Metrics/method names listed, decision procedure incomplete |
| Costs/execution and economic threshold | OPEN | Some paper assumptions stated; full sensitivity/primary endpoint absent |
| Stop, restart, update, missingness, publication | OPEN | No complete prospective policy located |
| Data/code/protocol identity | OPEN | No frozen preregistration/version record or immutable model state |

## Findings Register

**ID-01 — Asymmetry mechanism is confounded**

- **Status:** DOCUMENTED (design controls); **Severity:** BLOCKER
- **Evidence:** blueprint §§10.1, 12.3–12.5; `config/models.yaml` (TODO tiers).
- **Observed fact:** treatment combines partition/role differences with fast/strong tiers; proposed symmetric and all-strong ablations do not match the declared resources or isolate one asymmetry intervention.
- **Risk mechanism:** performance differences cannot be attributed to asymmetry rather than model capability, compute, prompt/role, information, or aggregation.
- **Required action:** choose a narrow asymmetry estimand and preregister a matched-information, matched-model/budget contrast that changes only the targeted asymmetry; include a strongest-single or matched-budget control if relevant to the claim. Otherwise relabel the claim as system value at the specified budget.
- **Closure evidence:** frozen treatment/control table with models, inputs, tokens/calls, tools, retries, wall-clock, prompts, calibration and aggregation matched or explicitly included in the estimand; runnable protocol config.

**ID-02 — Primary endpoint, horizon and inference are not frozen**

- **Status:** DOCUMENTED; **Severity:** BLOCKER
- **Evidence:** blueprint §§12.3–12.4, §18; progress “Open questions” items 2–3.
- **Observed fact:** two horizons are to be logged before selecting the primary; multiple metrics and benchmarks are listed; block-bootstrap and DSR lack a fully specified inference/search procedure.
- **Risk mechanism:** outcome-informed endpoint selection and unaccounted dependence/search can make winner performance appear confirmatory.
- **Required action:** preregister one primary target, horizon, loss/utility, comparator, inference unit, dependence-aware CI/test, selection family, minimum relevant effect or precision, and fixed endpoint/stopping policy; separate tuning from confirmation.
- **Closure evidence:** dated analysis plan mapping each confirmatory statistic to its sample, assumptions, dependence treatment, multiplicity treatment and decision threshold.

**ID-03 — Prospective protocol and durable identity are absent**

- **Status:** MISSING; **Severity:** BLOCKER
- **Evidence:** `docs/PROGRESS.md` marks P3–P8 not started; `docs/plans/` contains P0–P2 only; no preregistration/forward-test artifact found in `docs/`.
- **Observed fact:** forward-test operational rules and protocol identity are not available for audit.
- **Risk mechanism:** failed periods/restarts/updates or post-outcome changes could selectively alter the evaluated record; no evidence predictions precede outcomes.
- **Required action:** before P8, create and timestamp protocol version, start/minimum-sample rule, schedule/universe, prediction commitment, outages/missingness/restarts, update/recalibration governance, and all-sign reporting policy; archive code/config/prompts/data provenance and served-model records.
- **Closure evidence:** approved preregistration plus immutable/timestamped artifacts and a dry-run audit trail showing prediction commitment before outcome availability.

**ID-04 — Point-in-time source completeness is unverified**

- **Status:** DOCUMENTED (known open limitations); **Severity:** BLOCKER
- **Evidence:** `docs/PROGRESS.md` “Open issues (P1–P2)” and P1/P2 notes; blueprint §12.1.
- **Observed fact:** fixtures are synthetic; news timestamp/revision reliability unverified; delisted entities absent from initial seed; corporate-action history missing; P3 input path not built.
- **Risk mechanism:** revised/backfilled data, survivorship and split-contaminated returns can leak information or distort the measured signal.
- **Required action:** complete real-source timestamp/revision audit and provenance capture; reconstruct or exclude affected historical/security/action windows under a predeclared rule; validate the full prompt-input boundary at each `as_of`.
- **Closure evidence:** source recordings and audit output, point-in-time coverage/exclusion report, plus tests/logs proving all model-visible facts and universe membership were available by the prediction timestamp.

**ID-05 — Hosted model identity and fallbacks are not pinned**

- **Status:** DOCUMENTED; **Severity:** MAJOR
- **Evidence:** `config/models.yaml`; blueprint §§10.1–10.2.
- **Observed fact:** model slugs and metadata are TODO; provider routing/fallbacks may change served model and no immutable snapshot guarantee is specified.
- **Risk mechanism:** treatment capability, cutoff and calibration drift across observations, weakening reproducibility and the meaning of a single protocol.
- **Required action:** pin available provider/model IDs and request parameters; retain prompt hashes, response model/IDs, timestamps, tools/retrieval state and fallback outcome; govern model changes as protocol versions.
- **Closure evidence:** completed validated model config and durable per-call provenance demonstrating served model identity and treatment/control assignment.

## Required Changes Before Start

1. Resolve ID-01 with a matched resource design or narrow the claim to system value.
2. Freeze the primary horizon, target, comparator, forecast/trading endpoint, effect threshold, inference/dependence and multiplicity plan (ID-02).
3. Resolve point-in-time source gaps and model identity; exclude unsupported windows by a predeclared rule (IDs 04–05).
4. Write a timestamped prospective protocol covering duration/effective sample, all operational failure/update/restart/reporting rules and durable artifact identity (ID-03).
5. Complete P3–P6 implementation and acceptance gates before treating the designed controls and measurements as executable evidence; P8 cannot establish readiness retroactively.

## Falsification Conditions

After freezing the design, reject or materially downgrade the asymmetry claim if the matched-resource asymmetric system fails the preregistered primary comparison or is no better than its symmetric/targeted-ablation control. Reject the predictive claim if the preregistered proper score or directional endpoint misses its minimum effect/precision criterion. Reject the economic claim if net results fail the fixed cost model or plausible preregistered cost sensitivity. A result confined to post-hoc subgroups, or one that disappears under the preregistered selection/dependence adjustment, does not support confirmation. Retain all eligible runs, including outages/failed predictions, under the frozen missingness rule.

## Final Verdict

**NOT IDENTIFIABLE** — the documented controls confound asymmetry with model, information and resource differences, so the central mechanism claim cannot be isolated; multiple missing protocol elements are additional blockers.
