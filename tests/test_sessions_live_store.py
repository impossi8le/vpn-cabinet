"""Сессии кабинета на **живом** хранилище: вход переживает перезапуск.

Зачем отдельный файл. Кабинет держит сессии в памяти процесса
(`SingleSessionStore`), поэтому `systemctl restart cabinet-app` разлогинивает
всех: памяти процесса после перезапуска нет. У бота для этого есть таблица
`client_sessions` и роуты `/internal/sessions/*`, но кабинет их не зовёт. Здесь
проверяется **то, ради чего работа делается**: вход, сделанный в одном экземпляре
приложения, обязан быть узнан **вторым** экземпляром на том же хранилище.

Почему проверяется **реальный роут**, а не метод адаптера. Дефект живёт на стыке:
вход, роут и адаптер по отдельности непротиворечивы, а вместе не сходятся. Вход
идёт тем же путём, что у человека, — почта, код из письма, cookie, — и уже с этой
cookie второй экземпляр просит подписки: узнал — `200` со списком, не узнал —
`303` на вход.

Подставной HTTP повторяет внутренний роутер бота
(`docs/2026-10-17-backend-contract.md` §1 и §4): те же пути, та же форма ответа,
тот же заголовок с секретом. Соединение подменяется транспортом `httpx`, сеть не
открывается. `tests/fakes.py` **не трогается**: подставной бот остаётся общим для
всех волн, а стык проверяется здесь.

Сессии в подставном боте живут в обычном словаре — как в таблице `client_sessions`
у бота, — и общий словарь между двумя экземплярами приложения и есть «перезапуск
службы»: память процесса кабинета обнуляется, а хранилище бота остаётся.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from cabinet.app import (
    BOT_UNREACHABLE_ERROR,
    COOKIE_NAME,
    LOGIN_BOT_UNREACHABLE,
    build_app,
)
from cabinet.bot_client import (
    SECRET_HEADER,
    BotClient,
    BotError,
    BotSessionStore,
    BotStore,
)
from cabinet.mail_sender import MailSender
from tests.fakes import STATUS_ACTIVE, FakeApi, FakeMailSender

# Адрес бота — loopback: настоящий роутер `/internal/*` живёт там же, наружу не
# выставлен. Порт значения не имеет: соединение подменено транспортом.
BASE = "http://127.0.0.1:9"
SECRET = "sessions-live-store-test-secret"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"
SUBSCRIPTION_NAME = "Нидерланды · Амстердам"

# Срок сессии у живого бота — 30 дней и **без продления**: так его ставит
# `create_session`, а `touch_session` двигает только `last_seen_at`. Кабинет
# обязан брать этот срок, а не свой 180-дневный, иначе cookie обещает на 150
# дней больше, чем сессия живёт.
SESSION_TTL = timedelta(days=30)
# Срок по умолчанию у самого кабинета (память процесса) — для контраста.
CABINET_TTL_SECONDS = 180 * 24 * 3600

# Код из письма достаётся так же, как его достал бы человек: цифры в тексте.
_CODE_RE = re.compile(r"(\d{6})")


def _error(code: str, status: int) -> httpx.Response:
    """Отказ в формате контракта бота: `{"error": {"code": ...}}` (§1)."""
    return httpx.Response(status, json={"error": {"code": code, "retryable": False}})


class _Bot:
    """Подставной бот: аккаунты и таблица сессий, как в `client_sessions`.

    Словарь `sessions` — то самое постоянное хранилище, которого кабинету не
    хватает в памяти процесса. Он живёт снаружи приложения и переживает сборку
    второго экземпляра — ровно как таблица бота переживает перезапуск службы.
    """

    def __init__(self, api: FakeApi) -> None:
        self.api = api
        self.sessions: dict[str, dict] = {}
        self._seq = 0
        # Бот «пропал»: следующий запрос к нему не дойдёт. Переключается у того
        # же экземпляра приложения — так проверяется недоступность в середине
        # входа, а не только с самого начала.
        self.outage = False

    def issue(self, account_id: int) -> dict | None:
        """`POST /internal/sessions/for-account` (§4).

        Токен генерирует **бот**, а не кабинет: владелец таблицы сессий — он, и
        это тот же токен, которым живёт Android-клиент. Поэтому кабинет обязан
        принять токен из ответа, а не навязывать свой.
        """
        if self.api.find_by_id(account_id) is None:
            return None
        self._seq += 1
        token = f"bot-token-{self._seq}"
        self.sessions[token] = {"account_id": account_id, "expires_at": NOW + SESSION_TTL}
        return {
            "token": token,
            "expires_at": (NOW + SESSION_TTL).isoformat(),
            "account_id": account_id,
            "created_at": NOW.isoformat(),
        }

    def current(self, token: str) -> dict | None:
        """`GET /internal/sessions/current` (§4).

        `created_at` здесь не отдаётся — так же, как у живого бота: момент
        появления сессии знает только он, и для проверки его не шлют.
        """
        record = self.sessions.get(token)
        if record is None:
            return None
        account = self.api.find_by_id(record["account_id"])
        return {
            "account_id": record["account_id"],
            "chat_id": account.chat_id if account else None,
            "mail": account.mail if account else None,
            "expires_at": record["expires_at"].isoformat(),
        }

    def revoke(self, token: str) -> None:
        """`POST /internal/sessions/revoke` (§4). Идемпотентно."""
        self.sessions.pop(token, None)


def _account_json(account) -> dict:
    return {
        "id": account.id,
        "chat_id": account.chat_id,
        "mail": account.mail,
        "mail_verified_at": (
            account.mail_verified_at.isoformat() if account.mail_verified_at else None
        ),
    }


def _bearer(request: httpx.Request) -> str | None:
    """Токен из заголовка `Authorization: Bearer <token>` — как его читает бот."""
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value or None


def _transport(bot: _Bot) -> httpx.MockTransport:
    """Мост: HTTP-запросы кабинета → подставной бот (§1 и §4 контракта).

    `bot.outage` — бот не ответил вовсе (сеть). Так проверяется состояние экрана
    входа при недоступности: это не «сессии нет» и не сбой сервера.
    """
    api = bot.api

    def handler(request: httpx.Request) -> httpx.Response:
        if bot.outage:
            raise httpx.ConnectError("бот недоступен", request=request)
        if request.headers.get(SECRET_HEADER) != SECRET:
            return _error("invalid_secret", 401)

        path = request.url.path
        method = request.method
        parts = path.strip("/").split("/")

        if method == "POST" and path == "/internal/cabinet-events":
            return httpx.Response(201, json={"ok": True})
        if method == "POST" and path == "/internal/sessions/for-account":
            body = json.loads(request.content)
            issued = bot.issue(int(body["account_id"]))
            if issued is None:
                return _error("account_not_found", 404)
            return httpx.Response(200, json=issued)
        if method == "GET" and path == "/internal/sessions/current":
            token = _bearer(request)
            record = bot.current(token) if token else None
            if record is None:
                return _error("unauthorized", 401)
            return httpx.Response(200, json=record)
        if method == "POST" and path == "/internal/sessions/revoke":
            body = json.loads(request.content) if request.content else {}
            bot.revoke(body.get("token") or _bearer(request))
            return httpx.Response(200, json={"ok": True})

        if parts[:2] != ["internal", "accounts"]:
            return _error("not_found", 404)
        if method == "GET" and len(parts) == 3 and parts[2] == "by-mail":
            return _account_or_404(api.find_by_mail(request.url.params["mail"]))
        if method == "POST" and len(parts) == 2:
            body = json.loads(request.content) if request.content else {}
            verified = body.get("mail_verified_at")
            account = api.create_account(
                chat_id=body.get("chat_id"),
                mail=body.get("mail"),
                mail_verified_at=datetime.fromisoformat(verified) if verified else None,
            )
            return httpx.Response(201, json={"account": _account_json(account)})

        account_id = int(parts[2])
        account = api.find_by_id(account_id)
        if account is None:
            return _error("account_not_found", 404)
        rest = parts[3:]
        if method == "GET" and not rest:
            return httpx.Response(200, json={"account": _account_json(account)})
        if rest == ["subscriptions"]:
            items = [s.to_api() for s in api.subscriptions if s.account_id == account_id]
            return httpx.Response(200, json={"subscriptions": items})
        if rest in (["balance"], ["referral"]):
            return _error("telegram_required", 409)
        return _error("not_found", 404)

    return httpx.MockTransport(handler)


def _account_or_404(account) -> httpx.Response:
    if account is None:
        return _error("account_not_found", 404)
    return httpx.Response(200, json={"account": _account_json(account)})


def _store(bot: _Bot) -> BotStore:
    """Хранилище боевого вида: живой `BotClient` в синхронной форме `BotStore`.

    Оба клиента идут через один подставной транспорт — так же, как в бою они идут
    через один адрес: адаптер не подменяет клиент, а пользуется им.
    """
    transport = _transport(bot)
    return BotStore(
        BotClient(
            base_url=BASE,
            secret=SECRET,
            client=httpx.AsyncClient(base_url=BASE, transport=transport),
        ),
        http_client=httpx.Client(base_url=BASE, transport=transport),
    )


def _app(bot: _Bot, sender: FakeMailSender, *, live: bool):
    """Экземпляр приложения на общем хранилище.

    `live=True` — боевая сборка: сессии живут у бота (`BotSessionStore`).
    `live=False` — сессии в памяти процесса (`SingleSessionStore`, умолчание
    `build_app`): тот самый режим, который перезапуск не переживает, и пара к
    которому доказывает, что тест краснеет не зря.
    """
    store = _store(bot)
    builder: dict = {
        "store": store,
        "mail_sender": MailSender(sender=MAIL, transport=sender),
        "now": lambda: NOW,
    }
    if live:
        builder["session_store"] = BotSessionStore(store)
    return build_app(**builder)


def _sign_in(client: TestClient, sender: FakeMailSender) -> str:
    """Войти по почте через **настоящий** роут и вернуть токен из cookie.

    Тот же путь, что у человека: адрес → код в письме → ввод кода. Cookie берётся
    из ответа, а не подставляется: подставленный токен был бы неотличим от
    поддельного.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _CODE_RE.search(sender.sent_to(MAIL)[-1]["body"]).group(1)
    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    token = response.cookies.get(COOKIE_NAME)
    assert token, "вход не выдал cookie сессии"
    return token


