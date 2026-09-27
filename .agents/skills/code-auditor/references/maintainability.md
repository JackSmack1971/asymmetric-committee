# Maintainability and Comprehension Audit

Load when maintainability, complexity, readability, coupling, or refactoring risk is material.

## Treat metrics as signals, not truth

Cognitive Complexity, cyclomatic complexity, lines of code, nesting depth, churn, coupling, and duplication can help prioritize inspection. None is a context-free proof of understandability or maintainability.

Empirical studies have found Cognitive Complexity correlated with code understandability/change, but not consistently superior to traditional structural measures; structural measures alone have limited predictive accuracy. Therefore:

- compare suspicious units with repository norms and nearby code;
- inspect the actual control/data flow before filing a finding;
- prefer concrete change-amplification, defect-proneness, testability, or comprehension evidence over a naked threshold;
- do not derive a hard function limit from Miller's `7±2`; working-memory research does not justify an `ICP <= 7` software gate.

## Useful inspection questions

### Control and data flow

- Does nesting conceal mutually exclusive states, fallthrough, or cleanup behavior?
- Are boolean expressions mixing independent policy decisions that should be named or separated?
- Can a reader determine ownership and mutation of important state without tracing many files?
- Do early returns clarify guards, or do they fragment lifecycle reasoning?

### Coupling and change amplification

- Does a local requirement require coordinated edits across unrelated modules?
- Are duplicated rules likely to drift?
- Are generated files or derived artifacts being edited instead of their source of truth?
- Are boundaries represented through explicit APIs/types, or through shared mutable implementation details?

### Naming and variable roles

Use role-oriented naming as a heuristic, not a mandatory taxonomy. Flag names only when ambiguity creates a realistic maintenance or correctness cost. Booleans should make polarity clear; domain names should preserve established repository vocabulary.

## Refactoring recommendations

Recommend refactoring only when the audit identifies a concrete risk. Preserve externally observable contracts unless the requested change intentionally alters them. Call out signatures, serialized formats, user-visible strings, numeric/ordering semantics, public exceptions, and protocol timing when they are compatibility-sensitive.

Do not demand literal preservation merely because a literal exists; preserve behavior and declared contracts.
