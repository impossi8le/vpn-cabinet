"""Смена региона в кабинете (контракт §7).

В боте смена региона работает, в кабинете её не было: человек с почтой шёл за
ней в Telegram. Здесь проверяется адаптация — экран выбора направления, заявка
и состояние заявки, — и главное: что отказы платформы остаются различимыми.
«Вы уже здесь» и «направление недоступно» требуют разных действий, и общее
«не удалось» лишило бы человека единственной подсказки.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import BotUnreachable
from cabinet.mail_sender import MailSender
from cabinet.render import REGION_ERRORS, REGION_UNREACHABLE
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _client(api, account_id: int) -> TestClient:
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account_id).token)
    return client


@pytest.fixture
def telegram_account():
    api = FakeApi()
    account = api.create_account(chat_id=555_000_777)
    return api, account


def test_region_screen_requires_session() -> None:
    """Без сессии — на вход, а не 500 и не чужой кабинет."""
    api = FakeApi()
    client = TestClient(build_app(store=api), raise_server_exceptions=False)

    response = client.get("/region", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_region_screen_lists_directions_and_current_region(telegram_account) -> None:
    """Экран показывает направления и текущий регион — из ответа бота."""
    api, account = telegram_account

    html = _client(api, account.id).get("/region").text

    assert "Сменить регион" in html
    assert "Германия" in html
    assert "Нидерланды" in html
    assert 'name="target"' in html
    assert "Осталось 1 из 2" in html


def test_region_shows_when_the_limit_resets(telegram_account) -> None:
    """Экран говорит, **когда** лимит смен сбросится, — датой, а не «ждите».

    «Осталось 0 из 2» без даты читается как «непонятно сколько ждать»: день
    или месяц. Дату сброса бот называет в `limits.reset_at`.
    """
    api, account = telegram_account
    api.reissue_payload["limits"] = {
        "allowed": 2,
        "used": 2,
        "remaining": 0,
        "window_days": 7,
        "reset_at": "2026-11-09T09:09:57Z",
    }

    html = _client(api, account.id).get("/region").text

    assert "Осталось 0 из 2" in html
    assert "9 ноября" in html


def test_region_without_reset_date_shows_no_placeholder(telegram_account) -> None:
    """Нет `reset_at` — фразы про дату на экране нет вовсе.

    Ключа может не быть в ответе бота — это норма, а не сбой. Тогда строка про
    остаток остаётся как была, а обещания про дату не появляется. Проверяется
    **отсутствие всей фразы**, а не одной формы заглушки: сними guard в
    шаблоне — и на экране оказалось бы «… Лимит сбросится .», а проверка на
    «()» такое не поймала бы.
    """
    api, account = telegram_account
    api.reissue_payload["limits"] = {
        "allowed": 2,
        "used": 2,
        "remaining": 0,
        "window_days": 7,
    }

    html = _client(api, account.id).get("/region").text

    assert "Осталось 0 из 2" in html
    assert "Лимит сбросится" not in html
    assert "None" not in html
    assert "()" not in html


def test_region_request_sends_the_chosen_direction(telegram_account) -> None:
    """Выбор направления уходит боту, ответ — переход к состоянию заявки."""
    api, account = telegram_account
    client = _client(api, account.id)

    response = client.post(
        "/region",
        data={"target": "GE_CLUSTER", "profile_id": "17"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/region?request_id=req-1"
    assert api.reissue_requests == [{"target": "GE_CLUSTER", "profile_id": "17", "filename": None}]


def test_region_keeps_each_platform_refusal_as_its_own_text(telegram_account) -> None:
    """«Вы уже здесь» и «недоступно» — разные тексты, а не одно «не удалось».

    Коды платформы доходят до экрана без переименования; свести их в общий
    отказ значило бы отнять у человека подсказку, что делать дальше.
    """
    api, account = telegram_account
    client = _client(api, account.id)

    api.reissue_error = "target_is_current"
    current = client.post("/region", data={"target": "NL_CLUSTER"}).text

    api.reissue_error = "target_unavailable"
    unavailable = client.post("/region", data={"target": "GE_CLUSTER"}).text

    assert REGION_ERRORS["target_is_current"] in current
    assert REGION_ERRORS["target_unavailable"] in unavailable
    assert current != unavailable


def test_region_status_shows_the_request_state(telegram_account) -> None:
    """Заявка показывается своим состоянием: идёт или готово."""
    api, account = telegram_account
    client = _client(api, account.id)

    api.reissue_state = {"status": "queued", "region": "ge", "error": None}
    assert "Готовим ключ" in client.get("/region?request_id=req-1").text

    api.reissue_state = {
        "status": "completed",
        "region": "ge",
        "new_filename": "GE1.ovpn",
        "expiration_date": "2026-12-01",
        "error": None,
    }
    completed = client.get("/region?request_id=req-1").text
    assert "Готово" in completed and "GE1.ovpn" in completed


def test_region_without_telegram_explains_the_reason() -> None:
    """Почтовому аккаунту — объяснение, а не «не удалось».

    Платформа знает человека по `chat_id`, и перевыпускать ему нечего. Это
    отказ по существу, и текст говорит, чего не хватает.
    """
    api = FakeApi()
    account = api.create_account(mail="u@example.com", mail_verified_at=NOW)

    html = _client(api, account.id).get("/region").text

    assert REGION_ERRORS["telegram_required"] in html


def test_region_with_unreachable_bot_is_its_own_state() -> None:
    """Сбой бота — «сервис не отвечает», а не «направлений нет»."""
    api = FakeApi()
    account = api.create_account(chat_id=555_000_888)

    def unreachable(account_id: int):
        raise BotUnreachable("бот не отвечает")

    api.reissue_options = unreachable  # type: ignore[method-assign]

    html = _client(api, account.id).get("/region").text

    assert REGION_UNREACHABLE in html
