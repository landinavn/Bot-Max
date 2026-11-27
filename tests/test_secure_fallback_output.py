"""Isolated service processes; synthetic data, no DB, credentials or providers."""
import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEAK = 'SYNTHETIC_PRIVATE https://private.invalid/token user@example.invalid'


def run(code, tmp_path):
    # Do not inherit API credentials, Python startup hooks or production config.
    return subprocess.run([sys.executable, '-c', code], cwd=tmp_path,
        env={'PATH': os.defpath, 'PYTHONPATH': str(ROOT)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)


@pytest.mark.parametrize('output', [
    'print(secret)',
    'print(secret, file=sys.stderr)',
    'sys.__stdout__.write(secret); sys.__stdout__.flush()',
    'sys.__stderr__.write(secret); sys.__stderr__.flush()',
    'sys.stdout.buffer.write(secret.encode()); sys.stdout.buffer.flush()',
    'os.write(1, secret.encode()); os.write(2, secret.encode())',
    'warnings.warn(secret)',
    'logging.critical(secret, extra={"payload": secret}, stack_info=True)',
    'logging.getLogger(secret).addHandler(logging.StreamHandler(sys.__stderr__)); logging.getLogger(secret).error(secret)',
    'logging.disable(logging.NOTSET); logging.basicConfig(force=True); logging.error(secret)',
    'try:\n raise RuntimeError(secret)\nexcept Exception:\n logging.exception(secret)',
    'subprocess.run([sys.executable,"-c","import os; os.write(2,b\'child private\'); raise RuntimeError(\'child private\')"])',
    'faulthandler.dump_traceback(file=sys.__stderr__)',
])
def test_no_untrusted_output_reaches_journal(tmp_path, output):
    result = run('import os,sys,logging,warnings,subprocess,faulthandler\n'
        'from fallback_logging import install_process_guard, process_event\n'
        'install_process_guard()\n'
        f'secret={LEAK!r}\n' + output + '\nprocess_event("stopped")\n', tmp_path)
    assert result.returncode == 0
    assert result.stdout == b''
    assert result.stderr == b'secure_fallback starting\nsecure_fallback stopped\n'


@pytest.mark.parametrize('code,event,exit_code', [
    ('raise RuntimeError(secret)', 'fatal', 1),
    ('import threading\nt=threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError(secret)))\nt.start();t.join()', 'thread_failed', 0),
    ('import gc\nclass Bad:\n def __del__(self): raise RuntimeError(secret)\nb=Bad();del b;gc.collect()', 'unraisable', 0),
])
def test_exception_hooks_do_not_render_exception(tmp_path, code, event, exit_code):
    result = run('from fallback_logging import install_process_guard\n'
        f'install_process_guard()\nsecret={LEAK!r}\n' + code, tmp_path)
    assert result.returncode == exit_code
    assert result.stdout == b''
    assert result.stderr == f'secure_fallback starting\nsecure_fallback {event}\n'.encode()


def test_idempotent_policy_fixed_vocabulary_no_record_factory(tmp_path):
    result = run('''import logging, os, subprocess, sys
from fallback_logging import install_process_guard, process_event
factory=logging.getLogRecordFactory()
install_process_guard(); install_process_guard()
assert factory is logging.getLogRecordFactory()
process_event("https://private.invalid"); process_event(object())
process_event("polling"); process_event("stopped")
''', tmp_path)
    assert result.returncode == 0
    assert result.stdout == b''
    assert result.stderr == b'secure_fallback starting\nsecure_fallback polling_starting\nsecure_fallback stopped\n'


def test_guard_setup_failure_aborts_without_application_start(tmp_path):
    result = run('''import os
from fallback_logging import install_process_guard
def fail(*args): raise RuntimeError("private failure")
os.dup=fail
install_process_guard()
print("application started")
''', tmp_path)
    assert result.returncode == 78
    assert result.stdout == b''
    assert result.stderr == b'secure_fallback guard_failed\n'


def test_actual_bot_entrypoint_installs_before_config_failure(tmp_path):
    result = run('''import runpy
import config
def fail(*args, **kwargs): raise RuntimeError("synthetic private configuration")
config.Config.from_env=fail
runpy.run_module("bot", run_name="__main__")
''', tmp_path)
    assert result.returncode == 1
    assert result.stdout == b''
    assert result.stderr == b'secure_fallback starting\nsecure_fallback fatal\n'


def test_bot_import_has_no_output_policy_side_effects(tmp_path):
    result = run('import bot\nprint("library import intact")', tmp_path)
    assert result.returncode == 0
    assert result.stdout == b'library import intact\n'
    assert result.stderr == b''


def test_guard_is_before_first_application_or_sdk_import():
    tree = ast.parse((ROOT/'bot.py').read_text())
    guard = tree.body[1]
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == "__name__ == '__main__'"
    assert 'install_process_guard()' in ast.unparse(guard)
