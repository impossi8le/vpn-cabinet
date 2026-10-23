"""Живой клиент как хранилище: роуты кабинета против подставного HTTP (issue #46).

Зачем отдельный файл. `BotClient` разговаривает с ботом **корутинами** и
называет методы иначе, чем ждут роуты: `list_subscriptions` против
`subscriptions_for`, `find_account_by_chat_id` против `find_by_id`. Роуты
кабинета синхронные, поэтому выложенный кабинет отвечал 500 на первом же экране:
`store.find_by_id` у живого клиента нет вовсе.

Почему проверяется **роут**, а не адаптер сам по себе. Дефект #46 жил на стыке:
каждый кусок в отдельности был внутренне непротиворечив — клиент умел говорить с
ботом, роут умел показывать список, мок давал нужную форму, — а вместе они не
сходились. Тест против одного клиента или против `FakeApi` этого не увидел бы:
`FakeApi` нужную форму даёт по определению. Поэтому здесь поднимается настоящее
приложение и у него запрашивается настоящий экран.

Подставной HTTP повторяет внутренний роутер бота
(`docs/2026-10-17-backend-contract.md`): те же пути, та же форма ответа, тот же
заголовок с секретом. Соединение подменяется транспортом `httpx`, сеть не
открывается.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from cabinet.accounts import Account
from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import SECRET_HEADER, BotClient, BotStore
from cabinet.mail_sender import MailSender
from tests.fakes import STATUS_ACTIVE, FakeApi, FakeMailSender

# Адрес бота — loopback: настоящий роутер `/internal/*` живёт там же, наружу не
# выставлен. Порт значения не имеет: соединение подменено транспортом.
BASE = "http://127.0.0.1:9"
SECRET = "store-adapter-test-secret"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"
CHAT_ID = 123_456_789


def _error(code: str, status: int) -> httpx.Response:
    """Отказ в формате контракта бота: `{"error": {"code": ...}}` (§1)."""
    return httpx.Response(status, json={"error": {"code": code, "retryable": False}})


def _account_json(account: Account) -> dict:
    """Аккаунт в форме ответа бота — та же, что у `by-chat-id`/`by-mail`."""
    return {
        "id": account.id,
        "chat_id": account.chat_id,
        "mail": account.mail,
        "mail_verified_at": (
            account.mail_verified_at.isoformat() if account.mail_verified_at else None
        ),
    }


def _transport(api: FakeApi, paths: list[str]) -> httpx.MockTransport:
    """Мост: HTTP-запросы хранилища → вызовы подставного бота.

    `paths` — журнал путей. Он нужен тесту, чтобы утверждать **точные** имена
    маршрутов контракта, а не только «запрос дошёл»: расхождение имён в этом
    проекте уже стоило зелёного теста, который ничего не защищал.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Секрет проверяется на каждом запросе: роутер бота без него отвечает
        # 401, и адаптер не должен уметь сходить «мимо».
        if request.headers.get(SECRET_HEADER) != SECRET:
            return _error("invalid_secret", 401)

        paths.append(request.url.path)
        parts = request.url.path.strip("/").split("/")
        if request.method != "GET" or parts[:2] != ["internal", "accounts"] or len(parts) < 3:
            return _error("not_found", 404)

        account = api.find_by_id(int(parts[2]))
        if account is None:
            return _error("account_not_found", 404)

        rest = parts[3:]
        if not rest:
            return httpx.Response(200, json={"account": _account_json(account)})
        if rest == ["subscriptions"]:
            return httpx.Response(
                200,
                json={
                    "subscriptions": [
                        item.to_api() for item in api.subscriptions if item.account_id == account.id
                    ]
                },
            )
        if rest == ["balance"]:
            return _balance(account)
        if rest == ["referral"]:
            return _referral(account)
        if rest == ["unlink-preview"]:
            return _unlink_preview(account, request.url.params["method"])
        return _error("not_found", 404)

    return httpx.MockTransport(handler)


def _balance(account: Account) -> httpx.Response:
    """`GET /internal/accounts/{id}/balance`.

    Без Telegram — тот же `409 telegram_required`, что у живого бота: кредит
    живёт на `chat_id`, и суммы для такого аккаунта не существует.
    """
    if account.chat_id is None:
        return _error("telegram_required", 409)
    return httpx.Response(200, json={"credit_rub": 450})


def _referral(account: Account) -> httpx.Response:
    """`GET /internal/accounts/{id}/referral`. Без Telegram — тот же `409`."""
    if account.chat_id is None:
        return _error("telegram_required", 409)
    return httpx.Response(
        200,
        json={"referral_code": "CAB-9f2a", "invited_count": 3, "rub_earned": 150},
    )


