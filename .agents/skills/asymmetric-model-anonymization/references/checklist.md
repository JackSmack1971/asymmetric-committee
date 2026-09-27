# Model anonymization checklist

## Baseline
- [ ] Active instructions/spec/plan/progress read.
- [ ] Alias/store/renderer/partition implementation inspected.
- [ ] Relevant leak tests inspected.
- [ ] Missing historical paths called out.
- [ ] Dirty state preserved.

## Threat model
- [ ] Model-facing surface identified.
- [ ] Forbidden raw values enumerated.
- [ ] Safe representation defined.
- [ ] Point-in-time alias source defined.
- [ ] Partition ownership defined.
- [ ] Missing-coverage behavior defined.

## Aliases and partitions
- [ ] Alias lookup respects `as_of` when temporal.
- [ ] Tokens do not expose identity.
- [ ] Person/executive handling covered if applicable.
- [ ] Short-name/stop-word coverage checked.
- [ ] Cross-agent fields absent.

## Renderer/leak tests
- [ ] Identity leak tests.
- [ ] Numeric/date/currency/share leak tests.
- [ ] Evidence IDs preserved and non-sensitive.
- [ ] New model-visible fields reviewed.
- [ ] Missing alias fails closed or is surfaced.

## Verification
- [ ] Alias/config tests.
- [ ] Renderer tests.
- [ ] Partition tests.
- [ ] Coverage tests.
- [ ] Prompt/agent integration tests.
- [ ] Exact phase gate if applicable.

## Closure
- [ ] Unverified leak classes reported.
- [ ] Manual alias dependencies reported.
- [ ] Final diff reviewed.
