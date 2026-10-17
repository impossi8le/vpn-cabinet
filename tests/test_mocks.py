"""Сам мок-слой: он обязан воспроизводить инварианты, а не обходить их.

Мок, который разрешает больше живого сервиса, делает тесты бесполезными:
зелёный прогон перестаёт что-либо значить. Здесь фиксируется, что заглушки
ведут себя как боевые ограничения — связывание только на пустое место,
коллизия отвергается, отвязка последнего способа запрещена — и что ни один
тест не ходит в сеть.
"""

from __future__ import annotations

import inspect
import socket
from datetime import UTC, datetime

import pytest
from fakes import STATUS_ACTIVE, STATUS_EXPIRED, CollisionError, FakeApi

from cabinet.accounts import Account


def test_fixtures_do_not_touch_the_network(fake_api: FakeApi, fake_mail, monkeypatch):
    """Правило волны 0: ни один тест не ходит в сеть. Если этот тест краснеет,
    значит фикстура где-то открывает соединение — и прогон на чистом раннере
    перестанет быть воспроизводимым."""

    def forbid(*args: object, **kwargs: object) -> None:
        raise AssertionError("мок-слой не должен открывать сеть или базу")

    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(socket, "socket", forbid)

    account = fake_api.create_account(
        mail="user@example.com", mail_verified_at=datetime(2026, 10, 17, tzinfo=UTC)
    )
    fake_mail.send(to="user@example.com", subject="код", body="000000")
    assert fake_api.find_by_mail(account.mail) is not None


def test_email_only_account_fixture_is_usable(account_email_only: Account):
    """Главный случай: почта есть, Telegram нет — аккаунт рабочий."""
    assert account_email_only.id is not None
    assert account_email_only.chat_id is None
    assert account_email_only.has_any_login_method()


def test_link_chat_id_returns_rowcount_not_boolean(fake_api: FakeApi, account_email_only: Account):
    """`link_chat_id` повторяет `UPDATE ... WHERE chat_id IS NULL`: успех — 1,
    отказ — 0. Число, а не bool, чтобы вызывающий код не делал `if updated:`."""
    assert fake_api.link_chat_id(account_email_only.id, 555_000_111) == 1
    assert fake_api.find_by_chat_id(555_000_111).id == account_email_only.id


def test_linking_twice_is_refused_not_overwritten(fake_api: FakeApi, account_linked: Account):
    """Инвариант 3 на уровне мока: занятое значение не перезаписывается."""
    assert fake_api.link_chat_id(account_linked.id, 999) == 0
    assert fake_api.find_by_chat_id(999) is None


def test_collision_with_another_account_raises(fake_api: FakeApi, account_telegram_only: Account):
    """§5: коллизия — честная ошибка, не тихая перезапись."""
    other = fake_api.create_account()
    with pytest.raises(CollisionError):
        fake_api.link_chat_id(other.id, account_telegram_only.chat_id)


def test_mail_link_requires_verified_at_by_signature(
    fake_api: FakeApi, account_telegram_only: Account
):
    """Инвариант 2: у `link_mail` нет значения по умолчанию для
    `mail_verified_at` — незаподтверждённую почту подставить нельзя."""
    parameter = inspect.signature(fake_api.link_mail).parameters["mail_verified_at"]
    assert parameter.default is inspect.Parameter.empty


def test_unlinking_last_login_method_is_refused(fake_api: FakeApi, account_telegram_only: Account):
    """W3.5: аккаунт без способов входа недоступен навсегда — отвязку запрещаем."""
    assert fake_api.unlink_chat_id(account_telegram_only.id) == 0
    assert fake_api.find_by_chat_id(123_456_789) is not None


def test_unlinking_when_second_method_exists_succeeds(fake_api: FakeApi, account_linked: Account):
    """Отвязка разрешена, пока остаётся второй способ."""
    assert fake_api.unlink_chat_id(account_linked.id) == 1
    assert fake_api.find_by_chat_id(123_456_789) is None
    # Возвращённые фикстурой аккаунты — снимки; актуальное состояние читаем из мока.
    assert fake_api.find_by_mail("user@example.com").can_link_telegram() is True


def test_empty_subscription_list_is_not_an_error(fake_api: FakeApi, account_email_only: Account):
    """W4.3: «подписок нет» — обычный ответ, а не исключение."""
    assert fake_api.subscriptions_for(account_email_only.id) == []


def test_config_is_withheld_for_expired_subscription(fake_api: FakeApi, subscription_expired):
    """W4.2: по истёкшей подписке конфиг не отдаётся."""
    assert (
        fake_api.config_for_subscription(subscription_expired.account_id, subscription_expired.id)
        is None
    )


def test_config_is_served_for_active_subscription(fake_api: FakeApi, subscription_active):
    """Парный случай: по активной подписке конфиг отдаётся."""
    config = fake_api.config_for_subscription(
        subscription_active.account_id, subscription_active.id
    )
    assert config is not None
    assert subscription_active.status == STATUS_ACTIVE


def test_foreign_subscription_is_indistinguishable_from_missing(
    fake_api: FakeApi, subscription_active, account_telegram_only: Account
):
    """Отказы не различаются: иначе перебором `subscription_id` выяснялось бы,
    у кого что есть."""
    assert (
        fake_api.config_for_subscription(account_telegram_only.id, subscription_active.id) is None
    )
    assert fake_api.config_for_subscription(account_telegram_only.id, 424_242) is None


def test_expired_status_is_not_active(subscription_expired):
    assert subscription_expired.status == STATUS_EXPIRED
    assert subscription_expired.is_active() is False
