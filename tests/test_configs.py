"""W4.2/W4.4: скачивание `.ovpn` — право и лимит.

Тесты отвечают на вопросы, которые иначе всплыли бы у пользователя и у сервиса:

- конфиг не отдаётся по неработающей подписке (истёкшей, заблокированной) —
  и **к боту за ним не ходят вовсе**;
- «чужой конфиг» и «нет такого» неотличимы: и тип отказа, и текст одни;
- лимит срабатывает на N+1-м запросе, а неудачный запрос квоту не съедает;
- отказы бота переводятся в наши типы по коду, а не по тексту;
- имя файла и хеш безопасны и посчитаны по отданным байтам.

Файл пишется против интерфейса `ConfigSource`, а не против живого бота: как
`test_auth_mail.py` работает с `FakeMailSender`, так этот — с источником в
памяти. Сети здесь нет.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from fakes import FakeApi, FakeSubscription

from cabinet.bot_client import BotError, BotUnreachable
from cabinet.configs import (
    DOWNLOAD_WINDOW_SECONDS,
    MAX_DOWNLOADS_PER_WINDOW,
    OVPN_CONTENT_TYPE,
    ConfigError,
    ConfigNotFound,
    ConfigRetired,
    ConfigRevoked,
    ConfigService,
    DownloadLimiter,
    DownloadRateLimited,
    SubscriptionInactive,
    config_filename,
)
from cabinet.subscriptions import subscription_view

BODY = b"# fake .ovpn\nclient\n"


class Clock:
    """Управляемые часы: окно лимита проверяется сдвигом времени, а не
    ожиданием. Тот же приём, что в `test_auth_mail.py`."""

    def __init__(self, start: datetime) -> None:
        self._moment = start

    def __call__(self) -> datetime:
        return self._moment

    def advance(self, *, seconds: int = 0) -> None:
        self._moment += timedelta(seconds=seconds)


class FakeSource:
    """Источник конфигов в памяти. Отвечает как бот: либо байты, либо
    `BotError` с кодом контракта. Запросы считает, чтобы тест видел, ходили ли
    к источнику вовсе."""

    def __init__(self, api: FakeApi, *, body: bytes = BODY) -> None:
        self.api = api
        self.body = body
        self.requested: list[tuple[int, str]] = []
        # Что вернуть вместо ответа: код отказа бота или None — тогда идём в api.
        self.forced_code: str | None = None
        self.unreachable = False

    async def get_ovpn(self, account_id: int, config_id: str) -> bytes:
        self.requested.append((account_id, config_id))
        if self.unreachable:
            raise BotUnreachable("источник недоступен")
        if self.forced_code is not None:
            raise BotError(self.forced_code, status=403)
        config = self.api.config_for_subscription(account_id, int(config_id))
        if config is None:
            # «Нет конфига» и «чужой» — один и тот же код бота (§3 контракта).
            raise BotError("config_not_found", status=404)
        return self.body


def view_of(raw: FakeSubscription, now: datetime):
    """Состояние подписки для экрана — то же, чем пользуется сервис."""
    from cabinet.bot_client import Subscription as ClientSubscription

    return subscription_view(
        ClientSubscription.from_api(raw.to_api()),
        now=now,
    )


def build(api: FakeApi, now: datetime, *, clock: Clock | None = None):
    source = FakeSource(api)
    limiter = DownloadLimiter(now=clock) if clock else DownloadLimiter(now=lambda: now)
    return ConfigService(source=source, limiter=limiter), source


# --- право на конфиг: подписка решает -------------------------------------


async def test_active_subscription_downloads_config(
    fake_api: FakeApi, subscription_active: FakeSubscription, now: datetime
):
    """Главный случай: подписка работает — конфиг отдаётся."""
    service, source = build(fake_api, now)
    result = await service.download(
        account_id=subscription_active.account_id,
        config_id=str(subscription_active.id),
        subscription=view_of(subscription_active, now),
    )
    assert result.content == BODY
    assert result.content_type == OVPN_CONTENT_TYPE
    assert source.requested == [(subscription_active.account_id, str(subscription_active.id))]


async def test_expired_subscription_is_refused_before_touching_the_bot(
    fake_api: FakeApi, subscription_expired: FakeSubscription, now: datetime
):
    """Истёкшая подписка: отказ, и к боту за конфигом не ходим вовсе.

    Проверка «источник не вызывался» — не деталь: она фиксирует, что право
    решается по сведённому состоянию (`is_usable`), а не попыткой скачать.
    """
    service, source = build(fake_api, now)
    with pytest.raises(SubscriptionInactive):
        await service.download(
            account_id=subscription_expired.account_id,
            config_id=str(subscription_expired.id),
            subscription=view_of(subscription_expired, now),
        )
    assert source.requested == []


async def test_blocked_subscription_is_refused(
    fake_api: FakeApi, subscription_blocked: FakeSubscription, now: datetime
):
    """Заблокированная подписка закрывает конфиг так же, как истёкшая."""
    service, source = build(fake_api, now)
    with pytest.raises(SubscriptionInactive):
        await service.download(
            account_id=subscription_blocked.account_id,
            config_id=str(subscription_blocked.id),
            subscription=view_of(subscription_blocked, now),
        )
    assert source.requested == []


# --- «чужой» и «нет» неотличимы -------------------------------------------


async def test_foreign_config_is_indistinguishable_from_missing(
    fake_api: FakeApi, subscription_active: FakeSubscription, account_telegram_only, now: datetime
):
    """Чужой конфиг и несуществующий дают один и тот же отказ.

    Сравниваются и тип, и текст: если они разойдутся, по ответам кабинета
    перебором `config_id` выяснялось бы, у кого что есть.
    """
    service, _ = build(fake_api, now)
    other = view_of(subscription_active, now)

    with pytest.raises(ConfigNotFound) as foreign:
        # Чужой аккаунт просит конфиг активной подписки другого.
        await service.download(
            account_id=account_telegram_only.id,
            config_id=str(subscription_active.id),
            subscription=other,
        )
    with pytest.raises(ConfigNotFound) as missing:
        await service.download(
            account_id=account_telegram_only.id,
            config_id="424242",
            subscription=other,
        )

    assert type(foreign.value) is type(missing.value)
    assert foreign.value.args == missing.value.args


# --- отказы бота: по коду, не по тексту ------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("subscription_expired", SubscriptionInactive),
        ("config_not_found", ConfigNotFound),
        ("config_revoked", ConfigRevoked),
        ("config_retired", ConfigRetired),
    ],
)
async def test_bot_error_code_maps_to_our_refusal(
    fake_api: FakeApi,
    subscription_active: FakeSubscription,
    now: datetime,
    code: str,
    expected: type[ConfigError],
):
    """Код контракта решает, какой отказ показать. Ветвимся по коду — текст
    меняется, код нет."""
    service, source = build(fake_api, now)
    source.forced_code = code
    with pytest.raises(expected):
        await service.download(
            account_id=subscription_active.account_id,
            config_id=str(subscription_active.id),
            subscription=view_of(subscription_active, now),
        )


async def test_unknown_bot_code_is_not_shown_as_missing(
    fake_api: FakeApi, subscription_active: FakeSubscription, now: datetime
):
    """Незнакомый код — расхождение версий, а не «конфига нет»: не выдаём его
    за пустой конфиг."""
    service, source = build(fake_api, now)
    source.forced_code = "some_new_code"
    with pytest.raises(ConfigError) as exc:
        await service.download(
            account_id=subscription_active.account_id,
            config_id=str(subscription_active.id),
            subscription=view_of(subscription_active, now),
        )
    assert type(exc.value) is ConfigError


# --- лимит: N+1-й запрос отвергается --------------------------------------


async def test_download_limit_rejects_the_n_plus_first_request(
    fake_api: FakeApi, subscription_active: FakeSubscription, now: datetime
):
    """Первые N скачиваний проходят, N+1-е — нет."""
    service, source = build(fake_api, now)
    args = dict(
        account_id=subscription_active.account_id,
        config_id=str(subscription_active.id),
        subscription=view_of(subscription_active, now),
    )

    for _ in range(MAX_DOWNLOADS_PER_WINDOW):
        await service.download(**args)

    with pytest.raises(DownloadRateLimited):
        await service.download(**args)

    assert len(source.requested) == MAX_DOWNLOADS_PER_WINDOW


async def test_limit_window_slides(
    fake_api: FakeApi, subscription_active: FakeSubscription, now: datetime
):
    """Окно скользящее: после паузы место в журнале освобождается."""
    clock = Clock(now)
    service, _ = build(fake_api, now, clock=clock)
    args = dict(
        account_id=subscription_active.account_id,
        config_id=str(subscription_active.id),
        subscription=view_of(subscription_active, now),
    )
    for _ in range(MAX_DOWNLOADS_PER_WINDOW):
        await service.download(**args)

    clock.advance(seconds=DOWNLOAD_WINDOW_SECONDS + 1)
    # Не бросает: старое окно очистилось.
    await service.download(**args)


async def test_unreachable_bot_does_not_spend_the_quota(
    fake_api: FakeApi, subscription_active: FakeSubscription, now: datetime
):
    """Сеть — не действие человека: неудачная попытка квоту не съедает."""
    service, source = build(fake_api, now)
    source.unreachable = True
    args = dict(
        account_id=subscription_active.account_id,
        config_id=str(subscription_active.id),
        subscription=view_of(subscription_active, now),
    )
    for _ in range(MAX_DOWNLOADS_PER_WINDOW):
        with pytest.raises(BotUnreachable):
            await service.download(**args)

    source.unreachable = False
    await service.download(**args)


# --- файл: имя и хеш --------------------------------------------------------


def test_filename_from_config_id_has_ovpn_suffix():
    assert config_filename("nl-ams-1") == "nl-ams-1.ovpn"


def test_filename_strips_header_breaks_and_quotes():
    """`config_id` приходит из адреса запроса: перевод строки в имени дописал бы
    в ответ чужие заголовки."""
    assert "\n" not in config_filename("a\r\nX-Evil: 1")
    assert '"' not in config_filename('a"b')
    assert config_filename("../../etc/passwd").startswith("....etcpasswd")


def test_filename_falls_back_when_nothing_safe_remains():
    assert config_filename("\n\r\t") == "config.ovpn"


async def test_hash_is_sha256_over_the_served_bytes(
    fake_api: FakeApi, subscription_active: FakeSubscription, now: datetime
):
    service, source = build(fake_api, now)
    source.body = b"# another profile\n"
    result = await service.download(
        account_id=subscription_active.account_id,
        config_id=str(subscription_active.id),
        subscription=view_of(subscription_active, now),
    )
    assert result.hash == "sha256:" + hashlib.sha256(source.body).hexdigest()
    assert result.size == len(source.body)
