"""The run log: one complete, timestamped file per run."""

from __future__ import annotations

import logging

from bitbucket_export.migration_logging import start_run_log


def test_run_log_captures_header_and_subsequent_output(tmp_path):
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    log_path = start_run_log(tmp_path)
    try:
        logging.info("hello from the run")
        logging.warning("something worth noting")
    finally:
        for handler in root.handlers[:]:
            if isinstance(handler, logging.FileHandler) and handler.baseFilename == str(log_path):
                root.removeHandler(handler)
                handler.close()
        root.setLevel(previous_level)

    assert log_path.parent == tmp_path / "logs"
    assert log_path.name.startswith("migration-")

    content = log_path.read_text(encoding="utf-8")
    assert "Command:" in content
    assert "Workdir:" in content
    assert "Code revision:" in content
    assert "hello from the run" in content
    assert "WARNING something worth noting" in content
