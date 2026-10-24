"""Шаг 3 покупки: подтверждение и честное право на триал (#37).

Экран отвечает на два вопроса: «сколько платить» и «положен ли мне пробный
период». Первый — из калькулятора, и **не** из адреса: цена, принятая из
`?price=`, продала бы подписку за рубль. Второй — из ответа бота
(`trial-eligibility`), и он же главный: кабинет раньше не проверял право на
триал и потому честно молчал. Теперь проверяет — и обязан показать **факт**, а
не оговорку.

Три состояния права разведены и не сводятся: «доступен» — проверенный факт,
«недоступен» — решение о человеке с причиной, «не проверено» — сбой бота.
Слить последние два значит выдать недоступность сервиса за отказ человеку.

Причины отказа приходят **кодами** (`telegram_required`, ...), а не текстом:
тест проверяет, что на экран уходит русская формулировка, а код — нет.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import BotUnreachable
from cabinet.mail_sender import MailSender
from cabinet.render import (
    TRIAL_AVAILABLE_NOTE,
    TRIAL_REASON_TEXTS,
    TRIAL_UNKNOWN_REASON_NOTE,
    TRIAL_UNREACHABLE_NOTE,
)
from cabinet.theme import COLORS
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "buyer@example.com"


def _client_for(api: FakeApi, account_id: int) -> TestClient:
    """Клиент, вошедший аккаунтом. Сессия выдаётся `SessionService`, а не
    подставляется руками: она читает аккаунт через то же хранилище."""
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account_id).token)
    return client


def _mail_account(api: FakeApi) -> TestClient:
    """Аккаунт по почте: у него нет `chat_id`, и право на триал проверить не по чему."""
    account = api.create_account(mail=MAIL, mail_verified_at=NOW)
    return _client_for(api, account.id)


class _NoTrialCheck(FakeApi):
    """Подставной бот, не отвечающий на `trial-eligibility` — сбой проверки."""

    def trial_eligibility(self, account_id: int):  # type: ignore[override]
        raise BotUnreachable("нет связи")


def _markup_without_palette(html: str) -> str:
    """HTML без блока `:root`: зелёный в палитре — заготовка, а не применение."""
    start = html.find(":root {")
    end = html.find("}", start)
    return html[:start] + html[end:]


# --- без сессии — на вход -------------------------------------------------


def test_confirm_without_session_redirects_to_login() -> None:
    """Без cookie — редирект на вход, как у соседних экранов, а не 500."""
    client = TestClient(build_app(store=FakeApi()), raise_server_exceptions=False)

    response = client.get("/buy/confirm", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- сводка и цена из калькулятора ----------------------------------------


def test_confirm_shows_the_chosen_period_devices_and_total() -> None:
    """Срок, число устройств и итог — на экране, и итог из калькулятора.

    Для 180 дней и 3 устройств итог 1913 ₽ — из таблицы паритета с ботом, а не
    «какая-нибудь цифра».
    """
    html = _mail_account(FakeApi()).get("/buy/confirm?days=180&devices=3").text

    assert "180 дней" in html
    assert "3 устройства" in html
    assert "1\xa0913\xa0₽" in html


def test_total_is_calculated_not_taken_from_the_address() -> None:
    """Цена из адреса не принимается ни в каком виде.

    В адресе лежит заведомо чужое число (`price=999999`); экран обязан показать
    посчитанный калькулятором итог (140 ₽ для 30 дней на одно устройство), а не
    подставить пришедшее. Прими он цену из адреса — покупка стоила бы рубль.
    """
    html = _mail_account(FakeApi()).get("/buy/confirm?days=30&devices=1&price=999999").text

    assert "140\xa0₽" in html
    assert "999\xa0999" not in html


# --- право на триал: три состояния ----------------------------------------


def test_trial_available_is_shown_as_a_fact() -> None:
    """Доступный триал показан фактом, а не оговоркой.

    Аккаунт с Telegram: бот отвечает `eligible=True`, и экран говорит, что
    пробный период доступен, — а не «возможно, положен».
    """
    api = FakeApi()
    account = api.create_account(chat_id=555_000_111)

    html = _client_for(api, account.id).get("/buy/confirm").text

    assert TRIAL_AVAILABLE_NOTE in html


def test_trial_refusal_shows_a_russian_reason_not_the_code() -> None:
    """Отказ по причине: на экране — русская формулировка, а не код бота.

    Аккаунт без Telegram: бот отвечает `eligible=False, reason='telegram_required'`.
    Человек обязан прочитать объяснение, а не `telegram_required`: код — часть
    контракта, а не текст для экрана.
    """
    html = _mail_account(FakeApi()).get("/buy/confirm").text

    assert TRIAL_REASON_TEXTS["telegram_required"] in html
    assert "telegram_required" not in html


def test_trial_used_reason_is_shown_with_its_own_text() -> None:
    """«Уже использовал» — своя причина со своим текстом, а не общее «недоступен»."""
    api = FakeApi()
    account = api.create_account(chat_id=555_000_222)
    api.set_trial(account.id, eligible=False, reason="trial_used_by_chat_id")

    html = _client_for(api, account.id).get("/buy/confirm").text

    assert TRIAL_REASON_TEXTS["trial_used_by_chat_id"] in html
    assert "trial_used_by_chat_id" not in html


def test_unknown_trial_reason_is_not_shown_raw() -> None:
    """Незнакомая причина не выводится сырой и не превращается в «недоступен».

    Бот знает причину, которой кабинет не понимает; честный ответ — сказать, что
    право надо уточнить, а не подставить первое объяснение и не показать код.
    """
    api = FakeApi()
    account = api.create_account(chat_id=555_000_333)
    api.set_trial(account.id, eligible=False, reason="brand_new_reason")

    html = _client_for(api, account.id).get("/buy/confirm").text

    assert TRIAL_UNKNOWN_REASON_NOTE in html
    assert "brand_new_reason" not in html


def test_unreachable_bot_is_its_own_state_not_a_refusal() -> None:
    """Сбой бота — отдельное состояние, а не «триал недоступен».

    Недоступность сервиса не говорит о человеке ничего; покажи её как отказ — и
    человек прочёл бы «вам не положено» вместо «повторите позже».
    """
    api = _NoTrialCheck()
    account = api.create_account(mail=MAIL, mail_verified_at=NOW)

    html = _client_for(api, account.id).get("/buy/confirm").text

    assert TRIAL_UNREACHABLE_NOTE in html
    # Ни «доступен», ни формулировка отказа на экране не появляются.
    assert TRIAL_AVAILABLE_NOTE not in html
    assert TRIAL_REASON_TEXTS["telegram_required"] not in html


# --- переход на шаг 4 -----------------------------------------------------


def test_confirm_button_leads_to_server_selection() -> None:
    """Кнопка подтверждения ведёт на шаг 4 с той же парой — выбор выражен адресом."""
    html = _mail_account(FakeApi()).get("/buy/confirm?days=90&devices=2").text

    assert 'href="/buy/servers?days=90&amp;devices=2"' in html


def test_confirm_screen_renders_through_the_real_route() -> None:
    """Экран идёт через настоящий роут `TestClient`, а не прямой вызов рендера."""
    response = _mail_account(FakeApi()).get("/buy/confirm")

    assert response.status_code == 200
    assert "Подтвердите покупку" in response.text


# --- цвет -----------------------------------------------------------------


def test_confirm_screen_has_no_green() -> None:
    """Триал и цена — не подтверждённая защита (§7): зелёного на экране нет."""
    html = _mail_account(FakeApi()).get("/buy/confirm").text

    assert COLORS.green.upper() not in _markup_without_palette(html).upper()