def _api_with_subscription() -> tuple[FakeApi, int]:
    api = FakeApi()
    account = api.create_account(mail=MAIL, mail_verified_at=NOW)
    config = api.add_config(account.id, body="fake .ovpn\n")
    api.add_subscription(
        account.id,
        name=SUBSCRIPTION_NAME,
        status=STATUS_ACTIVE,
        expires_at=NOW + timedelta(days=10),
        config_id=config.id,
    )
    return api, account.id


# --- 1. главное требование: вход переживает перезапуск -----------------------


def test_sign_in_survives_a_restart_of_the_service() -> None:
    """Сессия, выданная одним экземпляром, узнаётся вторым — на том же хранилище.

    Это и есть предмет задачи: сессии живут у бота, а не в памяти процесса. Вход
    идёт настоящим роутом, cookie — из ответа; затем собирается **второй**
    экземпляр приложения на том же хранилище (память процесса кабинета обнулена),
    и он обязан узнать сессию: `200` со списком подписок, а не `303` на вход.
    """
    api, _account_id = _api_with_subscription()
    bot = _Bot(api)
    sender = FakeMailSender()

    first = TestClient(_app(bot, sender, live=True), raise_server_exceptions=False)
    token = _sign_in(first, sender)
    # Токен в cookie — тот, что выдал бот: кабинет не навязывает свой.
    assert token in bot.sessions

    second = TestClient(_app(bot, sender, live=True), raise_server_exceptions=False)
    # Без cookie второй экземпляр сессии не знает — иначе `200` ниже ничего не
    # доказывал бы.
    assert second.get("/subscriptions", follow_redirects=False).status_code == 303

    second.cookies.set(COOKIE_NAME, token)
    page = second.get("/subscriptions")

    assert page.status_code == 200
    assert SUBSCRIPTION_NAME in page.text


