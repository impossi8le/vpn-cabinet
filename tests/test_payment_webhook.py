"""Вебхук ЮKassa: единственное основание выдать оплаченное (#61).

Уведомление приходит извне и **никем не подписано** — подписи у современного
API ЮKassa нет. Поэтому проверок две, и они разной природы, а тесты держат обе:

1. **Доказательство — перезапрос платежа у провайдера.** Заказ берётся из
   `metadata` его ответа, а не из тела уведомления: `metadata.order_id` ставит
   только кабинет, и подделать эту связь нельзя. Тест `..._from_provider_metadata`
   шлёт тело с чужим заказом и проверяет, что исполнен всё равно тот, который
   назвал провайдер.
2. **Сверка адреса источника — фильтр, а не доказательство.** Уведомление из
   чужой сети всё равно проверяется у провайдера и **исполняется**: список сетей
   стареет, и отказ по устаревшему списку стоил бы настоящих покупок.

Коды ответа — часть договора с провайдером, и это тоже проверяется: **200** там,
где повтор не поможет (чужое событие, сумма не сошлась, заказа нет), и **503**
там, где повтор нужен (не дозвонились до ЮKassa или до бота). Ответ 200 в
последнем случае «съел» бы оплату.

Идемпотентность проверяется повтором, а не первым вызовом: одно уведомление —
один комплект конфигов и одно письмо.

Сети нет: подставной клиент ЮKassa и подставной бот (`tests/fakes.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from cabinet.app import build_app
from cabinet.bot_client import BotUnreachable
from cabinet.mail_sender import MailSender, MailTransportError
from cabinet.payments import Payment, PaymentsProviderError, PaymentsTransportError
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CHAT_ID = 123_456_789
MAIL = "buyer@example.com"
WEBHOOK = "/payments/webhook/yookassa"
PAYMENT_ID = "pay-1"
# Адрес заведомо не из сетей ЮKassa: проверка адреса — фильтр, и он не должен
# мешать исполнению настоящего платежа.
FOREIGN_IP = "203.0.113.7"


class _ScriptedPayments:
    """Подставной клиент ЮKassa: отдаёт заданный платёж или падает заданным сбоем."""

    def __init__(self, payment: Payment | None = None, error: Exception | None = None) -> None:
        self.payment = payment
        self.error = error
        self.reads: list[str] = []

    def create_payment(self, **_kwargs: object) -> Payment:  # pragma: no cover - вебхук не платит
        raise AssertionError("вебхук не создаёт платежи")

    def get_payment(self, payment_id: str) -> Payment:
        self.reads.append(payment_id)
        if self.error is not None:
            raise self.error
        if self.payment is None:  # pragma: no cover - тесты всегда задают платёж
            raise AssertionError("платёж не задан")
        return self.payment

    def return_url(self, order_id: str) -> str:  # pragma: no cover - не нужен вебхуку
        raise AssertionError("вебхук не собирает адрес возврата")


class _BrokenMail:
    """Транспорт, который не смог отправить письмо."""

    def send(self, **_kwargs: object) -> None:
        raise MailTransportError("smtp отвалился")


def _api_with_order(amount_rub: int = 140) -> tuple[FakeApi, str]:
    """Оплаченный на вид заказ аккаунта с почтой и Telegram."""
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID, mail=MAIL, mail_verified_at=NOW)
    order = api.create_order(
        account_id=account.id,
        days=30,
        devices=1,
        servers=["nl-1"],
        amount_rub=amount_rub,
    )
    return api, order.order_id


def _payment(
    *,
    order_id: str | None,
    amount_rub: int = 140,
    status: str = "succeeded",
    payment_id: str = PAYMENT_ID,
) -> Payment:
    return Payment(
        id=payment_id,
        status=status,
        amount_rub=amount_rub,
        order_id=order_id,
    )


def _client(
    api: FakeApi,
    payments: _ScriptedPayments,
    mail_sender: MailSender | None = None,
) -> tuple[TestClient, FakeMailSender]:
    sender = FakeMailSender()
    app = build_app(
        store=api,
        mail_sender=mail_sender or MailSender(sender="cabinet@example.com", transport=sender),
        payments=payments,
        now=lambda: NOW,
    )
    return TestClient(app, raise_server_exceptions=False), sender


def _body(event: str = "payment.succeeded", *, order_id: str | None = None) -> dict:
    """Тело уведомления. `metadata` в теле намеренно может врать — см. тест ниже."""
    obj: dict = {"id": PAYMENT_ID, "status": "succeeded"}
    if order_id is not None:
        obj["metadata"] = {"order_id": order_id}
    return {"event": event, "object": obj}


def _calls(api: FakeApi, name: str) -> list[dict]:
    return [kwargs for call, kwargs in api.calls if call == name]


def _post(client: TestClient, body: dict, *, ip: str | None = None):
    headers = {"X-Real-IP": ip} if ip else {}
    return client.post(WEBHOOK, json=body, headers=headers)


# --- событие ---------------------------------------------------------------


def test_other_events_are_ignored() -> None:
    """Не наше событие — не трогаем ничего и отвечаем 200, чтобы не было повторов."""
    api, _ = _api_with_order()
    payments = _ScriptedPayments(_payment(order_id=None))
    client, sender = _client(api, payments)

    response = _post(client, _body("payment.canceled"))

    assert response.status_code == 200
    assert payments.reads == []
    assert sender.outbox == []
    assert _calls(api, "mark_order_paid") == []


def test_payment_without_order_metadata_is_ignored() -> None:
    """Платёж без нашего заказа в метаданных — чужой или созданный не нами."""
    api, _ = _api_with_order()
    payments = _ScriptedPayments(_payment(order_id=None))
    client, sender = _client(api, payments)

    response = _post(client, _body())

    assert response.status_code == 200
    assert _calls(api, "mark_order_paid") == []
    assert sender.outbox == []


def test_payment_that_is_not_succeeded_does_nothing() -> None:
    """`pending`/`canceled` — деньги не пришли, выдавать нечего."""
    api, order_id = _api_with_order()
    payments = _ScriptedPayments(_payment(order_id=order_id, status="pending"))
    client, sender = _client(api, payments)

    response = _post(client, _body())

    assert response.status_code == 200
    assert _calls(api, "issue_order") == []
    assert sender.outbox == []


# --- счастливый путь -------------------------------------------------------


def test_confirmed_payment_issues_configs_and_sends_letter() -> None:
    """Подтверждённый платёж: заказ оплачен, конфиги выданы, письмо ушло файлом.

    Главный тест задачи. Проверяются все три шага сразу, потому что по
    отдельности они бы не значили ничего: оплата без выдачи — деньги ни за что,
    выдача без письма — оплаченное в месте, куда человек не может попасть, а
    письмо без файла — та же потерянная покупка.
    """
    api, order_id = _api_with_order()
    payments = _ScriptedPayments(_payment(order_id=order_id))
    client, sender = _client(api, payments)

    response = _post(client, _body(), ip=FOREIGN_IP)

    assert response.status_code == 200
    assert _calls(api, "mark_order_paid") == [{"order_id": order_id, "amount_rub": 140}]
    # Конфиг сгенерирован ботом ровно один раз — и письмо ушло на адрес аккаунта.
    assert len(_calls(api, "issue_order")) == 1
    message = sender.sent_to(MAIL)[-1]
    filename, content, mime = message["attachments"][0]  # type: ignore[index]
    assert filename == "cfg-1.ovpn"
    assert content == b"client\nremote cfg-1\n"
    assert mime == "application/x-openvpn-profile"
    # Ссылок вместо файла в письме нет: ссылка требует сессии кабинета, а из
    # почтового клиента человек приходит без неё.
    assert "http" not in str(message["body"])


def test_repeat_of_the_same_notification_sends_nothing_twice() -> None:
    """Повтор уведомления не создаёт второго комплекта и второго письма.

    ЮKassa повторяет уведомление при не-2xx, и это штатный ход, а не сбой.
    Проверка именно на повторе: первый вызов вёдет себя одинаково у верного и
    неверного кода, и тест на одном вызове был бы пустым.
    """
    api, order_id = _api_with_order()
    payments = _ScriptedPayments(_payment(order_id=order_id))
    client, sender = _client(api, payments)

    _post(client, _body())
    second = _post(client, _body())

    assert second.status_code == 200
    assert len(_calls(api, "issue_order")) == 1
    assert len(sender.outbox) == 1


# --- доказательство берётся у провайдера -----------------------------------


def test_order_comes_from_provider_metadata_not_from_the_body() -> None:
    """Тело уведомления не решает, какой заказ оплачен: решает ответ провайдера.

    Подделанное уведомление может назвать чужой заказ — и тогда выдан был бы
    чужой. Заказ берётся из `metadata` ответа ЮKassa, а её ставит только кабинет.
    """
    api, own_order = _api_with_order()
    other = api.create_order(
        account_id=api.create_account(
            chat_id=CHAT_ID + 1, mail="other@example.com", mail_verified_at=NOW
        ).id,
        days=30,
        devices=1,
        servers=["ua-1"],
        amount_rub=140,
    )
    payments = _ScriptedPayments(_payment(order_id=own_order))
    client, sender = _client(api, payments)

    response = _post(client, _body(order_id=other.order_id))

    assert response.status_code == 200
    assert _calls(api, "mark_order_paid") == [{"order_id": own_order, "amount_rub": 140}]
    assert _calls(api, "mark_order_paid")[0]["order_id"] != other.order_id
    assert sender.sent_to(MAIL) != []


def test_amount_that_differs_from_the_order_is_refused() -> None:
    """Сумма сверяется с заказом, и расхождение не отмечает заказ оплаченным.

    Спека §3.5. Проверяются обе стороны: заказ остаётся неоплаченным и письмо не
    уходит — иначе «проверка суммы» была бы строкой в журнале.
    """
    api, order_id = _api_with_order()
    payments = _ScriptedPayments(_payment(order_id=order_id, amount_rub=1))
    client, sender = _client(api, payments)

    response = _post(client, _body())

    assert response.status_code == 200
    assert _calls(api, "mark_order_paid") == []
    assert _calls(api, "issue_order") == []
    assert sender.outbox == []


# --- когда повтор нужен и когда нет ----------------------------------------


def test_unreachable_provider_asks_for_a_repeat() -> None:
    """Провайдер недоступен — отвечаем 503: уведомление надо повторить.

    200 здесь «съел» бы оплату: ЮKassa больше не напомнит, а заказ остался бы
    неоплаченным. Отличить этот случай от «провайдер отказал» — и есть причина,
    по которой у клиента два разных исключения.
    """
    api, _ = _api_with_order()
    payments = _ScriptedPayments(error=PaymentsTransportError("нет связи"))
    client, _sender = _client(api, payments)

    response = _post(client, _body())

    assert response.status_code == 503


def test_provider_refusal_does_not_ask_for_a_repeat() -> None:
    """Отказ провайдера повтором не лечится — отвечаем 200."""
    api, _ = _api_with_order()
    payments = _ScriptedPayments(error=PaymentsProviderError("ответил 400"))
    client, _sender = _client(api, payments)

    response = _post(client, _body())

    assert response.status_code == 200


def test_unreachable_bot_asks_for_a_repeat() -> None:
    """Бот не ответил — тоже 503: отметить оплату не удалось, повтор обязателен."""

    class _BlindApi(FakeApi):
        def get_order(self, order_id: str):
            raise BotUnreachable("нет связи")

    api, order_id = _api_with_order()
    payments = _ScriptedPayments(_payment(order_id=order_id))
    client, _sender = _client(_BlindApi(), payments)

    response = _post(client, _body())

    assert response.status_code == 503


# --- сбой письма не отменяет оплату ----------------------------------------


def test_broken_mail_does_not_fail_the_webhook() -> None:
    """Оплата и выдача уже совершены — сбой письма их не отменяет.

    Ответ остаётся 200: повтор уведомления не перешлёт письмо (заказ уже
    отмечен, `changed=false`), поэтому 503 здесь только гонял бы ЮKassa впустую.
    Доступ при этом не теряется: конфиги лежат в подписках кабинета.
    """
    api, order_id = _api_with_order()
    payments = _ScriptedPayments(_payment(order_id=order_id))
    client, _sender = _client(
        api,
        payments,
        mail_sender=MailSender(sender="cabinet@example.com", transport=_BrokenMail()),
    )

    response = _post(client, _body())

    assert response.status_code == 200
    assert _calls(api, "mark_order_paid") == [{"order_id": order_id, "amount_rub": 140}]
    assert _calls(api, "issue_order") != []


def test_account_without_mail_gets_no_letter_and_no_error() -> None:
    """Аккаунт без адреса: письма нет, но выдача состоялась и вебхук не упал."""
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)  # без почты: вошёл только Telegram
    order = api.create_order(
        account_id=account.id, days=30, devices=1, servers=["nl-1"], amount_rub=140
    )
    payments = _ScriptedPayments(_payment(order_id=order.order_id))
    client, sender = _client(api, payments)

    response = _post(client, _body())

    assert response.status_code == 200
    assert _calls(api, "issue_order") != []
    assert sender.outbox == []


@pytest.mark.parametrize("ip", [None, "", "not-an-ip", FOREIGN_IP])
def test_source_address_never_blocks_a_real_payment(ip: str | None) -> None:
    """Адрес источника — фильтр: вне сетей ЮKassa платёж всё равно исполняется.

    Если сделать проверку решающей, устаревший список сетей начал бы отказывать
    настоящим платежам — а это потерянные покупки, а не защита. Подлинность
    доказывает ответ провайдера, и он проверен выше.
    """
    api, order_id = _api_with_order()
    payments = _ScriptedPayments(_payment(order_id=order_id))
    client, sender = _client(api, payments)

    response = _post(client, _body(), ip=ip)

    assert response.status_code == 200
    assert _calls(api, "mark_order_paid") == [{"order_id": order_id, "amount_rub": 140}]
    assert len(sender.outbox) == 1


# --- право на экране возврата ----------------------------------------------


def test_done_screen_does_not_show_another_accounts_order() -> None:
    """Чужой заказ показывается как несуществующий: номер заказа — не пропуск.

    Номер заказа приходит из адреса, и если экран возврата отдаёт заказ по нему
    без проверки владельца, то любой вошедший открывает чужую покупку, зная
    номер. Отказ тот же, что у несуществующего заказа: подтверждать, что заказ с
    таким номером есть, тоже нельзя.
    """
    api, order_id = _api_with_order()
    stranger = api.create_account(
        chat_id=999_000_111, mail="stranger@example.com", mail_verified_at=NOW
    )
    payments = _ScriptedPayments(_payment(order_id=order_id))
    client, _sender = _client(api, payments)
    client.cookies.set("cabinet_session", client.app.state.sessions.sign_in(stranger.id).token)

    html = client.get(f"/buy/done?order_id={order_id}").text

    assert "Оплачено" not in html
    assert "не найден" in html.lower()
