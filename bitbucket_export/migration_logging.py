"""Log-file handling for export runs: one complete, timestamped log per run."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOGS_DIRNAME = "logs"


def start_run_log(archive_dir: Path) -> Path:
    """Mirror this run's entire log output into a new timestamped file in the archive.

    Every run gets its own file, so a retry never obscures what a previous attempt
    did. The file receives exactly what the console shows, preceded by a header
    recording the command, working directory, and code revision.
    """

    logs_dir = archive_dir / LOGS_DIRNAME
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"migration-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logging.getLogger().addHandler(handler)

    logging.info(f"Log file: {log_path}")
    logging.info(f"Command: {shlex.join([sys.executable, *sys.argv])}")
    logging.info(f"Workdir: {os.getcwd()}")
    logging.info(describe_tool_revision())
    return log_path


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
