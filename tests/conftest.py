"""Tests run on in-memory SQLite and fake Redis: no API key, Docker or network.

Environment variables beat .env, so a real key in .env is never used here.
"""

import json
import os

os.environ["BATCH_API_KEY"] = "test-key"
os.environ["BATCH_DATABASE_URL"] = "sqlite://"

import pytest  # noqa: E402

from app.db import Base, get_engine, new_session  # noqa: E402
from app.llm import Completion  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def session():
    with new_session() as s:
        yield s


def valid_posting(**overrides) -> dict:
    posting = {
        "title": "Python Developer",
        "company": "Acme",
        "seniority": None,
        "skills": ["Python", "Django"],
        "salary": {
            "is_stated": True, "min_amount": 100_000, "max_amount": 150_000,
            "currency": "PKR", "period": "monthly", "period_stated": True,
        },
        "remote": None,
        "confidence": 0.9,
    }
    posting.update(overrides)
    return posting


class FakeClient:
    """Plays back scripted answers: a dict (sent as JSON) or an exception."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, system, messages, schema):
        self.calls.append(messages)
        if not self.answers:
            raise AssertionError("FakeClient: unexpected extra model call")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Completion(json.dumps(answer), input_tokens=1000, output_tokens=200)
