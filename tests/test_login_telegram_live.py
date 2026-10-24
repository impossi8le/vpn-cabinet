"""Вход через Telegram: кабинет начинает и завершает флоу бота (issue #50).

Зачем отдельный файл. Экран `/login/telegram` существовал, но за ним не было
флоу: кабинет не начинал челлендж и не опрашивал бота, поэтому вход по почте
показывал пустой кабинет — подписки живут на `chat_id`, которого у почтового
аккаунта нет. Здесь проверяется недостающая половина: человек входит через
Telegram и видит **свой** аккаунт.

Что именно проверяется, и почему именно так:

- **`account_id` приходит только из ответа бота.** Ни форма, ни умолчание его не
  подставляют. Проверяется не «экран нарисовался», а что после входа на экране
  подписок видно подписки того аккаунта, которого назвал бот (§3.3).
- **`secret` челленджа не течёт.** В разметку уходит только `public_code` (он и
  так в ссылке у человека); `secret` живёт в памяти процесса кабинета и в теле
  запроса к боту. Тест берёт секрет из хранилища и требует его отсутствия в
  разметке.
- **Разные отказы — разные экраны.** «Отклонено человеком» и «сбой бота» ведут к
  разным действиям, поэтому тексты обязаны различаться.

Подставной HTTP повторяет публичный клиентский API бота (`client_api_svc.py`):
`POST /api/v1/auth/link` -> `deep_link`, `POST /api/v1/auth/poll` -> `pending` /
`denied` / `confirmed` с `account_id`. Соединение подменяется транспортом
`httpx`, сеть не открывается.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient

from cabinet import app as app_module
from cabinet import render
from cabinet.accounts import Account
from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import (
    BotUnreachable,
    TelegramAuthClient,
    public_code_and_secret,
    secret_hash,
)
from cabinet.mail_sender import MailSender
from tests.fakes import STATUS_ACTIVE, FakeApi, FakeMailSender

# Адрес публичного API бота — loopback: клиентский API слушает там же, наружу не
# выставлен. Порт значения не имеет — соединение подменено транспортом.
BASE = "http://127.0.0.1:9"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CHAT_ID = 987_654_321
SUB_NAME = "Нидерланды · Роттердам"


class FakePublicBot:
    """Подставной публичный API бота: `/api/v1/auth/link` и `/api/v1/auth/poll`.

    Решения опроса задаются очередью (`poll_script`): `pending`, `denied`,
    `confirmed` или отказ-ошибка. Это ровно то, что бот отвечает на разных шагах
    флоу, и тест проверяет реакцию кабинета на каждое.
    """

    def __init__(self, api: FakeApi) -> None:
        self.api = api
        self.link_calls: list[dict[str, Any]] = []
        self.poll_calls: list[dict[str, Any]] = []
        # Очередь ответов на опрос. Пустая — отвечаем `pending`: человек ещё не
        # подтвердил вход в Telegram.
        self.poll_script: list[dict[str, Any]] = []
        # Сбой транспорта: если задан — запрос падает как недоступность бота.
        self.fail_with: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with is not None:
            raise self.fail_with

        path = request.url.path
        body = json.loads(request.content) if request.content else {}

        if request.method == "POST" and path == "/api/v1/auth/link":
            self.link_calls.append(body)
            code = body["public_code"]
            return httpx.Response(
                201,
                json={
                    "public_code": code,
                    # Форму ссылки строит бот, а не кабинет: он владеет именем
                    # бота. Здесь — зарезервированный адрес.
                    "deep_link": f"https://example.invalid/bot?start=login_{code}",
                    "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                },
            )

        if request.method == "POST" and path == "/api/v1/auth/poll":
            self.poll_calls.append(body)
            return self._poll_response(body)

        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def _poll_response(self, body: dict[str, Any]) -> httpx.Response:
        decision = self.poll_script.pop(0) if self.poll_script else {"status": "pending"}
        if "error" in decision:
            code, status = decision["error"]
            return httpx.Response(status, json={"error": {"code": code, "retryable": False}})
        if decision["status"] == "confirmed":
            return httpx.Response(
                200,
                json={
                    "status": "confirmed",
                    "session_token": "bot-session-token",
                    "expires_at": (NOW + timedelta(days=30)).isoformat(),
                    "chat_id": decision.get("chat_id", CHAT_ID),
                    "account_id": decision.get("account_id"),
                },
            )
        return httpx.Response(200, json=decision)


def _client(bot: FakePublicBot, api: FakeApi) -> TestClient:
    """Приложение на подставных зависимостях и подставном публичном API бота."""
    transport = httpx.MockTransport(bot.handler)
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
        telegram_auth=TelegramAuthClient(
            base_url=BASE,
            client=httpx.Client(base_url=BASE, transport=transport),
        ),
    )
    return TestClient(app, raise_server_exceptions=False)


def _public_code_from(html: str) -> str:
    """Код челленджа из скрытого поля формы — то, что видит браузер человека."""
    match = re.search(r'name="public_code" value="([^"]+)"', html)
    assert match is not None, "на экране нет кода начатого челленджа"
    return match.group(1)


def _challenge_secret(client: TestClient, public_code: str) -> str:
    """Секрет челленджа из памяти кабинета — то, что наружу уйти не должно."""
    return client.app.state.telegram_login.secret_for(public_code)  # type: ignore[attr-defined]


def _account_with_subscription(api: FakeApi) -> int:
    """Аккаунт с Telegram и одной подпиской — «человек с подпиской»."""
    account = api.create_account(chat_id=CHAT_ID)
    api.add_subscription(
        account.id, name=SUB_NAME, status=STATUS_ACTIVE, expires_at=NOW + timedelta(days=10)
    )
    return account.id


# --- начало входа: ссылка, но не секрет ------------------------------------


def test_start_shows_the_bot_link_and_keeps_the_secret_out_of_the_page() -> None:
    """Старт отдаёт ссылку бота, а `secret` в разметку не попадает.

    Секрет берётся из хранилища челленджа и обязан отсутствовать в HTML: если он
    утечёт в разметку, любой, кто видит страницу (история, кеш, сохранённый
    файл), сможет завершить чужой вход. В ссылку уходит только `public_code` —
    он и так у человека в `deep_link`, поэтому скрытым полем его передавать
    безопасно.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)

    response = client.get("/login/telegram")

    assert response.status_code == 200
    # Бот получил ровно код и хеш секрета — ни `chat_id`, ни аккаунта, ни самого
    # секрета: начало входа ничего о человеке не заявляет (§3.3).
    assert len(bot.link_calls) == 1
    sent = bot.link_calls[0]
    assert set(sent) == {"public_code", "secret_hash"}
    # Код берём у бота, а не со страницы: тест обязан упасть на утечке секрета, а
    # не на том, что поле подменилось. Иначе «секрет в разметке» упал бы
    # `KeyError`-ом по чужому ключу и спрятал бы настоящую причину.
    public_code = sent["public_code"]
    assert sent["secret_hash"] == secret_hash(_challenge_secret(client, public_code))

    assert f"https://example.invalid/bot?start=login_{public_code}" in response.text
    assert _public_code_from(response.text) == public_code
    assert _challenge_secret(client, public_code) not in response.text