def test_in_memory_store_does_not_survive_a_restart() -> None:
    """Контроль: с хранилищем в памяти второй экземпляр сессии **не** знает.

    Пара к главному тесту, и она обязательна: без неё «зелёный» не отличался бы
    от «зелёного по случайности». Здесь сессии — умолчание `build_app`
    (`SingleSessionStore`), то есть память процесса; второй экземпляр её не
    видит и уводит человека на вход. Именно так ведёт себя кабинет до правки, и
    именно поэтому тест выше падал до неё.
    """
    api, _account_id = _api_with_subscription()
    bot = _Bot(api)
    sender = FakeMailSender()

    first = TestClient(_app(bot, sender, live=False), raise_server_exceptions=False)
    token = _sign_in(first, sender)

    second = TestClient(_app(bot, sender, live=False), raise_server_exceptions=False)
    second.cookies.set(COOKIE_NAME, token)

    assert second.get("/subscriptions", follow_redirects=False).status_code == 303
    # И у бота этой сессии нет: вход в память процесса до бота не дошёл вовсе.
    assert token not in bot.sessions


# --- 2. выход действует и на живой сессии ------------------------------------


def test_revoked_session_is_not_recognised_by_another_instance() -> None:
    """Выход гасит сессию **у бота** — и второй экземпляр её уже не узнаёт.

    Проверка сильнее «страница ушла на вход»: она смотрит на таблицу сессий
    бота. Иначе выход стирал бы копию в памяти, оставляя действующий токен в
    хранилище, — и «вышел» было бы неправдой.
    """
    api, _account_id = _api_with_subscription()
    bot = _Bot(api)
    sender = FakeMailSender()
    app = _app(bot, sender, live=True)
    client = TestClient(app, raise_server_exceptions=False)

    token = _sign_in(client, sender)
    assert token in bot.sessions

    # Выход — та дверь, которой человек закрывает себе доступ. Отзыв идёт в
    # хранилище сессий, и у бота токена после него быть не должно.
    app.state.sessions.sign_out(token)
    assert token not in bot.sessions

    # И новый экземпляр приложения отозванную cookie не принимает.
    second = TestClient(_app(bot, sender, live=True), raise_server_exceptions=False)
    second.cookies.set(COOKIE_NAME, token)
    assert second.get("/subscriptions", follow_redirects=False).status_code == 303


