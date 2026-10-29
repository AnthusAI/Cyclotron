"""Stored kinds of a decision job. New rows get the kind ``WRITTEN``; rows already stored keep the kind they
were written with, and every reader accepts both. History is never rewritten."""

DECISION = "decision"
LEGACY_DECISION = "publication"
DECISION_KINDS = (DECISION, LEGACY_DECISION)
# The kind new decision jobs are stored under. Every deployed worker reads both.
WRITTEN = DECISION


def is_decision(kind):
    return kind in DECISION_KINDS


def same_kind(stored, requested):
    """A request id replayed across the rename names the same command."""
    return stored == requested or (is_decision(stored) and is_decision(requested))
