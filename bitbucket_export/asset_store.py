"""Content-addressed storage for everything binary in an archive.

Blobs land at ``assets/<first-2-of-sha>/<sha><ext>``, so the same screenshot pasted
into a dozen issues is downloaded and stored once. The store owns both the ``assets``
table (id -> stored file) and the ``asset_index`` (original URL -> id) that the
manifest publishes, and it is the thing that makes re-runs cheap: ``has_url`` lets the
acquire pass skip any URL whose bytes are already on disk.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from .model import ASSETS_DIRNAME, Asset, UnresolvedAsset

SHARD_LENGTH = 2
UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]")


class AssetStore:
    """Writes blobs into an archive and tracks how they map back to Bitbucket URLs."""

    def __init__(
        self,
        archive_dir: Path,
        assets: dict[str, Asset] | None = None,
        asset_index: dict[str, str] | None = None,
    ) -> None:
        self.archive_dir = archive_dir
        self.assets: dict[str, Asset] = dict(assets or {})
        self.asset_index: dict[str, str] = dict(asset_index or {})
        self.unresolved: dict[str, UnresolvedAsset] = {}
        self.stored_count = 0
        self.reused_count = 0

    # ---- queries -----------------------------------------------------------

    def has_url(self, url: str) -> bool:
        """True when this URL's bytes are already stored and present on disk.

        Checked against the filesystem, not just the manifest, so an interrupted run
        that lost its manifest — or an archive whose files were partly deleted — heals
        on the next pass instead of silently claiming assets it no longer has.
        """

        asset_id = self.asset_index.get(url)
        if asset_id is None:
            return False
        asset = self.assets.get(asset_id)
        if asset is None:
            return False
        if not (self.archive_dir / asset.path).exists():
            self.asset_index.pop(url, None)
            self.assets.pop(asset_id, None)
            return False
        return True

    # ---- mutations ---------------------------------------------------------

    def add_bytes(
        self,
        body: bytes,
        source_url: str,
        filename: str,
        origin: str,
        content_type: str | None = None,
    ) -> Asset:
        """Store ``body`` (deduplicating by content) and point ``source_url`` at it."""

        digest = hashlib.sha256(body).hexdigest()
        clean_name = sanitize_filename(filename or derive_url_filename(source_url))
        asset = self.assets.get(digest)

        if asset is None:
            relative_path = self._relative_path(digest, clean_name)
            destination = self.archive_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            asset = Asset(
                id=digest,
                path=relative_path,
                size=len(body),
                sha256=digest,
                content_type=content_type or guess_content_type(clean_name),
                filename=clean_name,
            )
            self.assets[digest] = asset
            self.stored_count += 1
        else:
            self.reused_count += 1

        if origin not in asset.origins:
            asset.origins.append(origin)
        if source_url and source_url not in asset.source_urls:
            asset.source_urls.append(source_url)

        if source_url:
            self.asset_index[source_url] = digest
            self.unresolved.pop(source_url, None)

        return asset

    def mark_unresolved(self, url: str, reason: str, origin: str, referenced_by: str = "") -> None:
        """Record a reference we could not store, so gaps stay visible in the manifest."""

        entry = self.unresolved.get(url)
        if entry is None:
            entry = UnresolvedAsset(
                url=url,
                reason=reason,
                origin=origin,
                filename=derive_url_filename(url),
            )
            self.unresolved[url] = entry
        if referenced_by and referenced_by not in entry.referenced_by:
            entry.referenced_by.append(referenced_by)

    def unresolved_list(self) -> list[UnresolvedAsset]:
        return sorted(self.unresolved.values(), key=lambda entry: entry.url)

    # ---- internals ---------------------------------------------------------

    def _relative_path(self, digest: str, filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if len(suffix) > MAX_SUFFIX_LENGTH or not suffix[1:].isalnum():
            suffix = ""
        return f"{ASSETS_DIRNAME}/{digest[:SHARD_LENGTH]}/{digest}{suffix}"


MAX_SUFFIX_LENGTH = 10


def derive_url_filename(url: str) -> str:
    """The last path segment of a URL, percent-decoded."""

    return unquote(Path(urlparse(url).path).name)


def sanitize_filename(filename: str) -> str:
    sanitized = UNSAFE_FILENAME_RE.sub("_", filename).strip(" .")
    return sanitized or "asset"


def guess_content_type(filename: str) -> str | None:
    content_type, _ = mimetypes.guess_type(filename)
    return content_type