def test_public_code_and_secret_are_random_and_differ() -> None:
    """Код и секрет челленджа — случайные и разные строки достаточной длины.

    Предсказуемый `public_code` равносилен отсутствию защиты: его знание —
    половина входа. Источник — `secrets`, как у кодов `AuthMail`.
    """
    first = public_code_and_secret()
    second = public_code_and_secret()

    assert first != second
    assert len(first[0]) >= 16
    assert len(first[1]) >= 32
    assert first[0] != first[1]


# --- успех: сессия кабинета по `account_id` от бота ------------------------


def test_confirmed_poll_signs_in_the_account_the_bot_named() -> None:
    """Успешный опрос выдаёт сессию **того** аккаунта, которого назвал бот.

    Это и есть главная проверка «человек видит своё»: после входа на экране
    подписок обязана появиться подписка аккаунта из ответа бота, а не пустой
    список. `account_id` берётся из ответа опроса — ниоткуда больше.
    """
    api = FakeApi()
    account_id = _account_with_subscription(api)
    bot = FakePublicBot(api)
    bot.poll_script = [{"status": "confirmed", "account_id": account_id}]
    client = _client(bot, api)

    start = client.get("/login/telegram")
    public_code = _public_code_from(start.text)

    verified = client.post(
        "/login/telegram/verify",
        data={"public_code": public_code, "code": "123456"},
        follow_redirects=False,
    )

    assert verified.status_code == 303
    assert verified.headers["location"] == "/subscriptions"
    session_token = verified.cookies.get(COOKIE_NAME)
    assert session_token
    assert client.app.state.sessions.resolve(session_token).account_id == account_id  # type: ignore[attr-defined]

    subscriptions = client.get("/subscriptions")
    assert subscriptions.status_code == 200
    assert SUB_NAME in subscriptions.text


