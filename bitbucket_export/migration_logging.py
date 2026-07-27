"""Helpers for appending project-level migration logs."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def default_migration_log_path(archive_dir: Path | None) -> Path | None:
    """Return the log path for an archive: inside the archive it describes.

    It used to sit in the parent directory, from when the output directory was chosen
    by hand and its parent was the per-repository project folder. With archive paths
    derived as ``<root>/<workspace>/<repo>``, the parent is the workspace, so every
    repository in a workspace shared one log.
    """

    if archive_dir is None:
        return None
    return archive_dir / "migration.log"


def describe_tool_revision() -> str:
    """Describe the git revision of the exporter tooling."""

    try:
        revision = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status_output = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "Code revision: unknown"

    cleanliness = "dirty" if status_output else "clean"
    return f"Code revision: {revision} ({cleanliness})"


def append_migration_log(
    log_path: Path | None,
    argv: Sequence[str],
    summary_lines: Sequence[str],
    status: str,
) -> None:
    """Append a structured record of a migration command run."""

    if log_path is None:
        return

    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    command = shlex.join(argv)
    rendered_summary = "\n".join(f"- {line}" for line in summary_lines)
    revision_line = describe_tool_revision()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        if log_file.tell() != 0:
            log_file.write("\n")
        log_file.write(f"## {timestamp}\n")
        log_file.write(f"Status: {status}\n")
        log_file.write(f"{revision_line}\n")
        log_file.write(f"Workdir: {os.getcwd()}\n")
        log_file.write("Command:\n")
        log_file.write(f"`{command}`\n")
        if summary_lines:
            log_file.write("Summary:\n")
            log_file.write(f"{rendered_summary}\n")
