"""Full preflight tracebacks for journalctl, with configured secrets redacted."""
import logging
import traceback
from urllib.parse import unquote, urlsplit


def secrets_from(settings, providers):
    values = [getattr(settings,'kling_key',None), getattr(settings,'tts_key',None)]
    config = getattr(providers,'config',None)
    for key in ('token','image_api_key','piapi_key','ttapi_key','storage_access_key_id','storage_secret_access_key'):
        values.append(getattr(config,key,None))
    proxy = getattr(config,'openai_proxy_url',None)
    if proxy:
        values.append(proxy)
        try:
            parsed = urlsplit(proxy)
            values.extend([unquote(parsed.username or ''), unquote(parsed.password or '')])
        except ValueError:
            pass  # Invalid proxy settings must not hide the original traceback.
    return sorted({v for v in values if isinstance(v,str) and v},key=len,reverse=True)


class RedactedTraceback(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = secrets

    def clean(self, value):
        for secret in self.secrets:
            value = value.replace(secret,'[REDACTED]')
        return value

    def filter(self, record):
        record.msg = self.clean(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self.clean(''.join(traceback.format_exception(*record.exc_info)))
            # No raw secret-bearing exception object is forwarded to parent handlers.
            record.exc_info = None
        return True


def log_preflight_failure(logger, job_id, error, settings, providers):
    sanitizer = RedactedTraceback(secrets_from(settings,providers))
    logger.addFilter(sanitizer)
    try:
        logger.exception('Preflight failed: %s; job=%s', error, job_id)
    finally:
        logger.removeFilter(sanitizer)
