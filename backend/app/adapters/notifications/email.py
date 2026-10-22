"""Email: alerts (and the Bet-Slip ticket, Group 106) to the admin addresses over SMTP.

One message per configured address, sent through the configured server with STARTTLS (or implicit TLS when
``use_ssl``); a password never travels in the clear, and it is scrubbed from every error. The plain-text body is
the alert's; when the message carries an ``html`` detail (the ticket's HTML card) it is added as the HTML
alternative. ``smtplib`` blocks, so each send runs in a worker thread under ``timeout``.
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import socket
import ssl
from collections.abc import Iterable, Sequence
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import ClassVar

import httpx

from app.adapters.notifications.base import DispatcherConfigurationError, FinalDeliveryError, NotificationDispatcher, OutboundMessage, TransientDeliveryError
from app.models.sentinel import ChannelName

ADDRESS = re.compile(r"^[^@\s<>\"',;]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
SUBJECT_LIMIT = 200


def validate_address(address: str) -> str:
    address = address.strip()
    if len(address) > 254 or not ADDRESS.match(address):
        raise DispatcherConfigurationError(f"not an email address: {address[:3]}...")
    return address


def validate_port(port: str | int) -> int:
    try:
        value = int(str(port).strip())
    except ValueError:
        raise DispatcherConfigurationError("the SMTP port is a number, like 587 (STARTTLS) or 465 (SSL)") from None
    if not 1 <= value <= 65535:
        raise DispatcherConfigurationError("the SMTP port is between 1 and 65535")
    return value


class EmailDispatcher(NotificationDispatcher):
    channel: ClassVar[ChannelName] = ChannelName.EMAIL

    def __init__(self, http: httpx.AsyncClient, *, host: str, port: str | int, username: str, password: str, from_address: str, to_addresses: Sequence[str],
                 use_ssl: bool = False, timeout: float = 15.0, **kwargs: object) -> None:
        super().__init__(http, **kwargs)  # type: ignore[arg-type]
        host = host.strip()
        if not host or not HOST.match(host):
            raise DispatcherConfigurationError("no SMTP host configured")
        self.host, self.port, self.use_ssl, self.timeout = host, validate_port(port), use_ssl, timeout
        self.username, self.password = (username or from_address).strip(), password
        self.from_address = validate_address(from_address)
        self.to_addresses = tuple(validate_address(a) for a in to_addresses)

    def secrets(self) -> Iterable[str]:
        return (self.password,)

    def compose(self, message: OutboundMessage, to: str) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = message.headline()[:SUBJECT_LIMIT].replace("\n", " ")
        mail["From"], mail["To"] = self.from_address, to
        mail["Date"], mail["Message-ID"] = formatdate(localtime=False, usegmt=True), make_msgid(domain=self.from_address.rsplit("@", 1)[1])
        mail.set_content(message.plain(100_000))
        html = message.detail.get("html")
        if isinstance(html, str) and html:
            mail.add_alternative(html, subtype="html")
        return mail

    def _send(self, mail: EmailMessage) -> None:
        context = ssl.create_default_context()
        try:
            if self.use_ssl:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context) as smtp:
                    smtp.login(self.username, self.password)
                    smtp.send_message(mail)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                    smtp.starttls(context=context)  # never log in over a plain connection
                    smtp.login(self.username, self.password)
                    smtp.send_message(mail)
        except smtplib.SMTPAuthenticationError as exc:
            raise FinalDeliveryError(f"SMTP login refused ({exc.smtp_code})", exc.smtp_code) from None
        except smtplib.SMTPNotSupportedError:
            raise FinalDeliveryError("the server offers no STARTTLS: turn on SSL (port 465) instead") from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise FinalDeliveryError(f"SMTP refused the {'sender' if isinstance(exc, smtplib.SMTPSenderRefused) else 'recipient'}") from None
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code < 500:
                raise TransientDeliveryError(f"SMTP {exc.smtp_code}", exc.smtp_code) from None
            raise FinalDeliveryError(f"SMTP {exc.smtp_code}", exc.smtp_code) from None
        except ssl.SSLCertVerificationError:
            raise FinalDeliveryError("the SMTP server's TLS certificate is not trusted") from None
        except (smtplib.SMTPServerDisconnected, socket.timeout, ConnectionError, OSError) as exc:
            raise TransientDeliveryError(f"{type(exc).__name__}: SMTP server unreachable") from None

    async def deliver(self, message: OutboundMessage, done: set[str]) -> None:
        if not self.to_addresses:
            raise FinalDeliveryError("no recipient addresses configured")
        for to in self.to_addresses:
            if f"mail:{to}" not in done:
                await asyncio.to_thread(self._send, self.compose(message, to))
                done.add(f"mail:{to}")
