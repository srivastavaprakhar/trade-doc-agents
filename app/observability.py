"""run_id-threaded structured logging.

Every log line is one JSON object carrying the run_id, so a single shipment's
journey (upload -> extract -> validate -> route -> store -> query) can be traced
end-to-end with `grep <run_id> logs/pipeline.jsonl`. This is the POC-scale
stand-in for Langfuse + OpenTelemetry trace propagation.
"""

import json
import logging
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "pipeline.jsonl"

_logger = logging.getLogger("pipeline")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter("%(message)s"))
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(fh)
    _logger.addHandler(sh)


def log_event(run_id: str, event: str, **kwargs) -> None:
    _logger.info(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": run_id,
        "event": event,
        **kwargs,
    }, default=str))
