"""Стык реального клиента с подставным ботом.

Проверяет то, чего не видел ни один из модулей по отдельности: что `BotClient`
действительно разговаривает с тем интерфейсом, который имитирует `FakeApi`.

Зачем отдельный файл. Пока мок и клиент живут раздельно, каждый из них
внутренне непротиворечив, а вместе они не проверены. Ровно на этом стыке
ломается потом: имена полей, форма ответа, разбор ошибок. Тесты следующей
волны написаны против мока — если стык не сходится, они подтвердят не то
поведение, которое пойдёт в бой.

Соединение подменяется транспортом `httpx`, а не берётся из сети: тест не
должен открывать сокет.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from cabinet.bot_client import BotClient, BotConfigError, BotError
from tests.fakes import FakeApi

SECRET = "test-secret"
BASE = "http://127.0.0.1:9"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _transport_for(
    api: FakeApi,
    paths: list[str] | None = None,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Мост: HTTP-запросы клиента → вызовы подставного бота.

    Маршруты и форма ответа повторяют внутренний роутер бота. Совпадение имён
    здесь и есть предмет проверки: разойдётся — этот тест покраснеет раньше,
    чем это заметит боевой кабинет.

    `paths` — необязательный журнал путей: тест, которому нужно утверждать
    **точное** имя маршрута (а не только «запрос дошёл»), передаёт сюда список,
    и обработчик складывает в него пришедшие пути. Без него поведение прежнее.

    `requests` — журнал целых запросов: он нужен тесту про **заголовок** токена,
    потому что путь тут ни при чём — проверяется, где именно лежит токен (в
    заголовке, а не в query). Тело запроса туда тоже попадает: то, что токен не
    утёк в query и в путь, надо видеть целиком, а не догадываться.
    """

    # Реестр выданных сессий подставного бота: `token → account_id`. Живёт в
    # замыкании транспорта — это состояние бота, а не клиента, и наружу оно
    # отдаётся ровно так, как отдал бы живой роутер `sessions/*`.
    sessions: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # Секрет проверяется на каждом запросе: роутер бота не отвечает без него,
        # и клиент не должен уметь сходить «мимо».
        if request.headers.get("X-Internal-Secret") != SECRET:
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})

        if paths is not None:
            paths.append(request.url.path)
        if requests is not None:
            requests.append(request)

        path = request.url.path
        if request.method == "GET" and path == "/internal/accounts/by-chat-id":
            acc = api.find_by_chat_id(int(request.url.params["chat_id"]))
        elif request.method == "GET" and path == "/internal/accounts/by-mail":
            acc = api.find_by_mail(request.url.params["mail"])
        elif request.method == "POST" and path == "/internal/sessions/for-account":
            return _issue_session(api, sessions, request)
        elif request.method == "GET" and path == "/internal/sessions/current":
            return _current_session(api, sessions, request)
        elif request.method == "POST" and path == "/internal/sessions/revoke":
            return _revoke_session(sessions, request)
        elif request.method == "GET" and path.endswith("/balance"):
            return _balance_response(api, request)
        elif request.method == "GET" and path.endswith("/referral"):
            return _referral_response(api, request)
        elif request.method == "POST" and path.endswith("/promo/validate"):
            return _promo_response(request)
        elif request.method == "POST":
            # `/internal/accounts/{id}/unlink-telegram|unlink-mail`. Разбор по
            # сегментам, а не сравнение строк: `{id}` меняется от аккаунта к
            # аккаунту, но имя последнего сегмента — часть контракта (§1) и
            # именно его проверяет тест.
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "internal" and parts[1] == "accounts":
                return _unlink_response(api, int(parts[2]), parts[3])
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        else:
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        if acc is None:
            return httpx.Response(
                404, json={"error": {"code": "account_not_found", "retryable": False}}
            )
        return httpx.Response(200, json={"account": _account_json(acc)})

    return httpx.MockTransport(handler)


def _bearer_token(request: httpx.Request) -> str | None:
    """Токен из заголовка `Authorization: Bearer <token>` — как его читает бот.

    `None`, если заголовка нет вовсе: тогда токен искать негде, и ответ будет
    тем же `401 unauthorized`. Клиент, положивший токен в query, сюда не
    попадёт — и это ровно то, что ловит тест про заголовок.
    """
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value or None


