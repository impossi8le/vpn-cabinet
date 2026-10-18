"""Внутренний HTTP-клиент кабинета к боту.

Кабинет не ходит в MySQL: репозиторий публичный, и SQL со схемой таблиц
авторизации в нём выставил бы детали внутреннего устройства на вид. Вместо
этого кабинет дёргает внутренний роутер бота (`/internal/*`) на loopback с
общим секретом в заголовке — см. §3.4 спеки и «Границы сервиса» в CLAUDE.md.

Адрес и секрет берутся из окружения и **никогда** не хардкодятся: настоящие
значения не должны попасть в публичный репозиторий. Здесь стоит плейсхолдер
`127.0.0.1` — реальный хост и порт задаёт переменная `BOT_BASE_URL`.

Разбор ошибок. У бота единый формат отказа (тот же, что у `client_api_svc.py`
в боте):

    {"error": {"code": "...", "message": "...", "retryable": bool}}

Вызывающий код должен ветвиться по `code`, а не по тексту сообщения: текст
меняется, код — часть контракта. Отдельно разведены два случая, которые легко
спутать: **недоступность бота** (сеть, таймаут — `BotUnreachable`, повторяемо) и
**осмысленный отказ бота** (занятый `chat_id`, истёкшая подписка — `BotError`,
повторять бессмысленно и иногда вредно).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from cabinet.accounts import Account

__all__ = [
    "BOT_BASE_URL_ENV",
    "BOT_SECRET_ENV",
    "DEFAULT_BASE_URL",
    "NETWORK_ERROR_CODE",
    "SECRET_HEADER",
    "BotClient",
    "BotConfigError",
    "BotError",
    "BotUnreachable",
    "CurrentSession",
    "IssuedSession",
    "Subscription",
]

# Плейсхолдер: внутренний роутер бота живёт на loopback, но конкретный порт —
# деталь развёртывания, которой в публичном репозитории не место. Реальный
# адрес приходит из окружения (`BOT_BASE_URL`).
DEFAULT_BASE_URL = "http://127.0.0.1:8007"

BOT_BASE_URL_ENV = "BOT_BASE_URL"
BOT_SECRET_ENV = "BOT_INTERNAL_SECRET"

# Имя заголовка с общим секретом — часть контракта с роутером `/internal/*`.
SECRET_HEADER = "X-Internal-Secret"

# Код для случая «бот не ответил». Держим его отдельным от кодов бота, чтобы
# вызывающий код мог отличить недоступность сервиса от отказа логики.
NETWORK_ERROR_CODE = "bot_unreachable"

BOT_PATH = "/internal"

# Таймауты. Соединение к loopback не может занимать секунды — если занимает,
# бот не поднят. На чтение даём больше: за ним может стоять запрос к БД, но не
# бесконечно — запрос кабинета не должен висеть, пока пользователь смотрит на
# пустой экран. `total` держит потолок для обеих фаз.
DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_TOTAL_TIMEOUT = 15.0


class BotConfigError(RuntimeError):
    """Кабинет не настроен: нет адреса или общего секрета в окружении.

    Это ошибка развёртывания, а не отказ бота. Отделена намеренно: пустой
    секрет не должен приводить к запросу без заголовка и «непонятной» 401 от
    бота — причину надо называть сразу.
    """


class BotError(Exception):
    """Бот вернул осмысленный отказ в формате `{"error": {...}}`.

    Поля:
    - `code` — код из контракта бота (`invalid_secret`, `chat_id_taken`, ...).
      Ветвиться надо по нему, а не по `message`: сообщение для человека.
    - `message` — текст для лога/оператора, не для UI.
    - `retryable` — отметка бота, имеет ли смысл повторить. `False` по
      умолчанию: молчаливый повтор отказа-по-логике может ухудшить состояние.
    - `status` — HTTP-статус ответа; `None`, если тело пришло не в формате
      контракта и код подобран по статусу.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.status = status


class BotUnreachable(Exception):
    """Бот недоступен: сеть, таймаут, обрыв соединения.

    Отличается от `BotError` тем, что отказа по логике не было — запрос надо
    повторить позже, состояние бота неизвестно. `code` здесь всегда
    `NETWORK_ERROR_CODE`, чтобы вызывающий код мог единообразно ветвиться и по
    недоступности, и по отказам.
    """

    code = NETWORK_ERROR_CODE
    retryable = True

    def __init__(
        self, message: str = "бот недоступен", *, cause: BaseException | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


# Сетевые сбои httpx, которые для кабинета означают одно: бот не ответил.
# `HTTPError` покрывает `ConnectError`, `ReadTimeout`, `PoolTimeout` и прочее;
# отдельно ловим `OSError` (обрыв на уровне сокета).
_NETWORK_ERRORS = (httpx.HTTPError, OSError)


@dataclass(frozen=True)
class Subscription:
    """Подписка. Срок и статус — то, что видит пользователь; `id` нужен, чтобы
    запросить конфиг. Поля близки к элементам `configs` из `/me` контракта, но
    кабинету они нужны как объект, а не как словарь на рендере."""

    id: str
    name: str
    status: str
    end_date: datetime | None = None
    location: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Subscription:
        """Собрать подписку из ответа бота, не доверяя форме ответа.

        Ответ приходит извне этого репозитория, поэтому поля читаются через
        `.get` с осмысленными значениями по умолчанию: новая версия бота не
        должна ломать рендер кабинета None-ошибкой.
        """
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            status=str(raw.get("status", "")),
            end_date=_parse_datetime(raw.get("end_date")),
            location=raw.get("location"),
        )


