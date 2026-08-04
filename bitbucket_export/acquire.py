"""Build a complete archive from the Bitbucket REST API in a single pass.

The order here is deliberate:

1. Validate credentials *and* the inline-image session before touching the network in
   earnest, so a run that cannot finish fails in seconds rather than an hour in.
2. List the issues, then fetch in full only the ones that are new or whose
   ``updated_on`` moved, normalising into the :mod:`model` schema.
3. Discover every inline image by parsing the collected markdown — no HTML rendering
   involved, which is what lets acquisition finish in one pass.
4. Download attachments (API token) and inline images (browser session), skipping
   anything the archive already holds.
5. Write the manifest.

Re-running against an existing archive is incremental in both dimensions that cost
time: unchanged issues are not re-walked, and stored assets are not re-downloaded. A
re-run to retry a handful of failed images therefore costs a few list calls, not a
full traversal of the tracker.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .asset_store import AssetStore, derive_url_filename
from .bitbucket_client import BitbucketApiError, BitbucketClient
from .inline_images import (
    DEFAULT_CONCURRENCY,
    DEFAULT_REQUEST_TIMEOUT_MS,
    InlineImageSessionError,
    ensure_session,
    fetch_images,
)
from .markdown_images import iter_image_urls
from .model import (
    MENTION_RE,
    ORIGIN_ATTACHMENT,
    ORIGIN_INLINE_IMAGE,
    SCHEMA_VERSION,
    Archive,
    Attachment,
    Comment,
    Content,
    HistoryEntry,
    Issue,
    Repository,
    TrackerDefinition,
    User,
)


@dataclass
class AcquireOptions:
    archive_dir: Path
    repository: Repository
    title: str = ""
    auth_state_path: Path | None = None
    skip_inline_images: bool = False
    concurrency: int = DEFAULT_CONCURRENCY
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    max_issues: int | None = None
    issue_ids: set[int] = field(default_factory=set)
    refresh: bool = False


@dataclass
class AcquireResult:
    archive: Archive
    manifest_path: Path
    summary: list[str]
    failed_images: int = 0
    failed_attachments: int = 0


def acquire(client: BitbucketClient, options: AcquireOptions) -> AcquireResult:
    """Fetch everything for one repository into ``options.archive_dir``."""

    # Fail fast: an unusable image session must stop the run now, not after the walk.
    auth_state_path = options.auth_state_path
    if not options.skip_inline_images:
        if auth_state_path is None:
            raise InlineImageSessionError(
                "No auth-state path given, so inline images cannot be fetched. "
                "Pass --auth-state, or --skip-inline-images to opt out deliberately."
            )
        ensure_session(auth_state_path)

    previous = _load_previous(options)
    store = AssetStore(
        archive_dir=options.archive_dir,
        assets=previous.assets if previous else None,
        asset_index=previous.asset_index if previous else None,
    )

    archive = Archive(
        repository=options.repository,
        title=options.title or options.repository.full_name,
        generated_at=datetime.now(UTC).isoformat(),
    )

    # Seed from the previous manifest so a partial run (--issue-id, --max-issues) tops
    # the archive up instead of replacing it with just the issues it happened to fetch.
    issues_by_id: dict[int, Issue] = {issue.id: issue for issue in previous.issues} if previous else {}
    users: dict[str, User] = dict(previous.users) if previous else {}

    logging.info(f"Listing issues in {options.repository.full_name} ...")
    raw_issues = _select_issues(client, options)

    stale = _issues_needing_fetch(raw_issues, issues_by_id, previous, options)
    reused = len(raw_issues) - len(stale)
    if reused:
        logging.info(f"Found {len(raw_issues)} issues; {reused} unchanged since last run, fetching {len(stale)}.")
    else:
        logging.info(f"Found {len(raw_issues)} issues; fetching comments, history, and attachments ...")

    pending_attachments: list[tuple[Attachment, str]] = []
    total = len(stale)

    for index, raw_issue in enumerate(stale, start=1):
        issue_id = raw_issue["id"]
        logging.info(f"  [{index}/{total}] issue #{issue_id}: {raw_issue.get('title') or ''}")
        issues_by_id[issue_id] = _build_issue(client, raw_issue, options.repository, users)

    # Attachments come from every issue in the archive, not just the re-fetched ones, so
    # a previously failed download is retried without needing its issue re-walked.
    for issue in issues_by_id.values():
        for attachment in issue.attachments:
            if attachment.source_url:
                pending_attachments.append((attachment, f"issue-{issue.id}"))

    archive.issues = sorted(issues_by_id.values(), key=lambda issue: issue.id)
    archive.users = users
    _fetch_definitions(client, archive)
    _resolve_mentioned_users(client, archive.issues, users)

    _download_attachments(client, store, pending_attachments)
    failed_attachments = sum(1 for attachment, _ in pending_attachments if attachment.asset_id is None)

    # Discovery is offline and always runs, so the manifest records every image the
    # markdown references even when fetching is skipped. Only fetching is optional.
    referenced_images = _discover_inline_images(archive)
    if options.skip_inline_images:
        failed_images = _skip_inline_images(store, referenced_images)
    else:
        failed_images = _download_inline_images(store, referenced_images, options, auth_state_path)

    archive.assets = store.assets
    archive.asset_index = store.asset_index
    archive.unresolved_assets = store.unresolved_list()

    manifest_path = archive.save(options.archive_dir)

    summary = [
        f"Archived {len(archive.issues)} issues from {options.repository.full_name} into {options.archive_dir}",
        f"Issues fetched: {len(stale)} new or updated, {reused} reused unchanged",
        f"Comments: {sum(len(issue.comments) for issue in archive.issues)}",
        f"Assets stored: {store.stored_count} new, {store.reused_count} deduplicated, {len(store.assets)} total",
        f"Manifest: {manifest_path}",
    ]
    if failed_attachments:
        summary.append(f"Attachments that could not be downloaded: {failed_attachments}")
    if failed_images:
        summary.append(f"Inline images that could not be downloaded: {failed_images}")

    return AcquireResult(
        archive=archive,
        manifest_path=manifest_path,
        summary=summary,
        failed_images=failed_images,
        failed_attachments=failed_attachments,
    )


# ---- fetching ---------------------------------------------------------------


def _load_previous(options: AcquireOptions) -> Archive | None:
    if options.refresh:
        return None
    try:
        return Archive.load(options.archive_dir)
    except (FileNotFoundError, ValueError):
        return None


def _issues_needing_fetch(
    raw_issues: list[dict],
    stored_issues: dict[int, Issue],
    previous: Archive | None,
    options: AcquireOptions,
) -> list[dict]:
    """Which issues must be fetched in full.

    Listing issues is a handful of paginated calls, but each issue then costs three more
    (comments, changes, attachments). Bitbucket bumps ``updated_on`` whenever any of
    those change, so an issue whose timestamp matches the stored copy can be reused. That
    is what makes a re-run cheap: without it, retrying a few failed images would re-walk
    the entire tracker.
    """

    # Issues written by an older schema lack fields this version records, and nothing on
    # disk can supply them, so they have to be fetched again.
    outdated = previous is not None and previous.schema_version < SCHEMA_VERSION
    if outdated:
        logging.info(
            f"Archive was written by schema v{previous.schema_version}; "
            f"re-fetching all issues to capture fields added in v{SCHEMA_VERSION}."
        )

    # Ids the user named with --issue-id are always fetched. Forcing one issue is the
    # whole point of the flag, and the issue being unchanged on Bitbucket is the common
    # case when it is used — skipping it would make the flag a silent no-op.
    force = options.refresh or outdated
    return [raw for raw in raw_issues if force or raw["id"] in options.issue_ids or _needs_fetch(raw, stored_issues)]


def _needs_fetch(raw_issue: dict, stored_issues: dict[int, Issue]) -> bool:
    """True when an issue is new or changed since the stored copy."""

    stored = stored_issues.get(raw_issue["id"])
    if stored is None:
        return True
    updated_on = raw_issue.get("updated_on")
    # A missing timestamp on either side means we cannot prove it is unchanged.
    return not updated_on or not stored.updated_on or stored.updated_on != updated_on


def _select_issues(client: BitbucketClient, options: AcquireOptions) -> list[dict]:
    raw_issues = sorted(client.paginate("issues"), key=lambda issue: issue["id"])
    if options.issue_ids:
        raw_issues = [issue for issue in raw_issues if issue["id"] in options.issue_ids]
    if options.max_issues is not None:
        raw_issues = raw_issues[: options.max_issues]
    return raw_issues


def _build_issue(
    client: BitbucketClient,
    raw_issue: dict,
    repository: Repository,
    users: dict[str, User],
) -> Issue:
    issue_id = raw_issue["id"]
    issue = Issue(
        id=issue_id,
        title=raw_issue.get("title") or "",
        state=raw_issue.get("state"),
        kind=raw_issue.get("kind"),
        priority=raw_issue.get("priority"),
        reporter=_intern_user(users, raw_issue.get("reporter")),
        assignee=_intern_user(users, raw_issue.get("assignee")),
        created_on=raw_issue.get("created_on"),
        updated_on=raw_issue.get("updated_on"),
        edited_on=raw_issue.get("edited_on"),
        milestone=_named(raw_issue.get("milestone")),
        component=_named(raw_issue.get("component")),
        version=_named(raw_issue.get("version")),
        url=f"{repository.url}/issues/{issue_id}",
        votes=raw_issue.get("votes") or 0,
        watches=raw_issue.get("watches") or 0,
        content=_content(raw_issue.get("content")),
    )

    for raw_comment in client.paginate(f"issues/{issue_id}/comments"):
        issue.comments.append(
            Comment(
                id=raw_comment["id"],
                user=_intern_user(users, raw_comment.get("user")),
                created_on=raw_comment.get("created_on"),
                updated_on=raw_comment.get("updated_on"),
                deleted=bool(raw_comment.get("deleted")),
                content=_content(raw_comment.get("content")),
            )
        )
    issue.comments.sort(key=lambda comment: (comment.created_on or "", comment.id))

    comment_ids = {comment.id for comment in issue.comments}
    raw_changes = list(client.paginate(f"issues/{issue_id}/changes"))
    for raw_change in raw_changes:
        issue.history.extend(_expand_change(raw_change, comment_ids, users))
    issue.history.sort(key=lambda entry: (entry.created_on or "", entry.field_name))

    uploaders = _attachment_uploaders(raw_changes, users)
    for raw_attachment in client.paginate(f"issues/{issue_id}/attachments"):
        filename = raw_attachment.get("name") or "attachment"
        issue.attachments.append(
            Attachment(
                filename=filename,
                source_url=_self_href(raw_attachment),
                user=uploaders.get(filename),
            )
        )

    return issue


def _expand_change(raw_change: dict, comment_ids: set[int], users: dict[str, User]) -> Iterator[HistoryEntry]:
    """One API change record carries several field deltas; emit one entry each."""

    change_id = raw_change.get("id")
    # A change and its accompanying comment share an id, so we can cross-link them.
    comment_id = change_id if change_id in comment_ids else None
    user = _intern_user(users, raw_change.get("user"))
    created_on = raw_change.get("created_on")

    for field_name, delta in (raw_change.get("changes") or {}).items():
        yield HistoryEntry(
            field_name=field_name,
            old=(delta or {}).get("old"),
            new=(delta or {}).get("new"),
            user=user,
            created_on=created_on,
            comment_id=comment_id,
        )


def _attachment_uploaders(raw_changes: Iterable[dict], users: dict[str, User]) -> dict[str, str]:
    uploaders: dict[str, str] = {}
    for raw_change in raw_changes:
        delta = (raw_change.get("changes") or {}).get("attachment")
        if not delta:
            continue
        new_name = delta.get("new")
        if new_name:
            user_key = _intern_user(users, raw_change.get("user"))
            if user_key:
                uploaders.setdefault(new_name, user_key)
    return uploaders


# ---- assets -----------------------------------------------------------------


def _download_attachments(
    client: BitbucketClient,
    store: AssetStore,
    pending: list[tuple[Attachment, str]],
) -> None:
    wanted = []
    for attachment, referenced_by in pending:
        if store.has_url(attachment.source_url):
            attachment.asset_id = store.asset_index[attachment.source_url]
        else:
            # The bytes are not stored (or were evicted after going missing on disk), so
            # any asset id from a previous run points at nothing. Clear it now: if the
            # download below fails, the attachment must count as failed rather than keep
            # a dangling reference that looks resolved.
            attachment.asset_id = None
            wanted.append((attachment, referenced_by))

    if not wanted:
        return

    total = len(wanted)
    logging.info(f"Downloading {total} attachments ...")
    for index, (attachment, referenced_by) in enumerate(wanted, start=1):
        url = attachment.source_url
        try:
            body = client.fetch_bytes(url)
        except BitbucketApiError as error:
            store.mark_unresolved(url, str(error), ORIGIN_ATTACHMENT, referenced_by)
            logging.warning(f"  [{index}/{total}] {attachment.filename} ({referenced_by}): FAILED ({error})")
            continue
        asset = store.add_bytes(
            body=body,
            source_url=url,
            filename=attachment.filename,
            origin=ORIGIN_ATTACHMENT,
        )
        attachment.asset_id = asset.id
        logging.info(f"  [{index}/{total}] {attachment.filename} ({referenced_by}): ok, {len(body)} bytes")


def _discover_inline_images(archive: Archive) -> dict[str, str]:
    """Parse all collected markdown for inline images; returns url -> where it appears.

    Pure string work over data already in memory, which is why the whole archive can be
    acquired in one pass rather than rendering a site to find out what is missing.
    """

    referenced_by: dict[str, str] = {}
    for issue in archive.issues:
        _record_image_refs(issue.content, f"issue-{issue.id}", referenced_by)
        for comment in issue.comments:
            _record_image_refs(comment.content, f"issue-{issue.id}#comment-{comment.id}", referenced_by)
    return referenced_by


def _skip_inline_images(store: AssetStore, referenced_by: dict[str, str]) -> int:
    skipped = 0
    for url, location in referenced_by.items():
        if store.has_url(url):
            continue
        store.mark_unresolved(url, "inline image fetching was skipped", ORIGIN_INLINE_IMAGE, location)
        skipped += 1
    if skipped:
        logging.info(f"Inline images: {skipped} referenced but not fetched (--skip-inline-images).")
    return skipped


def _download_inline_images(
    store: AssetStore,
    referenced_by: dict[str, str],
    options: AcquireOptions,
    auth_state_path: Path,
) -> int:
    """Fetch every inline image the markdown references that we do not already hold."""

    wanted = [url for url in referenced_by if not store.has_url(url)]
    if not wanted:
        logging.info(f"Inline images: {len(referenced_by)} referenced, all already stored.")
        return 0

    logging.info(f"Inline images: {len(referenced_by)} referenced, fetching {len(wanted)} ...")
    completed = 0

    def report(result) -> None:
        nonlocal completed
        completed += 1
        line = f"  [{completed}/{len(wanted)}] {derive_url_filename(result.url)}"
        if result.ok:
            logging.info(f"{line}: ok")
        else:
            # The referenced-by tag is issue-{id}[#comment-{id}], which maps straight
            # onto Bitbucket's issue URL and comment anchor.
            location = referenced_by[result.url].removeprefix("issue-")
            logging.warning(f"{line}: FAILED ({result.error}) — {options.repository.url}/issues/{location}")

    results = fetch_images(
        urls=wanted,
        auth_state_path=auth_state_path,
        concurrency=options.concurrency,
        request_timeout_ms=options.request_timeout_ms,
        on_result=report,
    )

    failed = 0
    for result in results:
        if result.body is not None:
            store.add_bytes(
                body=result.body,
                source_url=result.url,
                filename=derive_url_filename(result.url),
                origin=ORIGIN_INLINE_IMAGE,
                content_type=result.content_type,
            )
        else:
            failed += 1
            store.mark_unresolved(
                result.url,
                result.error or "unknown error",
                ORIGIN_INLINE_IMAGE,
                referenced_by.get(result.url, ""),
            )

    return failed


def _record_image_refs(content: Content, location: str, referenced_by: dict[str, str]) -> None:
    content.image_urls = iter_image_urls(content.markdown)
    for url in content.image_urls:
        referenced_by.setdefault(url, location)


# ---- normalisation helpers --------------------------------------------------


def _content(raw_content: dict | None) -> Content:
    content = raw_content or {}
    return Content(markdown=content.get("raw") or "", markup=content.get("markup") or "markdown")


def _fetch_definitions(client: BitbucketClient, archive: Archive) -> None:
    """Record the tracker's components, milestones, and versions, including unused ones.

    Issues only reference the values they use, so the definitions are the only record
    of a component that exists but has not been applied to anything yet.
    """

    for path, target in (
        ("components", archive.components),
        ("milestones", archive.milestones),
        ("versions", archive.versions),
    ):
        target.clear()
        try:
            for raw in client.paginate(path):
                name = raw.get("name")
                if name:
                    target.append(TrackerDefinition(name=name, id=raw.get("id")))
        except BitbucketApiError as error:
            # A tracker with a feature disabled returns an error rather than an empty
            # list; that is not a reason to fail the export.
            logging.warning(f"Could not list {path}: {error}")
        target.sort(key=lambda entry: entry.name.lower())


def _resolve_mentioned_users(client: BitbucketClient, issues: list[Issue], users: dict[str, User]) -> None:
    """Capture the names of users who are only ever @-mentioned in issue text.

    A mentioned user who never reported, commented, or changed anything appears in no
    API record the export walks, so their ``@{account-id}`` would stay unresolvable
    forever once Bitbucket is gone. Discovery is offline over the collected markdown;
    only ids not already in the users table cost an API call, so re-runs are cheap.
    """

    mentioned: set[str] = set()
    for issue in issues:
        mentioned.update(MENTION_RE.findall(issue.content.markdown))
        for comment in issue.comments:
            mentioned.update(MENTION_RE.findall(comment.content.markdown))

    unknown = sorted(mentioned - users.keys())
    if not unknown:
        return

    logging.info(f"Resolving {len(unknown)} users known only from @-mentions ...")
    for account_id in unknown:
        try:
            raw_user = client.fetch_user(account_id)
        # A deleted account 404s; the mention stays raw rather than failing the export.
        except BitbucketApiError as error:
            logging.warning(f"Could not resolve mentioned user {account_id}: {error}")
            continue
        _intern_user(users, raw_user)


def _intern_user(users: dict[str, User], raw_user: dict | None) -> str | None:
    """Store the user once and return the key records should refer to."""

    if not raw_user:
        return None

    account_id = raw_user.get("account_id") or raw_user.get("uuid")
    nickname = raw_user.get("nickname")
    display_name = raw_user.get("display_name") or nickname or account_id or ""
    key = account_id or nickname or display_name
    if not key:
        return None

    existing = users.get(key)
    if existing is None:
        users[key] = User(
            key=key,
            display_name=display_name,
            account_id=account_id,
            nickname=nickname,
        )
    elif not existing.display_name and display_name:
        existing.display_name = display_name

    return key


def _named(value: dict | None) -> str | None:
    return (value or {}).get("name")


def _self_href(raw_attachment: dict) -> str:
    href = (((raw_attachment.get("links") or {}).get("self")) or {}).get("href")
    # Attachment self links arrive as a one-element list; issue/comment links are strings.
    if isinstance(href, list):
        return href[0] if href else ""
    return href or ""
