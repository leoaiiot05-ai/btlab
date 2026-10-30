"""Optional email notifications over SMTP (SPEC.md §17.6).

smtplib runs in a thread, so the event loop never waits on a mail server. A failed send is logged by its type and never
affects a job; the password never reaches a log line (§21).
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Protocol

from btlab.config import EmailConfig

TIMEOUT_S = 30.0
IMPLICIT_TLS_PORT = 465
ONCE_PER_RUN = ("INTERNAL_ERROR",)
# Each email's subject and body by language; the caller fills the fields.
TEXTS = {
    "UPDATE_FAILED": {
        "en": ("{market} update failed",
               "The scheduled {market} update failed, and failed again in each of its 3 retries.\nLast error: {error}"),
        "zh-TW": ("{market} 資料更新失敗", "排程的 {market} 資料更新失敗，3 次重試也都失敗。\n最後的錯誤：{error}"),
    },
    "BATCH_READY": {
        "en": ("{market} batch ready for {session}",
               "A {market} batch with {lines} lines waits for your confirmation before {cutoff}."),
        "zh-TW": ("{market} 委託批次待確認（{session}）", "{market} 有一批 {lines} 筆的委託待確認，截止時間 {cutoff}。"),
    },
    "BATCH_DONE": {
        "en": ("batch {batch} done ({session})", "Every order of batch {batch} for {session} has ended.\nFills: {fills}"),
        "zh-TW": ("批次 {batch} 已完成（{session}）", "批次 {batch}（{session}）的委託都已結束。\n成交：{fills}"),
    },
    "PROTECTION_RESTORED": {
        "en": ("ibguard protects {symbol} again",
               "btlab's order on {symbol} ended with {filled} shares filled and a position left: ibguard protects it again."),
        "zh-TW": ("ibguard 已恢復保護 {symbol}", "btlab 對 {symbol} 的委託已結束，成交 {filled} 股且仍有部位：ibguard 已恢復保護。"),
    },
    "IBGUARD_RESTORE_FAILED": {
        "en": ("ibguard's protection of {symbol} is still lifted",
               "btlab could not give {symbol} back to ibguard: {error}\nbtlab retries every minute; check ibguard."),
        "zh-TW": ("{symbol} 的 ibguard 保護仍未恢復", "btlab 無法讓 ibguard 恢復保護 {symbol}：{error}\nbtlab 每分鐘重試；請檢查 ibguard。"),
    },
    "READ_ONLY_API": {
        "en": ("IB's API is read-only", "IB refused an order of batch {batch} because its API is read-only; the batch stopped.\n"
               "Uncheck \"Read-Only API\" in IB Gateway's settings, then regenerate before the cutoff."),
        "zh-TW": ("IB API 為唯讀", "IB 因 API 為唯讀而拒絕批次 {batch} 的委託；批次已停止。\n"
                  "請在 IB Gateway 設定取消勾選「Read-Only API」，並在截止前重新產生批次。"),
    },
    "INTERNAL_ERROR": {
        "en": ("internal error",
               "btlab met an unexpected error: {error}\nDetails are in logs/btlab.log. Later errors of this run send no email."),
        "zh-TW": ("內部錯誤", "btlab 發生未預期的錯誤：{error}\n詳情請見 logs/btlab.log。本次執行之後的錯誤不再寄信。"),
    },
}

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, kind: str, **fields: str) -> None: ...


class Silent:
    """Email is off: smtp_host is empty."""

    async def send(self, kind: str, **fields: str) -> None:
        return None


class SmtpNotifier:
    """Subjects start with [btlab], or [btlab][TRADE] in trade mode; texts are in email.language. smtp stands in for
    the smtplib module in tests."""

    def __init__(self, email: EmailConfig, mode: str, smtp: Any = smtplib) -> None:
        self._email, self._smtp = email, smtp
        self._prefix = "[btlab][TRADE]" if mode == "trade" else "[btlab]"
        self._sent: set[str] = set()

    async def send(self, kind: str, **fields: str) -> None:
        if kind in ONCE_PER_RUN and kind in self._sent:
            return
        self._sent.add(kind)
        subject, body = (text.format(**fields) for text in TEXTS[kind][self._email.language])
        try:
            await asyncio.to_thread(self._deliver, f"{self._prefix} {subject}", body)
        except Exception as error:
            log.warning("email %s was not sent: %s", kind, type(error).__name__)

    def _deliver(self, subject: str, body: str) -> None:
        """Implicit TLS on port 465, STARTTLS on any other."""
        email = self._email
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = subject, email.sender, email.recipient
        message.set_content(body)
        context = ssl.create_default_context()
        implicit = email.smtp_port == IMPLICIT_TLS_PORT
        if implicit:
            server = self._smtp.SMTP_SSL(email.smtp_host, email.smtp_port, timeout=TIMEOUT_S, context=context)
        else:
            server = self._smtp.SMTP(email.smtp_host, email.smtp_port, timeout=TIMEOUT_S)
        with server:
            if not implicit:
                server.starttls(context=context)
            if email.username:
                server.login(email.username, email.password)
            server.send_message(message)


def make_notifier(email: EmailConfig, mode: str) -> Notifier:
    """The SMTP notifier, or Silent with a startup warning when smtp_host is empty."""
    if not email.smtp_host:
        log.warning("email is off: email.smtp_host is empty")
        return Silent()
    return SmtpNotifier(email, mode)
