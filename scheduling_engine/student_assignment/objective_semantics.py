"""Pure-engine objective semantics for student-assignment runs.

Version 1 keeps the historical label-to-lexicographic-level behavior. Version
2 separates the raw penalty, its deterministic input-derived scale, the
canonical counselor importance score, and the weighted normalized contribution.
The module deliberately contains no Django or persistence code.
"""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Integral


OBJECTIVE_SEMANTICS_V1 = "v1"
OBJECTIVE_SEMANTICS_V2 = "v2"
OBJECTIVE_SEMANTICS_VERSIONS = (
    OBJECTIVE_SEMANTICS_V1,
    OBJECTIVE_SEMANTICS_V2,
)

CANONICAL_IMPORTANCE_MIN = 0
CANONICAL_IMPORTANCE_MAX = 10
NORMALIZED_OBJECTIVE_SCALE = 10_000

# Compatibility presets.  These values are intentionally owned by the pure
# engine so labels and explicit numeric settings resolve to one immutable
# semantic value before model construction.  The endpoints preserve the
# historical meaning of disabled and maximum importance; the interior values
# provide an understandable increasing linear scale.
IMPORTANCE_LABEL_TO_SCORE = {
    "not_important": 0,
    "a_little_bit_important": 2,
    "important": 5,
    "really_important": 8,
    "extremely_important": 10,
}

OBJECTIVE_KEYS = (
    "section_utilization_balance",
    "student_semester_balance",
    "course_sequence_preferences",
    "difficulty_balance",
    "course_category_diversity",
)


@dataclass(frozen=True)
class ObjectiveScale:
    """One deterministic denominator for one raw soft-objective penalty."""

    name: str
    denominator: int
    normalized_scale: int = NORMALIZED_OBJECTIVE_SCALE

    def normalize(self, raw_penalty: int) -> int:
        return normalize_penalty(
            raw_penalty,
            self.denominator,
            scale=self.normalized_scale,
        )


def validate_importance_score(value: int) -> int:
    """Validate and return one canonical integer counselor importance score."""

    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError("Counselor importance must be an integer from 0 through 10.")
    value = int(value)
    if not CANONICAL_IMPORTANCE_MIN <= value <= CANONICAL_IMPORTANCE_MAX:
        raise ValueError("Counselor importance must be an integer from 0 through 10.")
    return value


def importance_score_for_label(label: str) -> int:
    try:
        return IMPORTANCE_LABEL_TO_SCORE[label]
    except KeyError as error:
        raise ValueError(f"Unknown counselor importance label: {label!r}.") from error


def resolve_importance_scores(*, labels=None, scores=None) -> dict[str, int]:
    """Resolve labels/numeric input into one canonical score mapping.

    Explicit scores are accepted for v2.  Missing scores fall back to the
    compatibility preset, which lets a label-only v2 request use exactly the
    same engine semantics as an explicit request carrying that preset value.
    """

    labels = dict(labels or {})
    scores = dict(scores or {})
    unknown = (set(labels) | set(scores)) - set(OBJECTIVE_KEYS)
    if unknown:
        raise ValueError(f"Unknown student-assignment objective keys: {sorted(unknown)!r}.")
    resolved = {}
    for key in OBJECTIVE_KEYS:
        if key in scores:
            resolved[key] = validate_importance_score(scores[key])
        elif key in labels:
            resolved[key] = importance_score_for_label(labels[key])
        else:
            raise ValueError(f"Missing counselor importance for {key!r}.")
    return resolved


def normalize_penalty(raw_penalty: int, denominator: int, *, scale=NORMALIZED_OBJECTIVE_SCALE) -> int:
    """Map a non-negative raw penalty into a bounded deterministic integer."""

    raw_penalty = int(raw_penalty)
    denominator = int(denominator)
    scale = int(scale)
    if raw_penalty < 0:
        raise ValueError("Raw objective penalties must be non-negative.")
    if denominator <= 0:
        return 0
    if scale <= 0:
        raise ValueError("The normalized objective scale must be positive.")
    return min(scale, (raw_penalty * scale) // denominator)


def weighted_normalized_penalty(
    raw_penalty: int,
    denominator: int,
    importance_score: int,
    *,
    scale=NORMALIZED_OBJECTIVE_SCALE,
) -> int:
    """Return the v2 linear weighted contribution for one raw penalty."""

    return normalize_penalty(raw_penalty, denominator, scale=scale) * validate_importance_score(
        importance_score
    )


def denominator_from_pair_capacities(capacities) -> int:
    """Return the input-derived maximum for pairwise section imbalance."""

    values = tuple(int(value) for value in capacities)
    return sum(max(left, right) for index, left in enumerate(values) for right in values[index + 1:])


def denominator_from_maxima(maxima) -> int:
    """Return a deterministic sum of independent per-entity maxima."""

    return sum(max(0, int(value)) for value in maxima)
