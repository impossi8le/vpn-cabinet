"""Экран оплаты (шаг 5, #61): заказ → платёж → страница оплаты.

Экран оплаты — самый нагруженный на состояния: нет сессии, нет Telegram, бот не
ответил, набор серверов не собран, платёж не создан. Каждое состояние проверяется
своим текстом — слить их в одно «оплата не удалась» значило бы отнять у человека
единственную подсказку, что делать.

Главное правило денег — здесь же: **сумма берётся из заказа**, а не из адреса.
Платёж создаётся на `order.amount_rub`, и подмена цены в query ничего не меняет.

Ссылка на экран берётся **от разметки**: тест идёт по `href`, нарисованному на
экране выбора серверов, а не собирает адрес руками.

Ни сети, ни базы: подставной бот (`FakeApi`) и подставная ЮKassa в памяти.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import BotUnreachable
from cabinet.mail_sender import MailSender
from cabinet.payments import Payment, PaymentsProviderError
from cabinet.render import (
    PAY_BOT_UNREACHABLE_NOTE,
    PAY_FAILED_NOTE,
    PAY_INCOMPLETE_NOTE,
    PAY_ORDER_REJECTED_NOTE,
    PAY_TELEGRAM_REQUIRED_NOTE,
)
from cabinet.theme import COLORS
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CHAT_ID = 123_456_789
CONFIRM_URL = "https://pay.example.com/confirm"


class StubPayments:
    """Подставная ЮKassa: платежа нет, но запись о вызове есть.

    Тесту нужно видеть, на какую сумму и по какому заказу кабинет попросил платёж.
    Наружу ничего не уходит; `fail` задаёт отказ провайдера для проверки
    отдельного состояния.
    """

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.created: list[dict] = []
        self.fail = fail

    def return_url(self, order_id: str) -> str:
        return f"https://cabinet.example.com/buy/done?order_id={order_id}"

    def create_payment(
        self, *, order_id: str, amount_rub: int, description: str, return_url: str
    ) -> Payment:
        if self.fail is not None:
            raise self.fail
        self.created.append(
            {
                "order_id": order_id,
                "amount_rub": amount_rub,
                "description": description,
                "return_url": return_url,
            }
        )
        return Payment(
            id="pay-1", status="pending", amount_rub=amount_rub, confirmation_url=CONFIRM_URL
        )

    def get_payment(self, payment_id: str) -> Payment:  # pragma: no cover - не зовётся экраном
        raise NotImplementedError


class _LinkParser(HTMLParser):
    """Ссылки разметки: адрес и подпись — «от разметки» для этого экрана."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, self._text.strip()))
            self._href = None
            self._text = ""


def _hrefs(html: str, label: str) -> list[str]:
    parser = _LinkParser()
    parser.feed(html)
    return [href for href, text in parser.links if text == label]


def _api_with_servers() -> FakeApi:
    api = FakeApi()
    api.add_server("nl-1", "Нидерланды · Амстердам")
    api.add_server("de-1", "Германия · Франкфурт")
    api.add_server("fi-1", "Финляндия · Хельсинки")
    return api


def _client(api: FakeApi, payments: StubPayments, account_id: int) -> TestClient:
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
        payments=payments,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account_id).token)
    return client


def _telegram_client(
    payments: StubPayments | None = None,
) -> tuple[TestClient, FakeApi, StubPayments]:
    """Вошедший аккаунт с Telegram: только ему бот продаёт конфиг."""
    api = _api_with_servers()
    account = api.create_account(chat_id=CHAT_ID)
    payments = payments if payments is not None else StubPayments()
    return _client(api, payments, account.id), api, payments


class _NoServers(FakeApi):
    """Подставной бот, не отвечающий на список серверов — сбой связи."""

    def list_servers(self):  # type: ignore[override]
        raise BotUnreachable("нет связи")


def _markup_without_palette(html: str) -> str:
    start = html.find(":root {")
    end = html.find("}", start)
    return html[:start] + html[end:]


# --- без сессии — на вход -------------------------------------------------


