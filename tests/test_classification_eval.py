"""Keep offline classifier scores honest about bad outputs and ignored labels."""

import pytest
from experiments.classification import evaluate


@pytest.fixture
def case():
    return {
        "words": [
            {"id": i, "start": float(i), "end": float(i + 1), "word": str(i)}
            for i in range(5)
        ],
        "remove_ids": [1, 3],
        "keep_ids": [0, 4],
    }


def test_word_score_separates_content_loss_missed_ads_and_unscored_words(case):
    result = evaluate.score(case, {"ranges": [{"first": 0, "last": 2}]}, "words")
    assert result["lost_ids"] == [0]
    assert result["missed_ids"] == [3]
    assert result["unscored_selected_ids"] == [2]


@pytest.mark.parametrize(
    "span",
    [
        {"first": 1, "last": 10},
        {"first": 3, "last": 1},
        {"first": True, "last": 1},
        {"first": 1.0, "last": 1},
    ],
)
def test_invalid_word_ranges_are_errors_not_perfect_scores(case, span):
    with pytest.raises(ValueError):
        evaluate.score(case, {"ranges": [span]}, "words")


def test_timestamp_score_applies_threshold_and_word_midpoints(case):
    result = evaluate.score(
        case,
        {
            "ad_segments": [
                {"start": 0.9, "end": 3.1, "confidence": 0.9},
                {"start": 3, "end": 5, "confidence": 0.2},
            ]
        },
        "current",
    )
    assert result["lost_ids"] == []
    assert result["missed_ids"] == [3]


def test_omitted_required_output_is_an_error(case):
    with pytest.raises(ValueError):
        evaluate.score(case, {}, "words")


def test_overlapping_predictions_are_not_double_counted(case):
    result = evaluate.score(
        case, {"ranges": [{"first": 1, "last": 3}, {"first": 3, "last": 3}]}, "words"
    )
    assert result["missed_removable_words"] == 0
    assert result["lost_content_words"] == 0


def test_live_request_defaults_are_cheap_and_bypass_response_cache():
    body = evaluate.request_body("dsf", "test prompt")
    assert body["cache"] == {"no-cache": True, "no-store": True}
    assert body["model"] == "dsf"
    assert body["extra_body"]["thinking"]["type"] == "disabled"
