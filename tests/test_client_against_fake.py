"""Стык реального клиента с подставным ботом.

Проверяет то, чего не видел ни один из модулей по отдельности: что `BotClient`
действительно разговаривает с тем интерфейсом, который имитирует `FakeApi`.

Зачем отдельный файл. Пока мок и клиент живут раздельно, каждый из них
внутренне непротиворечив, а вместе они не проверены. Ровно на этом стыке
ломается потом: имена полей, форма ответа, разбор ошибок. Тесты следующей
волны написаны против мока — если стык не сходится, они подтвердят не то
поведение, которое пойдёт в бой.

Соединение подменяется транспортом `httpx`, а не берётся из сети: тест не
должен открывать сокет.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from cabinet.bot_client import BotClient, BotConfigError, BotError
from tests.fakes import FakeApi

SECRET = "test-secret"
BASE = "http://127.0.0.1:9"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _transport_for(api: FakeApi) -> httpx.MockTransport:
    """Мост: HTTP-запросы клиента → вызовы подставного бота.

    Маршруты и форма ответа повторяют внутренний роутер бота. Совпадение имён
    здесь и есть предмет проверки: разойдётся — этот тест покраснеет раньше,
    чем это заметит боевой кабинет.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Секрет проверяется на каждом запросе: роутер бота не отвечает без него,
        # и клиент не должен уметь сходить «мимо».
        if request.headers.get("X-Internal-Secret") != SECRET:
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})

        path = request.url.path
        if request.method == "GET" and path == "/internal/accounts/by-chat-id":
            acc = api.find_by_chat_id(int(request.url.params["chat_id"]))
        elif request.method == "GET" and path == "/internal/accounts/by-mail":
            acc = api.find_by_mail(request.url.params["mail"])
        else:
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        if acc is None:
            return httpx.Response(
                404, json={"error": {"code": "account_not_found", "retryable": False}}
            )
        return httpx.Response(200, json={"account": _account_json(acc)})

    return httpx.MockTransport(handler)


def _account_json(acc) -> dict:
    return {
        "id": acc.id,
        "chat_id": acc.chat_id,
        "mail": acc.mail,
        "mail_verified_at": acc.mail_verified_at.isoformat() if acc.mail_verified_at else None,
    }


def _client_for(api: FakeApi) -> BotClient:
    """Клиент, включённый в подставного бота.

    `base_url` задаётся на самом httpx-клиенте: пути в запросах относительные,
    и без этого их не к чему приклеить. Это же показывает, что для теста
    достаточно подставить транспорт — боевой адрес при этом не нужен.
    """
    return BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api)),
    )


async def test_client_finds_account_created_by_fake():
    """Главный стык: аккаунт, заведённый моком, находится настоящим клиентом."""
    api = FakeApi()
    created = api.create_account(mail="user@example.com", mail_verified_at=NOW)

    client = _client_for(api)
    async with client:
        found = await client.find_account_by_mail("user@example.com")

    assert found.id == created.id
    assert found.mail == "user@example.com"


async def test_client_reports_missing_account_as_bot_error():
    """Отсутствие аккаунта — осмысленный отказ с кодом, а не сетевая ошибка.

    Разница важна вызывающему коду: по коду он решает «завести аккаунт»,
    а по сетевой ошибке — «повторить»."""
    api = FakeApi()

    client = _client_for(api)
    async with client:
        with pytest.raises(BotError) as exc:
            await client.find_account_by_mail("nobody@example.com")

    assert exc.value.code == "account_not_found"


async def test_client_without_secret_never_reaches_the_wire():
    """Клиент без секрета не должен молча ходить без заголовка."""
    with pytest.raises(BotConfigError):
        BotClient(base_url=BASE, secret="")


async def test_telegram_only_account_round_trips():
    """Аккаунт без почты — не вырожденный случай: у него `mail` пуст,
    и это не должно ломать разбор ответа."""
    api = FakeApi()
    api.create_account(chat_id=777_000_111)

    client = _client_for(api)
    async with client:
        found = await client.find_account_by_chat_id(777_000_111)

    assert found.chat_id == 777_000_111
    assert found.mail is None
