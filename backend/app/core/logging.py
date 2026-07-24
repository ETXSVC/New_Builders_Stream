"""Process-wide logging setup.

Plain key-value lines to stdout, not JSON: the deployment target is a
single Docker Compose host where the consumer is `docker logs` + grep —
JSON logging would add a dependency (structlog / python-json-logger) with
no consumer to read it. Called once from app/main.py at import time and
from app/scheduler.py's __main__ (which also silences APScheduler's
"no handlers could be found" noise). The Dramatiq CLI configures its own
worker logging and is deliberately left alone.

force=True so this wins over any handler a library (uvicorn imports the
app after installing its own handlers) may have installed first —
without it, basicConfig silently no-ops and app loggers stay unrouted.
"""
import logging

from app.config import settings


def configure_logging() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
