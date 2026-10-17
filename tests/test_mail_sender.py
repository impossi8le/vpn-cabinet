"""Транспорт почты: одна функция отправки, транспорт из конфига (W2.1).

Готово, когда: тест с подставным транспортом, письмо не уходит наружу. Здесь
проверяется именно это — что `send_code` доставляет через то, что подставлено,
и что без `fake_mail` наружу не уходит ничего.

Отдельная тема — SMTP со STARTTLS. Письмо с кодом входа нельзя отправлять
открытым текстом: STARTTLS переводит соединение в шифрованное до передачи
пароля. Если этот тест когда-нибудь покраснеет, слать почту нельзя до починки.
"""

from __future__ import annotations

import smtplib

import pytest
from fakes import FakeMailSender

from cabinet.mail_sender import (
    MailAddressError,
    MailConfigError,
    MailSender,
    MailSettings,
    MailTransportError,
    SmtpTransport,
    from_env,
)

SMTP_ENV = {
    "MAIL_TRANSPORT": "smtp",
    "MAIL_FROM": "cabinet@example.com",
    "MAIL_SMTP_HOST": "smtp.example.com",
    "MAIL_SMTP_PORT": "587",
    "MAIL_SMTP_USER": "user@example.com",
    "MAIL_SMTP_PASSWORD": "secret",
}


def test_code_is_delivered_through_substituted_transport():
    """Одна функция, транспорт подставлен. Письмо уходит в `fake_mail`, наружу —
    ничего: ни сети, ни бота, ни почты."""
    mail = FakeMailSender()
    sender = MailSender(sender="cabinet@example.com", transport=mail)

    sender.send_code("user@example.com", code="123456", ttl_minutes=10)

    sent = mail.sent_to("user@example.com")
    assert len(sent) == 1
    assert "123456" in sent[0]["body"]


def test_caller_does_not_choose_transport():
    """Вызывающий код не знает, каким транспортом ушло письмо. `send_code` —
    единственная точка входа, и она не принимает транспорт параметром: иначе
    выбор снова оказался бы у вызывающего."""
    mail = FakeMailSender()
    sender = MailSender(sender="cabinet@example.com", transport=mail)

    sender.send_code("user@example.com", code="123456")

    # Транспорт — свойство объекта, а не аргумента вызова.
    assert sender.transport is mail


def test_settings_pick_smtp_from_config():
    settings = MailSettings.from_env(SMTP_ENV)
    assert settings.transport == "smtp"
    assert settings.smtp_host == "smtp.example.com"
    assert settings.sender == "cabinet@example.com"


def test_settings_pick_api_from_config():
    settings = MailSettings.from_env(
        {
            "MAIL_TRANSPORT": "api",
            "MAIL_FROM": "cabinet@example.com",
            "MAIL_API_URL": "https://mail.example.com/send",
            "MAIL_API_KEY": "key",
        }
    )
    assert settings.transport == "api"
    assert settings.api_url == "https://mail.example.com/send"


def test_build_selects_transport_by_config():
    """Транспорт выбирается в одном месте — `MailSender.build` — и больше нигде."""
    from_smtp = MailSender.build(MailSettings.from_env(SMTP_ENV))
    assert isinstance(from_smtp.transport, SmtpTransport)


def test_unknown_transport_is_a_config_error():
    with pytest.raises(MailConfigError, match="MAIL_TRANSPORT"):
        MailSettings.from_env({**SMTP_ENV, "MAIL_TRANSPORT": "pigeon"})


def test_incomplete_smtp_config_is_rejected_not_silently_downgraded():
    """Неполный конфиг — отказ, а не тихий откат на другой транспорт: иначе
    письмо ушло бы не тем путём, которого ждёт развёртывание."""
    broken = {key: value for key, value in SMTP_ENV.items() if key != "MAIL_SMTP_PASSWORD"}
    with pytest.raises(MailConfigError, match="MAIL_SMTP_PASSWORD"):
        MailSettings.from_env(broken)


def test_empty_recipient_is_rejected():
    """Пустой адрес — ошибка вызывающего, и упасть она должна до обращения к
    транспорту."""
    sender = MailSender(sender="cabinet@example.com", transport=FakeMailSender())
    with pytest.raises(MailAddressError):
        sender.send_code("   ", code="123456")


def test_header_injection_in_recipient_is_rejected():
    """Адрес приходит из формы. Перевод строки в нём дописал бы в письмо
    произвольные заголовки — вплоть до подмены получателя."""
    sender = MailSender(sender="cabinet@example.com", transport=FakeMailSender())
    with pytest.raises(MailAddressError):
        sender.send_code("victim@example.com\r\nBcc: attacker@example.com", code="123456")


def test_smtp_transport_uses_starttls():
    """STARTTLS обязателен: без него код и пароль уходят открытым текстом.
    Тест фиксирует порядок — соединение шифруется до `login`."""

    calls: list[str] = []

    class FakeSmtp:
        def __init__(self, host, port, timeout=None):
            calls.append("connect")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self, context=None):
            calls.append("starttls")

        def login(self, user, password):
            calls.append("login")

        def send_message(self, message):
            calls.append("send_message")

    transport = SmtpTransport(
        host="smtp.example.com",
        port=587,
        user="user@example.com",
        password="secret",
        sender="cabinet@example.com",
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(smtplib, "SMTP", FakeSmtp)
    try:
        transport.send(to="user@example.com", subject="код", body="123456")
    finally:
        monkey.undo()

    assert "starttls" in calls
    assert calls.index("starttls") < calls.index("login")


def test_smtp_failure_is_reported_not_swallowed():
    """Сбой транспорта не глушится: код на почту — единственный путь входа, и
    «сделали вид, что отправили» оставило бы человека у неработающей формы."""

    class BrokenSmtp:
        def __init__(self, *args, **kwargs):
            raise smtplib.SMTPConnectError(421, "недоступен")

    transport = SmtpTransport(
        host="smtp.example.com",
        port=587,
        user="user@example.com",
        password="secret",
        sender="cabinet@example.com",
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(smtplib, "SMTP", BrokenSmtp)
    try:
        with pytest.raises(MailTransportError):
            transport.send(to="user@example.com", subject="код", body="123456")
    finally:
        monkey.undo()


def test_from_env_returns_ready_sender():
    """Точка входа сервиса: конфиг — из окружения, наружу ничего не уходит,
    потому что транспорт пока не вызывается."""
    sender = from_env(SMTP_ENV)
    assert sender.sender == "cabinet@example.com"
