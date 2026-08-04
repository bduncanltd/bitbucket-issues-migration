"""Minimal Bitbucket Cloud REST API 2.0 client.

Authentication uses an Atlassian API token (app passwords are removed from
Bitbucket Cloud as of 2026-07-28). The token is combined with the account email in an
HTTP Basic ``Authorization`` header, exactly the way Bitbucket documents API-token use.

Required token scopes: ``read:issue:bitbucket`` and ``read:repository:bitbucket``
(``read:account`` helps resolve user display names).
"""

from __future__ import annotations

import base64
import json
import logging
import time
import typing
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_ROOT = "https://api.bitbucket.org/2.0"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_PAGE_LENGTH = 50
USER_AGENT = "bitbucket-issue-archive-api/1.0"

# An export fires hundreds of requests in quick succession, which trips transient
# faults: Bitbucket's edge sheds bursts by resetting connections, and 429/5xx come and
# go. One blip must not kill a long run, so requests retry with doubling backoff.
MAX_ATTEMPTS = 3
RETRY_INITIAL_DELAY_SECONDS = 2.0
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class _StripAuthOnRedirect(HTTPRedirectHandler):
    """Follow redirects but drop the Authorization header when the host changes.

    Attachment downloads redirect from api.bitbucket.org to a signed S3 URL that
    carries its own credentials in the query string. Forwarding our Basic auth to
    S3 makes it reject the request, so we remove it whenever we cross hosts.
    """

    @typing.override
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is None:
            return None

        original_host = req.host
        if new_request.host != original_host:
            new_request.headers = {
                key: value for key, value in new_request.headers.items() if key.lower() != "authorization"
            }
            new_request.unredirected_hdrs.pop("Authorization", None)
        return new_request


class BitbucketClient:
    """Talks to a single Bitbucket repository's issue tracker over the REST API."""

    def __init__(
        self,
        workspace: str,
        repo_slug: str,
        email: str,
        token: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace = workspace
        self.repo_slug = repo_slug
        self.timeout = timeout
        self.repo_url = f"{API_ROOT}/repositories/{workspace}/{repo_slug}"
        auth_bytes = f"{email}:{token}".encode()
        self._auth_header = f"Basic {base64.b64encode(auth_bytes).decode('ascii')}"
        self._opener = build_opener(_StripAuthOnRedirect())

    def _request(self, url: str, accept: str = "application/json") -> Request:
        return Request(
            url,
            headers={
                "Authorization": self._auth_header,
                "Accept": accept,
                "User-Agent": USER_AGENT,
            },
        )

    def _fetch(self, request: Request, url: str) -> bytes:
        """Open ``request`` and read the body, retrying transient failures."""

        attempt = 1
        delay = RETRY_INITIAL_DELAY_SECONDS
        while True:
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return response.read()
            # URLError, ConnectionResetError, and timeouts all derive from OSError.
            except OSError as error:
                fault = _transient_fault(error)
                if fault is None or attempt == MAX_ATTEMPTS:
                    raise
                reason, server_wait = fault
            wait = max(delay, server_wait)
            logging.warning(
                f"{reason} for GET {url}; retrying in {wait:.0f}s (attempt {attempt} of {MAX_ATTEMPTS}) ..."
            )
            time.sleep(wait)
            attempt += 1
            delay *= 2

    def get_json(self, url: str) -> dict:
        request = self._request(url)
        try:
            body = self._fetch(request, url)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise BitbucketApiError(f"GET {url} failed: HTTP {error.code} {detail}") from error
        # Anything still failing after the retries: connection resets, DNS, timeouts.
        except OSError as error:
            raise BitbucketApiError(f"GET {url} failed: {error}") from error
        return json.loads(body.decode("utf-8"))

    def paginate(self, path: str, page_length: int = DEFAULT_PAGE_LENGTH) -> Iterator[dict]:
        """Yield every object from a paginated collection under this repository."""

        yield from self.paginate_url(f"{self.repo_url}/{path}?pagelen={page_length}")

    def paginate_url(self, url: str) -> Iterator[dict]:
        """Yield every object from a paginated collection at an absolute URL, following ``next`` links."""

        next_url: str | None = url
        while next_url:
            payload = self.get_json(next_url)
            yield from payload.get("values", [])
            next_url = payload.get("next")

    def fetch_user(self, account_id: str) -> dict:
        """Fetch a user's public profile by Atlassian account id.

        Used to resolve users who are only ever @-mentioned in issue text and so never
        appear in any issue, comment, or change record.
        """

        return self.get_json(f"{API_ROOT}/users/{account_id}")

    def fetch_bytes(self, url: str) -> bytes:
        """Fetch a binary resource into memory.

        Returns bytes rather than writing to a path because the caller stores assets
        content-addressed, so it needs to hash the body before it knows the filename.
        """

        request = self._request(url, accept="*/*")
        try:
            return self._fetch(request, url)
        # HTTPError and URLError both derive from OSError, as does a socket timeout.
        except OSError as error:
            raise BitbucketApiError(f"Download {url} failed: {error}") from error


def _transient_fault(error: OSError) -> tuple[str, float] | None:
    """Describe a retryable failure as (reason, server-requested wait), or None if fatal.

    HTTP errors are only worth retrying for throttling and server-side statuses; a 404
    will be a 404 next time too. Everything else at the OS level — resets, DNS, timeouts
    — is transient far more often than not.
    """

    if isinstance(error, HTTPError):
        if error.code not in RETRYABLE_HTTP_STATUSES:
            return None
        retry_after = (error.headers or {}).get("Retry-After")
        wait = float(retry_after) if retry_after and retry_after.isdigit() else 0.0
        return f"HTTP {error.code}", wait
    return str(getattr(error, "reason", None) or error), 0.0


class BitbucketApiError(RuntimeError):
    """Raised when the Bitbucket API returns an error response."""


def read_token(token: str | None, token_file: Path | None) -> str:
    """Resolve an API token from an explicit value or a file."""

    if token:
        return token.strip()
    if token_file is not None:
        return token_file.read_text(encoding="utf-8").strip()
    raise BitbucketApiError("No API token provided. Pass --token or --token-file.")
