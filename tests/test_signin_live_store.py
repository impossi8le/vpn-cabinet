"""Вход и привязка на **живом** хранилище (issue #47).

Зачем отдельный файл. `cabinet/signin.py` и `cabinet/linking.py` зовут у
хранилища семь методов — `find_by_mail`, `find_by_chat_id`, `create_account`,
`link_chat_id`, `link_mail`, `unlink_chat_id`, `unlink_mail`, — которых у
живого `BotStore` не было. Тесты модулей этого не видели: они идут против
`tests.fakes.FakeApi`, у которого нужная форма есть по определению. Выложенный
кабинет на входе по почте отвечал бы `500` — а вход по почте и есть главная
дверь: у человека за границей Telegram может не работать, пока не работает VPN.

Почему проверяется **реальный роут**, а не метод адаптера. Дефект живёт на стыке:
клиент, роут и мок по отдельности непротиворечивы, а вместе не сходятся.
Поэтому здесь поднимается настоящее приложение поверх настоящего `BotStore`, у
которого роль бота играет подставной HTTP.

Подставной HTTP повторяет внутренний роутер бота
(`docs/2026-10-17-backend-contract.md` §1): те же пути, та же форма ответа,
тот же заголовок с секретом. Соединение подменяется транспортом `httpx`, сеть не
открывается. `tests/fakes.py` **не трогается**: подставной бот остаётся общим для
всех волн, а стык проверяется здесь.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from cabinet.accounts import Account
from cabinet.app import (
    COOKIE_NAME,
    LINK_REFUSED_ERROR,
    UNLINK_DONE_TELEGRAM,
    UNLINK_LAST_METHOD_ERROR,
    build_app,
)
from cabinet.bot_client import SECRET_HEADER, BotClient, BotStore
from cabinet.linking import LinkRefused, UnlinkRefused
from cabinet.mail_sender import MailSender
from tests.fakes import CollisionError, FakeApi, FakeMailSender

# Адрес бота — loopback: настоящий роутер `/internal/*` живёт там же, наружу не
# выставлен. Порт значения не имеет: соединение подменено транспортом.
BASE = "http://127.0.0.1:9"
SECRET = "signin-live-store-test-secret"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"
CHAT_ID = 123_456_789

# Из письма код достаётся так же, как его достал бы человек из текста: цифры
# после двоеточия. Тест не лезет во внутренности `AuthMail` — иначе он не
# проверял бы настоящий путь «письмо → код → вход».
_CODE_RE = re.compile(r"(\d{6})")


def _error(code: str, status: int) -> httpx.Response:
    """Отказ в формате контракта бота: `{"error": {"code": ...}}` (§1)."""
    return httpx.Response(status, json={"error": {"code": code, "retryable": False}})


def _account_json(account: Account) -> dict:
    """Аккаунт в форме ответа бота — та же, что у `by-chat-id`/`by-mail` (§1)."""
    return {
        "id": account.id,
        "chat_id": account.chat_id,
        "mail": account.mail,
        "mail_verified_at": (
            account.mail_verified_at.isoformat() if account.mail_verified_at else None
        ),
    }


class _Race:
    """Расхождения между чтением и записью, которые бот закрывает отказом.

    Это не удобство теста, а именно тот случай, ради которого в контракте стоит
    `AND ... IS NULL`: между `find_by_mail` («место свободно») и `link-mail` его
    мог занять кто-то другой. Подставной бот воспроизводит это, отвечая отказом
    на запись при «свободном» чтении.
    """

    def __init__(
        self,
        *,
        link_chat_id_taken: bool = False,
        link_mail_taken: bool = False,
        unlink_chat_last_method: bool = False,
        unlink_mail_last_method: bool = False,
    ) -> None:
        self.link_chat_id_taken = link_chat_id_taken
        self.link_mail_taken = link_mail_taken
        self.unlink_chat_last_method = unlink_chat_last_method
        self.unlink_mail_last_method = unlink_mail_last_method


def _transport(
    api: FakeApi,
    paths: list[str] | None = None,
    race: _Race | None = None,
) -> httpx.MockTransport:
    """Мост: HTTP-запросы хранилища → вызовы подставного бота (§1 контракта).

    `paths` — журнал путей: тесту нужно утверждать **точные** имена маршрутов, а
    не только «запрос дошёл». Расхождение имён в этом проекте уже стоило
    зелёного теста, который ничего не защищал.
    """
    guard = race or _Race()

    def handler(request: httpx.Request) -> httpx.Response:
        # Секрет проверяется на каждом запросе: роутер бота без него отвечает
        # 401, и адаптер не должен уметь сходить «мимо».
        if request.headers.get(SECRET_HEADER) != SECRET:
            return _error("invalid_secret", 401)

        if paths is not None:
            paths.append(request.url.path)

        parts = request.url.path.strip("/").split("/")
        if parts[:2] != ["internal", "accounts"]:
            return _error("not_found", 404)

        method = request.method
        # /internal/accounts/by-mail | by-chat-id
        if method == "GET" and len(parts) == 3 and parts[2] == "by-mail":
            return _account_or_404(api.find_by_mail(request.url.params["mail"]))
        if method == "GET" and len(parts) == 3 and parts[2] == "by-chat-id":
            return _account_or_404(api.find_by_chat_id(int(request.url.params["chat_id"])))
        # POST /internal/accounts — создание
        if method == "POST" and len(parts) == 2:
            return _create_response(api, request)
        # /internal/accounts/{id}...
        if len(parts) >= 3:
            account_id = int(parts[2])
            if method == "GET" and len(parts) == 3:
                return _account_or_404(api.find_by_id(account_id))
            if method == "POST" and len(parts) == 4:
                return _action_response(api, account_id, parts[3], request, guard)
        return _error("not_found", 404)

    return httpx.MockTransport(handler)


def _account_or_404(account: Account | None) -> httpx.Response:
    if account is None:
        return _error("account_not_found", 404)
    return httpx.Response(200, json={"account": _account_json(account)})


def _link_ok(account: Account) -> httpx.Response:
    """Успешный ответ `link-*`: `{"ok": true, "account": {...}}` (§1)."""
    return httpx.Response(200, json={"ok": True, "account": _account_json(account)})


def _create_response(api: FakeApi, request: httpx.Request) -> httpx.Response:
    """`POST /internal/accounts` (§1): тело — либо `chat_id`, либо почта с
    подтверждением. Ответ `201` с аккаунтом; занятое значение — `409`."""
    body = json.loads(request.content) if request.content else {}
    verified = body.get("mail_verified_at")
    try:
        account = api.create_account(
            chat_id=body.get("chat_id"),
            mail=body.get("mail"),
            mail_verified_at=datetime.fromisoformat(verified) if verified else None,
        )
    except CollisionError:
        code = "chat_id_taken" if body.get("chat_id") is not None else "mail_taken"
        return _error(code, 409)
    return httpx.Response(201, json={"account": _account_json(account)})


def _action_response(
    api: FakeApi,
    account_id: int,
    action: str,
    request: httpx.Request,
    race: _Race,
) -> httpx.Response:
    """`link-*` / `unlink-*` по имени последнего сегмента пути (§1).

    Успех `link-*` отдаётся фактической формой живого роутера —
    `{"ok": true, "account": {...}}`, а не только `{"ok": true}`. Это не
    косметика: адаптер отвечает числом и **не должен** читать `account`; тест,
    у которого тела с аккаунтом нет, пропустил бы адаптер, читающий его зря.
    """
    if api.find_by_id(account_id) is None:
        return _error("account_not_found", 404)

    if action == "link-telegram":
        body = json.loads(request.content)
        # Гонка: чтение (`by-chat-id`) сказало «свободно», а запись — «занято».
        if race.link_chat_id_taken:
            return _error("chat_id_taken", 409)
        if api.link_chat_id(account_id, int(body["chat_id"])) != 1:
            return _error("chat_id_taken", 409)
        return _link_ok(api.find_by_id(account_id))

    if action == "link-mail":
        body = json.loads(request.content)
        # Гонка: чтение сказало «свободно», а запись — «занято».
        if race.link_mail_taken:
            return _error("mail_taken", 409)
        verified = body.get("mail_verified_at")
        rowcount = api.link_mail(
            account_id,
            body["mail"],
            datetime.fromisoformat(verified) if verified else None,
        )
        if rowcount != 1:
            return _error("mail_taken", 409)
        return _link_ok(api.find_by_id(account_id))

    if action == "unlink-telegram":
        if race.unlink_chat_last_method:
            return _error("last_login_method", 409)
        if api.unlink_chat_id(account_id) != 1:
            return _error("last_login_method", 409)
        return httpx.Response(200, json={"account": _account_json(api.find_by_id(account_id))})

    if action == "unlink-mail":
        if race.unlink_mail_last_method:
            return _error("last_login_method", 409)
        if api.unlink_mail(account_id) != 1:
            return _error("last_login_method", 409)
        return httpx.Response(200, json={"account": _account_json(api.find_by_id(account_id))})

    return _error("not_found", 404)


def _store_over(
    api: FakeApi,
    paths: list[str] | None = None,
    race: _Race | None = None,
) -> BotStore:
    """Хранилище боевого вида: живой `BotClient` в синхронной форме `BotStore`.

    Оба клиента идут через один подставной транспорт — так же, как в бою они
    идут через один адрес: адаптер не подменяет клиент, а пользуется им.
    """
    transport = _transport(api, paths, race)
    return BotStore(
        BotClient(
            base_url=BASE,
            secret=SECRET,
            client=httpx.AsyncClient(base_url=BASE, transport=transport),
        ),
        http_client=httpx.Client(base_url=BASE, transport=transport),
    )


def _app_over(api: FakeApi, sender: FakeMailSender, **kwargs: object):
    """Приложение поверх живой формы хранилища.

    Отдельно от `_client_over`: часть проверок идёт на уровне **хранилища и
    `Linking`**, а не через HTTP (роуты привязки Telegram заглушены до W6), и им
    нужен доступ к собранным `app.state.linking`/`app.state.sessions`.
    """
    return build_app(
        store=_store_over(api, **kwargs),  # type: ignore[arg-type]
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        now=lambda: NOW,
    )


def _client_over(api: FakeApi, sender: FakeMailSender, **kwargs: object) -> TestClient:
    """Клиент приложения поверх живой формы хранилища.

    `raise_server_exceptions=False`: 500 обязан прийти как код ответа, а не
    исключением наружу, — иначе тест «вход работает» видел бы стектрейс вместо
    ответа и не отличил бы сбой от дефекта.
    """
    app = _app_over(api, sender, **kwargs)
    client = TestClient(app, raise_server_exceptions=False)
    # Сессии приложения — для выдачи сессии вошедшему в тесте. Cookie не
    # подставляется напрямую: подставленный cookie был бы не отличим от
    # поддельного.
    client.app_state = app.state  # type: ignore[attr-defined]
    return client


def _enter_mail_code(client: TestClient, sender: FakeMailSender, mail: str) -> str:
    """Запросить код на адрес и вернуть его из подставного письма."""
    client.post("/login", data={"mail": mail}, follow_redirects=False)
    body = sender.sent_to(mail)[-1]["body"]
    return _CODE_RE.search(body).group(1)


# --- 1. вход по почте: неизвестный адрес заводит аккаунт -------------------


def test_signin_with_unknown_mail_creates_account_against_the_live_store() -> None:
    """Неизвестный адрес: код введён — аккаунт заведён, сессия выдана (#47).

    Это тот путь, что в выложенном кабинете отвечал `500`: `signin` зовёт
    `find_by_mail` и `create_account`, а у живого клиента их не было. Проверяется
    и результат входа (редирект на подписки + cookie), и **точные пути** бота,
    по которым он прошёл.
    """
    api = FakeApi()
    sender = FakeMailSender()
    paths: list[str] = []
    client = _client_over(api, sender, paths=paths)

    code = _enter_mail_code(client, sender, MAIL)
    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/subscriptions"
    assert COOKIE_NAME in response.cookies
    # Аккаунт действительно заведён у бота — ровно один, на введённый адрес.
    assert len(api.accounts) == 1
    created = api.accounts[0]
    assert created.mail == MAIL
    assert created.mail_verified_at is not None
    # Поиск, создание и чтение аккаунта на выдаче сессии — те самые маршруты §1.
    assert paths[0] == "/internal/accounts/by-mail"
    assert "/internal/accounts" in paths
    assert f"/internal/accounts/{created.id}" in paths


def test_signin_with_known_mail_reuses_the_account_and_creates_no_second() -> None:
    """Известный адрес: вход находит аккаунт — второй не заводится.

    Ключевое свойство «вход, а не регистрация»: на тот же ящик не появляется
    второй аккаунт. Тест держится за число аккаунтов у бота и за отсутствие
    `POST /internal/accounts`: одного «вошли» мало — мог завестись и дубль.
    """
    api = FakeApi()
    existing = api.create_account(mail=MAIL, mail_verified_at=NOW)
    sender = FakeMailSender()
    paths: list[str] = []
    client = _client_over(api, sender, paths=paths)

    code = _enter_mail_code(client, sender, MAIL)
    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/subscriptions"
    assert len(api.accounts) == 1
    assert api.accounts[0].id == existing.id
    # Создания не было вовсе: в журнале есть поиск, но нет `POST /internal/accounts`.
    assert paths[0] == "/internal/accounts/by-mail"
    assert "/internal/accounts" not in paths


# --- 2. привязка: занятое место — отказ, который ждут роуты ---------------


def test_occupied_slot_on_link_mail_surfaces_as_the_refusal_routes_expect() -> None:
    """Занятое место при привязке почты — `LinkRefused`, а не 500.

    Воспроизводится гонка: `find_by_mail` сказал «свободно», а запись бот
    отверг `409 mail_taken`. Живое хранилище обязано перевести этот ответ в ту
    форму, которую ждёт `Linking` (число `0` или исключение с тем же `code`), —
    иначе отказ бота всплыл бы как 500 на экране. Роут показывает **общий** текст
    отказа привязки: разница между «занято» и «уже привязано» выдала бы чужие
    регистрации (§5).
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)  # почты нет — привязывать можно
    sender = FakeMailSender()
    client = _client_over(api, sender, race=_Race(link_mail_taken=True))
    client.cookies.set(COOKIE_NAME, client.app_state.sessions.sign_in(account.id).token)  # type: ignore[attr-defined]

    # Первый шаг — код на адрес; второй — код из письма.
    client.post("/account/link/mail", data={"mail": MAIL}, follow_redirects=False)
    code = _CODE_RE.search(sender.sent_to(MAIL)[-1]["body"]).group(1)
    response = client.post(
        "/account/link/mail/confirm",
        data={"mail": MAIL, "code": code},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert LINK_REFUSED_ERROR in response.text
    # Место не перезаписано: почта к аккаунту так и не привязана.
    assert api.find_by_id(account.id).mail is None


# --- 3. отвязка: последний способ входа — свой отказ ----------------------


def test_last_login_method_on_unlink_surfaces_as_unlink_refusal() -> None:
    """`last_login_method` от бота — `UnlinkRefused`, а не общий отказ.

    Запрет отвязать последний способ входа — не ошибка человека, которую можно
    пережить, а защита от недоступного навсегда аккаунта (W3.5). Ответ бота
    `409 last_login_method` обязан дойти до `UnlinkRefused` и получить **свой**
    текст на экране, а не свернуться в «не удалось привязать» и не вылиться в
    500: свернувшись, запрет читался бы как поправимая опечатка.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID, mail=MAIL, mail_verified_at=NOW)
    sender = FakeMailSender()
    client = _client_over(api, sender, race=_Race(unlink_mail_last_method=True))
    client.cookies.set(COOKIE_NAME, client.app_state.sessions.sign_in(account.id).token)  # type: ignore[attr-defined]

    response = client.post("/account/unlink/mail", follow_redirects=False)

    assert response.status_code == 200
    assert UNLINK_LAST_METHOD_ERROR in response.text
    # Почта осталась: отказ на стороне бота ничего не меняет.
    assert api.find_by_id(account.id).mail == MAIL


# --- 4. успешная отвязка Telegram через живое хранилище --------------------


def test_successful_unlink_telegram_through_the_live_store_shows_success() -> None:
    """Успешная отвязка Telegram: хранилище вернуло `1` — экран показал успех.

    `POST /account/unlink/telegram` — реальный роут, зовущий `store.unlink_chat_id`
    напрямую. Успешную сторону семантики не пиннил ни один тест: все проверки
    отказа гоняли запись через подставной флаг. Здесь бот отвечает успехом, и
    хранилище обязано вернуть `1`, а экран — текст успеха (`UNLINK_DONE_TELEGRAM`),
    а не запрет. Регрессия, в которой из записи возвращается объект (`Account`),
    дала бы `Account != 1` → `UnlinkRefused` → запрет на экране человеку, который
    отвязать вправе.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID, mail=MAIL, mail_verified_at=NOW)
    client = _client_over(api, FakeMailSender())
    client.cookies.set(COOKIE_NAME, client.app_state.sessions.sign_in(account.id).token)  # type: ignore[attr-defined]

    response = client.post("/account/unlink/telegram", follow_redirects=False)

    assert response.status_code == 200
    assert UNLINK_DONE_TELEGRAM in response.text
    assert UNLINK_LAST_METHOD_ERROR not in response.text
    # Telegram отвязан, почта — второй способ — не тронута.
    assert api.find_by_id(account.id).chat_id is None
    assert api.find_by_id(account.id).mail == MAIL