@dataclass(frozen=True)
class IssuedSession:
    """Только что выданная сессия — ответ `POST /internal/sessions/for-account`.

    Токен здесь есть, и это единственный момент, когда он ходит наружу: его
    кладёт в cookie вызывающий код. Держать его дольше незачем — в хранилище
    лежит хеш (`cabinet.sessions`), и токен оттуда не вернётся.
    """

    token: str
    account_id: int
    expires_at: datetime | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> IssuedSession:
        """Собрать из ответа бота, не доверяя его форме: ответ приходит из
        другого репозитория, и пропавшее поле не должно ронять вход."""
        return cls(
            token=str(raw.get("token", "")),
            account_id=int(raw.get("account_id", 0)),
            expires_at=_parse_datetime(raw.get("expires_at")),
        )


@dataclass(frozen=True)
class CurrentSession:
    """Сессия, какой её видит `GET /internal/sessions/current`.

    `chat_id` и `mail` — атрибуты аккаунта, а не сессии: они нужны кабинету,
    чтобы показать, кто вошёл, но владелец сессии — только `account_id`
    (см. `cabinet.sessions`). Токена здесь нет: он уже у вызывающего в cookie.
    """

    account_id: int
    chat_id: int | None = None
    mail: str | None = None
    expires_at: datetime | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> CurrentSession:
        """Собрать из ответа бота. Поля через `.get`: новая версия бота не
        должна ломать проверку сессии `KeyError`-ом."""
        return cls(
            account_id=int(raw.get("account_id", 0)),
            chat_id=raw.get("chat_id"),
            mail=raw.get("mail"),
            expires_at=_parse_datetime(raw.get("expires_at")),
        )


def _bearer(token: str) -> dict[str, str]:
    """Заголовок с токеном сессии.

    Не в query: query оседает в логах доступа и истории браузера, а заголовок —
    нет. Тот же выбор, что у секрета в `_headers()`: секрет — не единственное,
    что нельзя показывать в строке запроса, и токен сессии здесь не исключение.
    """
    return {"Authorization": f"Bearer {token}"}


