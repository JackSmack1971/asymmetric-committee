"""Errors that cross the store / orchestration boundary. Defined here so both may import them."""

from __future__ import annotations


class RunHaltedError(RuntimeError):
    """A kill-switch halted an earlier run for this as_of; a new run must be requested."""


class ResetRefusedError(RuntimeError):
    """The run holds committed, anchored, ordered or halt evidence and can never be reset."""


class AnchorIncompleteError(ValueError):
    """ANCHORED needs both the OpenTimestamps proof and the public git commit."""


class ImmutableConflictError(RuntimeError):
    """An insert-only row was presented again with a different payload under one natural key."""