def test_pay_without_session_redirects_to_login() -> None:
    """Без cookie — редирект на вход, а не 500 и не чужой экран."""
    client = TestClient(
        build_app(store=FakeApi(), payments=StubPayments()), raise_server_exceptions=False
    )

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- успех: заказ и платёж ------------------------------------------------


def test_pay_creates_order_and_redirects_to_payment() -> None:
    """Набор полон — заказ создан, платёж создан, человек уходит на оплату."""
    client, api, payments = _telegram_client()

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == CONFIRM_URL
    assert len(api.orders) == 1
    assert api.orders[0].servers == ["nl-1"]
    assert len(payments.created) == 1
    assert payments.created[0]["order_id"] == api.orders[0].order_id


def test_payment_amount_comes_from_the_order_not_the_address() -> None:
    """Сумма — из заказа, а не из адреса.

    В query лежат заведомо чужие числа (`amount=1`, `price=1`); платёж обязан
    просить посчитанную сумму (1913 ₽ для 180 дней на 3 устройства), ту же, что
    записана в заказ. Прими экран сумму из адреса — покупка стоила бы рубль.
    """
    client, api, payments = _telegram_client()

    client.get(
        "/buy/pay?days=180&devices=3&pick=nl-1&pick=de-1&pick=fi-1&amount=1&price=1",
        follow_redirects=False,
    )

    assert api.orders[0].amount_rub == 1913
    assert payments.created[0]["amount_rub"] == 1913
    assert payments.created[0]["amount_rub"] == api.orders[0].amount_rub


def test_pay_passes_servers_and_amount_to_the_order() -> None:
    """Заказ получает выбранные серверы и посчитанную цену ровно один раз."""
    client, api, _payments = _telegram_client()

    client.get("/buy/pay?days=30&devices=2&pick=nl-1&pick=nl-1", follow_redirects=False)

    assert api.orders[0].servers == ["nl-1", "nl-1"]
    assert api.orders[0].devices == 2
    assert api.orders[0].amount_rub == 252  # 2×140 со скидкой комплекта 10%


# --- нет Telegram: граница сервиса, а не сбой -----------------------------


def test_telegram_required_is_its_own_state() -> None:
    """У аккаунта нет Telegram — свой текст, и к оплате не ведём.

    Это граница сервиса: бот отказывается продавать, потому что конфиг выдаётся
    Telegram-аккаунту. Экран объясняет это и ведёт привязать Telegram, а не
    показывает «ошибку оплаты».
    """
    api = _api_with_servers()
    account = api.create_account(mail="nolink@example.com", mail_verified_at=NOW)
    payments = StubPayments()
    client = _client(api, payments, account.id)

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1", follow_redirects=False)

    assert response.status_code == 200
    assert PAY_TELEGRAM_REQUIRED_NOTE in response.text
    # Платежа нет и заказа нет: до оплаты дело не дошло.
    assert api.orders == []
    assert payments.created == []
    # Ведём привязать Telegram, а не просто сообщаем отказ.
    assert _hrefs(response.text, "Привязать Telegram") == ["/account"]


# --- набор не собран ------------------------------------------------------


def test_incomplete_selection_returns_to_servers() -> None:
    """Выбрано меньше, чем устройств, — шаг 4 не закончен, и экран ведёт туда."""
    client, api, payments = _telegram_client()

    response = client.get("/buy/pay?days=30&devices=2&pick=nl-1", follow_redirects=False)

    assert response.status_code == 200
    assert PAY_INCOMPLETE_NOTE in response.text
    assert api.orders == []
    assert payments.created == []
    # Возврат — к выбору серверов с тем же набором.
    assert _hrefs(response.text, "Выбрать серверы") == ["/buy/servers?days=30&devices=2&pick=nl-1"]


def test_unknown_server_code_does_not_complete_the_selection() -> None:
    """Чужой код сервера не считается выбором: заказ на несуществующий сервер не создаётся."""
    client, api, _payments = _telegram_client()

    response = client.get("/buy/pay?days=30&devices=1&pick=нет-такого", follow_redirects=False)

    assert PAY_INCOMPLETE_NOTE in response.text
    assert api.orders == []


