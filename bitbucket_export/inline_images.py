"""Fetch inline images using a saved, authenticated Bitbucket browser session.

Inline images pasted into issue markdown live on Bitbucket's asset server, which does
not accept API tokens — only a logged-in browser session. Playwright replays a session
saved once by ``prepare-auth``.

The session is validated *before* any fetching starts and a missing or unusable one is
a hard error. Continuing without it would produce an archive that looks complete but
silently omits every screenshot, and the omission would only surface later, once the
source may be gone.

Validation makes a real request, because a stale session is indistinguishable from a
good one on disk: the cookie file still parses and the expiry dates still look fine,
but Bitbucket answers every asset request with a 200 and an HTML sign-in or two-step
verification page. Checking only the file's shape lets that sail through and turns a
two-second failure into one that surfaces after the whole tracker has been walked.
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .bitbucket_client import MAX_ATTEMPTS, RETRY_INITIAL_DELAY_SECONDS, RETRYABLE_HTTP_STATUSES

DEFAULT_AUTH_STATE = Path.home() / ".cache" / "bitbucket-playwright" / "auth-state.json"
DEFAULT_LOGIN_URL = "https://bitbucket.org/account/signin/"
DEFAULT_CONCURRENCY = 2
DEFAULT_REQUEST_TIMEOUT_MS = 120_000
ACCEPTED_CONTENT_PREFIXES = ("image/", "application/octet-stream", "binary/octet-stream")

# Cheap authenticated endpoint on the same host that serves inline images. What it
# returns matters less than whether Bitbucket bounces us to a login flow.
SESSION_PROBE_URL = "https://bitbucket.org/!api/internal/user"
SESSION_PROBE_TIMEOUT_MS = 30_000
TWO_STEP_MARKER = "/two-step-verification/"
SIGNIN_MARKERS = ("/account/signin", "/login/", "id.atlassian.com")


class InlineImageSessionError(RuntimeError):
    """Raised when the saved Bitbucket session is missing, malformed, or expired."""


@dataclass(frozen=True)
class FetchedImage:
    url: str
    body: bytes | None
    content_type: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.body is not None


def import_sync_playwright():
    try:
        sync_api = importlib.import_module("playwright.sync_api")
    except ImportError as error:
        raise InlineImageSessionError(
            "Playwright is not installed, so inline images cannot be fetched.\n"
            "Install it with:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from error
    return sync_api.sync_playwright


def ensure_session(auth_state_path: Path, verify: bool = True) -> Path:
    """Validate the saved session up front, or raise. Never returns a degraded state."""

    if not auth_state_path.exists():
        raise InlineImageSessionError(
            f"No saved Bitbucket session at {auth_state_path}.\n"
            "Inline images are hosted on Bitbucket's asset server and need a logged-in\n"
            "browser session. Create one once with:\n"
            "  python -m bitbucket_export --prepare-auth\n"
            "Then re-run the export."
        )

    try:
        state = json.loads(auth_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InlineImageSessionError(f"Saved session at {auth_state_path} is unreadable: {error}") from error

    if not state.get("cookies"):
        raise InlineImageSessionError(
            f"Saved session at {auth_state_path} contains no cookies.\n"
            "Re-create it with: python -m bitbucket_export --prepare-auth"
        )

    if verify:
        verify_session_live(auth_state_path)

    return auth_state_path


def verify_session_live(auth_state_path: Path) -> None:
    """Make one request and raise if Bitbucket bounces it to a login flow.

    Cookie expiry dates are no guide — Bitbucket invalidates sessions server-side and
    then answers asset requests with ``200 text/html`` sign-in pages, which would be
    saved as if they were screenshots.
    """

    sync_playwright = import_sync_playwright()
    with sync_playwright() as playwright:
        request_context = playwright.request.new_context(storage_state=str(auth_state_path))
        try:
            response = request_context.get(
                SESSION_PROBE_URL,
                fail_on_status_code=False,
                timeout=SESSION_PROBE_TIMEOUT_MS,
            )
            final_url = response.url
        # Network trouble is not proof of a bad session, so warn rather than block.
        except Exception as error:
            logging.warning(f"Could not verify the Bitbucket session ({error}). Continuing.")
            return
        finally:
            request_context.dispose()

    reason = _login_redirect_reason(final_url)
    if reason is None:
        # Anything that is not a login bounce — including an unexpected 404 if this
        # endpoint ever moves — is treated as fine. The probe exists to catch the one
        # common failure, not to become a new source of false ones.
        return

    raise InlineImageSessionError(
        f"The saved Bitbucket session at {auth_state_path} is no longer valid.\n"
        f"{reason}\n"
        "Every inline image would download as an HTML login page instead of a picture.\n\n"
        "Re-authorise with:\n"
        "  python -m bitbucket_export --prepare-auth\n"
        "then re-run the export. Already-downloaded assets are kept, so only the missing\n"
        "images are fetched."
    )


def _login_redirect_reason(final_url: str) -> str | None:
    """Describe why a probe URL looks like a login bounce, or None if it does not."""

    if TWO_STEP_MARKER in final_url:
        return "Bitbucket redirected to two-step verification, so it wants a 2FA code."
    if any(marker in final_url for marker in SIGNIN_MARKERS):
        return "Bitbucket redirected to its sign-in page, so the session has expired."
    return None


def prepare_auth(
    auth_state_path: Path = DEFAULT_AUTH_STATE,
    login_url: str = DEFAULT_LOGIN_URL,
    channel: str = "chromium",
) -> Path:
    """Open a browser, wait for a manual login, and save the session for later runs."""

    sync_playwright = import_sync_playwright()
    auth_state_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(login_url, wait_until="load")
        print()
        print(f"Opened login page: {login_url}")
        print("Sign in to Bitbucket in the browser window (including 2FA).")
        print("When you are fully logged in, press Enter here to save the session.")
        input()
        context.storage_state(path=str(auth_state_path))
        context.close()
        browser.close()

    print(f"Saved session to {auth_state_path}")
    return auth_state_path


def fetch_images(
    urls: Sequence[str],
    auth_state_path: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS,
    on_result=None,
) -> list[FetchedImage]:
    """Fetch every URL through the saved session. Raises if the session is unusable."""

    # Only the cheap on-disk checks here: the acquire pass live-verifies the session
    # once up front, and repeating the network probe for every batch of fetches would
    # add a round-trip without catching anything new.
    ensure_session(auth_state_path, verify=False)
    if not urls:
        return []

    worker_count = max(1, min(concurrency, len(urls)))
    batches = _split(urls, worker_count)
    results: list[FetchedImage] = []
    lock = threading.Lock()

    def run_batch(batch: Sequence[str]) -> list[FetchedImage]:
        return _fetch_batch(batch, auth_state_path, request_timeout_ms, on_result, lock)

    if worker_count == 1:
        results.extend(run_batch(batches[0]))
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="bb-inline-image") as executor:
            for batch_results in executor.map(run_batch, batches):
                results.extend(batch_results)

    by_url = {result.url: result for result in results}
    return [by_url[url] for url in urls if url in by_url]


def _fetch_batch(
    urls: Sequence[str],
    auth_state_path: Path,
    request_timeout_ms: int,
    on_result,
    lock: threading.Lock,
) -> list[FetchedImage]:
    sync_playwright = import_sync_playwright()
    results: list[FetchedImage] = []

    with sync_playwright() as playwright:
        request_context = playwright.request.new_context(storage_state=str(auth_state_path))
        try:
            for url in urls:
                result = _fetch_one(request_context, url, request_timeout_ms)
                results.append(result)
                if on_result is not None:
                    with lock:
                        on_result(result)
        finally:
            request_context.dispose()

    return results


def _fetch_one(request_context, url: str, request_timeout_ms: int) -> FetchedImage:
    attempt = 1
    delay = RETRY_INITIAL_DELAY_SECONDS
    while True:
        try:
            response = request_context.get(url, fail_on_status_code=False, timeout=request_timeout_ms)
        # Playwright raises bare Exceptions for transport faults; any of them is just a
        # failed image, recorded as unresolved rather than aborting the whole run.
        except Exception as error:
            return FetchedImage(url=url, body=None, content_type=None, error=str(error))

        # The asset host throttles bursts just like the API edge does, so 429/5xx get
        # the same retry policy as the API client rather than becoming failed images.
        if response.status in RETRYABLE_HTTP_STATUSES and attempt < MAX_ATTEMPTS:
            retry_after = response.headers.get("retry-after")
            wait = max(delay, float(retry_after) if retry_after and retry_after.isdigit() else 0.0)
            logging.warning(
                f"HTTP {response.status} for GET {url}; retrying in "
                f"{wait:.0f}s (attempt {attempt} of {MAX_ATTEMPTS}) ..."
            )
            time.sleep(wait)
            attempt += 1
            delay *= 2
            continue

        break

    if not response.ok:
        return FetchedImage(url=url, body=None, content_type=None, error=f"HTTP {response.status}")

    content_type = (response.headers.get("content-type") or "").lower().split(";")[0].strip()
    if content_type and not content_type.startswith(ACCEPTED_CONTENT_PREFIXES):
        return FetchedImage(
            url=url,
            body=None,
            content_type=content_type,
            error=f"Unexpected content type: {content_type}",
        )

    return FetchedImage(url=url, body=response.body(), content_type=content_type or None, error=None)


def _split(items: Sequence[str], count: int) -> list[list[str]]:
    batches: list[list[str]] = [[] for _ in range(count)]
    for index, item in enumerate(items):
        batches[index % count].append(item)
    return [batch for batch in batches if batch]