def _parse_datetime(value: Any) -> datetime | None:
    """Разобрать ISO-8601 из ответа бота. Непонятное — `None`: подписка без
    срока показывается как «срок неизвестен», а не роняет экран."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class BotClient:
    """Клиент внутреннего API бота.

    Один экземпляр держит один `httpx.AsyncClient` и переиспользует соединения.
    Клиент создаётся один раз на процесс кабинета, а не на запрос пользователя:
    иначе на каждый вход заводилось бы своё соединение к loopback. Закрывается
    через `aclose()` на остановке приложения.
    """

    def __init__(
        self,
        base_url: str | None = None,
        secret: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Аргументы важнее окружения: так тесты подставляют мок и не зависят от
        # переменных процесса. Порядок «аргумент → окружение → плейсхолдер»
        # повторяет то, как настраивается развёртывание.
        self.base_url = (base_url or os.getenv(BOT_BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
        self._secret = secret if secret is not None else os.getenv(BOT_SECRET_ENV)
        if not self._secret:
            # Пустой секрет — не «работаем без него», а неверная конфигурация:
            # роутер бота без секрета отвечает 401, и настоящую причину надо
            # назвать здесь, а не выводить из ответа.
            raise BotConfigError(
                f"не задан общий секрет: передайте secret или переменную окружения {BOT_SECRET_ENV}"
            )
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                DEFAULT_TOTAL_TIMEOUT,
                connect=DEFAULT_CONNECT_TIMEOUT,
                read=DEFAULT_READ_TIMEOUT,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BotClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ---------- Аккаунты ----------

    async def find_account_by_chat_id(self, chat_id: int) -> Account | None:
        """Найти аккаунт по Telegram id. `None` — не найден, а не ошибка:
        «не нашёл» и «бот сломался» вызывающий код различает по типу ответа."""
        data = await self._request(
            "GET", f"{BOT_PATH}/accounts/by-chat-id", params={"chat_id": chat_id}
        )
        return _account_from_api(data["account"]) if data.get("account") else None

    async def find_account_by_mail(self, mail: str) -> Account | None:
        """Найти аккаунт по почте. Симметрично поиску по `chat_id`."""
        data = await self._request("GET", f"{BOT_PATH}/accounts/by-mail", params={"mail": mail})
        return _account_from_api(data["account"]) if data.get("account") else None

    async def create_account(
        self, *, chat_id: int | None = None, mail: str | None = None
    ) -> Account:
        """Создать аккаунт. Хотя бы один способ входа обязателен — иначе
        получился бы недоступный навсегда аккаунт (инвариант W3.5)."""
        if chat_id is None and not mail:
            raise ValueError("аккаунт без способа входа недоступен навсегда")
        body: dict[str, Any] = {}
        if chat_id is not None:
            body["chat_id"] = chat_id
        if mail:
            body["mail"] = mail
        data = await self._request("POST", f"{BOT_PATH}/accounts", json=body)
        return _account_from_api(data["account"])

    async def link_telegram(self, account_id: int, chat_id: int) -> Account:
        """Привязать Telegram к аккаунту.

        `chat_id` присваивается только на пустое место — бот делает это через
        `WHERE id = ? AND chat_id IS NULL`. Занятый `chat_id` приходит отказом
        `code` (коллизия), который вызывающий код показывает человеку: тихой
        перезаписи здесь быть не должно (§3.3).
        """
        data = await self._request(
            "POST", f"{BOT_PATH}/accounts/{account_id}/link-telegram", json={"chat_id": chat_id}
        )
        return _account_from_api(data["account"])

    async def link_mail(self, account_id: int, mail: str) -> Account:
        """Привязать почту к аккаунту.

        Подтверждение владением почтой проверяет `auth_mail` до вызова; сюда
        адрес приходит уже подтверждённым, и бот ставит `mail_verified_at`.
        Привязка — тоже на пустое место (`mail IS NULL`), занятая почта даёт
        коллизию, а не перезапись.
        """
        data = await self._request(
            "POST", f"{BOT_PATH}/accounts/{account_id}/link-mail", json={"mail": mail}
        )
        return _account_from_api(data["account"])

    async def unlink_telegram(self, account_id: int) -> Account:
        """Отвязать Telegram от аккаунта.

        Отвязка последнего способа входа запрещена (W3.5): после неё аккаунт
        стал бы недоступен навсегда. Бот отвечает на это `409` с
        `code: last_login_method` (§1 контракта). Это **осмысленный отказ**, а
        не сбой связи: он приходит сюда как `BotError` с этим кодом, и
        вызывающий ветвится по нему, а не по тексту сообщения.
        """
        data = await self._request("POST", f"{BOT_PATH}/accounts/{account_id}/unlink-telegram")
        return _account_from_api(data["account"])

    async def unlink_mail(self, account_id: int) -> Account:
        """Отвязать почту от аккаунта. Симметрично отвязке Telegram.

        Тот же запрет и тот же код отказа: отвязка последней почты приходит
        `409 code: last_login_method`. Разбор ошибок общий для всех методов
        (`_error_from_response`), поэтому отказ не превращается в сетевую
        ошибку и не теряет код.
        """
        data = await self._request("POST", f"{BOT_PATH}/accounts/{account_id}/unlink-mail")
        return _account_from_api(data["account"])

    # ---------- Подписки и конфиги ----------

    async def list_subscriptions(self, account_id: int) -> list[Subscription]:
        """Список подписок аккаунта. Пустой список — норма: «подписок нет» —
        это состояние экрана, а не ошибка (W4.3)."""
        data = await self._request("GET", f"{BOT_PATH}/accounts/{account_id}/subscriptions")
        return [Subscription.from_api(item) for item in data.get("subscriptions", [])]

    async def get_ovpn(self, account_id: int, config_id: str) -> bytes:
        """Содержимое `.ovpn`. Возвращается как байты, а не текст: профиль
        отдаётся файлом, и превращать его в строку по дороге незачем.

        Отказ по истёкшей подписке или отозванному конфигу приходит сюда как
        `BotError` с кодом из контракта — кабинет по нему решает, что показать.
        """
        return await self._request_bytes(
            "GET", f"{BOT_PATH}/accounts/{account_id}/configs/{config_id}"
        )

    # ---------- Сессии (§4) ----------

    async def create_session_for_account(self, account_id: int) -> IssuedSession:
        """Выдать сессию кабинета по `account_id` (§4).

        Сессия выдаётся **по аккаунту**, а не по `chat_id`: главный случай —
        человек, зарегистрировавшийся по почте и Telegram не привязавший. Это
        тот же выбор, что в `cabinet.sessions`, и клиент его не отменяет.

        Ответ возвращается с токеном: он показывается один раз, и вызывающий
        сам кладёт его в cookie. В лог токен не идёт — `_send` и `_request`
        заголовки и тело не логируют.
        """
        data = await self._request(
            "POST", f"{BOT_PATH}/sessions/for-account", json={"account_id": account_id}
        )
        return IssuedSession.from_api(data)

    async def get_current_session(self, token: str) -> CurrentSession:
        """Проверить сессию по токену (§4).

        Токен идёт **в заголовке** `Authorization: Bearer`, а не в query: query
        попадает в логи доступа и историю, заголовок — нет. Причина та же, что
        у секрета в `_headers()`.

        Неизвестный, истёкший или отозванный токен бот отдаёт как `401` с
        `code: unauthorized`. Это **осмысленный отказ**, а не сбой связи: он
        приходит сюда как `BotError` с этим кодом (разбор общий —
        `_error_from_response`), и вызывающий ветвится по коду, а не по тексту.
        """
        data = await self._request("GET", f"{BOT_PATH}/sessions/current", headers=_bearer(token))
        return CurrentSession.from_api(data)

    async def revoke_session(self, token: str) -> None:
        """Выход: отозвать сессию по токену (§4). Идемпотентно.

        Повторный выход — не ошибка, поэтому здесь нечего различать: бот
        отвечает успехом и на уже отозванный токен.

        Токен идёт и в заголовке, и в теле. Заголовок — потому что он не
        попадает в логи доступа (та же причина, что у секрета в `_headers()`);
        тело `{"token": ...}` — потому что так требует §4 контракта. Оба места
        заполняются намеренно: пока W6-роутер не написан, источник правды —
        контракт, и разойтись с ним на пустом месте нельзя.
        """
        await self._request(
            "POST",
            f"{BOT_PATH}/sessions/revoke",
            headers=_bearer(token),
            json={"token": token},
        )

    # ---------- Транспорт ----------

    def _headers(self) -> dict[str, str]:
        # Секрет в заголовке, а не в query: query попадает в логи доступа и
        # историю, заголовок — нет.
        return {SECRET_HEADER: self._secret or ""}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._send(method, path, **kwargs)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            # 2xx с телом не-JSON — расхождение контракта; это отказ, не сеть.
            raise BotError(
                "invalid_response", "бот вернул не-JSON", status=response.status_code
            ) from exc
        if not isinstance(payload, dict):
            raise BotError("invalid_response", "бот вернул не объект", status=response.status_code)
        return payload

    async def _request_bytes(self, method: str, path: str, **kwargs: Any) -> bytes:
        response = await self._send(method, path, **kwargs)
        return response.content

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Один запрос: сеть, статус, разбор отказа. Общая точка для JSON и
        байтовых ответов, чтобы обработка ошибок не разъезжалась между ними."""
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except _NETWORK_ERRORS as exc:
            # Сеть/таймаут: отказа по логике не было, повтор имеет смысл.
            raise BotUnreachable(
                f"запрос к боту не удался: {exc.__class__.__name__}", cause=exc
            ) from exc

        if response.status_code >= 400:
            raise _error_from_response(response)
        return response


