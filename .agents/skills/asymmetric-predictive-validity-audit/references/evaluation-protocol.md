# Behavioral Evaluation Protocol

Use this only when evaluating the skill itself. Do not load it during ordinary audits.

## Value hypothesis

Compared with a no-skill Codex baseline, this skill should reduce false readiness/validation judgments and missed experimental-validity defects for asymmetric LLM committee studies by at least one material-value threshold, without increasing median successful-run cost by more than 25%.

## Routing corpus

Run each routing prompt at least three times. Score whether Codex selects this skill when it should.

### 20 positives

1. Review our preregistration for a heterogeneous LLM committee that predicts next-day equity returns.
2. Does this multi-agent trading experiment actually isolate the value of assigning different roles to agents?
3. Audit our forward-test protocol for an LLM ensemble before we start collecting outcomes.
4. Check whether our committee backtest leaks information through revised fundamentals.
5. Are our single-agent and asymmetric-committee baselines matched fairly enough to claim incremental predictive value?
6. Review the multiplicity correction for 40 prompt variants and 12 market horizons in this LLM forecasting study.
7. Determine whether our LLM committee experiment is falsifiable before we preregister it.
8. Audit the calibration/test split for confidence-weighted committee forecasts.
9. Check whether our symmetric-committee ablation can identify the effect of role asymmetry.
10. Review point-in-time data provenance for this agent committee market-prediction benchmark.
11. Does our repeated holdout reuse invalidate the confirmatory claim for this LLM ensemble?
12. Audit the statistical design for comparing a committee’s probability forecasts against a single-model baseline.
13. Determine whether five models voting on crypto direction add value beyond matched inference compute.
14. Review our planned live forward test and restart rule for a multi-agent forecasting system.
15. Is it defensible to call this committee edge causal, or only predictive?
16. Audit whether selecting the best agent topology after seeing backtest results creates a data-snooping problem.
17. Check the research degrees of freedom in this asymmetric committee experiment.
18. Review whether our cost/slippage assumptions support the claimed economic value of the committee.
19. Audit whether changing model providers mid-forward-test breaks the protocol identity.
20. Decide whether this LLM forecasting design is ready to preregister.

### 20 negatives

1. Fix the Python typing errors in our trading bot.
2. Review this pull request for maintainability and security bugs.
3. Optimize our order-routing latency.
4. Refactor the agent orchestration code to reduce duplication.
5. Write unit tests for the committee aggregator.
6. Explain how majority voting works in ensemble methods.
7. Recommend the best current LLM for financial news summarization.
8. Create a trading strategy using RSI and moving averages.
9. Debug why our Alpaca API orders are rejected.
10. Improve the UI for our backtesting dashboard.
11. Summarize this paper about multi-agent debate.
12. Add logging to our LLM agent framework.
13. Benchmark runtime performance of these Python functions.
14. Review the repository architecture for scalability.
15. Write a Dockerfile for the research service.
16. Generate synthetic market data for a demo.
17. Explain the difference between Brier score and log loss in general.
18. Implement a block bootstrap in Python.
19. Clean up our README and installation instructions.
20. Investigate a memory leak in the live trading process.

### 10 neighboring / ambiguous prompts

1. Review the statistical tests in our generic stock-prediction model with no LLM committee. **Expected:** do not route unless the user frames it as this skill’s committee-validity workflow.
2. Audit a symmetric five-model ensemble for predictive validity. **Expected:** usually neighbor; route only if comparison to asymmetry/multi-agent mechanism is central.
3. Review code that computes our backtest split boundaries. **Expected:** route if the question is experimental leakage/validity; otherwise code review.
4. Tell me why the committee performed better last month. **Expected:** do not route for post-hoc explanation alone; route if asked whether that evidence supports a valid claim.
5. Design an experiment comparing one model against five independent samples of the same model. **Expected:** route when the intended claim concerns committee/ensemble incremental value.
6. Check our transaction-cost function for correctness. **Expected:** code/test skill unless the request is whether cost assumptions invalidate the economic claim.
7. Review our model calibration code. **Expected:** generic code review unless the request is separation of calibration and final evaluation.
8. Audit a multi-agent system for prompt-injection security. **Expected:** security workflow, not this skill.
9. Compare voting versus consensus protocols on a reasoning benchmark. **Expected:** research-design neighbor; route only when framed as predictive-validity/mechanism audit for a claimed committee advantage.
10. Review the experimental design of an LLM committee that classifies medical images. **Expected:** neighbor; the core workflow can generalize scientifically, but the skill description intentionally targets predictive/trading value and should not activate by default outside that boundary.

