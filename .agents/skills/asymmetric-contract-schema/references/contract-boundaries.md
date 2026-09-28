# Contract boundary reference

Verify these rules against the live repository before relying on them.

## Canonical source

The repository uses shared models/enums under `contracts/` for inter-stage
message shapes. Consumers should import those contracts rather than recreate
equivalent dictionaries, TypedDicts, literals, or ad-hoc schemas.

## LLM model versus system envelope

The repository deliberately separates what a model is allowed to emit from what
the system owns.

Examples of system-owned evidence/state include identifiers, run context,
served-model identity, validity/cutoff state, and deterministic portfolio
weights. A model-facing schema must not gain such fields merely because the full
system record contains them.

When a `*LLM` model feeds a full model through a constructor such as `from_llm`,
test both boundaries:

1. the LLM schema contains only model-owned fields;
2. the system fills and validates the envelope fields separately.

## Generated schema authority

`contracts/schema_export.py` is the generator for checked-in strict JSON schemas.
The Python model is the editable source; JSON under the schema directory is
derived.

Expected lifecycle:

`Python model -> generator -> checked-in JSON -> --check freshness`

Do not use:

`edit Python + edit JSON by hand until tests pass`

because that destroys source/derived provenance.

## Strict-schema transformation

The repository generator intentionally normalizes provider-facing schemas. It
can inline references, enforce closed objects/required fields, and omit
unsupported constraint keywords while Pydantic performs final validation after
parsing.

Therefore provider-schema shape and full runtime validation semantics are
related but not identical. Test both.

## Compatibility questions

For every serialized shape change, determine:

- Are previously stored payloads read anywhere?
- Are fixtures versioned or assumed current?
- Does a database column/table persist the representation?
- Does a prompt/parser expect the exact field set?
- Is another phase/branch consuming the current shape?
- Is the change additive, required-additive, rename/removal, or semantic-only?

Do not label a change backward compatible merely because Python imports still
succeed.