# --- 5. успешная привязка: число, а не объект -----------------------------


def test_link_chat_id_returns_rowcount_not_an_object() -> None:
    """`link_chat_id` на успехе возвращает `1`, а не объект аккаунта.

    Протокол связывания требует «сколько строк изменилось»; форма прежнего
    `BotClient` (объект `Account`) — та самая, от которой ушли в #47. Здесь
    пиннится **успешная** сторона: `Account != 1` в `Linking` дал бы отказ на
    законной привязке. Роут привязки Telegram заглушён до W6 (показывает «открой
    бота»), поэтому уровень хранилища здесь единственно доступный.

    Тест гоняет и подставной бот с фактической формой успеха
    (`{"ok": true, "account": {...}}`): адаптер обязан отвечать числом и **не**
    читать `account`.
    """
    api = FakeApi()
    account = api.create_account()  # без способов входа: привязывать есть куда
    store = _store_over(api)

    result = store.link_chat_id(account.id, CHAT_ID)

    assert type(result) is int
    assert result == 1
    assert api.find_by_id(account.id).chat_id == CHAT_ID


def test_link_mail_returns_rowcount_not_an_object() -> None:
    """`link_mail` на успехе возвращает `1`, а не объект. Симметрично Telegram."""
    api = FakeApi()
    account = api.create_account()
    store = _store_over(api)

    result = store.link_mail(account.id, MAIL, NOW)

    assert type(result) is int
    assert result == 1
    assert api.find_by_id(account.id).mail == MAIL