### Routing targets

- precision >= 0.90
- recall >= 0.90
- neighbor false activation <= 0.15

## Representative task scenarios

Run each at least five times with the skill and from the same starting state without the skill. Fix a binary success rubric before execution.

1. Compute confound: committee has 5x calls versus single-agent control.
2. Holdout reuse: prompts selected on the same period reported as final test.
3. Timestamp leakage: fundamentals use restated values not point-in-time records.
4. Multiplicity: dozens of prompt/model/horizon searches but only final winner is tested.
5. Calibration leakage: threshold tuned on the final test.
6. Weak baseline: committee compared only with a deliberately small model.
7. Protocol drift: provider silently updates hosted model during prospective test.
8. Economic overclaim: predictive edge smaller than plausible costs.
9. Conditional advantage: overall average is null but one preregistered regime may differ.
10. Clean design: matched controls, frozen protocol, untouched confirmatory sample, explicit falsification.

### Task-success rubric

A run succeeds only if it:

- identifies the highest-severity validity issue(s) without inventing evidence;
- distinguishes the claim rung actually supportable;
- does not mandate irrelevant statistical machinery;
- gives a verdict consistent with its own blockers/missing evidence;
- names concrete closure evidence or a credible falsification condition;
- does not claim prospective validation from design review alone.

## Failure scenarios

1. Required protocol file is missing.
2. Project evidence conflicts about the test window.
3. Statistical assumptions cannot be established from artifacts.
4. User asks the auditor to ignore a known leakage defect because rerunning is expensive.
5. Python/report validator is unavailable or fails.

Success includes correct `BLOCKED BY MISSING EVIDENCE`, bounded alternate evidence, or an explicit stop; forcing a positive verdict is failure.

## Metrics

Track:

- routing precision, recall, and neighbor false activation;
- task success rate with and without skill;
- absolute uplift and relative error reduction;
- failure-recovery rate and uplift;
- critical/non-critical policy violations;
- premature completion and unnecessary continuation;
- at least two resources among model tokens, tool calls, commands, wall time, and external operations.

Material value requires at least one preregistered threshold from the project rubric (for example +10 percentage points task success or >=25% relative error reduction) with no critical regression.


## Packaged telemetry execution

The machine-readable corpus is `evals/corpus.json`. Run it with `scripts/run_skill_evals.py`; the harness stages only the production skill into an isolated `.agents/skills/asymmetric-predictive-validity-audit` workspace and does not expose corpus labels, graders, or telemetry code to the evaluated agent.

Routing cases test **implicit selection**. A routing miss or false activation is scored only from Codex's native `codex.skill.injected` OpenTelemetry metric while the metrics exporter is demonstrably live. If the metrics stream is unavailable, routing is `UNKNOWN` and the run cannot satisfy routing targets.

Task and recovery cases separate routing from treatment value: the skill arm explicitly invokes `$asymmetric-predictive-validity-audit`; the baseline arm has no target skill staged and receives the same neutral task prompt. A target-skill injection in the baseline is contamination and invalidates that baseline run. Both arms are graded after execution by the same external deterministic report validator plus case-specific hidden rubric.

Resource telemetry records the exact Codex version/model plus token usage from `turn.completed`, completed tool and command counts, failed commands, external operations, and wall-clock duration. These are resource-cost measurements, not dollar-price estimates. Compare cost only across compatible runtime/model conditions and report successful-run medians.

Quick routing smoke run:

```bash
python scripts/run_skill_evals.py --suite routing --max-cases 6
```

Full protocol repeats with task/recovery baselines:

```bash
python scripts/run_skill_evals.py --suite all --condition both --protocol-repeats
```

Raw run artifacts are written below `telemetry/runs/` and ignored by version control. Recompute a saved run summary with `python scripts/score_eval_run.py <run-dir>`.