def _error_from_response(response: httpx.Response) -> BotError:
    """Разобрать отказ бота.

    Контрактный путь — тело `{"error": {...}}`. Если тело не в этом формате
    (прокси, упавший роутер, HTML 502), код подбирается по HTTP-статусу, чтобы
    вызывающий код всё равно получал `code`, а не падал на разборе. `retryable`
    при этом выставляется по статусу: 5xx — повторяемо, 4xx — нет.
    """
    status = response.status_code
    try:
        payload = response.json()
        err = payload["error"]
        return BotError(
            code=str(err["code"]),
            message=str(err.get("message", "")),
            retryable=bool(err.get("retryable", False)),
            status=status,
        )
    except (ValueError, KeyError, TypeError):
        return BotError(
            code=f"http_{status}",
            message=f"бот ответил статусом {status} не в формате контракта",
            retryable=status >= 500,
            status=status,
        )


def _account_from_api(raw: dict[str, Any]) -> Account:
    """Собрать `Account` из ответа бота.

    Даты бот отдаёт строками ISO-8601; `Account` ждёт `datetime`. Поля читаются
    через `.get` — ответ приходит из другого репозитория, и его форма не
    гарантирована этим кодом.
    """
    return Account(
        id=raw.get("id"),
        chat_id=raw.get("chat_id"),
        mail=raw.get("mail"),
        mail_verified_at=_parse_datetime(raw.get("mail_verified_at")),
        created_at=_parse_datetime(raw.get("created_at")),
        updated_at=_parse_datetime(raw.get("updated_at")),
        last_login_at=_parse_datetime(raw.get("last_login_at")),
        blocked=bool(raw.get("blocked", False)),
    )