# --- 3. недоступность бота — состояние экрана входа, а не 500 ---------------


def test_unreachable_bot_does_not_turn_the_login_screen_into_a_500() -> None:
    """Бот не ответил — вход показывает своё состояние, а не `500`.

    Проверяются оба шага: запрос кода и ввод кода. Второй особенно важен: код
    верен, но аккаунт прочитать не у кого, и недоступность обязана стать текстом
    экрана (`LOGIN_BOT_UNREACHABLE`), а не сбоем сервера.
    """
    api, _account_id = _api_with_subscription()
    bot = _Bot(api)
    sender = FakeMailSender()
    client = TestClient(_app(bot, sender, live=True), raise_server_exceptions=False)

    # Код получен, пока бот был жив, — как у настоящего человека.
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _CODE_RE.search(sender.sent_to(MAIL)[-1]["body"]).group(1)

    # Бот пропал посреди входа, и в браузере осталась старая cookie.
    bot.outage = True
    client.cookies.set(COOKIE_NAME, "stale-token")

    # Экран входа обязан остаться формой: проверка cookie у живого хранилища
    # идёт в бот, и её отказ не должен выливаться в 500.
    assert client.get("/login").status_code == 200

    # Код верен, но аккаунт прочитать не у кого: свой текст отказа, а не 500.
    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )
    assert response.status_code == 303
    page = client.get("/login")
    assert page.status_code == 200
    assert LOGIN_BOT_UNREACHABLE in page.text


