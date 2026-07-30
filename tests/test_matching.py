from __future__ import annotations

from cratebot.matching import Candidate, best_match, rank_candidates, score_candidate

GOOD = Candidate(track_id="t1", uri="spotify:track:t1", title="Blinding Lights", artist="The Weeknd")
WRONG = Candidate(track_id="t2", uri="spotify:track:t2", title="Save Your Tears", artist="The Weeknd")
ISRC_MATCH = Candidate(
    track_id="t3", uri="spotify:track:t3", title="Totally Different Name", artist="Someone Else", isrc="USUG12000123"
)


def test_exact_title_artist_scores_high() -> None:
    score = score_candidate("Blinding Lights", "The Weeknd", GOOD)
    assert score > 0.95


def test_wrong_song_same_artist_scores_lower() -> None:
    score = score_candidate("Blinding Lights", "The Weeknd", WRONG)
    assert score < 0.7


def test_isrc_exact_match_short_circuits_to_perfect_score() -> None:
    score = score_candidate(
        "Blinding Lights", "The Weeknd", ISRC_MATCH, query_isrc="USUG12000123"
    )
    assert score == 1.0


def test_isrc_case_insensitive() -> None:
    score = score_candidate("x", "y", ISRC_MATCH, query_isrc="usug12000123")
    assert score == 1.0


def test_rank_candidates_orders_best_first() -> None:
    ranked = rank_candidates("Blinding Lights", "The Weeknd", [WRONG, GOOD])
    assert ranked[0][0] is GOOD
    assert ranked[0][1] >= ranked[1][1]


def test_best_match_returns_none_below_threshold() -> None:
    result = best_match("Some Obscure Remix Edit", "Nobody Known", [GOOD, WRONG], threshold=0.85)
    assert result is None


def test_best_match_returns_candidate_above_threshold() -> None:
    result = best_match("Blinding Lights", "The Weeknd", [WRONG, GOOD], threshold=0.85)
    assert result is not None
    candidate, score = result
    assert candidate is GOOD
    assert score >= 0.85


def test_best_match_empty_candidates() -> None:
    assert best_match("anything", "anyone", [], threshold=0.85) is None
