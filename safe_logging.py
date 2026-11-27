"""Fail-closed application/SDK diagnostics: no formatting of arbitrary content.

Only SafeEvent's allowlisted fields reach logging handlers. Legacy application
calls retain their source operation/line, never their message or arguments.
Import this before constructing network clients, including CLI/auxiliary clients.
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import secrets
import ssl
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_CORRELATION = ContextVar('safe_log_correlation', default=None)
_OPERATION = frozenset({'diagnostic', 'api_error', 'invalid_response', 'request_failed',
                        'tls_rejected', 'result_saved', 'delivery_failed', 'preflight_failed',
                        'queue_failed', 'payment_incident', 'started', 'completed'})
_PRODUCT = frozenset({'image', 'song', 'cartoon', 'photosession', 'animation', 'music_card',
                      'ded_moroz', 'studio', 'payment', 'analytics', 'max'})
_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]{0,95}$')
_SDK = frozenset({'maxapi', 'httpx', 'httpcore', 'aiohttp', 'asyncio', 'openai', 'botocore', 'boto3', 'urllib3'})


def error_code(error):
    # Do not stringify an exception, its response, cause, traceback or class name.
    if isinstance(error, (ssl.SSLError, ssl.CertificateError)):
        return 'tls_error'
    if isinstance(error, TimeoutError):
        return 'timeout'
    if isinstance(error, ConnectionError):
        return 'connection_error'
    if isinstance(error, ValueError):
        return 'invalid_input'
    return 'operation_failed' if error is not None else None


@dataclass(frozen=True)
class SafeEvent:
    """Built only from validated metadata; never keep original objects/arguments."""
    encoded: str


def _event(source, operation='diagnostic', *, stage=None, line=None, product=None,
           http_status=None, error=None, duration_ms=None):
    payload = {'event': operation if operation in _OPERATION else 'diagnostic',
               'source': source if isinstance(source, str) and _IDENTIFIER.fullmatch(source) else 'application',
               'correlation': _CORRELATION.get() or secrets.token_hex(8)}
    if isinstance(stage, str) and _IDENTIFIER.fullmatch(stage):
        payload['stage'] = stage
    if type(line) is int and 0 < line < 1000000:
        payload['line'] = line
    if isinstance(product, str) and product in _PRODUCT:
        payload['product'] = product
    if type(http_status) is int and 100 <= http_status <= 599:
        payload['http_status'] = http_status
    if type(duration_ms) is int and 0 <= duration_ms <= 86400000:
        payload['duration_ms'] = duration_ms
    code = error_code(error)
    if code:
        payload['error_code'] = code
    return SafeEvent(json.dumps(payload, ensure_ascii=True, sort_keys=True))


class _RecordFields(dict):
    """Logger.makeRecord adds extra after the factory: discard unknown fields."""
    allowed = frozenset(logging.LogRecord('', 0, '', 0, '', (), None).__dict__) | {'message', 'asctime'}

    def __setitem__(self, key, value):
        if key in self.allowed:
            super().__setitem__(key, value)


def _record_factory(previous):
    def factory(name, level, pathname, lineno, msg, args, exc_info, func=None, sinfo=None, **kwargs):
        if isinstance(msg, SafeEvent):
            encoded = msg.encoded
        else:
            # Unknown SDK/stdlib logs, including HTTP URLs/headers and tracebacks,
            # have no trusted schema. Retain only a known source family + severity.
            family = name.split('.', 1)[0] if isinstance(name, str) else ''
            name = family if family in _SDK else 'external'
            encoded = json.dumps({'event': 'external_diagnostic', 'source': name}, sort_keys=True)
        record = previous(name, level, pathname, lineno, encoded, (), None, func, None, **kwargs)
        record.exc_info = record.exc_text = record.stack_info = None
        if hasattr(record, 'taskName'): record.taskName = None
        record.__dict__ = _RecordFields({k: v for k, v in record.__dict__.items() if k in _RecordFields.allowed})
        return record
    factory._safe_logging_policy = True
    return factory


class SafeLogFilter(logging.Filter):
    """Strip arbitrary extra fields before handlers owned by the application."""
    def filter(self, record):
        standard = logging.LogRecord('', 0, '', 0, '', (), None).__dict__.keys()
        for key in list(record.__dict__):
            if key not in standard and key not in {'message', 'asctime', 'taskName'}:
                del record.__dict__[key]
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def install_safe_logging():
    previous = logging.getLogRecordFactory()
    if not getattr(previous, '_safe_logging_policy', False):
        logging.setLogRecordFactory(_record_factory(previous))
    # The record factory also protects handlers added later (e.g. pytest/caplog).
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SafeLogFilter) for f in handler.filters):
            handler.addFilter(SafeLogFilter())


class SafeLogger:
    def __init__(self, name):
        self.name = name
        self._logger = logging.getLogger(name)

    def event(self, operation, *, level=logging.INFO, **fields):
        frame = inspect.currentframe().f_back
        try:
            fields.setdefault('stage', frame.f_code.co_name)
            fields.setdefault('line', frame.f_lineno)
        finally:
            del frame
        self._logger.log(level, _event(self.name, operation, **fields), stacklevel=2)

    def _legacy(self, level, *discarded, **kwargs):
        if not self._logger.isEnabledFor(level):
            return
        frame = inspect.currentframe().f_back.f_back
        try:
            # Metadata comes from Python code, not caller-provided message/args.
            event = _event(self.name, stage=frame.f_code.co_name, line=frame.f_lineno,
                           error=sys.exc_info()[1] if level >= logging.ERROR else None)
        finally:
            del frame
        self._logger.log(level, event, stacklevel=3)

    def debug(self, *args, **kwargs): self._legacy(logging.DEBUG, *args, **kwargs)
    def info(self, *args, **kwargs): self._legacy(logging.INFO, *args, **kwargs)
    def warning(self, *args, **kwargs): self._legacy(logging.WARNING, *args, **kwargs)
    def error(self, *args, **kwargs): self._legacy(logging.ERROR, *args, **kwargs)
    def exception(self, *args, **kwargs): self._legacy(logging.ERROR, *args, **kwargs)
    def critical(self, *args, **kwargs): self._legacy(logging.CRITICAL, *args, **kwargs)


def get_logger(name):
    install_safe_logging()
    return SafeLogger(name)


@contextmanager
def correlation_scope():
    """Random operation correlation, independent of MAX/order/provider IDs."""
    token = _CORRELATION.set(secrets.token_hex(8))
    try:
        yield
    finally:
        _CORRELATION.reset(token)