def _unlink_preview(account: Account, method: str) -> httpx.Response:
    """`GET /internal/accounts/{id}/unlink-preview` — последствия отвязки (§4а).

    Конфиги живут на `chat_id`: при отвязке Telegram они остаются без ключа, при
    отвязке почты не затронуты вовсе. Ответ — число конфигов и остающийся способ.
    """
    if method == "telegram" and account.chat_id is not None:
        return httpx.Response(200, json={"configs_affected": 2, "remaining_method": "mail"})
    return httpx.Response(200, json={"configs_affected": 0, "remaining_method": "telegram"})


def _store_over(api: FakeApi, paths: list[str] | None = None) -> BotStore:
    """Хранилище боевого вида: живой `BotClient`, приведённый к синхронной форме.

    Оба клиента идут через один подставной транспорт — так же, как в бою они
    идут через один адрес: адаптер не подменяет клиента, а пользуется им.
    """
    journal = paths if paths is not None else []
    transport = _transport(api, journal)
    return BotStore(
        BotClient(
            base_url=BASE,
            secret=SECRET,
            client=httpx.AsyncClient(base_url=BASE, transport=transport),
        ),
        http_client=httpx.Client(base_url=BASE, transport=transport),
    )


def _client_over(api: FakeApi, *, account_id: int, paths: list[str] | None = None) -> TestClient:
    """Клиент приложения, вошедший аккаунтом, — поверх того же хранилища.

    Сессия выдаётся `SessionService`, а не подставляется в cookie вручную: она
    читает аккаунт через то же хранилище (`find_by_id`), и это часть проверки —
    сломайся адаптер, и вход не состоялся бы задолго до экрана.
    """
    app = build_app(
        store=_store_over(api, paths),
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account_id).token)
    return client


def test_subscriptions_screen_shows_what_the_bot_returned() -> None:
    """Первый экран: бот вернул подписку — экран её показал (issue #46).

    Это тот самый путь, который в выложенном кабинете отвечал 500: роут зовёт
    `store.find_by_id` и `store.subscriptions_for`, а у живого клиента таких
    методов нет. Проверяются и данные на экране, и **точные пути** контракта, по
    которым они пришли.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)
    api.add_subscription(
        account.id,
        name="Нидерланды · Амстердам",
        status=STATUS_ACTIVE,
        expires_at=NOW + timedelta(days=10),
    )
    paths: list[str] = []

    response = _client_over(api, account_id=account.id, paths=paths).get("/subscriptions")

    assert response.status_code == 200
    assert "Нидерланды · Амстердам" in response.text
    # Первый путь — чтение аккаунта на выдаче сессии, дальше — сам экран:
    # аккаунт, подписки и деньги человека. Ровно те маршруты, что стоят в
    # контракте бота, и ни одного лишнего.
    base = f"/internal/accounts/{account.id}"
    assert paths == [base, base, f"{base}/subscriptions", f"{base}/balance", f"{base}/referral"]


def test_money_from_the_bot_reaches_the_screen() -> None:
    """Баланс и рефералка доезжают числами бота, а не нулём по умолчанию."""
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)

    response = _client_over(api, account_id=account.id).get("/subscriptions")

    assert response.status_code == 200
    # Сумма рисуется с неразрывным пробелом (`render._rubles`): «450 ₽» не должно
    # переноситься по строке посередине. Проверяется то самое число, что отдал
    # бот, — а не ноль по умолчанию.
    assert "450\u00a0\u20bd" in response.text
    assert "CAB-9f2a" in response.text


def test_account_without_telegram_shows_no_zero_balance() -> None:
    """Аккаунт без Telegram: `409 telegram_required` — не «ноль», а «недоступен»."""
    api = FakeApi()
    account = api.create_account(mail=MAIL, mail_verified_at=NOW)

    response = _client_over(api, account_id=account.id).get("/subscriptions")

    assert response.status_code == 200
    assert "недоступен" in response.text
    # Ноль не рисуется ни в каком написании: сумма не «равна нулю», её здесь нет.
    assert "0\u00a0\u20bd" not in response.text


def test_account_screen_works_against_the_live_client() -> None:
    """Второй экран: способы входа и последствия отвязки пришли от бота.

    Последствия считает только бот (`unlink_preview`), и на экране они обязаны
    появиться: без числа конфигов человек не узнал бы, что потеряет доступ, —
    а узнать это он должен **до** нажатия, а не после.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID, mail=MAIL, mail_verified_at=NOW)

    response = _client_over(api, account_id=account.id).get("/account")

    assert response.status_code == 200
    assert MAIL in response.text
    assert "Конфиги привязаны к Telegram" in response.text


def test_missing_account_reads_as_none_not_as_an_error() -> None:
    """`404 account_not_found` от бота — «аккаунта нет», а не сбой связи.

    Роут по этому гасит сессию и ведёт на вход; прими он `None` за ошибку, экран
    отвечал бы 500 на расхождение состояния. Через роут этот случай не
    воспроизвести: `FakeApi` не умеет забывать аккаунт, — поэтому проверяется
    сам ответ хранилища, ради которого ветка роута и написана.
    """
    store = _store_over(FakeApi())

    assert store.find_by_id(999) is None
