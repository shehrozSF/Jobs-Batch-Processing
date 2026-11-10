import httpx
import pytest

from app.config import Settings
from app.llm import CerebrasClient, FatalError, RetryableError

SETTINGS = Settings(api_key="k", _env_file=None)


def client_returning(status=200, json=None, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=json, headers=headers)
    return CerebrasClient(SETTINGS, http=httpx.Client(transport=httpx.MockTransport(handler)))


def ok_body(content='{"a": 1}', finish="stop"):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }


def call(client):
    return client.complete("system", [{"role": "user", "content": "hi"}], {})


def test_a_completion_carries_text_and_tokens():
    result = call(client_returning(json=ok_body()))
    assert (result.text, result.input_tokens, result.output_tokens) == ('{"a": 1}', 120, 30)


def test_a_rate_limit_is_retryable_and_honours_retry_after():
    with pytest.raises(RetryableError) as exc:
        call(client_returning(429, json={}, headers={"retry-after": "7"}))
    assert exc.value.retry_after == 7.0


def test_a_spent_daily_quota_is_fatal():
    with pytest.raises(FatalError, match="daily"):
        call(client_returning(429, json={}, headers={"x-ratelimit-remaining-requests-day": "0"}))


@pytest.mark.parametrize("status", [401, 402, 400])
def test_client_errors_are_fatal(status):
    # 402 is "add billing": retrying it on every posting would be pointless.
    with pytest.raises(FatalError):
        call(client_returning(status, json={"message": "no"}))


def test_server_errors_are_retryable():
    with pytest.raises(RetryableError):
        call(client_returning(503, json={}))


def test_a_truncated_answer_is_not_accepted():
    with pytest.raises(RetryableError, match="incomplete"):
        call(client_returning(json=ok_body(finish="length")))


def test_a_response_without_usage_is_rejected():
    body = ok_body()
    del body["usage"]
    with pytest.raises(RetryableError, match="malformed"):
        call(client_returning(json=body))


def test_the_limiter_is_asked_before_every_call():
    class CountingLimiter:
        count = 0

        def acquire(self):
            self.count += 1

    client = client_returning(json=ok_body())
    client.limiter = CountingLimiter()
    call(client)
    call(client)
    assert client.limiter.count == 2
