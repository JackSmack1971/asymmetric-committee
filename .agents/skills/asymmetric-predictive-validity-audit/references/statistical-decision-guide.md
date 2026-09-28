# Statistical Decision Guide

This is a routing guide, not a substitute for a statistician. Choose methods from the scientific question and data-generating structure, not from a checklist.

## 1. Start with the forecast object

- **Point forecast / trading score:** define a loss or utility function before evaluation.
- **Binary probability:** use a strictly proper score such as log loss or Brier score as a primary probabilistic comparison when appropriate; inspect calibration separately.
- **Quantile/interval:** use a proper quantile/interval scoring rule appropriate to the stated target.
- **Full predictive distribution:** use a strictly proper distributional score appropriate to support/dimension.

A metric chosen after seeing outcomes is itself a researcher degree of freedom.

## 2. Pairwise forecast comparison

Use a pairwise predictive-accuracy test only when the comparison and loss are preregistered and the dependence assumptions are handled.

- Diebold–Mariano-type logic addresses equal predictive accuracy for forecast losses; overlapping horizons commonly induce serial dependence in loss differentials and require appropriate variance estimation.
- Giacomini–White is relevant when conditional predictive ability or estimation-window effects are part of the question and its assumptions match the design.
- Nested-model comparisons can require methods that account for the nesting/estimation setting; do not mechanically apply a generic pairwise test.

The audit should require justification of the chosen method, not a specific named test in every project.

## 3. Many models / searched alternatives

If the promoted committee was selected after searching many alternatives, an unadjusted p-value for the winner is not confirmatory evidence.

Methods that may be relevant depending on the design include:

- **White Reality Check** for data-snooping-aware comparison of the best searched model against a benchmark;
- **Hansen SPA** as a more powerful alternative in settings with many poor/irrelevant alternatives;
- **Model Confidence Set** when the scientific goal is to identify a set of statistically indistinguishable best models rather than crown one winner;
- stepdown/bootstrap familywise procedures or false-discovery-rate control when the inferential family and dependence structure make them appropriate.

If the true search family is unknown or cannot be reconstructed, prefer a fresh untouched confirmatory sample rather than inventing a correction family.

## 4. Financial backtest selection

Financial prediction has low signal-to-noise ratios and extensive specification search. Treat prompt/model/topology/horizon/asset/threshold/metric exploration as part of the discovery family when it can influence promotion.

Sharpe-ratio evidence is especially vulnerable to selection and non-normal/dependent returns. A deflated or selection-aware Sharpe analysis may be useful as a diagnostic, but it does not repair leakage, weak controls, or reused holdouts.

Do not reduce multiplicity to a universal t-statistic threshold; the appropriate correction depends on the number/dependence of tests and the selection process.

## 5. Dependence and effective sample

Inspect before inference:

- serial correlation in outcomes or loss differentials;
- overlapping forecast horizons;
- cross-sectional dependence across assets;
- event clustering/regime blocks;
- common market shocks;
- repeated predictions derived from the same underlying information event.

Use clustering, HAC/block bootstrap, or another dependence-aware method justified by the design. The number of rows is not automatically the number of independent observations.

## 6. Cross-validation and temporal evaluation

Do not assume ordinary random K-fold validation is valid for every time series. Use a split scheme that preserves the information boundary and matches the deployment question. Rolling/expanding or blocked schemes are often appropriate; ordinary K-fold can be defensible under narrower assumptions (for example, certain autoregressive settings with suitable error properties).

The audit criterion is **no future information and an evaluation scheme justified for the model/data**, not a ritual prohibition on all K-fold CV.

## 7. Calibration and proper scoring

For probabilistic forecasts:

- preregister the primary proper score;
- evaluate calibration on data not used to fit the calibrator;
- report sharpness/discrimination only alongside calibration as appropriate;
- do not tune thresholds/weights on the final test and then report that same sample as confirmation.

Calibration is a property of forecasts and outcomes in an operating regime; model/provider drift can invalidate old calibration.

## 8. Effect size, power, and practical relevance

Before confirmatory evaluation, state either:

- a minimum effect worth detecting, plus a design/power calculation; or
- a target precision/interval width sufficient for the scientific decision.

For economic claims, the minimum relevant effect should exceed realistic implementation costs with appropriate uncertainty. A tiny statistically detectable edge is not automatically economically meaningful.

## 9. Sequential looks and adaptation

Repeated peeking inflates false-positive risk unless the design uses a valid sequential procedure. If the project wants continuous monitoring, require a predeclared sequential/e-value/alpha-spending or other justified framework, or prohibit confirmatory decisions until the fixed endpoint.

Prompt/model/topology updates during a forward test create adaptive selection. Govern them prospectively or start a new protocol version.

## 10. What the auditor should report

For each statistical method, state:

1. the question it answers;
2. the data/forecast assumptions it relies on;
3. how dependence is handled;
4. how selection/multiplicity is handled;
5. what sample was used for tuning versus confirmation;
6. the effect-size/uncertainty output needed;
7. any assumption that remains unverified.

A method name without this mapping is not sufficient evidence of validity.
