"""Скачивание `.ovpn` на **живом** хранилище (issue #48).

Зачем отдельный файл. Кабинет существует ради одного действия: войти, увидеть
подписки и **скачать конфиг**. `_StoreConfigSource` в `cabinet/app.py` ищет у
хранилища `get_ovpn` (или `config_for_subscription`), а у живого `BotStore` не
было ни того, ни другого — и выложенный кабинет отвечал на каждое скачивание
отказом «источник конфигов не подключён». Отказ при этом мягкий: `200` с экраном,
а не `500`, — поэтому ничто не «кричало», а функции просто не было.

Тесты модулей этого не видели: они идут против `tests.fakes.FakeApi`, у которого
`config_for_subscription` есть по определению. Ровно поэтому проверяется
**реальный роут** `GET /subscriptions/{id}/config` поверх настоящего `BotStore`:
дефект жил на стыке, а не внутри модуля.

Подставной HTTP повторяет внутренний роутер бота (§1 контракта): те же пути, та
же форма ответа, тот же заголовок с секретом. Соединение подменяется транспортом
`httpx`, сеть не открывается. `tests/fakes.py` **не трогается**: подставной бот
остаётся общим для всех волн.

Содержимого настоящего `.ovpn` здесь нет и быть не может: репозиторий публичный,
и настоящий профиль с адресом и ключом был бы утечкой (CLAUDE.md, «Публичность»).
Скачивается подставная строка.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from cabinet.accounts import Account
from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import SECRET_HEADER, BotClient, BotStore
from cabinet.configs import OVPN_CONTENT_TYPE
from cabinet.mail_sender import MailSender
from tests.fakes import STATUS_ACTIVE, FakeApi, FakeMailSender, FakeSubscription

# Адрес бота — loopback: настоящий роутер `/internal/*` живёт там же, наружу не
# выставлен. Порт значения не имеет: соединение подменено транспортом.
BASE = "http://127.0.0.1:9"
SECRET = "config-live-store-test-secret"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
CHAT_ID = 123_456_789

# Подставное содержимое профиля. Настоящего `.ovpn` здесь не будет никогда:
# адрес сервера и ключ — это то, чего в публичном репозитории быть не должно.
FAKE_BODY = b"# fake .ovpn: podstavnoe soderzhimoe\n"


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


def _transport(
    api: FakeApi,
    paths: list[str],
    refuse: str | None = None,
) -> httpx.MockTransport:
    """Мост: HTTP-запросы хранилища → вызовы подставного бота (§1 контракта).

    `paths` — журнал путей: тесту нужно утверждать **точные** имена маршрутов, а
    не только «запрос дошёл». Расхождение имён в этом проекте уже стоило зелёного
    теста, который ничего не защищал.

    `refuse` — код, которым бот отказывает в конфиге. Так воспроизводится
    **осмысленный отказ бота** при живой подписке: кабинет обязан показать его
    своим текстом, а не выдать «источник не подключён».
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Секрет проверяется на каждом запросе: роутер бота без него отвечает
        # 401, и адаптер не должен уметь сходить «мимо».
        if request.headers.get(SECRET_HEADER) != SECRET:
            return _error("invalid_secret", 401)

        paths.append(request.url.path)
        parts = request.url.path.strip("/").split("/")
        if request.method != "GET" or parts[:2] != ["internal", "accounts"]:
            return _error("not_found", 404)

        account = api.find_by_id(int(parts[2])) if len(parts) >= 3 else None
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
        if len(rest) == 2 and rest[0] == "configs":
            return _config(api, account, rest[1], refuse)
        return _error("not_found", 404)

    return httpx.MockTransport(handler)


def _config(
    api: FakeApi,
    account: Account,
    config_id: str,
    refuse: str | None,
) -> httpx.Response:
    """`GET /internal/accounts/{id}/configs/{config_id}` (§4 контракта).

    Содержимое отдаётся **байтами**, как его отдаёт бот: профиль — файл, и
    превращать его в строку по дороге незачем.

    `config_id` здесь равен id подписки: в кабинете конфиг у подписки один, и
    адрес скачивания называет подписку (`cabinet/app.py`, `subscription_config`).
    Различить «нет конфига» и «чужой конфиг» бот не даёт — оба приходят
    `config_not_found` (§3).
    """
    if refuse is not None:
        return _error(refuse, 409)
    subscription = next(
        (s for s in api.subscriptions if str(s.id) == config_id and s.account_id == account.id),
        None,
    )
    config = (
        next((c for c in api.configs if c.id == subscription.config_id), None)
        if subscription is not None
        else None
    )
    if config is None or not config.is_downloadable_by(subscription):
        return _error("config_not_found", 404)
    return httpx.Response(
        200, content=config.body.encode(), headers={"content-type": OVPN_CONTENT_TYPE}
    )


def _balance(account: Account) -> httpx.Response:
    """`GET /internal/accounts/{id}/balance`. Без Telegram — тот же `409`, что у
    живого бота: кредит живёт на `chat_id`."""
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


def _store_over(
    api: FakeApi,
    paths: list[str] | None = None,
    refuse: str | None = None,
) -> BotStore:
    """Хранилище боевого вида: живой `BotClient`, приведённый к синхронной форме.

    Оба клиента идут через один подставной транспорт — так же, как в бою они идут
    через один адрес: адаптер не подменяет клиент, а пользуется им.
    """
    journal = paths if paths is not None else []
    transport = _transport(api, journal, refuse)
    return BotStore(
        BotClient(
            base_url=BASE,
            secret=SECRET,
            client=httpx.AsyncClient(base_url=BASE, transport=transport),
        ),
        http_client=httpx.Client(base_url=BASE, transport=transport),
    )


