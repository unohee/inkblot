"""Inkblot: eval infrastructure for Codex and Claude agent harnesses.

Every harness artifact is a claim: "this text changes what the model does".
This package turns that claim into a measurement — run the same probe with and
without the artifact, N times, and check the outputs with deterministic rules.
"""

__version__ = "0.1.0"