# --- 6. отказы на уровне BotStore → LinkRefused / UnlinkRefused ------------
#
# Роуты привязки Telegram заглушены до W6 (`POST /account/link/telegram` не зовёт
# `Linking.link_telegram`, `/login/telegram` — тоже), поэтому отказы этих путей
# пиннятся на уровне хранилища и `Linking` — другом уровне здесь нет.


def test_chat_id_taken_reaches_link_refused() -> None:
    """`409 chat_id_taken` → `0` → `LinkRefused` (место занято, не сбой).

    Код `chat_id_taken` — из брифа задачи. Гонка: чтение (`by-chat-id`) читается
    как «свободно», запись бот отвергает. Отказ обязан стать `LinkRefused`
    (коллизия), а не проброшенным `BotError` и не 500.
    """
    api = FakeApi()
    account = api.create_account()
    app = _app_over(api, FakeMailSender(), race=_Race(link_chat_id_taken=True))

    assert app.state.store.link_chat_id(account.id, CHAT_ID) == 0
    with pytest.raises(LinkRefused):
        app.state.linking.link_telegram(account.id, chat_id=CHAT_ID, confirmed=True)
    # Место не занято: отказ бота ничего не перезаписал.
    assert api.find_by_id(account.id).chat_id is None


def test_last_login_method_on_unlink_chat_reaches_unlink_refused() -> None:
    """`409 last_login_method` на отвязке Telegram → `0` → `UnlinkRefused`.

    Парная к тесту почты проверка: запрет последнего способа входа обязан
    доходить до `UnlinkRefused` и на Telegram-пути. Аккаунт с двумя способами:
    предварительная проверка `Linking` пропускает (останется почта), а бот
    отвергает запись — гонка, запрет приходит по коду.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID, mail=MAIL, mail_verified_at=NOW)
    app = _app_over(api, FakeMailSender(), race=_Race(unlink_chat_last_method=True))

    assert app.state.store.unlink_chat_id(account.id) == 0
    with pytest.raises(UnlinkRefused):
        app.state.linking.unlink_telegram(account.id)
    # Telegram остался: отказ бота ничего не меняет.
    assert api.find_by_id(account.id).chat_id == CHAT_ID


def test_find_by_chat_id_reads_the_contract_path() -> None:
    """`find_by_chat_id` идёт на **точный** путь `by-chat-id` и разбирает аккаунт.

    Telegram-вход и связывание ищут этим методом; сейчас он не пиннут ничем.
    Путь сверяется целиком: подмена сегмента (`by-tg-id`) прошла бы мимо проверки
    на вхождение подстроки.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)
    paths: list[str] = []
    store = _store_over(api, paths=paths)

    found = store.find_by_chat_id(CHAT_ID)

    assert paths == ["/internal/accounts/by-chat-id"]
    assert found is not None and found.id == account.id
