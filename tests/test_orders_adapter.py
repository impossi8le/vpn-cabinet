"""Заказы через живой адаптер: роуты кабинета против подставного HTTP (#59, #61).

Тот же класс дефекта, что #46: каждый кусок в отдельности цел, а вместе они не
сходятся. Роуты кабинета синхронные и зовут `store.create_order`/`get_order`, а у
живого `BotClient` эти методы — корутины; форму им даёт адаптер `BotStore`. Тест
против `FakeApi` этого не увидел бы: у мока нужная форма есть по определению.
Поэтому здесь поднимается **настоящий** клиент поверх подставного HTTP и
запрашивается настоящий роут — как в `test_store_adapter.py`.

Проверяются и **точные пути** контракта бота (план, «Контракты»): расхождение
имён в этом проекте уже стоило зелёного теста, который ничего не защищал.

Сети нет: соединение подменено транспортом `httpx`. Адрес бота — loopback, как и
у прочих тестов стыка.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient

from cabinet.accounts import Account
from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import SECRET_HEADER, BotClient, BotError, BotStore
from cabinet.mail_sender import MailSender
from cabinet.payments import Payment
from tests.fakes import FakeMailSender

BASE = "http://127.0.0.1:9"
SECRET = "orders-adapter-test-secret"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CHAT_ID = 123_456_789
ORDER_ID = "ord-1"
CONFIRM_URL = "https://pay.example.com/confirm"


def _error(code: str, status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "retryable": False}})


def _account_json(account: Account) -> dict:
    return {
        "id": account.id,
        "chat_id": account.chat_id,
        "mail": account.mail,
        "mail_verified_at": (
            account.mail_verified_at.isoformat() if account.mail_verified_at else None
        ),
    }


class _StubPayments:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def return_url(self, order_id: str) -> str:
        return f"https://cabinet.example.com/buy/done?order_id={order_id}"

    def create_payment(
        self, *, order_id: str, amount_rub: int, description: str, return_url: str
    ) -> Payment:
        self.created.append({"order_id": order_id, "amount_rub": amount_rub})
        return Payment(
            id="pay-1", status="pending", amount_rub=amount_rub, confirmation_url=CONFIRM_URL
        )

    def get_payment(self, payment_id: str) -> Payment:  # pragma: no cover
        raise NotImplementedError


def _handler(account: Account, journal: list[tuple[str, str]]):
    """Подставной внутренний роутер бота: заказы кабинета по контракту плана."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get(SECRET_HEADER) != SECRET:
            return _error("invalid_secret", 401)
        path = request.url.path
        journal.append((request.method, path))

        if request.method == "GET" and path == "/internal/servers":
            return httpx.Response(200, json={"servers": [{"code": "nl-1", "name": "Амстердам"}]})
        if request.method == "GET" and path == f"/internal/accounts/{account.id}":
            return httpx.Response(200, json={"account": _account_json(account)})
        if request.method == "POST" and path == "/internal/cabinet-orders":
            return httpx.Response(
                201, json={"order_id": ORDER_ID, "status": "pending", "amount_rub": 140}
            )
        if request.method == "GET" and path == f"/internal/cabinet-orders/{ORDER_ID}":
            return httpx.Response(
                200,
                json={
                    "order_id": ORDER_ID,
                    "status": "pending",
                    "amount_rub": 140,
                    "days": 30,
                    "devices": 1,
                    "servers": ["nl-1"],
                    "configs": [],
                },
            )
        if request.method == "POST" and path == f"/internal/cabinet-orders/{ORDER_ID}/paid":
            return httpx.Response(
                200, json={"order_id": ORDER_ID, "status": "completed", "changed": True}
            )
        if request.method == "POST" and path == f"/internal/cabinet-orders/{ORDER_ID}/cancel":
            return httpx.Response(200, json={"order_id": ORDER_ID, "status": "cancelled"})
        if request.method == "POST" and path == f"/internal/cabinet-orders/{ORDER_ID}/issue":
            return httpx.Response(
                200,
                json={
                    "order_id": ORDER_ID,
                    "configs": [{"id": "cfg-1", "name": "nl-1"}],
                    "already": False,
                },
            )
        return _error("not_found", 404)

    return handler


