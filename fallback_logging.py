"""Emergency Linux service policy: discard all untrusted process output.

No logger facade, LogRecord factory, regex redaction or message formatting.
Install before application/SDK imports, only in the bot service entry point.
The private, non-inheritable journal descriptor accepts fixed byte literals only.
"""
import os
import sys

_journal_fd = None
_EVENTS = {
    'starting': b'secure_fallback starting\n',
    'polling': b'secure_fallback polling_starting\n',
    'stopped': b'secure_fallback stopped\n',
    'fatal': b'secure_fallback fatal\n',
    'thread': b'secure_fallback thread_failed\n',
    'unraisable': b'secure_fallback unraisable\n',
}


def process_event(event):
    """No exception, URL, ID, arbitrary string or other value is serialized."""
    if _journal_fd is None or type(event) is not str:
        return
    payload = _EVENTS.get(event)
    if payload is not None:
        try:
            os.write(_journal_fd, payload)
        except OSError:
            pass  # Never fall back to an untrusted stream or error formatter.


def install_process_guard():
    """Irreversible for this service process; repeated installation is harmless.

    FD-level suppression includes print, sys.__stderr__, warnings, tracebacks,
    native writes and inherited child stdout/stderr. Existing data/files in the
    application are untouched. No runtime switch re-enables verbose output.
    """
    global _journal_fd
    if _journal_fd is not None:
        return
    try:
        journal_fd = os.dup(2)
        os.set_inheritable(journal_fd, False)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_fd, 1)
            os.dup2(null_fd, 2)
        finally:
            os.close(null_fd)
        _journal_fd = journal_fd
    except BaseException:
        # Setup failure must not continue with production's verbose logging.
        try:
            os.write(2, b'secure_fallback guard_failed\n')
        except BaseException:
            pass
        os._exit(78)

    import logging
    import threading

    logging.disable(sys.maxsize)
    logging.raiseExceptions = False
    sys.excepthook = lambda *_: process_event('fatal')
    threading.excepthook = lambda *_: process_event('thread')
    sys.unraisablehook = lambda *_: process_event('unraisable')
    process_event('starting')