def _issue_session(
    api: FakeApi, sessions: dict[str, int], request: httpx.Request
) -> httpx.Response:
    """`POST /internal/sessions/for-account` — выдать сессию по аккаунту.

    Токен детерминированный: тесту не нужна случайность бота, ему нужно, чтобы
    один и тот же `account_id` давал один и тот же ответ при повторе. Форма —
    `{"token", "expires_at", "account_id"}` из §4.
    """
    account_id = int(json.loads(request.content)["account_id"])
    if api.find_by_id(account_id) is None:
        return httpx.Response(
            404, json={"error": {"code": "account_not_found", "retryable": False}}
        )
    token = f"token-for-{account_id}"
    sessions[token] = account_id
    return httpx.Response(
        200,
        json={"token": token, "expires_at": NOW.isoformat(), "account_id": account_id},
    )


def _current_session(
    api: FakeApi, sessions: dict[str, int], request: httpx.Request
) -> httpx.Response:
    """`GET /internal/sessions/current` — проверка токена из заголовка (§4).

    Неизвестный токен — `401 code: unauthorized`, ровно как у живого бота:
    «нет сессии» и «сессия истекла» снаружи неразличимы.
    """
    token = _bearer_token(request)
    account_id = sessions.get(token) if token else None
    if account_id is None:
        return httpx.Response(401, json={"error": {"code": "unauthorized"}})
    acc = api.find_by_id(account_id)
    return httpx.Response(
        200,
        json={
            "account_id": account_id,
            "chat_id": acc.chat_id,
            "mail": acc.mail,
            "expires_at": NOW.isoformat(),
        },
    )


def _revoke_session(sessions: dict[str, int], request: httpx.Request) -> httpx.Response:
    """`POST /internal/sessions/revoke` — выход. Идемпотентно (§4).

    Токен читается из тела `{"token": ...}`: так требует контракт. Повторный
    отзыв не ошибка — `.pop(..., None)` и успех в обоих случаях.
    """
    body = json.loads(request.content) if request.content else {}
    token = body.get("token") or _bearer_token(request)
    sessions.pop(token, None)
    return httpx.Response(200, json={"revoked": True})


def _account_id_from(path: str) -> int:
    """`id` из пути `/internal/accounts/{id}/...`. Берётся по сегментам, а не
    регуляркой: часть контракта здесь — позиция, и разбор по позиции это и
    проверяет."""
    return int(path.strip("/").split("/")[2])


def _balance_response(api: FakeApi, request: httpx.Request) -> httpx.Response:
    """`GET /internal/accounts/{id}/balance`.

    Аккаунт без Telegram получает тот же `409`, что живой бот: кредит живёт на
    `chat_id`, и без него суммы для аккаунта не существует. Это не «ноль» —
    именно поэтому тест на кабинет требует **не** рисовать `0 ₽`.
    """
    account_id = _account_id_from(request.url.path)
    account = api.find_by_id(account_id)
    if account is None:
        return httpx.Response(
            404, json={"error": {"code": "account_not_found", "retryable": False}}
        )
    if account.chat_id is None:
        return httpx.Response(409, json={"error": {"code": "telegram_required"}})
    return httpx.Response(200, json={"credit_rub": 450})


def _referral_response(api: FakeApi, request: httpx.Request) -> httpx.Response:
    """`GET /internal/accounts/{id}/referral`. Без Telegram — тот же `409`."""
    account_id = _account_id_from(request.url.path)
    account = api.find_by_id(account_id)
    if account is None:
        return httpx.Response(
            404, json={"error": {"code": "account_not_found", "retryable": False}}
        )
    if account.chat_id is None:
        return httpx.Response(409, json={"error": {"code": "telegram_required"}})
    return httpx.Response(
        200,
        json={"referral_code": "CAB-9f2a", "invited_count": 3, "rub_earned": 150},
    )


def _promo_response(request: httpx.Request) -> httpx.Response:
    """`POST /internal/accounts/{id}/promo/validate`.

    Недействительный код — **успешный** ответ `200` с `valid: false`: «код не
    подходит» это результат проверки, а не отказ запроса. Тест на клиент
    держится за это различие: клиент обязан вернуть разобранный результат, а не
    бросить исключение.
    """
    code = json.loads(request.content).get("code", "")
    if code == "GOOD10":
        return httpx.Response(200, json={"valid": True, "discount_percent": 10, "reason": None})
    return httpx.Response(200, json={"valid": False, "discount_percent": 0, "reason": "not_found"})


