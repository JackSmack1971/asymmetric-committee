# Skill evaluation telemetry

The telemetry in this directory measures the **packaged evaluation corpus**, not normal user audits.

Run a small routing smoke evaluation:

```bash
python scripts/run_skill_evals.py --suite routing --max-cases 6
```

Run the protocol repetition counts and compare explicit skill treatment versus a treatment-neutral no-skill baseline:

```bash
python scripts/run_skill_evals.py --suite all --condition both --protocol-repeats
```

The runner stages a production-only copy of the skill into a temporary repository under `.agents/skills/`, excluding the packaged eval corpus and telemetry so the model cannot read its own expected answers. It invokes `codex exec --json --ephemeral` and uses `--sandbox workspace-write` only for task/recovery cases that must write `report.md`.

Routing cases remain implicit and are fail-closed: an actual activation is determined from Codex's native `codex.skill.injected` OTel metric. If the local OTLP metrics collector receives no Codex metrics, the case is `unknown` and does not count as a miss or correct non-activation.

Runtime output defaults to `telemetry/runs/<eval-run-id>/`, which is ignored by version control. Use `--out` when the installed package is read-only.

Task/recovery treatment arms are valid only when the native metric proves the target skill was injected. Baseline runs are invalidated if that same metric shows the target skill was injected despite not being staged. Cost fields are resource measurements (tokens/tool calls/commands/external operations/wall time), not dollar estimates.