def test_poll_sends_only_code_secret_and_nonce_never_a_claimed_identity() -> None:
    """Опрос несёт ровно `public_code`, `secret` и код человека — и ничего больше.

    Подставленные в форме `chat_id`/`account_id` до бота не доходят: личность
    человека не заявляется браузером ни в каком виде (§3.3). Только тот
    `account_id`, который бот вернул сам, решает, чья будет сессия.
    """
    api = FakeApi()
    real_account = _account_with_subscription(api)
    other = api.create_account(chat_id=111_222_333)
    bot = FakePublicBot(api)
    bot.poll_script = [{"status": "confirmed", "account_id": real_account}]
    client = _client(bot, api)

    public_code = _public_code_from(client.get("/login/telegram").text)
    # Секрет читаем до опроса: на успехе челлендж гасится, и взять его будет негде.
    expected_secret = _challenge_secret(client, public_code)

    client.post(
        "/login/telegram/verify",
        data={
            "public_code": public_code,
            "code": "123456",
            # Подставная личность: не должна ни уехать боту, ни повлиять на вход.
            "chat_id": "111222333",
            "account_id": str(other.id),
        },
        follow_redirects=False,
    )

    assert len(bot.poll_calls) == 1
    sent = bot.poll_calls[0]
    assert set(sent) == {"public_code", "secret", "device_nonce"}
    assert sent["public_code"] == public_code
    assert sent["device_nonce"] == "123456"
    assert sent["secret"] == expected_secret
    # Подставленный аккаунт проигнорирован: сессия выдана тому, кого назвал бот.
    assert (
        client.app.state.sessions.resolve(  # type: ignore[attr-defined]
            client.cookies.get(COOKIE_NAME)
        ).account_id
        == real_account
    )


# --- отказы: разные причины — разные экраны -------------------------------


def _poll(client: TestClient, decision: dict[str, Any], bot: FakePublicBot) -> httpx.Response:
    """Начать челлендж и один раз опросить его с заданным ответом бота."""
    bot.poll_script = [decision]
    public_code = _public_code_from(client.get("/login/telegram").text)
    return client.post(
        "/login/telegram/verify",
        data={"public_code": public_code, "code": "123456"},
        follow_redirects=False,
    )


def test_denied_inside_telegram_shows_its_own_refusal_and_no_session() -> None:
    """«Отклонено человеком» — свой текст, сессии нет.

    Отказ пришёл от человека в Telegram, а не от сети: повторить имеет смысл,
    перейдя в бота. Сессии здесь быть не должно — иначе отказ входа выдавал бы
    доступ.
    """
    api = FakeApi()
    _account_with_subscription(api)
    bot = FakePublicBot(api)
    client = _client(bot, api)

    response = _poll(client, {"status": "denied"}, bot)

    assert response.status_code == 200
    assert app_module.LOGIN_TELEGRAM_DENIED in response.text
    assert COOKIE_NAME not in response.cookies


