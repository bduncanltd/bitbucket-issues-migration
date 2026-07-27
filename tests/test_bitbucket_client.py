"""Retry behaviour: transient faults are retried, real errors are not."""

from __future__ import annotations

import io
from urllib.error import HTTPError, URLError

import pytest

from bitbucket_export import bitbucket_client
from bitbucket_export.bitbucket_client import MAX_ATTEMPTS, BitbucketApiError, BitbucketClient


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._body


class _ScriptedOpener:
    """Raises each scripted failure in turn, then answers successfully."""

    def __init__(self, failures: list[Exception], body: bytes = b'{"values": []}') -> None:
        self.failures = list(failures)
        self.body = body
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return _FakeResponse(self.body)


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(bitbucket_client.time, "sleep", slept.append)
    return slept


def _client(opener: _ScriptedOpener) -> BitbucketClient:
    client = BitbucketClient("ws", "repo", "you@example.com", "token")
    client._opener = opener
    return client


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://api.example/x", code, "boom", None, io.BytesIO(b"detail"))


def test_connection_reset_is_retried(no_sleep):
    opener = _ScriptedOpener([URLError(ConnectionResetError("forcibly closed"))])
    client = _client(opener)

    assert client.get_json("https://api.example/x") == {"values": []}
    assert opener.calls == 2
    assert len(no_sleep) == 1


def test_throttling_and_server_errors_are_retried(no_sleep):
    opener = _ScriptedOpener([_http_error(429), _http_error(503)])
    client = _client(opener)

    assert client.get_json("https://api.example/x") == {"values": []}
    assert opener.calls == 3


def test_gives_up_after_max_attempts(no_sleep):
    opener = _ScriptedOpener([URLError("reset")] * MAX_ATTEMPTS)
    client = _client(opener)

    with pytest.raises(BitbucketApiError, match="failed"):
        client.get_json("https://api.example/x")
    assert opener.calls == MAX_ATTEMPTS


def test_client_errors_are_not_retried(no_sleep):
    opener = _ScriptedOpener([_http_error(404)])
    client = _client(opener)

    with pytest.raises(BitbucketApiError, match="HTTP 404"):
        client.get_json("https://api.example/x")
    assert opener.calls == 1
    assert no_sleep == []


def test_fetch_bytes_retries_too(no_sleep):
    opener = _ScriptedOpener([URLError("reset")], body=b"png-bytes")
    client = _client(opener)

    assert client.fetch_bytes("https://api.example/a.png") == b"png-bytes"
    assert opener.calls == 2
