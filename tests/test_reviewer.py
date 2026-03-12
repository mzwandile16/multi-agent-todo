"""Tests for agents/reviewer.py: _evaluate_review pure logic."""

import pytest

from agents.reviewer import ReviewerAgent


@pytest.fixture
def evaluator():
    """Return a ReviewerAgent with a dummy client — we only test _evaluate_review."""
    class _DummyClient:
        pass
    return ReviewerAgent(model="test-model", client=_DummyClient())


class TestEvaluateReview:

    # ── Explicit verdict keywords ──

    def test_approve_keyword(self, evaluator):
        assert evaluator._evaluate_review("APPROVE\nAll looks good.") is True

    def test_request_changes_keyword(self, evaluator):
        assert evaluator._evaluate_review("REQUEST_CHANGES\nPlease fix the bug.") is False

    def test_request_changes_overrides_approve(self, evaluator):
        """If both keywords appear, REQUEST_CHANGES wins."""
        text = "I considered APPROVE but ultimately REQUEST_CHANGES are needed."
        assert evaluator._evaluate_review(text) is False

    # ── Heuristic fallback (no explicit keywords) ──

    def test_heuristic_positive(self, evaluator):
        text = "LGTM, looks good to me."
        assert evaluator._evaluate_review(text) is True

    def test_heuristic_negative(self, evaluator):
        text = "There is a bug in the implementation and the result is wrong."
        assert evaluator._evaluate_review(text) is False

    def test_heuristic_tie_rejects(self, evaluator):
        """Equal positive and negative signals → not passed (pos_count > neg_count is False)."""
        text = "Looks good but there is a bug."
        assert evaluator._evaluate_review(text) is False

    def test_empty_output_passes(self, evaluator):
        """No signals at all → 0 > 0 is False → rejected."""
        assert evaluator._evaluate_review("") is False

    # ── Case insensitivity ──

    def test_approve_case_insensitive(self, evaluator):
        assert evaluator._evaluate_review("approve") is True

    def test_request_changes_case_insensitive(self, evaluator):
        assert evaluator._evaluate_review("request_changes") is False