def test_pending_shows_a_waiting_screen_and_no_session() -> None:
    """«Ещё не подтверждено» — экран ожидания, а не отказ и не сессия.

    Ожидание и отказ требуют разного: здесь надо подтвердить вход в Telegram и
    нажать «Проверить вход» снова, а не начинать заново.
    """
    api = FakeApi()
    _account_with_subscription(api)
    bot = FakePublicBot(api)
    client = _client(bot, api)

    response = _poll(client, {"status": "pending", "retry_after_ms": 2000}, bot)

    assert response.status_code == 200
    assert app_module.LOGIN_TELEGRAM_PENDING in response.text
    assert COOKIE_NAME not in response.cookies


def test_pending_and_denied_are_different_screens() -> None:
    """«Отклонено» и «не подтверждено» нельзя свести к одному тексту.

    Разные причины — разные действия: одно ждёт человека в Telegram, другое
    отправляет начинать заново. Один текст спрятал бы, что произошло.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)

    denied = _poll(client, {"status": "denied"}, bot).text
    pending = _poll(client, {"status": "pending"}, bot).text

    assert app_module.LOGIN_TELEGRAM_DENIED in denied
    assert app_module.LOGIN_TELEGRAM_DENIED not in pending
    assert app_module.LOGIN_TELEGRAM_PENDING in pending


def test_wrong_code_from_telegram_has_its_own_text() -> None:
    """Код не подошёл — свой текст, а не «вход отклонён» и не сбой связи.

    `nonce_mismatch` бот возвращает, когда человек подтвердил вход, но код
    введён неверно (или не введён). Сказать «подтвердите в Telegram» значило бы
    отправить его по кругу, которого не было.
    """
    api = FakeApi()
    _account_with_subscription(api)
    bot = FakePublicBot(api)
    client = _client(bot, api)

    response = _poll(client, {"error": ("nonce_mismatch", 403)}, bot)

    assert response.status_code == 200
    assert app_module.LOGIN_TELEGRAM_CODE_ERROR in response.text
    assert COOKIE_NAME not in response.cookies


def test_expired_or_used_challenge_says_start_over() -> None:
    """Истёкший или уже использованный челлендж — «начните заново».

    `operation_not_found` и `already_consumed` — состояние челленджа, а не
    ошибка ввода: повторять тот же код бессмысленно, надо начать вход заново.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)

    expired = _poll(client, {"error": ("operation_not_found", 404)}, bot)
    assert app_module.LOGIN_TELEGRAM_EXPIRED in expired.text
    assert COOKIE_NAME not in expired.cookies

    consumed = _poll(client, {"error": ("already_consumed", 409)}, bot)
    assert app_module.LOGIN_TELEGRAM_EXPIRED in consumed.text
    assert COOKIE_NAME not in consumed.cookies


