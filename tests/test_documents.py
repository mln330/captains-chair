import pytest

from make_it_so.documents import assert_durable_document, normalize_durable_document


def test_durable_plan_rejects_live_state() -> None:
    with pytest.raises(ValueError, match="volatile state"):
        assert_durable_document("# Plan\nGenerated: 2026-07-10\nOpen PRs: 2\n")


def test_normalization_removes_live_state() -> None:
    result = normalize_durable_document(
        "# Plan\nGenerated: 2026-07-10\nOpen PRs: 2\nCommit abcdef1234567890\n"
    )
    assert "Generated" not in result
    assert "Open PRs" not in result
    assert "<checked-live>" in result