def _unlink_response(api: FakeApi, account_id: int, action: str) -> httpx.Response:
    """Ответ подставного роутера на отвязку — как его отдаёт живой бот.

    `rowcount = 0` — отвязка оставила бы аккаунт без способа входа, и это
    отказ `409 code: last_login_method` (§1 контракта), а не сетевой сбой.
    """
    if api.find_by_id(account_id) is None:
        return httpx.Response(
            404, json={"error": {"code": "account_not_found", "retryable": False}}
        )

    unlink = api.unlink_chat_id if action == "unlink-telegram" else api.unlink_mail
    if unlink(account_id) != 1:
        return httpx.Response(409, json={"error": {"code": "last_login_method"}})
    return httpx.Response(200, json={"account": _account_json(api.find_by_id(account_id))})


def _account_json(acc) -> dict:
    return {
        "id": acc.id,
        "chat_id": acc.chat_id,
        "mail": acc.mail,
        "mail_verified_at": acc.mail_verified_at.isoformat() if acc.mail_verified_at else None,
    }


def _client_for(api: FakeApi) -> BotClient:
    """Клиент, включённый в подставного бота.

    `base_url` задаётся на самом httpx-клиенте: пути в запросах относительные,
    и без этого их не к чему приклеить. Это же показывает, что для теста
    достаточно подставить транспорт — боевой адрес при этом не нужен.
    """
    return BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api)),
    )


async def test_client_finds_account_created_by_fake():
    """Главный стык: аккаунт, заведённый моком, находится настоящим клиентом."""
    api = FakeApi()
    created = api.create_account(mail="user@example.com", mail_verified_at=NOW)

    client = _client_for(api)
    async with client:
        found = await client.find_account_by_mail("user@example.com")

    assert found.id == created.id
    assert found.mail == "user@example.com"


async def test_client_reports_missing_account_as_bot_error():
    """Отсутствие аккаунта — осмысленный отказ с кодом, а не сетевая ошибка.

    Разница важна вызывающему коду: по коду он решает «завести аккаунт»,
    а по сетевой ошибке — «повторить»."""
    api = FakeApi()

    client = _client_for(api)
    async with client:
        with pytest.raises(BotError) as exc:
            await client.find_account_by_mail("nobody@example.com")

    assert exc.value.code == "account_not_found"


async def test_client_without_secret_never_reaches_the_wire():
    """Клиент без секрета не должен молча ходить без заголовка."""
    with pytest.raises(BotConfigError):
        BotClient(base_url=BASE, secret="")


async def test_telegram_only_account_round_trips():
    """Аккаунт без почты — не вырожденный случай: у него `mail` пуст,
    и это не должно ломать разбор ответа."""
    api = FakeApi()
    api.create_account(chat_id=777_000_111)

    client = _client_for(api)
    async with client:
        found = await client.find_account_by_chat_id(777_000_111)

    assert found.chat_id == 777_000_111
    assert found.mail is None


async def test_unlink_telegram_hits_the_contract_path_and_parses_the_account():
    """Отвязка Telegram идёт на **точный** путь из §1 и разбирает ответ.

    Проверяется именно имя маршрута: `unlink-telegram` должно совпасть буква в
    букву с контрактом бота. Расхождение имён путей в этом проекте уже
    случалось, поэтому путь ловится журналом, а не только фактом «дошли».
    """
    api = FakeApi()
    account = api.create_account(chat_id=555_000_111, mail="user@example.com", mail_verified_at=NOW)
    paths: list[str] = []

    client = BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api, paths)),
    )
    async with client:
        result = await client.unlink_telegram(account.id)

    assert paths == [f"/internal/accounts/{account.id}/unlink-telegram"]
    assert result.chat_id is None
    # Почта осталась — второй способ входа не тронут.
    assert api.find_by_id(account.id).mail == "user@example.com"


async def test_unlink_mail_hits_the_contract_path_and_parses_the_account():
    """Симметрично: у отвязки почты свой путь — `unlink-mail`."""
    api = FakeApi()
    account = api.create_account(chat_id=555_000_111, mail="user@example.com", mail_verified_at=NOW)
    paths: list[str] = []

    client = BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api, paths)),
    )
    async with client:
        result = await client.unlink_mail(account.id)

    assert paths == [f"/internal/accounts/{account.id}/unlink-mail"]
    assert result.mail is None
    assert result.chat_id == 555_000_111


