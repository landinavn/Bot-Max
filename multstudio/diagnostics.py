"""Fail-closed preflight diagnostics; no job IDs, secret lists or tracebacks."""
import logging
from safe_logging import get_logger


def log_preflight_failure(logger, job_id, error, settings, providers):
    # Keep the API for recovery callers. The diagnostic never reads credentials.
    get_logger(logger.name).event('preflight_failed', level=logging.ERROR,
                                  product='studio', error=error)