# --- 4. недоступность бота на экране под сессией — экран, а не 500 ----------


def test_unreachable_bot_on_a_signed_in_screen_shows_the_outage_screen() -> None:
    """`/subscriptions` при недоступном боте — экран сбоя, а не `500`.

    Сессия проверяется у бота **в начале роута**, а не только в middleware
    (`resolve` идёт первым делом). Пока этот отказ не пойман одним местом на всё
    приложение, недоступность бота давала `500` на авторизованных экранах —
    регресс относительно прежнего экрана «бот недоступен». Сессия при этом
    обязана остаться целой: отказ связи — не выход, и человек не должен
    оказываться на форме входа из-за того, что бот на минуту пропал.
    """
    api, _account_id = _api_with_subscription()
    bot = _Bot(api)
    sender = FakeMailSender()
    client = TestClient(_app(bot, sender, live=True), raise_server_exceptions=False)
    token = _sign_in(client, sender)

    bot.outage = True
    response = client.get("/subscriptions")

    assert response.status_code == 200
    assert BOT_UNREACHABLE_ERROR in response.text
    # Сессия цела: отказ связи не гасит её и не уводит на вход.
    assert token in bot.sessions
    assert "/login" not in response.headers.get("location", "")


# --- 5. срок живой сессии — срок бота, а не TTL кабинета -------------------


def test_live_session_and_its_cookie_use_the_bot_term_not_the_cabinet_ttl() -> None:
    """Срок живой сессии — 30 дней бота, а не 180 дней кабинета.

    Бот ставит `expires_at = created_at + 30 дней` и `current` его отдаёт;
    продления у него нет. Кабинет, считая `now + 180`, обещал бы браузеру вход
    ещё 150 дней после того, как сессия умерла, и человек с живой cookie получал
    бы отказ. Здесь проверяются оба обещания разом: и `resolve` возвращает срок
    из записи, и `Max-Age` cookie — тот же, а не кабинетный TTL.
    """
    api, _account_id = _api_with_subscription()
    bot = _Bot(api)
    sender = FakeMailSender()
    app = _app(bot, sender, live=True)
    client = TestClient(app, raise_server_exceptions=False)

    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _CODE_RE.search(sender.sent_to(MAIL)[-1]["body"]).group(1)
    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )
    token = response.cookies.get(COOKIE_NAME)
    assert token

    max_age = int(re.search(r"Max-Age=(\d+)", response.headers["set-cookie"]).group(1))
    expected = int(SESSION_TTL.total_seconds())
    assert abs(max_age - expected) <= 60
    assert abs(max_age - CABINET_TTL_SECONDS) > 60

    view = app.state.sessions.resolve(token)
    assert view is not None
    assert abs((view.expires_at - (NOW + SESSION_TTL)).total_seconds()) <= 60


# --- 6. 401 не по коду — отказ сервиса, а не «сессии нет» -------------------


def test_401_with_a_non_contract_code_is_not_read_as_a_missing_session() -> None:
    """`401` с кодом `invalid_secret` — отказ сервиса, а не «все разлогинены».

    Ошибка конфигурации (неверный общий секрет) приходит тем же статусом `401`,
    что и неизвестный токен. Решать «сессии нет» по статусу значило бы молча
    разлогинить всех из-за опечатки в развёртывании; решение принимается только
    по коду контракта `unauthorized`.
    """
    bot = _Bot(FakeApi())
    transport = _transport(bot)
    wrong = BotStore(
        BotClient(
            base_url=BASE,
            secret="wrong-secret",
            client=httpx.AsyncClient(base_url=BASE, transport=transport),
        ),
        http_client=httpx.Client(base_url=BASE, transport=transport),
    )

    with pytest.raises(BotError) as caught:
        BotSessionStore(wrong).find("any-token")

    assert caught.value.code == "invalid_secret"
