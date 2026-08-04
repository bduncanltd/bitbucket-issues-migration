"""Retry behavior of the inline-image fetcher."""

from bitbucket_export import inline_images
from bitbucket_export.inline_images import _fetch_one


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None):
        self.status = status
        self.headers = {"content-type": "image/png", **(headers or {})}
        self._body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def body(self) -> bytes:
        return self._body


class FakeRequestContext:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls = 0

    def get(self, url, fail_on_status_code, timeout):
        response = self.responses[self.calls]
        self.calls += 1
        return response


def test_retries_throttled_request_until_it_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(inline_images.time, "sleep", sleeps.append)
    context = FakeRequestContext([FakeResponse(429), FakeResponse(200, body=b"png-bytes")])

    result = _fetch_one(context, "https://bitbucket.org/x.png", request_timeout_ms=1000)

    assert result.ok
    assert result.body == b"png-bytes"
    assert context.calls == 2
    assert sleeps == [inline_images.RETRY_INITIAL_DELAY_SECONDS]


def test_persistent_throttling_fails_after_max_attempts(monkeypatch):
    monkeypatch.setattr(inline_images.time, "sleep", lambda _: None)
    context = FakeRequestContext([FakeResponse(429)] * inline_images.MAX_ATTEMPTS)

    result = _fetch_one(context, "https://bitbucket.org/x.png", request_timeout_ms=1000)

    assert not result.ok
    assert result.error == "HTTP 429"
    assert context.calls == inline_images.MAX_ATTEMPTS


def test_retry_waits_at_least_the_server_requested_delay(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(inline_images.time, "sleep", sleeps.append)
    context = FakeRequestContext(
        [FakeResponse(429, headers={"retry-after": "7"}), FakeResponse(200, body=b"png-bytes")]
    )

    result = _fetch_one(context, "https://bitbucket.org/x.png", request_timeout_ms=1000)

    assert result.ok
    assert sleeps == [7.0]


def test_non_retryable_status_fails_immediately(monkeypatch):
    monkeypatch.setattr(inline_images.time, "sleep", lambda _: None)
    context = FakeRequestContext([FakeResponse(404)])

    result = _fetch_one(context, "https://bitbucket.org/x.png", request_timeout_ms=1000)

    assert not result.ok
    assert result.error == "HTTP 404"
    assert context.calls == 1
