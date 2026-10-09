"""Twilio: SMS to the admin phones, and a voice call for FATAL.

Messages (https://www.twilio.com/docs/messaging/api/message-resource#create-a-message-resource) go
to every configured number; a FATAL alert also rings each one (``Calls`` with inline TwiML that reads
the headline twice), when ``voice_on_fatal`` is set. HTTP basic auth with the account SID and auth
token; the token never appears in an error. Numbers must be E.164 (``+919876543210``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import ClassVar
from xml.sax.saxutils import escape

import httpx

from app.adapters.notifications.base import DispatcherConfigurationError, FinalDeliveryError, NotificationDispatcher, OutboundMessage, check
from app.models.sentinel import ChannelName, Severity

API_BASE = "https://api.twilio.com/2010-04-01"
SMS_LIMIT = 320  # two segments: enough for a headline and the first lines of the body
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")


def validate_number(number: str) -> str:
    number = number.strip().replace(" ", "")
    if not E164.match(number):
        raise DispatcherConfigurationError(f"phone numbers must be E.164, like +919876543210 (got {number[:4]}...)")
    return number


class TwilioDispatcher(NotificationDispatcher):
    channel: ClassVar[ChannelName] = ChannelName.TWILIO

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_numbers: Sequence[str],
        voice_on_fatal: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(http, **kwargs)  # type: ignore[arg-type]
        if not _SID.match(account_sid or ""):
            raise DispatcherConfigurationError("a Twilio account SID starts with AC and has 32 hex digits")
        if not auth_token:
            raise DispatcherConfigurationError("a Twilio dispatcher needs its auth token")
        self.account_sid, self.auth_token = account_sid, auth_token
        self.from_number = validate_number(from_number)
        self.to_numbers = tuple(validate_number(n) for n in to_numbers)
        self.voice_on_fatal = voice_on_fatal

    def secrets(self) -> Iterable[str]:
        return (self.auth_token,)

    def sms_body(self, message: OutboundMessage) -> str:
        return message.plain(SMS_LIMIT)

    def twiml(self, message: OutboundMessage) -> str:
        spoken = escape(f"BetDoc Sentinel. {message.severity}. {message.title}.")
        return f'<Response><Say voice="alice">{spoken}</Say><Pause length="1"/><Say voice="alice">{spoken}</Say></Response>'

    async def deliver(self, message: OutboundMessage, done: set[str]) -> None:
        if not self.to_numbers:
            raise FinalDeliveryError("no phone numbers configured")
        auth = (self.account_sid, self.auth_token)
        call = message.severity is Severity.FATAL and self.voice_on_fatal and not message.resolves
        for number in self.to_numbers:
            if f"sms:{number}" not in done:
                check(await self.http.post(f"{API_BASE}/Accounts/{self.account_sid}/Messages.json", data={"To": number, "From": self.from_number, "Body": self.sms_body(message)}, auth=auth))
                done.add(f"sms:{number}")
            if call and f"call:{number}" not in done:
                check(await self.http.post(f"{API_BASE}/Accounts/{self.account_sid}/Calls.json", data={"To": number, "From": self.from_number, "Twiml": self.twiml(message)}, auth=auth))
                done.add(f"call:{number}")
