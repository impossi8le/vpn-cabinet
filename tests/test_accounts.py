"""Инварианты аккаунта (§4 спеки).

Каждый тест отвечает на вопрос «можно ли было это делать», а не «что вернула
функция»: схема БД ловит UNIQUE, но не отвечает на этот вопрос.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cabinet.accounts import Account, LinkError

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def test_mail_without_verification_is_rejected():
    """Инвариант 2. Незаподтверждённая почта — это ровно тот вход, через
    который проходит pre-account hijacking: связывание по заявленному адресу."""
    with pytest.raises(LinkError, match="без подтверждения"):
        Account(mail="user@example.com", mail_verified_at=None)


def test_mail_with_verification_is_accepted():
    acc = Account(mail="user@example.com", mail_verified_at=NOW)
    assert acc.mail == "user@example.com"


def test_telegram_only_account_is_valid():
    """Аккаунт без почты — нормальное состояние, не ошибка."""
    acc = Account(chat_id=123456789)
    assert acc.mail is None
    assert acc.has_any_login_method()


def test_email_only_account_is_valid():
    """Главный случай: человек без Telegram. `chat_id` пуст, аккаунт рабочий."""
    acc = Account(mail="user@example.com", mail_verified_at=NOW)
    assert acc.chat_id is None
    assert acc.has_any_login_method()


def test_empty_mail_is_rejected():
    """Пустая строка — не почта. Иначе аккаунт с `mail = ''` выглядел бы
    привязанным, не имея способа входа."""
    with pytest.raises(LinkError):
        Account(mail="   ", mail_verified_at=NOW)


def test_cannot_link_telegram_when_already_linked():
    """Инвариант 3. Занятый `chat_id` не перезаписывается: иначе знание чужого
    `chat_id` давало бы доступ к аккаунту."""
    acc = Account(chat_id=111)
    assert acc.can_link_telegram() is False


def test_can_link_telegram_when_empty():
    acc = Account(mail="user@example.com", mail_verified_at=NOW)
    assert acc.can_link_telegram() is True


def test_cannot_link_mail_when_already_linked():
    acc = Account(mail="user@example.com", mail_verified_at=NOW)
    assert acc.can_link_mail() is False


def test_can_link_mail_when_empty():
    """Аккаунт из Telegram привязывает почту — обратное направление."""
    acc = Account(chat_id=123456789)
    assert acc.can_link_mail() is True


def test_account_without_login_methods_is_detected():
    """Аккаунт без способов входа недоступен навсегда — отвязку последнего
    надо запрещать. Тест фиксирует, что такое состояние распознаётся."""
    assert Account().has_any_login_method() is False


def test_both_methods_linked():
    acc = Account(chat_id=123456789, mail="user@example.com", mail_verified_at=NOW)
    assert acc.has_any_login_method()
    assert acc.can_link_telegram() is False
    assert acc.can_link_mail() is False
