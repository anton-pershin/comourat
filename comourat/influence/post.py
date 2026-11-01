"""Normalized post influence scoring.

Compares each post's raw upvote score against a reference level supplied by
the caller. The influence-score formula lives in `compute_influence_score`
alone; pipeline structure (assessor and filter placement) is independent of
the formula and stays with the consuming application.
"""


def compute_influence_score(score: int, reference: float) -> int:
    """Saturating normalized influence: 0 -> 0, at reference -> 50, large -> 100."""
    ratio = score / max(reference, 1)
    return round(100 * ratio / (ratio + 1))
