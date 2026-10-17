"""Фикстуры мок-слоя (W0.3).

Точка входа для тестов остальных модулей: здесь собираются готовые к работе
`fake_api`, `fake_mail` и типовые аккаунты. Тест, которому нужен бот или почта,
просит фикстуру и не знает, что под ней мок, — на W6 подменится реализация, а
тесты останутся.

Правило волны: **ни одна фикстура не открывает сеть и не трогает базу.** Всё
состояние — в памяти и живёт ровно один тест.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fakes import (
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_EXPIRED,
    FakeApi,
    FakeMailSender,
    FakeSubscription,
)

from cabinet.accounts import Account


@pytest.fixture
def now() -> datetime:
    """Фиксированное «сейчас». С реальными часами тесты на TTL и истечение
    подписки становятся недетерминированными."""
    return datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake_api() -> FakeApi:
    """Пустой подставной бот. Свежий на каждый тест — тесты не протекают."""
    return FakeApi()


@pytest.fixture
def fake_mail() -> FakeMailSender:
    """Подставная почта. Читать отправленное — через `fake_mail.outbox`."""
    return FakeMailSender()


# --- аккаунты ------------------------------------------------------------
#
# Четыре состояния из §4–5 спеки. Фикстуры возвращают уже созданные в
# `fake_api` аккаунты, поэтому их `id` проставлен: тесту не нужно самому
# вызывать `create_account`.


@pytest.fixture
def account_email_only(fake_api: FakeApi, now: datetime) -> Account:
    """Главный случай: человек без Telegram. `chat_id` пуст, аккаунт рабочий."""
    return fake_api.create_account(mail="user@example.com", mail_verified_at=now)


@pytest.fixture
def account_telegram_only(fake_api: FakeApi) -> Account:
    """Аккаунт, заведённый через Telegram. Почты нет — это нормально."""
    return fake_api.create_account(chat_id=123_456_789)


@pytest.fixture
def account_linked(fake_api: FakeApi, now: datetime) -> Account:
    """Аккаунт с обоими способами входа. Привязывать больше нечего."""
    return fake_api.create_account(
        chat_id=123_456_789,
        mail="user@example.com",
        mail_verified_at=now,
    )


@pytest.fixture
def account_empty(fake_api: FakeApi) -> Account:
    """Аккаунт без способов входа — состояние, которое обязано быть недостижимо
    через отвязку (W3.5). Нужен, чтобы зафиксировать такой отказ."""
    return fake_api.create_account()


# --- подписки и конфиги --------------------------------------------------


@pytest.fixture
def subscription_active(
    fake_api: FakeApi, account_email_only: Account, now: datetime
) -> FakeSubscription:
    """Активная подписка со свежим конфигом: всё, что нужно для W4.1–W4.2."""
    config = fake_api.add_config(account_email_only.id, body="# fake .ovpn")
    return fake_api.add_subscription(
        account_email_only.id,
        status=STATUS_ACTIVE,
        expires_at=now + timedelta(days=30),
        config_id=config.id,
    )


@pytest.fixture
def subscription_expired(
    fake_api: FakeApi, account_email_only: Account, now: datetime
) -> FakeSubscription:
    """Истёкшая подписка. Конфиг по ней отдавать нельзя (W4.2)."""
    config = fake_api.add_config(account_email_only.id, body="# fake .ovpn")
    return fake_api.add_subscription(
        account_email_only.id,
        status=STATUS_EXPIRED,
        expires_at=now - timedelta(days=1),
        config_id=config.id,
    )


@pytest.fixture
def subscription_blocked(
    fake_api: FakeApi, account_email_only: Account, now: datetime
) -> FakeSubscription:
    """Подписка при заблокированном аккаунте: срок не истёк, доступа нет."""
    config = fake_api.add_config(account_email_only.id, body="# fake .ovpn")
    return fake_api.add_subscription(
        account_email_only.id,
        status=STATUS_BLOCKED,
        expires_at=now + timedelta(days=30),
        config_id=config.id,
    )
