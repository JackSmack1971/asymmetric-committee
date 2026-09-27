# Research Basis and Methodology Decisions

This reference records why the skill avoids several common but weak audit heuristics. Re-check current versions before making time-sensitive claims.

## Codex / Agent Skills design

- OpenAI, **Rethinking skills and prompts for GPT-6 Astra** (2026-09-11): keep descriptions short and activation-specific, use progressive disclosure, remove unnecessary itineraries, and define completion/boundaries without over-constraining capable models.  
  https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra
- OpenAI, **Skills** documentation: skill discovery uses `name` and `description`; supporting references/scripts should be linked as needed; a ZIP should contain a single top-level skill folder.  
  https://developers.openai.com/api/docs/guides/tools-skills
- OpenAI, **Harness engineering: leveraging Codex in an agent-first world** (2026-02-11): improve agent reliability by making systems legible and enforcing consequential invariants while preserving implementation judgment.  
  https://openai.com/index/harness-engineering/

## Code review and maintainability evidence

- Sadowski et al., **Modern Code Review: A Case Study at Google**, ICSE-SEIP 2018: large-scale evidence supports lightweight review as a multi-purpose engineering practice; audit findings should focus on concrete defects/maintainability effects rather than ceremonial process.  
  https://research.google/pubs/modern-code-review-a-case-study-at-google/
- Scalabrino et al., **An empirical evaluation of the Cognitive Complexity measure as a predictor of code understandability**, Journal of Systems and Software 2023: Cognitive Complexity was approximately comparable to traditional metrics, not a clear superior predictor.  
  https://doi.org/10.1016/j.jss.2022.111561
- Scalabrino et al., **An empirical study on software understandability and its dependence on code characteristics**, Empirical Software Engineering 2024: structural code measures alone produced limited understandability prediction accuracy.  
  https://doi.org/10.1007/s10664-023-10396-7
- Kochhar et al., **Code Coverage and Post-release Defects**, IEEE Transactions on Reliability 2017: across 100 large Java projects, coverage showed insignificant project-level correlation and no file-level correlation with post-release bug counts. Coverage remains useful as execution evidence, not a universal quality score.  
  https://www.microsoft.com/en-us/research/publication/code-coverage-and-post-release-defects-a-large-scale-study-on-open-source-projects/
- Cowan, **The magical number 4 in short-term memory** (2001), and subsequent working-memory literature: Miller's `7±2` is not a justified software function-complexity limit. Do not translate it into an `ICP <= 7` gate.

## Security / quality standards

- ISO/IEC 25010:2023 is the current edition of the product quality model and contains nine characteristics; the 2011 edition is withdrawn.  
  https://www.iso.org/standard/78176.html
- NIST SP 800-218 SSDF v1.1 is final; SSDF v1.2 is an Initial Public Draft as of this skill version.  
  https://csrc.nist.gov/projects/ssdf/publications
- OWASP ASVS lists 5.0.0 as the current stable version as of this skill version.  
  https://owasp.org/projects/asvs
- AICPA Trust Services Criteria `CC8.1` is the change-management criterion; repository evidence may demonstrate only part of the organizational control lifecycle.

## Consequences for this skill

1. No universal complexity, coverage, or function-length gate.
2. Structural metrics prioritize human inspection; they do not independently establish a defect.
3. Static tools and human review are complementary; both need applicability checks.
4. Compliance conclusions are evidence-scoped and versioned, never implied certification.
5. `INCONCLUSIVE` is a first-class outcome when the evidence needed to support the requested claim is unavailable.