def test_poll_without_a_started_challenge_is_a_refusal_not_a_500() -> None:
    """Опрос без начатого челленджа — отказ на экране, а не 500.

    Человек может открыть форму опроса в старой вкладке или после перезапуска
    кабинета (состояние челленджа живёт в памяти и перезапуск не переживает).
    Ответом обязан быть отказ с действием, а не страница сбоя сервера.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)

    response = client.post(
        "/login/telegram/verify",
        data={"public_code": "никогда-не-начинался", "code": "123456"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert app_module.LOGIN_TELEGRAM_NOT_STARTED in response.text
    assert COOKIE_NAME not in response.cookies
    # До бота дело не дошло: челленджа, который можно опросить, нет.
    assert bot.poll_calls == []


def test_empty_poll_without_any_form_fields_is_a_refusal_not_a_500() -> None:
    """Пустой POST (ни кода, ни челленджа) — тот же честный отказ."""
    api = FakeApi()
    client = _client(FakePublicBot(api), api)

    response = client.post("/login/telegram/verify", data={}, follow_redirects=False)

    assert response.status_code == 200
    assert app_module.LOGIN_TELEGRAM_NOT_STARTED in response.text
    assert COOKIE_NAME not in response.cookies


def test_confirmed_without_account_id_is_a_refusal_not_a_sign_in() -> None:
    """`confirmed` без `account_id` — отказ, а не догадка о личности.

    Отсутствие `account_id` — расхождение контракта (§5): кабинет не должен
    выводить аккаунт из `chat_id` или подставлять что-то своё. Иначе появился бы
    путь входа в обход подтверждения ботом.
    """
    api = FakeApi()
    _account_with_subscription(api)
    bot = FakePublicBot(api)
    client = _client(bot, api)

    response = _poll(client, {"status": "confirmed", "account_id": None}, bot)

    assert response.status_code == 200
    assert app_module.LOGIN_TELEGRAM_FAILED in response.text
    assert COOKIE_NAME not in response.cookies


# --- недоступность бота: три разных ветки, каждая своим тестом ------------
#
# Сбой сети — не отказ человека: пятисотка показала бы сбой сервиса внутренним
# устройством, а «вход отклонён» свалил бы на человека то, чего он не делал
# (#49). Ветки три, и они не сводятся одна к другой: падение на **начале**
# (ссылки нет вовсе), на **опросе живого челленджа** (ссылка есть, человек может
# повторить) и **после подтверждения** (бот подтвердил, а аккаунт прочитать не
# удалось — сессии быть не должно). Тест обязан держать именно ту ветку, о
# которой говорит; поэтому у каждой свой.


def test_unreachable_bot_on_start_is_honest_not_a_500() -> None:
    """Бот не ответил на старте: честный экран и **без** кнопки «Открыть Telegram».

    Ссылки нет вовсе — начать не удалось, — и рисовать кнопку в никуда значило бы
    обещать переход, которого сервис не сделал.
    """
    api = FakeApi()
    _account_with_subscription(api)
    bot = FakePublicBot(api)
    client = _client(bot, api)

    bot.fail_with = httpx.ConnectError("бот не отвечает")
    start = client.get("/login/telegram")

    assert start.status_code == 200
    assert app_module.LOGIN_TELEGRAM_BOT_UNREACHABLE in start.text
    assert app_module.LOGIN_TELEGRAM_DENIED not in start.text
    assert "https://example.invalid/bot?start=login_" not in start.text
    assert COOKIE_NAME not in start.cookies


def test_unreachable_bot_during_poll_keeps_the_challenge_and_gives_no_session() -> None:
    """Сбой на опросе **живого** челленджа: экран про сервис, сессии нет.

    Это ветка, ради которой стоит вся защита: челлендж уже начат и лежит в
    памяти, человек подтвердил вход, а бот перестал отвечать. Сказать ему «вход
    отклонён» или «вход не начат» значило бы соврать, а пятисотка — показать
    сбой сервиса внутренним устройством. Челлендж при этом не теряется:
    «Проверить вход» должна работать снова, как только бот вернётся.

    Проверка опирается на **начатое состояние** (`telegram_login.get` не `None`):
    без него POST упирается в «челленджа нет» и до сбоя транспорта не доходит —
    то есть тест прошёл бы и без обработчика недоступности вовсе.
    """
    api = FakeApi()
    _account_with_subscription(api)
    bot = FakePublicBot(api)
    client = _client(bot, api)

    # Челлендж начинается по-настоящему: сначала бот отвечает, потом падает.
    public_code = _public_code_from(client.get("/login/telegram").text)
    assert client.app.state.telegram_login.get(public_code) is not None  # type: ignore[attr-defined]

    bot.fail_with = httpx.ConnectError("бот не отвечает")
    response = client.post(
        "/login/telegram/verify",
        data={"public_code": public_code, "code": "123456"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert app_module.LOGIN_TELEGRAM_BOT_UNREACHABLE in response.text
    # Не подменяем причину: это не отказ человека и не «вход не начат».
    assert app_module.LOGIN_TELEGRAM_DENIED not in response.text
    assert app_module.LOGIN_TELEGRAM_NOT_STARTED not in response.text
    assert COOKIE_NAME not in response.cookies
    # Челлендж не потерян — иначе повторить опрос было бы нечем.
    assert client.app.state.telegram_login.get(public_code) is not None  # type: ignore[attr-defined]


class _AccountReadsFail:
    """Хранилище, у которого чтение аккаунта обрывается сбоем бота.

    Нужен ровно для одной ветки: бот подтвердил вход и назвал `account_id`, а
    следующий же его ответ (чтение аккаунта) потерялся. `FakeApi` так не умеет —
    он читает из памяти, — и подделывать ради этого бот нельзя: подставной бот,
    разрешающий больше живого, делает тесты бесполезными. Обёртка над `FakeApi`
    воспроизводит только недоступность, ничего больше не меняя.
    """

    def __init__(self, api: FakeApi) -> None:
        self._api = api
        self.fail = False

    def find_by_id(self, account_id: int) -> Account | None:
        if self.fail:
            raise BotUnreachable("аккаунт не прочитать: бот не отвечает")
        return self._api.find_by_id(account_id)

    def subscriptions_for(self, account_id: int) -> list[object]:
        return self._api.subscriptions_for(account_id)


def test_unreachable_bot_after_confirmation_gives_no_session_and_no_false_denial() -> None:
    """Бот подтвердил вход, но аккаунт прочитать не удалось: сессии нет.

    Подтверждение состоялось — код верен, человек в Telegram нажал «войти», — а
    дальше бот пропал на чтении аккаунта. Сессии здесь быть не должно: обещать
    вход, которого не вышло, значит отдать человеку билет в никуда. И сказать
    «вход отклонён» тоже нельзя: отказ пришёл не от человека.
    """
    api = FakeApi()
    account_id = _account_with_subscription(api)
    store = _AccountReadsFail(api)
    bot = FakePublicBot(api)
    bot.poll_script = [{"status": "confirmed", "account_id": account_id}]
    client = _client(bot, store)  # type: ignore[arg-type]

    # Челлендж начат обычным путём, пока бот ещё отвечает.
    public_code = _public_code_from(client.get("/login/telegram").text)
    # Дальше отвечает бот — но уже не хранилище: оно обрывается на чтении аккаунта.
    store.fail = True

    response = client.post(
        "/login/telegram/verify",
        data={"public_code": public_code, "code": "123456"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert app_module.LOGIN_TELEGRAM_BOT_UNREACHABLE in response.text
    # Ложного «вход отклонён» быть не должно: подтверждение состоялось.
    assert app_module.LOGIN_TELEGRAM_DENIED not in response.text
    assert COOKIE_NAME not in response.cookies


def test_restart_returns_to_a_fresh_challenge() -> None:
    """«Войти заново» начинает новый челлендж, а не отказывает.

    После отказа человеку нужно место, где начать снова. Возврат ошибкой
    оставлял бы тупик ровно там, где экран советует начать заново.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)

    # Обработчик «заново» уводит на старт, откуда экран начинается заново.
    response = client.post("/login/telegram", follow_redirects=True)

    assert response.status_code == 200
    assert "https://example.invalid/bot?start=login_" in response.text
    # Челлендж начат именно переходом: бот получил запрос ссылки.
    assert len(bot.link_calls) == 1


def test_telegram_screen_offers_a_button_that_works_without_javascript() -> None:
    """Опрос доступен кнопкой, а не только автоопросом.

    Экран обязан работать без JavaScript: кнопка отправляет форму обычным POST,
    и вход завершается теми же роутами, что проверены выше.
    """
    api = FakeApi()
    _account_with_subscription(api)
    bot = FakePublicBot(api)
    client = _client(bot, api)

    html = client.get("/login/telegram").text

    assert app_module.LOGIN_TELEGRAM_CHECK in html
    assert render.TELEGRAM_VERIFY_ACTION in html
    assert 'name="public_code"' in html
    assert 'method="post"' in html
