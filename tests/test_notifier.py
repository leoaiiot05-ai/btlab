"""The optional SMTP notifier (SPEC.md §17.6, the notifier part of AC-28)."""

import asyncio
import logging

import pytest

from btlab.config import EmailConfig
from btlab.notifier import Silent, SmtpNotifier, make_notifier

PASSWORD = "Sentinel-Smtp-Password-9a1f"


def email(port: int = 587, username: str = "me", language: str = "en", host: str = "smtp.example.org") -> EmailConfig:
    return EmailConfig(host, port, username, PASSWORD, "btlab@example.org", "owner@example.org", language)


class FakeSmtp:
    """Stands in for the smtplib module: records each server's calls; fail makes the connection raise."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail = fail
        smtp = self

        class Server:
            def __init__(self, host, port, timeout=None, context=None) -> None:
                if smtp.fail is not None:
                    raise smtp.fail
                smtp.calls.append((type(self).__name__, host, port))

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                smtp.calls.append(("quit",))

            def starttls(self, context=None):
                smtp.calls.append(("starttls",))

            def login(self, user, password):
                smtp.calls.append(("login", user, password))

            def send_message(self, message):
                smtp.calls.append(("send", message["Subject"], message["To"], message.get_content()))

        self.SMTP = type("SMTP", (Server,), {})
        self.SMTP_SSL = type("SMTP_SSL", (Server,), {})


def send(notifier, kind: str = "UPDATE_FAILED", **fields) -> None:
    asyncio.run(notifier.send(kind, **({"market": "US", "error": "MISSING_KEY: the Tiingo API key is empty"} | fields)))


def test_starttls_on_587_with_login():
    smtp = FakeSmtp()
    send(SmtpNotifier(email(), "test", smtp))
    assert smtp.calls[:3] == [("SMTP", "smtp.example.org", 587), ("starttls",), ("login", "me", PASSWORD)]
    kind, subject, to, body = smtp.calls[3]
    assert (kind, subject, to) == ("send", "[btlab] US update failed", "owner@example.org")
    assert "MISSING_KEY" in body and smtp.calls[4] == ("quit",)


def test_implicit_tls_on_465_without_a_username_and_trade_mode_subjects_in_chinese():
    smtp = FakeSmtp()
    send(SmtpNotifier(email(465, username="", language="zh-TW"), "trade", smtp))
    assert [call[0] for call in smtp.calls] == ["SMTP_SSL", "send", "quit"]
    assert smtp.calls[1][1] == "[btlab][TRADE] US 資料更新失敗"


def test_internal_error_is_sent_once_per_run():
    smtp = FakeSmtp()
    notifier = SmtpNotifier(email(), "test", smtp)
    for _ in range(3):
        send(notifier, "INTERNAL_ERROR", error="ValueError: boom")
    assert sum(call[0] == "send" for call in smtp.calls) == 1


@pytest.mark.parametrize("failure", [OSError("connection refused"), RuntimeError(f"auth failed for {PASSWORD}")])
def test_a_failed_send_raises_nothing_and_logs_no_secret(failure, caplog):
    caplog.set_level(logging.DEBUG)
    send(SmtpNotifier(email(), "test", FakeSmtp(failure)))
    assert type(failure).__name__ in caplog.text and PASSWORD not in caplog.text


def test_an_empty_host_turns_email_off(caplog):
    caplog.set_level(logging.WARNING)
    notifier = make_notifier(email(host=""), "test")
    assert isinstance(notifier, Silent) and "smtp_host is empty" in caplog.text
    send(notifier)
    assert isinstance(make_notifier(email(), "test"), SmtpNotifier)
