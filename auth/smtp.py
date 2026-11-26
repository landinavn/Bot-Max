"""TLS-only SMTP adapter. A completed send means server acceptance, not inbox delivery."""
import asyncio
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
import math
import os
import re
import smtplib
import ssl
import threading

from accounts.models import normalize_email
from .errors import DeliveryFailed


class DeliveryUncertain(Exception):
    """Submission may have been accepted. Do not automatically retry."""


def header(value, maximum):
    if not isinstance(value,str) or not 1<=len(value)<=maximum or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('Invalid email header')
    value.encode('utf8')
    return value


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    from_email: str
    username: str = field(default='',repr=False)
    password: str = field(default='',repr=False)
    from_name: str = 'Нейрофото'
    tls_mode: str = 'starttls'
    timeout: float = 5

    def __post_init__(self):
        if not isinstance(self.host,str) or not re.fullmatch(r'[A-Za-z0-9.:-]{1,253}',self.host):
            raise ValueError('Invalid SMTP host')
        if type(self.port) is not int or not 1<=self.port<=65535 or self.tls_mode not in ('tls','starttls'):
            raise ValueError('SMTP requires TLS or STARTTLS')
        object.__setattr__(self,'from_email',normalize_email(self.from_email))
        header(self.from_name,100)
        if not isinstance(self.username,str) or not isinstance(self.password,str) or bool(self.username)!=bool(self.password):
            raise ValueError('SMTP credentials must be supplied together')
        if self.username:header(self.username,254)
        if isinstance(self.timeout,bool) or not isinstance(self.timeout,(int,float)) or not math.isfinite(self.timeout) or not 0<self.timeout<=30:
            raise ValueError('Invalid SMTP timeout')

    @classmethod
    def from_env(cls):
        return cls(host=os.environ['SMTP_HOST'],port=int(os.environ['SMTP_PORT']),
            from_email=os.environ['SMTP_FROM_EMAIL'],username=os.getenv('SMTP_USERNAME',''),
            password=os.getenv('SMTP_PASSWORD',''),from_name=os.getenv('SMTP_FROM_NAME','Нейрофото'),
            tls_mode=os.getenv('SMTP_TLS_MODE','starttls'),timeout=float(os.getenv('SMTP_TIMEOUT','5')))


class SMTPEmailSender:
    def __init__(self,settings):
        self.settings=settings
        # At most two outstanding thread operations, including cancelled/timeout submissions.
        self._slots=threading.BoundedSemaphore(2)

    async def send(self,message):
        if not self._slots.acquire(blocking=False):
            raise DeliveryFailed()  # Not queued, no network operation was started.
        try:
            future=asyncio.get_running_loop().run_in_executor(None,self._run,message)
        except BaseException:
            self._slots.release()
            raise
        # Cancellation of the coroutine must not release a slot while its thread still sends.
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        await asyncio.shield(future)

    def _run(self,message):
        connection=None;submitting=False;accepted=False
        try:
            recipient=normalize_email(message.recipient)
            subject=header(message.subject,200)
            if not isinstance(message.body,str) or len(message.body)>10000:
                raise ValueError('Invalid email body')
            mail=EmailMessage()
            s=self.settings
            mail['From']=Address(display_name=s.from_name,addr_spec=s.from_email)
            mail['To']=Address(addr_spec=recipient)
            mail['Subject']=subject
            mail.set_content(message.body,charset='utf-8',cte='quoted-printable')
            context=ssl.create_default_context()  # CERT_REQUIRED + hostname checks; never overridden.
            if s.tls_mode=='tls':
                connection=smtplib.SMTP_SSL(s.host,s.port,timeout=s.timeout,context=context)
                connection.ehlo()
            else:
                connection=smtplib.SMTP(s.host,s.port,timeout=s.timeout)
                connection.ehlo()
                connection.starttls(context=context)  # Fails closed if unavailable.
                connection.ehlo()
            if s.username:connection.login(s.username,s.password)
            submitting=True
            refused=connection.send_message(mail,from_addr=s.from_email,to_addrs=[recipient])
            if refused:raise DeliveryFailed()
            accepted=True
        except DeliveryFailed:
            raise
        except (smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused,smtplib.SMTPDataError):
            raise DeliveryFailed() from None
        except Exception:
            if submitting and not accepted:
                raise DeliveryUncertain() from None
            raise DeliveryFailed() from None
        finally:
            if connection is not None:
                try:connection.close()  # Never retry send or let QUIT failure change proven acceptance.
                except Exception:pass
            self._slots.release()
