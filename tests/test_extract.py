import pytest

from app.extract import ExtractionFailed, extract
from tests.conftest import FakeClient, valid_posting


def test_a_valid_first_answer_takes_one_call():
    result = extract("posting", FakeClient([valid_posting()]))
    assert result.posting.company == "Acme"
    assert result.usage.calls == 1


def test_an_invalid_answer_gets_one_repair_turn_with_the_errors():
    client = FakeClient([valid_posting(confidence=4), valid_posting()])
    result = extract("posting", client)

    assert result.usage.calls == 2
    assert result.usage.input_tokens == 2000
    repair_messages = client.calls[1]
    assert repair_messages[-2]["role"] == "assistant"  # its own answer, echoed back
    assert "confidence" in repair_messages[-1]["content"]  # and what was wrong with it


def test_still_invalid_after_the_repair_turn_fails_without_a_third_call():
    client = FakeClient([valid_posting(confidence=4)] * 2)
    with pytest.raises(ExtractionFailed) as exc:
        extract("posting", client)
    assert exc.value.usage.calls == 2
    assert "confidence" in exc.value.errors