# --- бот недоступен: не «ошибка оплаты» -----------------------------------


def test_bot_unreachable_is_not_a_payment_error() -> None:
    """Сбой бота — своё состояние, а не «оплата не удалась».

    Причина не в оплате и не в человеке, и действие другое — повторить. Слей их —
    и недоступность сервиса читалась бы как отказ человека.
    """
    api = _NoServers()
    account = api.create_account(chat_id=CHAT_ID)
    payments = StubPayments()
    client = _client(api, payments, account.id)

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1", follow_redirects=False)

    assert PAY_BOT_UNREACHABLE_NOTE in response.text
    assert PAY_FAILED_NOTE not in response.text
    assert payments.created == []


# --- платёж не создан: своё состояние -------------------------------------


def test_payment_provider_failure_is_its_own_state() -> None:
    """Провайдер отказал — «оплата не начата», и это **не** недоступность бота.

    Заказ уже создан, страница оплаты не выдана. Текст говорит про оплату, а не
    про бота: у отказов разные причины и разные действия.
    """
    payments = StubPayments(fail=PaymentsProviderError("отказ провайдера"))
    client, api, _payments = _telegram_client(payments)

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1", follow_redirects=False)

    assert response.status_code == 200
    assert PAY_FAILED_NOTE in response.text
    assert PAY_BOT_UNREACHABLE_NOTE not in response.text
    # Заказ успел создаться — отказ на шаге платежа, а не заказа.
    assert len(api.orders) == 1


class _RejectingBot(FakeApi):
    """Подставной бот, отказывающий в заказе по причине, отличной от Telegram."""

    def create_order(self, **kwargs):  # type: ignore[override]
        from cabinet.bot_client import BotError

        raise BotError("order_rejected", "бот отказал")


def test_order_refusal_has_its_own_state() -> None:
    """Прочий отказ бота — не «нет Telegram» и не «оплата не удалась»."""
    api = _RejectingBot()
    api.add_server("nl-1", "Нидерланды · Амстердам")
    account = api.create_account(chat_id=CHAT_ID)
    client = _client(api, StubPayments(), account.id)

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1", follow_redirects=False)

    assert PAY_ORDER_REJECTED_NOTE in response.text
    assert PAY_TELEGRAM_REQUIRED_NOTE not in response.text


# --- ссылка на оплату из разметки шага 4 ----------------------------------


def test_pay_link_appears_when_the_selection_is_complete() -> None:
    """Ссылка «Перейти к оплате» появляется там, где набор объявлен полным."""
    client, _api, _payments = _telegram_client()

    html = client.get("/buy/servers?days=30&devices=1&pick=nl-1").text

    assert _hrefs(html, "Перейти к оплате") == ["/buy/pay?days=30&devices=1&pick=nl-1"]


def test_pay_link_is_absent_while_the_selection_is_incomplete() -> None:
    """Пока набор не полон, оплаты на экране нет: шаг не закончен."""
    client, _api, _payments = _telegram_client()

    html = client.get("/buy/servers?days=30&devices=2&pick=nl-1").text

    assert _hrefs(html, "Перейти к оплате") == []


def test_pay_link_from_the_page_leads_to_payment() -> None:
    """Адрес оплаты берётся у страницы, а не собирается руками: идём по `href`."""
    client, _api, _payments = _telegram_client()
    page = client.get("/buy/servers?days=30&devices=1&pick=nl-1").text
    pay_href = _hrefs(page, "Перейти к оплате")[0]

    response = client.get(pay_href, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == CONFIRM_URL


# --- цвет ----------------------------------------------------------------


def test_pay_refusal_screen_has_no_green() -> None:
    """Оплата — ни подтверждённая защита, ни опасность (§7): зелёного нет."""
    client, _api, _payments = _telegram_client()

    html = client.get("/buy/pay?days=30&devices=2&pick=nl-1").text

    assert COLORS.green.upper() not in _markup_without_palette(html).upper()