async def test_unlinking_last_login_method_is_a_bot_error_not_a_network_failure():
    """Отказ `last_login_method` — `BotError` с этим кодом, не сбой связи.

    Разница для вызывающего принципиальна: по коду он решает «показать отказ,
    объяснить последствие», а по `BotUnreachable` — «повторить запрос». Если бы
    отказ протекал как сетевая ошибка, кабинет молча предлагал бы повторить
    запрещённое действие.
    """
    api = FakeApi()
    account = api.create_account(chat_id=123_456_789)  # единственный способ входа

    client = _client_for(api)
    async with client:
        with pytest.raises(BotError) as exc:
            await client.unlink_telegram(account.id)

    assert exc.value.code == "last_login_method"
    # Аккаунт не тронут: отказ на стороне бота ничего не меняет.
    assert api.find_by_id(account.id).chat_id == 123_456_789


async def test_create_session_hits_the_contract_path_and_parses_the_token():
    """Выдача сессии идёт на **точный** путь §4 и разбирает ответ целиком.

    Путь сверяется строкой целиком, а не на «`sessions` где-то есть»: подмена
    `for-account` на `for_account` или на `session` оставила бы подстроку
    `sessions` на месте и прошла бы мимо. Ровно этот класс ошибки уже стоил
    проекту зелёного теста, не защищавшего ничего (`"JOIN accounts" in sql`).
    """
    api = FakeApi()
    account = api.create_account(mail="user@example.com", mail_verified_at=NOW)
    paths: list[str] = []

    client = BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api, paths)),
    )
    async with client:
        issued = await client.create_session_for_account(account.id)

    assert paths == ["/internal/sessions/for-account"]
    assert issued.token == f"token-for-{account.id}"
    assert issued.account_id == account.id
    assert issued.expires_at == NOW


async def test_current_session_sends_token_in_header_not_query():
    """Токен идёт **заголовком** `Authorization: Bearer`, а не в query.

    Причина та же, что у секрета: query оседает в логах доступа и истории —
    заголовок нет. Проверка точная: токен обязан быть в заголовке и обязан
    **отсутствовать** в строке запроса и в пути. Тест, у которого токен нашёлся
    бы в query, покраснеет — а он бы прошёл, если бы сверялась подстрока.
    """
    api = FakeApi()
    account = api.create_account(chat_id=42_000_111, mail="user@example.com", mail_verified_at=NOW)
    requests: list[httpx.Request] = []

    client = BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api, None, requests)),
    )
    async with client:
        issued = await client.create_session_for_account(account.id)
        got = await client.get_current_session(issued.token)

    assert len(requests) == 2
    verify = requests[1]
    assert verify.url.path == "/internal/sessions/current"
    assert verify.headers["Authorization"] == f"Bearer {issued.token}"
    assert issued.token not in verify.url.query.decode()
    assert issued.token not in verify.url.path
    assert got.account_id == account.id
    assert got.chat_id == 42_000_111
    assert got.mail == "user@example.com"


async def test_unknown_session_token_is_a_bot_error_with_unauthorized_code():
    """`401 unauthorized` — `BotError` с этим кодом, а не сетевая ошибка.

    Разница для вызывающего принципиальна: по коду он убирает cookie и ведёт на
    вход, а по `BotUnreachable` — повторяет запрос. Неизвестный, истёкший и
    отозванный токен снаружи обязаны выглядеть одинаково (§4).
    """
    api = FakeApi()
    api.create_account(mail="user@example.com", mail_verified_at=NOW)

    client = _client_for(api)
    async with client:
        with pytest.raises(BotError) as exc:
            await client.get_current_session("never-issued-token")

    assert exc.value.code == "unauthorized"
    assert exc.value.status == 401


