"""Conservative title/artist matching for cross-platform track resolution.

Silently adding the wrong song to a shared playlist is the worst failure
mode here, so this biases hard toward "don't add, ask a human" (see brief
5.2). An exact ISRC match is the only way to short-circuit straight to a
perfect score; everything else is fuzzy and must clear a threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz


def _clean(text: str | None) -> str:
    return (text or "").strip().lower()


@dataclass(frozen=True)
class Candidate:
    track_id: str
    uri: str
    title: str
    artist: str
    isrc: str | None = None


def score_candidate(
    query_title: str,
    query_artist: str,
    candidate: Candidate,
    query_isrc: str | None = None,
) -> float:
    """Score in [0, 1]. 1.0 for an exact ISRC match; otherwise fuzzy title+artist."""
    if query_isrc and candidate.isrc and query_isrc.strip().upper() == candidate.isrc.strip().upper():
        return 1.0

    title_score = fuzz.token_sort_ratio(_clean(query_title), _clean(candidate.title)) / 100.0
    artist_score = fuzz.token_sort_ratio(_clean(query_artist), _clean(candidate.artist)) / 100.0
    return (title_score * 0.6) + (artist_score * 0.4)


def rank_candidates(
    query_title: str,
    query_artist: str,
    candidates: list[Candidate],
    query_isrc: str | None = None,
) -> list[tuple[Candidate, float]]:
    scored = [
        (candidate, score_candidate(query_title, query_artist, candidate, query_isrc))
        for candidate in candidates
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def best_match(
    query_title: str,
    query_artist: str,
    candidates: list[Candidate],
    threshold: float,
    query_isrc: str | None = None,
) -> tuple[Candidate, float] | None:
    """Returns the best candidate if it clears `threshold`, else None (caller should ask a human)."""
    ranked = rank_candidates(query_title, query_artist, candidates, query_isrc)
    if not ranked:
        return None
    top_candidate, top_score = ranked[0]
    if top_score >= threshold:
        return top_candidate, top_score
    return None
