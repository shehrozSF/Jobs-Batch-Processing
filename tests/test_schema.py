import pytest
from pydantic import ValidationError

from app.llm import strict_schema
from app.schema import JobPosting, Salary
from tests.conftest import valid_posting

NOT_STATED = dict(is_stated=False, min_amount=None, max_amount=None,
                  currency=None, period=None, period_stated=False)


def test_a_valid_posting_passes():
    JobPosting.model_validate(valid_posting())


def test_no_salary_is_a_valid_answer():
    Salary(**NOT_STATED)


@pytest.mark.parametrize("change, message", [
    ({"min_amount": 50_000}, "is_stated is false"),           # "competitive, PKR 50,000"
    ({"is_stated": True}, "no amount"),
    ({"is_stated": True, "min_amount": 9, "max_amount": 5}, "greater than"),
    ({"is_stated": True, "min_amount": 9, "max_amount": 9, "period": "monthly"},
     "period_stated"),                                         # a guessed period
])
def test_contradictions_are_rejected(change, message):
    with pytest.raises(ValidationError, match=message):
        Salary(**{**NOT_STATED, **change})


def test_confidence_must_be_a_fraction():
    with pytest.raises(ValidationError, match="confidence"):
        JobPosting.model_validate(valid_posting(confidence=4))


def test_strict_schema_keeps_a_field_named_title():
    schema = strict_schema(JobPosting)
    assert "title" in schema["properties"]  # a field, not the annotation keyword
    assert "$defs" not in schema and "$ref" not in str(schema)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(JobPosting.model_fields)
    salary = schema["properties"]["salary"]
    assert set(salary["required"]) == set(Salary.model_fields)
    # Required, but still allowed to be null.
    assert {"type": "null"} in salary["properties"]["min_amount"]["anyOf"]
