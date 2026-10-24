"""Экран возврата с оплаты (шаг 5, #61): состояние **заказа**, а не перехода.

Главное правило: «оплачено» стоит здесь только когда так говорит заказ. Возврат
на страницу подделывается, подтверждение приходит вебхуком (спека §3), поэтому
экран читает заказ у бота и показывает его состояние как есть:

- заказ `completed` — «Оплачено»;
- заказ `pending` — честно «оплата ещё не подтверждена» (вебхук может дойти через
  секунды);
- заказ не найден и «бот не ответил» — **разные** состояния: недоступность
  сервиса не значит «заказа нет».

Про письмо с конфигом на экране не сказано: доставка письма — отдельная работа,
и обещать её, пока её нет, нечем. Тест это и держит.

Сети нет: подставной бот (`FakeApi`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import BotUnreachable
from cabinet.mail_sender import MailSender
from cabinet.render import (
    PAY_DONE_CANCELLED_NOTE,
    PAY_DONE_NOT_FOUND_NOTE,
    PAY_DONE_PAID_NOTE,
    PAY_DONE_PENDING_NOTE,
    PAY_DONE_UNREACHABLE_NOTE,
)
from cabinet.theme import COLORS
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CHAT_ID = 123_456_789
MAIL = "buyer@example.com"


def _client(api: FakeApi, account_id: int) -> TestClient:
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account_id).token)
    return client


def _api_with_order() -> tuple[FakeApi, int, str]:
    """Аккаунт и его заказ на 30 дней, одно устройство, 140 ₽."""
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)
    order = api.create_order(
        account_id=account.id,
        days=30,
        devices=1,
        servers=["nl-1"],
        amount_rub=140,
    )
    return api, account.id, order.order_id


class _NoOrderRead(FakeApi):
    """Подставной бот, не отвечающий на чтение заказа — сбой связи."""

    def get_order(self, order_id: str):  # type: ignore[override]
        raise BotUnreachable("нет связи")


def _markup_without_palette(html: str) -> str:
    start = html.find(":root {")
    end = html.find("}", start)
    return html[:start] + html[end:]


# --- без сессии — на вход -------------------------------------------------


def test_done_without_session_redirects_to_login() -> None:
    """Без cookie — редирект на вход, как у соседних экранов, а не 500."""
    client = TestClient(build_app(store=FakeApi()), raise_server_exceptions=False)

    response = client.get("/buy/done?order_id=ord-1", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- состояние заказа -----------------------------------------------------


def test_pending_order_is_not_shown_as_paid() -> None:
    """Заказ ещё `pending` — «оплачено» не говорится, а сказано, что подтверждения нет.

    Возврат на страницу — не подтверждение: вебхук может дойти через секунды, и
    объявлять оплату по переходу значило бы выдать подделываемый переход за факт.
    """
    api, account_id, order_id = _api_with_order()

    response = _client(api, account_id).get(f"/buy/done?order_id={order_id}")

    assert response.status_code == 200
    assert PAY_DONE_PENDING_NOTE in response.text
    # Слова «Оплачено» на экране нет — заказ его не говорил.
    assert "Оплачено" not in response.text


def test_completed_order_says_paid() -> None:
    """Заказ `completed` — экран говорит «оплачено», потому что так сказал заказ."""
    api, account_id, order_id = _api_with_order()
    api.mark_order_paid(order_id)

    response = _client(api, account_id).get(f"/buy/done?order_id={order_id}")

    assert PAY_DONE_PAID_NOTE in response.text
    assert "Оплачено" in response.text


def test_cancelled_order_is_its_own_state() -> None:
    """Отменённый заказ — не «не подтверждён» и не «оплачено»: свой текст."""
    api, account_id, order_id = _api_with_order()
    api.cancel_order(order_id)

    response = _client(api, account_id).get(f"/buy/done?order_id={order_id}")

    assert PAY_DONE_CANCELLED_NOTE in response.text
    assert PAY_DONE_PAID_NOTE not in response.text


def test_done_shows_the_order_summary() -> None:
    """Сводка заказа — срок, устройства и сумма из самого заказа."""
    api, account_id, order_id = _api_with_order()

    response = _client(api, account_id).get(f"/buy/done?order_id={order_id}")

    assert "30 дней" in response.text
    assert "1 устройство" in response.text
    assert "140 ₽" in response.text


# --- заказ не найден и бот не ответил: разные состояния -------------------


def test_unknown_order_is_not_found_not_an_error() -> None:
    """Неизвестный заказ — «не найден», состояние экрана, а не 500."""
    api, account_id, _order_id = _api_with_order()

    response = _client(api, account_id).get("/buy/done?order_id=нет-такого")

    assert response.status_code == 200
    assert PAY_DONE_NOT_FOUND_NOTE in response.text


def test_missing_order_id_is_not_found() -> None:
    """Без номера заказа показывать нечего — тот же «не найден»."""
    api, account_id, _order_id = _api_with_order()

    response = _client(api, account_id).get("/buy/done")

    assert PAY_DONE_NOT_FOUND_NOTE in response.text


def test_unreachable_bot_is_not_the_same_as_missing_order() -> None:
    """Бот не ответил — не «заказа нет»: это разные состояния.

    Недоступность сервиса не говорит о заказе ничего; покажи её как «не найден» —
    и человек прочёл бы сбой как пропажу покупки.
    """
    api = _NoOrderRead()
    account = api.create_account(chat_id=CHAT_ID)

    response = _client(api, account.id).get("/buy/done?order_id=ord-1")

    assert PAY_DONE_UNREACHABLE_NOTE in response.text
    assert PAY_DONE_NOT_FOUND_NOTE not in response.text


# --- про письмо не обещаем ------------------------------------------------


def test_done_screen_does_not_promise_an_email() -> None:
    """До подтверждения оплаты про письмо не сказано: его ещё нет.

    Обещать письмо, которого никто не отправил, — то же, что нарисованная
    кнопка: человек ждал бы его и не дождался. Письмо уходит после
    подтверждения вебхуком, поэтому и упоминание появляется только в оплаченном
    состоянии — см. тест ниже.
    """
    api, account_id, order_id = _api_with_order()

    html = _client(api, account_id).get(f"/buy/done?order_id={order_id}").text

    assert "письм" not in html.lower()


def test_free_order_is_not_called_paid() -> None:
    """Заказ с нулевой суммой — пробный период, и «оплата прошла» здесь неправда.

    Ничего не платили: доступ выдан по праву на триал. Экран, который говорит
    «Оплачено» и «Сумма 0 ₽», противоречит сам себе, и человек ищет списание,
    которого не было. Нашёл живой прогон бесплатного пути.
    """
    api = FakeApi()
    account = api.create_account(mail=MAIL, mail_verified_at=NOW, chat_id=CHAT_ID)
    order = api.create_order(
        account_id=account.id, days=30, devices=1, servers=["nl-1"], amount_rub=0
    )
    api.mark_order_paid(order.order_id, amount_rub=0)
    client = _client(api, account.id)

    html = client.get(f"/buy/done?order_id={order.order_id}").text

    assert "Доступ выдан" in html
    assert "Бесплатно" in html
    assert "Оплата прошла" not in html
    assert "0\xa0₽" not in html


def test_paid_screen_leads_to_the_configs_and_mentions_the_letter() -> None:
    """Оплачено: экран ведёт к конфигам и честно говорит про письмо.

    Два случая и один текст на оба. Аккаунту с почтой письмо уходит, аккаунту
    без неё — нет, но конфигурации у него есть в подписках, и это первое, что
    сказано. Поэтому на экране **не** написано «мы отправили вам письмо»:
    человеку без почты такое обещание было бы ложью, а он бы ждал письма.
    """
    api, account_id, order_id = _api_with_order()
    api.mark_order_paid(order_id)

    html = _client(api, account_id).get(f"/buy/done?order_id={order_id}").text

    assert "Оплата прошла" in html
    assert "подписок" in html.lower()
    assert 'href="/subscriptions"' in html
    assert "письм" in html.lower()
    assert "Если к аккаунту привязана почта" in html


# --- цвет -----------------------------------------------------------------


def test_done_screen_has_no_green() -> None:
    """Оплата — не подтверждённая защита (§7): зелёного на экране нет."""
    api, account_id, order_id = _api_with_order()
    api.mark_order_paid(order_id)

    html = _client(api, account_id).get(f"/buy/done?order_id={order_id}").text

    assert COLORS.green.upper() not in _markup_without_palette(html).upper()
