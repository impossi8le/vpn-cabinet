"""Воронка кабинета: восемь шагов одной последовательностью (issue #64).

Кабинет пишет шаги воронки боту (`POST /internal/cabinet-events`) — от показа
экрана входа до выданных после оплаты конфигов. Здесь проверяется то, чего не
видит ни один тест экрана: **последовательность** шагов за один проход человека
(вход → покупка → оплата), а не «событие вообще было». Рядом — правило, которое
нельзя нарушить: сбой записи не ломает экран.

Переходы берутся из разметки (`tests/markup.py`), адреса руками не собираются.
Сети нет: подставной бот и подставная ЮKassa (`tests/fakes.py`).

Первые два шага анонимны (`account_id` нет) — их связывает идентификатор
посещения в cookie (`cabinet_visitor`), и тест это утверждает: без него воронка
считала бы показы, а не людей.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient

from cabinet.app import VISITOR_COOKIE, build_app
from cabinet.bot_client import EVENTS_PATH, SECRET_HEADER, BotClient, BotStore, BotUnreachable
from cabinet.mail_sender import MailSender
from cabinet.payments import Payment
from tests.fakes import FakeApi, FakeMailSender
from tests.markup import form, hrefs, link

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "buyer@example.com"
CHAT_ID = 123_456_789
CONFIRM_URL = "https://pay.example.com/confirm"
PAYMENT_ID = "pay-1"
WEBHOOK = "/payments/webhook/yookassa"
BASE = "http://127.0.0.1:9"
SECRET = "funnel-events-test-secret"
# Адрес заведомо вне сетей ЮKassa: проверка адреса — фильтр, а не отказ.
FOREIGN_IP = "203.0.113.7"

# Ровно эти имена и в этом порядке — контракт воронки с ботом (issue #64).
FUNNEL = (
    "login_shown",
    "code_requested",
    "signed_in",
    "buy_shown",
    "confirm_shown",
    "pay_started",
    "paid",
    "issued",
)


class _StubPayments:
    """Подставная ЮKassa: помнит заказ, подтверждает его же на перезапросе.

    Сумма и `order_id` возвращаются **те** же, что кабинет положил в заказ, —
    иначе сверка суммы в вебхуке отвергла бы настоящее уведомление.
    """

    def __init__(self) -> None:
        self._order_id: str | None = None
        self._amount_rub: int = 0

    def return_url(self, order_id: str) -> str:
        return f"https://cabinet.example.com/buy/done?order_id={order_id}"

    def create_payment(
        self, *, order_id: str, amount_rub: int, description: str, return_url: str
    ) -> Payment:
        self._order_id = order_id
        self._amount_rub = amount_rub
        return Payment(
            id=PAYMENT_ID,
            status="pending",
            amount_rub=amount_rub,
            confirmation_url=CONFIRM_URL,
        )

    def get_payment(self, payment_id: str) -> Payment:
        return Payment(
            id=payment_id,
            status="succeeded",
            amount_rub=self._amount_rub,
            order_id=self._order_id,
        )


def _client(api: FakeApi) -> tuple[TestClient, FakeMailSender, _StubPayments]:
    sender = FakeMailSender()
    payments = _StubPayments()
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        payments=payments,
        now=lambda: NOW,
    )
    return TestClient(app, raise_server_exceptions=False), sender, payments


def _sign_in_by_mail(client: TestClient, sender: FakeMailSender) -> None:
    """Войти по почте настоящими формами разметки — как это делает браузер."""
    login_page = client.get("/login").text
    entry = form(login_page, "/login")
    fields = dict(entry["fields"])
    fields["mail"] = MAIL
    sent = client.post(entry["action"], data=fields, follow_redirects=False)
    assert sent.status_code == 303, "письмо с кодом не ушло"

    code = "".join(ch for ch in str(sender.sent_to(MAIL)[-1]["body"]) if ch.isdigit())[:6]
    waiting = client.get(sent.headers["location"]).text
    verify = form(waiting, "/login/verify")
    payload = dict(verify["fields"])
    payload["mail"] = MAIL
    payload["code"] = code
    response = client.post(verify["action"], data=payload, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/subscriptions"


def _buy_up_to_pay(client: TestClient) -> None:
    """Провести покупку по ссылкам разметки до `/buy/pay` (успех — редирект)."""
    subscriptions = client.get("/subscriptions").text
    buy = client.get(link(subscriptions, "Купить подписку")).text
    confirm = client.get(link(buy, "Продолжить")).text
    servers = client.get(link(confirm, "Выбрать серверы")).text
    # Одно устройство по умолчанию — ровно один сервер, иначе шаг не завершён.
    servers = client.get(hrefs(servers, "Добавить")[0]).text
    pay = client.get(link(servers, "Перейти к оплате"), follow_redirects=False)
    assert pay.status_code == 303 and pay.headers["location"] == CONFIRM_URL


def _paid_journey(api: FakeApi) -> TestClient:
    """Один проход: вход → покупка → оплата вебхуком. Аккаунт с Telegram."""
    account = api.create_account(mail=MAIL, mail_verified_at=NOW, chat_id=CHAT_ID)
    api.set_trial(account.id, eligible=True)
    api.add_server(code="nl-1", name="Нидерланды · Амстердам")
    client, sender, _payments = _client(api)
    _sign_in_by_mail(client, sender)
    _buy_up_to_pay(client)
    webhook = client.post(
        WEBHOOK,
        json={"event": "payment.succeeded", "object": {"id": PAYMENT_ID, "status": "succeeded"}},
        headers={"X-Real-IP": FOREIGN_IP},
    )
    assert webhook.status_code == 200
    return client


# --- последовательность восьми шагов за один проход ------------------------


def test_funnel_is_written_step_by_step_in_order() -> None:
    """Все восемь шагов за один проход — и **в том порядке**, что в контракте.

    Утверждается последовательность, а не наличие: сдвинь шаг с места — тест
    обязан покраснеть, потому что воронка меряет путь, а порядок шагов и есть
    путь.
    """
    api = FakeApi()
    _paid_journey(api)
    assert [event["step"] for event in api.events] == list(FUNNEL)


def test_anonymous_steps_share_one_visitor_and_have_no_account() -> None:
    """Два первых шага анонимны, но связаны одним посещением.

    Без идентификатора посещения `login_shown` и `code_requested` считались бы
    двумя разными людьми, и воронка показывала бы показы, а не людей (issue #64).
    """
    api = FakeApi()
    _paid_journey(api)
    events = {event["step"]: event for event in api.events}

    assert events["login_shown"]["account_id"] is None
    assert events["code_requested"]["account_id"] is None
    visitor = events["login_shown"]["visitor"]
    assert visitor, "идентификатор посещения пуст — людей не сосчитать"
    assert events["code_requested"]["visitor"] == visitor, "шаги одного входа разошлись"


def test_identified_steps_carry_account_and_order() -> None:
    """Шаги после входа называют человека и заказ — по ним бот и строит воронку."""
    api = FakeApi()
    _paid_journey(api)
    events = {event["step"]: event for event in api.events}

    assert events["signed_in"]["account_id"] == api.accounts[0].id
    order_id = events["pay_started"]["order_id"]
    assert order_id, "у шага оплаты нет номера заказа"
    # Вебхук — та же покупка: `paid` и `issued` несут тот же заказ и человека.
    assert events["paid"]["order_id"] == order_id
    assert events["issued"]["order_id"] == order_id
    assert events["paid"]["account_id"] == api.accounts[0].id


def test_visitor_cookie_is_set_http_only_and_comes_back_on_the_journey() -> None:
    """Cookie посещения ставится на экране входа и едет дальше.

    `HttpOnly` и `SameSite=Lax` — как у сессии: значение не секрет, но разметке
    в нём делать нечего, а переход по ссылке обязан его донести.
    """
    api = FakeApi()
    client, _sender, _payments = _client(api)
    response = client.get("/login")
    set_cookie = response.headers["set-cookie"]
    assert VISITOR_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower() or "samesite=lax" in set_cookie.lower()
    assert client.cookies.get(VISITOR_COOKIE), "cookie не сохранилась у клиента"


# --- правило: аналитика не ломает экраны (issue #64) -----------------------


def test_broken_event_recording_does_not_break_the_screen() -> None:
    """Бот падает на записи события — человек этого не замечает.

    Экран отвечает как обычно, вход и покупка работают: иначе сломанная
    аналитика стала бы отказом сервиса. Проверяется целым проходом, а не одним
    GET: сбой случается на **каждом** шаге, и ни один не должен уронить страницу.
    """
    api = FakeApi()
    api.events_error = BotUnreachable("бот недоступен")
    account = api.create_account(mail=MAIL, mail_verified_at=NOW, chat_id=CHAT_ID)
    api.set_trial(account.id, eligible=True)
    api.add_server(code="nl-1", name="Нидерланды · Амстердам")
    client, sender, _payments = _client(api)

    assert client.get("/login").status_code == 200
    _sign_in_by_mail(client, sender)  # 303 и сессия, хотя `signed_in` не записался
    _buy_up_to_pay(client)  # покупка доходит до оплаты, хотя шаги не записались
    # Ни одного события не сохранилось — запись падала на каждом шаге.
    assert api.events == []


# --- контракт записи: тело дословно, «нет значения» это `null` -------------


async def test_client_posts_the_contract_body_with_nulls_not_missing_keys() -> None:
    """Оба клиента шлют ровно четыре поля, а `None` — как `null`, не пробелом.

    Контракт бота различает «значения нет» (`null`) и «поле забыли» (ключа нет),
    поэтому синхронный `BotStore` и асинхронный `BotClient` обязаны собирать тело
    одинаково — по шагам до входа `account_id` и `order_id` именно `null`.
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EVENTS_PATH, f"чужой путь: {request.url.path}"
        assert request.headers.get(SECRET_HEADER), "запись идёт без общего секрета"
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"ok": True})

    transport = httpx.MockTransport(handler)
    bot = BotClient(
        base_url=BASE, secret=SECRET, client=httpx.AsyncClient(base_url=BASE, transport=transport)
    )
    store = BotStore(bot, http_client=httpx.Client(base_url=BASE, transport=transport))

    await bot.record_event(account_id=None, visitor="v-1", step="login_shown")
    store.record_event(account_id=7, visitor="v-1", step="paid", order_id="ord-1")
    await bot.aclose()

    assert seen == [
        {"account_id": None, "visitor": "v-1", "step": "login_shown", "order_id": None},
        {"account_id": 7, "visitor": "v-1", "step": "paid", "order_id": "ord-1"},
    ]