def _client_over(
    api: FakeApi,
    *,
    account_id: int,
    paths: list[str] | None = None,
    refuse: str | None = None,
) -> TestClient:
    """Клиент приложения, вошедший аккаунтом, — поверх того же хранилища.

    Источник конфигов не подставляется: приложение собирается так же, как на
    выкладке, — по умолчанию (`_StoreConfigSource` над хранилищем). Подставь
    здесь свой источник, и тест проверял бы обвязку теста, а не боевой путь.

    `raise_server_exceptions=False`: сбой обязан прийти кодом ответа, а не
    исключением наружу, — иначе тест увидел бы стектрейс вместо ответа.
    """
    app = build_app(
        store=_store_over(api, paths, refuse),  # type: ignore[arg-type]
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account_id).token)
    return client


def _active_subscription(api: FakeApi, account_id: int) -> FakeSubscription:
    """Подписка, по которой конфиг отдавать **можно**: активна и не истекла.

    Именно такая нужна тесту на отказ бота: локальная проверка `is_usable` пропускает
    запрос дальше, и решение принимает ответ бота — то есть проверяется разбор его
    кода, а не наша собственная проверка срока.
    """
    config = api.add_config(account_id, body=FAKE_BODY.decode())
    return api.add_subscription(
        account_id,
        name="Нидерланды · Амстердам",
        status=STATUS_ACTIVE,
        expires_at=NOW + timedelta(days=10),
        config_id=config.id,
    )


# --- 1. главное действие кабинета: файл доезжает -----------------------------


def test_downloading_config_against_the_live_store_returns_the_file() -> None:
    """Скачивание по живой подписке отдаёт байты бота, а не «источник не подключён».

    Это и есть предмет #48: роут зовёт `configs.download`, а тот — `get_ovpn` у
    хранилища. У живого `BotStore` такого метода не было, и на выкладке каждое
    скачивание кончалось отказом при `200` — то есть действием, которого нет.

    Проверяется не «200 пришёл», а сам файл: его байты, тип содержимого и имя в
    `Content-Disposition` (по нему браузер решает сохранить, а не открыть), плюс
    **точные пути** контракта, по которым он пришёл.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)
    config = api.add_config(account.id, body=FAKE_BODY.decode())
    subscription = api.add_subscription(
        account.id,
        name="Нидерланды · Амстердам",
        status=STATUS_ACTIVE,
        expires_at=NOW + timedelta(days=10),
        config_id=config.id,
    )
    paths: list[str] = []

    response = _client_over(api, account_id=account.id, paths=paths).get(
        f"/subscriptions/{subscription.id}/config", follow_redirects=False
    )

    assert response.status_code == 200
    assert response.content == FAKE_BODY
    assert response.headers["content-type"] == OVPN_CONTENT_TYPE
    # Имя в `Content-Disposition`: по нему браузер решает сохранить файл, а не
    # открыть. Собирается оно из id подписки — тем же белым списком символов,
    # что в `configs.config_filename` (перевод строки из адреса в заголовок не
    # попадёт).
    disposition = f'attachment; filename="{subscription.id}.ovpn"'
    assert response.headers["content-disposition"] == disposition
    # Первый путь — чтение аккаунта на выдаче сессии, дальше — сам экран:
    # подписки и запрос профиля. Ровно маршруты контракта, и ни одного лишнего.
    base = f"/internal/accounts/{account.id}"
    assert paths == [base, f"{base}/subscriptions", f"{base}/configs/{subscription.id}"]


# --- 2. отказ бота — своим текстом, а не «источник не подключён» -------------


@pytest.mark.parametrize(
    ("code", "expected_text"),
    [
        ("config_revoked", "Доступ к подключению отозван"),
        ("subscription_expired", "Конфиг недоступен"),
    ],
)
def test_refusal_code_from_the_bot_reaches_its_own_screen(code: str, expected_text: str) -> None:
    """Код отказа бота доезжает до **своего** экрана, а не сворачивается в общий.

    У каждого кода в `cabinet/configs` свой тип, а у типа — свой текст в
    `cabinet/render` («обновите список» и «возьмите другое подключение» — не одно
    и то же). Свернув код в общий отказ, кабинет объяснял бы отозванный конфиг
    как «не удалось получить», а подсказку человек потерял бы совсем.

    Подписка здесь **активна**: локальная проверка пропускает запрос дальше, и
    решение принимает ответ бота — то есть проверяется разбор его кода.

    Ответ — `200` с экраном подписок: отказ в конфиге это состояние экрана, а не
    ошибка HTTP, и человек обязан остаться там, откуда нажал (W5.4). Файла в
    ответе нет — иначе «отказ» отдал бы профиль.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)
    subscription = _active_subscription(api, account.id)

    response = _client_over(api, account_id=account.id, refuse=code).get(
        f"/subscriptions/{subscription.id}/config", follow_redirects=False
    )

    assert response.status_code == 200
    assert expected_text in response.text
    assert "Content-Disposition" not in response.headers
    assert FAKE_BODY not in response.content
