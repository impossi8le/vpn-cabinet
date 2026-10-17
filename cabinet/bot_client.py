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
