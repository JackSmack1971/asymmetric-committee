# Research Basis

Use this file when the audit needs rationale or source provenance. It summarizes sources that materially changed this skill; it is not a literature review.

## Codex / skill architecture

- OpenAI, **Skills**: skills are directories with `SKILL.md`; discovery uses skill metadata, with supporting files available after selection. https://developers.openai.com/api/docs/guides/tools-skills
- OpenAI Developers, **Rethinking skills and prompts for GPT-6 Astra** (2026-09-11): remove obsolete handholding, load context conditionally, and preserve judgment rather than overconstraining capable models. https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra
- OpenAI Codex repository, **skill-creator**: current examples emphasize concise `name`/`description`, progressive disclosure, deterministic scripts where they improve reliability, and optional `agents/openai.yaml` UI metadata.
- OpenAI, **Harness engineering**: make outcomes and operational state legible to the agent and use verification loops rather than asking the model to “try harder.” https://openai.com/index/harness-engineering/
- OpenAI Developers, **Automating repetitive work at OpenAI with Codex** (2026-08-25): preserve decision/approval boundaries and capture commands, outcomes, and decisions as reusable evidence. https://developers.openai.com/blog/automating-repetitive-work-at-openai-with-codex

## Forecast evaluation and data snooping

- White, H. (2000), **A Reality Check for Data Snooping**, *Econometrica* 68(5): 1097–1126. Establishes the need to account for specification search when evaluating the best model encountered. DOI: 10.1111/1468-0262.00152.
- Hansen, P. R. (2005), **A Test for Superior Predictive Ability**, *Journal of Business & Economic Statistics* 23(4): 365–380. Provides a data-snooping-aware test designed to improve power relative to the Reality Check in relevant settings. DOI: 10.1198/073500105000000063.
- Giacomini, R. & White, H. (2006), **Tests of Conditional Predictive Ability**, *Econometrica* 74(6): 1545–1578. Supports out-of-sample predictive-ability evaluation under possible misspecification and conditional objectives. DOI: 10.1111/j.1468-0262.2006.00718.x.
- Hansen, P. R., Lunde, A. & Nason, J. M. (2011), **The Model Confidence Set**, *Econometrica* 79(2): 453–497. Provides set-valued model comparison that acknowledges when data cannot distinguish a unique winner. DOI: 10.3982/ECTA5771.
- Harvey, C. R., Liu, Y. & Zhu, H. (2016), **… and the Cross-Section of Expected Returns**, *Review of Financial Studies* 29(1): 5–68. Demonstrates why extensive multiple testing in empirical finance raises the evidentiary threshold and why hidden search matters. DOI: 10.1093/rfs/hhv059.

## Probabilistic forecasting and time series

- Gneiting, T. & Raftery, A. E. (2007), **Strictly Proper Scoring Rules, Prediction, and Estimation**, *JASA* 102(477): 359–378. Proper scoring rules incentivize truthful probabilistic forecasts and provide principled evaluation objectives. DOI: 10.1198/016214506000001437.
- Bergmeir, C., Hyndman, R. J. & Koo, B., work on cross-validation for autoregressive time-series prediction: standard K-fold CV is not universally invalid, but validity depends on time-series/model assumptions. The skill therefore audits the information boundary and split justification instead of imposing a blanket rule.

## Multi-agent / LLM committee evidence

The empirical literature shows that multi-agent voting/debate and diverse sampling can improve some benchmark tasks, but gains depend on composition, diversity, protocol, and compute. This is evidence **for testing mechanism claims carefully**, not evidence that an asymmetric committee will improve financial prediction.

- Wang et al. (2022), **Self-Consistency Improves Chain of Thought Reasoning in Language Models**, arXiv:2203.11171: diverse sampled reasoning paths can improve benchmark accuracy, making matched-compute controls important when comparing committees to single calls.
- Becker et al. (2025), **Voting or Consensus? Decision-Making in Multi-Agent Debate**, Findings of ACL 2025: decision protocol and number of agents materially affect benchmark outcomes; discussion depth can hurt. This motivates treating topology/aggregation as experimental degrees of freedom.
- Wu, Li & Li (2025), **Can LLM Agents Really Debate? A Controlled Study of Multi-Agent Debate in Logical Reasoning**, arXiv:2511.07784: controlled evidence suggests base reasoning strength and diversity can dominate structural debate parameters, reinforcing the need to separate diversity/model strength from “asymmetry” as the claimed mechanism.

## Interpretation discipline

No source above establishes that an asymmetric LLM committee predicts markets. The research basis supports the audit mechanisms: matched-resource controls, point-in-time evaluation, search-aware inference, proper forecast scoring, explicit falsification, and prospective evidence.