async def test_revoke_session_is_idempotent():
    """Выход идемпотентен: повторный отзыв — не ошибка (§4).

    Проверяется на живом стыке, потому что «не ошибка при повторе» — это
    свойство бота, а не клиента: клиент лишь не должен сам придумывать отказ
    там, где контракт его не предусматривает.
    """
    api = FakeApi()
    account = api.create_account(chat_id=99_000_111)
    requests: list[httpx.Request] = []

    client = BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api, None, requests)),
    )
    async with client:
        issued = await client.create_session_for_account(account.id)
        await client.revoke_session(issued.token)
        # Повтор — не исключение.
        await client.revoke_session(issued.token)

        # После отзыва токен недействителен: тот же `401`, что у неизвестного.
        with pytest.raises(BotError) as exc:
            await client.get_current_session(issued.token)

    revokes = [r for r in requests if r.url.path == "/internal/sessions/revoke"]
    assert len(revokes) == 2
    assert exc.value.code == "unauthorized"


# --- деньги человека: баланс, рефералка, промокод (W5б, #40) --------------
#
# Пути проверяются строкой целиком, как у отвязки и сессий: подмена `balance`
# на `bonus` или `promo/validate` на `promo` оставила бы подстроку и прошла бы
# мимо сверки на вхождение. Ровно на такой подстроке проект уже терял зелёный
# тест.


async def test_balance_hits_the_contract_path_and_parses_the_amount():
    """Баланс идёт на **точный** путь §1 и разбирает сумму числом."""
    api = FakeApi()
    account = api.create_account(chat_id=123_456_789)
    paths: list[str] = []

    client = BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api, paths)),
    )
    async with client:
        balance = await client.get_balance(account.id)

    assert paths == [f"/internal/accounts/{account.id}/balance"]
    assert balance.credit_rub == 450


async def test_balance_without_telegram_is_a_bot_error_telegram_required():
    """У аккаунта без Telegram баланс — `BotError` с кодом `telegram_required`.

    Это **не** «баланс 0»: кредит живёт под `chat_id`, которого у аккаунта ещё
    нет, и суммы не существует. Тест держится за код, а не за текст: по коду
    кабинет решает показать «баланс недоступен», а не `0 ₽`.
    """
    api = FakeApi()
    account = api.create_account(mail="user@example.com", mail_verified_at=NOW)

    client = _client_for(api)
    async with client:
        with pytest.raises(BotError) as exc:
            await client.get_balance(account.id)

    assert exc.value.code == "telegram_required"
    assert exc.value.status == 409


async def test_referral_hits_the_contract_path_and_parses_all_three_fields():
    """Рефералка идёт на **точный** путь §1 и разбирает код, счёт и сумму."""
    api = FakeApi()
    account = api.create_account(chat_id=123_456_789)
    paths: list[str] = []

    client = BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api, paths)),
    )
    async with client:
        referral = await client.get_referral(account.id)

    assert paths == [f"/internal/accounts/{account.id}/referral"]
    assert referral.referral_code == "CAB-9f2a"
    assert referral.invited_count == 3
    assert referral.rub_earned == 150


async def test_referral_without_telegram_is_telegram_required():
    """У аккаунта без Telegram рефералка — тот же `409 telegram_required`."""
    api = FakeApi()
    account = api.create_account(mail="user@example.com", mail_verified_at=NOW)

    client = _client_for(api)
    async with client:
        with pytest.raises(BotError) as exc:
            await client.get_referral(account.id)

    assert exc.value.code == "telegram_required"


async def test_valid_promo_returns_parsed_discount():
    """Действительный код — разобранный результат со скидкой, без исключения."""
    api = FakeApi()
    account = api.create_account(chat_id=123_456_789)
    paths: list[str] = []

    client = BotClient(
        base_url=BASE,
        secret=SECRET,
        client=httpx.AsyncClient(base_url=BASE, transport=_transport_for(api, paths)),
    )
    async with client:
        result = await client.validate_promo(account.id, "GOOD10")

    assert paths == [f"/internal/accounts/{account.id}/promo/validate"]
    assert result.valid is True
    assert result.discount_percent == 10


async def test_invalid_promo_is_a_result_not_an_error():
    """Недействительный код — результат `valid: false` с причиной, не исключение.

    Это различие и есть предмет проверки: «код не подходит» бот отдаёт
    успешным `200`, и клиент обязан вернуть разобранный ответ. Клиент, который
    бросил бы здесь `BotError`, заставил бы кабинет показывать «сбой» на
    обычное «код неверный».
    """
    api = FakeApi()
    account = api.create_account(chat_id=123_456_789)

    client = _client_for(api)
    async with client:
        result = await client.validate_promo(account.id, "NOPE")

    assert result.valid is False
    assert result.discount_percent == 0
    assert result.reason == "not_found"