def _store(handler) -> BotStore:
    transport = httpx.MockTransport(handler)
    return BotStore(
        BotClient(
            base_url=BASE,
            secret=SECRET,
            client=httpx.AsyncClient(base_url=BASE, transport=transport),
        ),
        http_client=httpx.Client(base_url=BASE, transport=transport),
    )


# --- точные пути контракта ------------------------------------------------


def test_create_order_hits_the_contract_path() -> None:
    """`POST /internal/cabinet-orders` — заказ создаётся на контрактном пути."""
    account = Account(id=1, chat_id=CHAT_ID)
    journal: list[tuple[str, str]] = []
    store = _store(_handler(account, journal))

    order = store.create_order(account_id=1, days=30, devices=1, servers=["nl-1"], amount_rub=140)

    assert ("POST", "/internal/cabinet-orders") in journal
    assert order.order_id == ORDER_ID
    assert order.amount_rub == 140


def test_get_order_hits_the_contract_path() -> None:
    """`GET /internal/cabinet-orders/{order_id}` — заказ читается по контракту."""
    account = Account(id=1, chat_id=CHAT_ID)
    journal: list[tuple[str, str]] = []
    store = _store(_handler(account, journal))

    order = store.get_order(ORDER_ID)

    assert ("GET", f"/internal/cabinet-orders/{ORDER_ID}") in journal
    assert order.status == "pending"
    assert order.days == 30
    assert order.servers == ("nl-1",)


def test_paid_cancel_issue_hit_the_contract_paths() -> None:
    """Отметка оплаты, отмена и выдача — на своих путях контракта."""
    account = Account(id=1, chat_id=CHAT_ID)
    journal: list[tuple[str, str]] = []
    store = _store(_handler(account, journal))

    paid = store.mark_order_paid(ORDER_ID)
    cancelled = store.cancel_order(ORDER_ID)
    issued = store.issue_order(ORDER_ID)

    assert ("POST", f"/internal/cabinet-orders/{ORDER_ID}/paid") in journal
    assert ("POST", f"/internal/cabinet-orders/{ORDER_ID}/cancel") in journal
    assert ("POST", f"/internal/cabinet-orders/{ORDER_ID}/issue") in journal
    assert paid.changed is True
    assert cancelled.status == "cancelled"
    assert issued.already is False
    assert [c.id for c in issued.configs] == ["cfg-1"]


# --- отказ бота по коду доезжает как `BotError` ---------------------------


def _telegram_required_handler(request: httpx.Request) -> httpx.Response:
    if request.headers.get(SECRET_HEADER) != SECRET:
        return _error("invalid_secret", 401)
    return _error("telegram_required", 409)


def test_create_order_passes_telegram_required_through() -> None:
    """`409 telegram_required` — осмысленный отказ, не свёрнутый в общий.

    По коду экран оплаты выбирает своё состояние («нет Telegram»), поэтому код
    обязан доехать неизменным, а не превратиться в «что-то пошло не так».
    """
    store = _store(_telegram_required_handler)

    try:
        store.create_order(account_id=1, days=30, devices=1, servers=["nl-1"], amount_rub=140)
    except BotError as exc:
        assert exc.code == "telegram_required"
    else:  # pragma: no cover - отказ обязан быть
        raise AssertionError("ожидался BotError telegram_required")


# --- стык «роут → адаптер → бот» ------------------------------------------


def test_buy_pay_screen_works_through_the_live_client() -> None:
    """Экран оплаты доходит до оплаты через живой адаптер, а не только на моке.

    Это и есть проверка стыка: роут синхронный, клиент — корутины, и без
    адаптера выложенный кабинет ответил бы 500 на самой дорогой части покупки.
    """
    account = Account(id=1, chat_id=CHAT_ID)
    journal: list[tuple[str, str]] = []
    payments = _StubPayments()
    app = build_app(
        store=_store(_handler(account, journal)),
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
        payments=payments,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(1).token)

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == CONFIRM_URL
    assert ("POST", "/internal/cabinet-orders") in journal
    assert payments.created[0]["amount_rub"] == 140
