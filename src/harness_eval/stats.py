"""Verdict arithmetic.

Trial counts here are small (n=6 per arm is typical), so the test has to be
exact rather than asymptotic: Fisher's exact test on the 2x2 pass/fail table,
computed from stdlib combinatorics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

LOAD_BEARING = "load-bearing"
NO_OP = "no-op"
INEFFECTIVE = "ineffective"
INCONCLUSIVE = "inconclusive"
NO_DATA = "no-data"
ACTIVATION_PASS = "activation-pass"
ACTIVATION_FAIL = "activation-fail"
CONTAMINATED = "contaminated"

# A rule is only "already satisfied without me" if the treatment arm actually
# reaches the target behaviour. Below this rate, equal arms mean the rule fails
# in both, which is a different problem with a different fix.
COMPLIANCE_FLOOR = 0.5


def fisher_exact_greater(a: int, b: int, c: int, d: int) -> float:
    """One-sided p-value for the table [[a, b], [c, d]].

    Rows are arms (treatment, control), columns are (pass, fail). Returns
    P(treatment passes >= a | margins fixed) under the null of no effect.
    """
    for v in (a, b, c, d):
        if v < 0:
            raise ValueError("counts must be non-negative")
    n = a + b + c + d
    if n == 0:
        return 1.0
    row1, col1 = a + b, a + c
    denom = math.comb(n, col1)
    if denom == 0:
        return 1.0
    hi = min(row1, col1)
    tail = sum(math.comb(row1, x) * math.comb(n - row1, col1 - x) for x in range(a, hi + 1))
    return min(1.0, tail / denom)


@dataclass
class ArmStats:
    n: int
    passes: int

    @property
    def rate(self) -> float:
        return self.passes / self.n if self.n else 0.0


@dataclass
class Verdict:
    label: str
    p_value: float
    delta: float
    treatment: ArmStats
    control: ArmStats
    rationale: str


def classify(treatment: ArmStats, control: ArmStats, alpha: float = 0.05,
             compliance_floor: float = COMPLIANCE_FLOOR) -> Verdict:
    """Turn two arms of pass counts into a keep/drop verdict.

    `no-op` is deliberately conservative: it is only claimed when the control
    arm did at least as well as the treatment arm *and* the treatment arm
    actually produced the target behaviour. Two arms that both fail are
    `ineffective`, not `no-op` — saying "the model already does this without the
    rule" about a 0/6 treatment arm would be plainly false.

    A positive but statistically weak effect stays `inconclusive` so that an
    underpowered run never deletes a rule that actually works.
    """
    if treatment.n == 0 or control.n == 0:
        return Verdict(NO_DATA, 1.0, 0.0, treatment, control, "one or both arms produced no trials")

    delta = treatment.rate - control.rate
    p = fisher_exact_greater(
        treatment.passes, treatment.n - treatment.passes,
        control.passes, control.n - control.passes,
    )

    if delta > 0 and p <= alpha:
        rationale = (
            f"treatment {treatment.passes}/{treatment.n} vs control "
            f"{control.passes}/{control.n}, p={p:.4f} <= alpha={alpha}"
        )
        return Verdict(LOAD_BEARING, p, delta, treatment, control, rationale)

    if delta <= 0:
        if treatment.rate < compliance_floor:
            rationale = (
                f"neither arm reaches the target behaviour "
                f"(treatment {treatment.passes}/{treatment.n}, control {control.passes}/{control.n}) "
                f"— the rule is not producing the outcome it asks for"
            )
            return Verdict(INEFFECTIVE, p, delta, treatment, control, rationale)
        rationale = (
            f"control already satisfies the check ({control.passes}/{control.n}) "
            f"at least as often as treatment ({treatment.passes}/{treatment.n})"
        )
        return Verdict(NO_OP, p, delta, treatment, control, rationale)

    rationale = (
        f"effect present but underpowered: delta={delta:+.2f}, p={p:.4f} > alpha={alpha}; "
        f"raise n to decide"
    )
    return Verdict(INCONCLUSIVE, p, delta, treatment, control, rationale)


def classify_activation(treatment: ArmStats, control: ArmStats) -> Verdict:
    """Classify a deterministic transport/discovery gate without significance math."""
    if treatment.n == 0 or control.n == 0:
        return Verdict(NO_DATA, 1.0, 0.0, treatment, control,
                       "one or both arms produced no usable trials")
    delta = treatment.rate - control.rate
    if treatment.passes == treatment.n and control.passes == 0:
        return Verdict(ACTIVATION_PASS, 0.0, delta, treatment, control,
                       "every treatment activated and every control stayed clean")
    if control.passes:
        return Verdict(CONTAMINATED, 1.0, delta, treatment, control,
                       "the control arm observed the activation marker")
    return Verdict(ACTIVATION_FAIL, 1.0, delta, treatment, control,
                   "one or more treatment trials failed the activation marker")


def min_n_for_significance(alpha: float = 0.05) -> int:
    """Smallest per-arm n where a perfect split (all pass vs all fail) reaches alpha.

    Useful as a planning guard: below this, `load-bearing` is unreachable no
    matter how clean the separation is.
    """
    n = 1
    while n < 100:
        if fisher_exact_greater(n, 0, 0, n) <= alpha:
            return n
        n += 1
    return n
