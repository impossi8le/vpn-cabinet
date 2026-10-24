"""Внутренний HTTP-клиент кабинета к боту.

Кабинет не ходит в MySQL: репозиторий публичный, и SQL со схемой таблиц
авторизации в нём выставил бы детали внутреннего устройства на вид. Вместо
этого кабинет дёргает внутренний роутер бота (`/internal/*`) на loopback с
общим секретом в заголовке — см. §3.4 спеки и «Границы сервиса» в CLAUDE.md.

Адрес и секрет берутся из окружения и **никогда** не хардкодятся: настоящие
значения не должны попасть в публичный репозиторий. Здесь стоит плейсхолдер
`127.0.0.1` — реальный хост и порт задаёт переменная `BOT_BASE_URL`.

Клиент асинхронный, а роуты кабинета — синхронные. Поэтому у клиента есть
синхронная форма — `BotStore`: она держит свой `httpx.Client` и отдаёт клиента
таким, каким его ждут роуты (issue #46). Асинхронность снимается в одном месте, а
не по месту в каждом роуте, — тот же приём, что у `_StoreConfigSource` в
`cabinet/app.py` для `.ovpn`.

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

import hashlib
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from cabinet.accounts import Account

__all__ = [
    "BOT_BASE_URL_ENV",
    "BOT_PUBLIC_BASE_ENV",
    "BOT_SECRET_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_PUBLIC_BASE",
    "NETWORK_ERROR_CODE",
    "POLL_CONFIRMED",
    "POLL_DENIED",
    "POLL_PENDING",
    "PUBLIC_ERROR_CODE",
    "SECRET_HEADER",
    "Balance",
    "BotClient",
    "BotConfigError",
    "BotError",
    "BotStore",
    "BotUnreachable",
    "Challenge",
    "CurrentSession",
    "IssuedSession",
    "LoginLink",
    "LoginPoll",
    "PromoValidation",
    "Referral",
    "Subscription",
    "TelegramAuthClient",
    "TelegramLogin",
    "UnlinkPreview",
    "public_code_and_secret",
    "secret_hash",
]

# Плейсхолдер: внутренний роутер бота живёт на loopback, но конкретный порт —
# деталь развёртывания, которой в публичном репозитории не место. Реальный
# адрес приходит из окружения (`BOT_BASE_URL`).
DEFAULT_BASE_URL = "http://127.0.0.1:8007"

BOT_BASE_URL_ENV = "BOT_BASE_URL"
# Имя общего секрета — то же, которым его задаёт юнит выкладки в репозитории
# бота. Второе имя для того же секрета разошлось бы с ним на развёртывании:
# развёртывание заполнило бы одно, а кабинет читал другое, и запуск выглядел бы
# рабочим, оставаясь нерабочим.
BOT_SECRET_ENV = "CABINET_INTERNAL_SECRET"

# Имя заголовка с общим секретом — часть контракта с роутером `/internal/*`.
SECRET_HEADER = "X-Internal-Secret"

# Код для случая «бот не ответил». Держим его отдельным от кодов бота, чтобы
# вызывающий код мог отличить недоступность сервиса от отказа логики.
NETWORK_ERROR_CODE = "bot_unreachable"

BOT_PATH = "/internal"

# --- публичный клиентский API бота: вход по Telegram ----------------------
#
# Это **другой** сервис, а не второй путь того же роутера. Внутренний
# (`/internal/*`) отдаёт аккаунты и подписки и требует общий секрет; публичный
# (`/api/v1/*`) ведёт вход и работает без секрета — он и так выставлен наружу,
# его адрес знает клиент. Смешать их значило бы либо отправить общий секрет в
# сервис, который его не ждёт, либо оставить вход без секрета там, где он нужен.
# Поэтому у публичного клиента своя база и свой транспорт, а общий с внутренним
# у них ровно один — разбор отказа и недоступности (`_error_from_response`,
# `BotUnreachable`): формат ошибок у бота один (§1).
#
# Адрес тоже из окружения: в бою рядом с кабинетом клиентский API слушает на
# loopback, но это деталь развёртывания, а не константа публичного репозитория.
DEFAULT_PUBLIC_BASE = "http://127.0.0.1:8006"
BOT_PUBLIC_BASE_ENV = "BOT_PUBLIC_BASE"

AUTH_LINK_PATH = "/api/v1/auth/link"
AUTH_POLL_PATH = "/api/v1/auth/poll"

# Статусы опроса (§5 публичного контракта). Имена — часть контракта бота, а не
# подписи: ветвиться по ним, а не по тексту.
POLL_PENDING = "pending"
POLL_DENIED = "denied"
POLL_CONFIRMED = "confirmed"

# Длина кода и секрета челленджа. Числа как у кодов `AuthMail`: `secrets` даёт
# непредсказуемость, а длина — запас против перебора. `public_code` короче не
# потому, что он менее важен, а потому что он уходит в ссылку и в `start`-payload
# Telegram, где длина ограничена.
PUBLIC_CODE_BYTES = 16
SECRET_BYTES = 32

# Код для случая «публичный API ответил не в формате контракта». Отделён от кодов
# бота по той же причине, что `NETWORK_ERROR_CODE`: расхождение формата — это не
# отказ входа, и вызывающий код не должен принимать его за «человек отклонил».
PUBLIC_ERROR_CODE = "public_invalid_response"

# Коды бота (§1 контракта), которые протокол связывания моделирует «нулём
# изменённых строк», а не исключением: занятое место при привязке и единственный
# оставшийся способ входа при отвязке. Для привязки `0` — «место заняли между
# чтением и записью», для отвязки — «аккаунт остался бы недоступен навсегда».
# Список — имена контракта, а не текст сообщения: текст меняется, код — нет.
_REFUSAL_CODES = frozenset({"chat_id_taken", "mail_taken", "last_login_method"})

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


@dataclass(frozen=True)
class UnlinkPreview:
    """Последствие отвязки — ответ `GET /internal/accounts/{id}/unlink-preview`.

    Показать это надо **до** отвязки, а не после: конфиги живут на `chat_id`,
    не на аккаунте, поэтому судьба доступа у двух способов разная. Число
    конфигов знает только бот — кабинет их не считает, — и без этого числа
    экран не может честно сказать, затронута ли потеря. Поэтому же ноль здесь
    значит «про конфиги говорить нечего», а не «ноль конфигов» в тексте.
    """

    configs_affected: int
    remaining_method: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> UnlinkPreview:
        """Собрать из ответа бота, не доверяя его форме.

        Ответ приходит из другого репозитория, и `configs_affected` там не
        гарантирован числом: приведение через `int(..., 0)` не даёт пропавшему
        полю уронить экран `TypeError`-ом. Незнакомый `remaining_method`
        сохраняется как есть — трактовка его значения дело рендера, а не
        разбора: молча превращать «unknown» в «осталось что-то» здесь нельзя.
        """
        return cls(
            configs_affected=int(raw.get("configs_affected", 0)),
            remaining_method=raw.get("remaining_method"),
        )


@dataclass(frozen=True)
class Balance:
    """Накопленный бонусный кредит — ответ
    `GET /internal/accounts/{id}/balance`.

    Только `credit_rub`: это деньги, которые бот списывает как скидку при
    выборе тарифа. Число читается через `.get(..., 0)`, а не индексируется —
    ответ приходит из другого репозитория, и пропавшее поле должно читаться как
    «кредита нет», а не ронять экран `KeyError`-ом.
    """

    credit_rub: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Balance:
        return cls(credit_rub=int(raw.get("credit_rub", 0)))


@dataclass(frozen=True)
class Referral:
    """Реферальная программа — ответ
    `GET /internal/accounts/{id}/referral`.

    `referral_code` — свой код человека; `invited_count` — сколько по нему
    пришло; `rub_earned` — сколько на этом начислено. Сумма числом, как её
    считает бот: кабинет её не пересчитывает и не округляет.
    """

    referral_code: str
    invited_count: int
    rub_earned: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Referral:
        """Собрать из ответа бота, не доверяя его форме: новой версии бота
        простительно не прислать поле, и это не должно ронять экран."""
        return cls(
            referral_code=str(raw.get("referral_code", "")),
            invited_count=int(raw.get("invited_count", 0)),
            rub_earned=int(raw.get("rub_earned", 0)),
        )


@dataclass(frozen=True)
class PromoValidation:
    """Проверенный промокод — ответ
    `POST /internal/accounts/{id}/promo/validate`.

    `valid` решает, показывать ли скидку; `discount_percent` имеет смысл только
    при `valid=True`. `reason` — код причины отказа (`expired`, `not_found`…),
    по которому ветвится вызывающий код, а не текст сообщения: текст меняется,
    код — часть контракта.
    """

    valid: bool
    discount_percent: int = 0
    reason: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> PromoValidation:
        """Собрать из ответа бота, не доверяя его форме. Отсутствие `valid`
        читается как «код не подходит»: обещать скидку, которой бот не
        подтвердил, опаснее, чем её не показать."""
        return cls(
            valid=bool(raw.get("valid", False)),
            discount_percent=int(raw.get("discount_percent", 0)),
            reason=raw.get("reason"),
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

    async def unlink_preview(self, account_id: int, method: str) -> UnlinkPreview:
        """Что будет с конфигами, если отвязать `method` (`mail` | `telegram`).

        Число затронутых конфигов знает только бот (§4а): они живут в `users`
        на `chat_id`, и кабинет их не считает. Без этого числа экран не мог бы
        честно сказать, затронет ли отвязка доступ, — а он обязан сказать это
        **до** нажатия, а не после.

        Это чтение, а не действие: ничего не меняет и ничего не решает за
        человека. Незнакомый `method` бот отвергает `400 bad_request` — сюда
        это придёт как `BotError` и не превратится в сбой связи.
        """
        data = await self._request(
            "GET",
            f"{BOT_PATH}/accounts/{account_id}/unlink-preview",
            params={"method": method},
        )
        return UnlinkPreview.from_api(data)

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

    # ---------- Деньги человека: баланс, рефералка, промокод -----------

    async def get_balance(self, account_id: int) -> Balance:
        """Накопленный бонусный кредит аккаунта.

        Кредит живёт на `chat_id` у бота, поэтому у аккаунта без Telegram его
        нет — бот отвечает `409 code: telegram_required`. Это **осмысленный
        отказ**, а не сбой: он приходит сюда как `BotError` с этим кодом, и
        вызывающий решает по коду «показать нечего», а не «повторить запрос».
        Именно поэтому в кабинете при таком коде не рисуется `0 ₽`: ноль и
        «кредита нет» — разные вещи, и первый читался бы как «вам ничего не
        начислили».
        """
        data = await self._request("GET", f"{BOT_PATH}/accounts/{account_id}/balance")
        return Balance.from_api(data)

    async def get_referral(self, account_id: int) -> Referral:
        """Реферальная программа: свой код, сколько приглашено, сколько earned.

        Рефералы тоже живут на `chat_id` у бота, поэтому без Telegram ответ —
        тот же `409 code: telegram_required`, что у баланса.
        """
        data = await self._request("GET", f"{BOT_PATH}/accounts/{account_id}/referral")
        return Referral.from_api(data)

    async def validate_promo(self, account_id: int, code: str) -> PromoValidation:
        """Проверить промокод до оплаты.

        Возвращается разобранный ответ, а не исключение на недействительный код:
        «код не подходит» — это **результат проверки** (`valid=False` с причиной
        в `reason`), а не сбой запроса. Отказ связи и отказ формата, напротив,
        приходят исключениями (`BotUnreachable`, `BotError`) — вызывающий их
        различает, потому что «повторить» и «код истёк» требуют разного.
        """
        data = await self._request(
            "POST",
            f"{BOT_PATH}/accounts/{account_id}/promo/validate",
            json={"code": code},
        )
        return PromoValidation.from_api(data)

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


class BotStore:
    """Живой клиент в форме, которую ждут роуты кабинета (issue #46).

    **Зачем.** Роуты кабинета синхронные и зовут хранилище синхронно:
    `find_by_id`, `subscriptions_for`, `get_balance`, `get_referral`,
    `unlink_preview`. У `BotClient` таких методов нет вовсе: его методы —
    корутины и называются иначе (`list_subscriptions` против
    `subscriptions_for`, `find_account_by_chat_id` против `find_by_id`).
    Пока адаптера не было, выложенный кабинет отвечал 500 на первом же экране:
    тесты этого не видели, потому что шли против `tests.fakes.FakeApi`, у
    которого нужная форма есть по определению.

    **Как снимается асинхронность.** Здесь, в одном месте, и синхронным
    `httpx.Client`, а не `asyncio.run` поверх корутин. Причина: роуты
    объявлены как `def`, и FastAPI исполняет их в пуле потоков, где цикла
    событий нет. `asyncio.run` заводил бы **новый** цикл и **новое**
    соединение на каждый запрос экрана — работа впустую там, где соединение
    уже может переиспользоваться; а внутри уже запущенного цикла он падает
    вовсе. Синхронный клиент держит пул соединений и переиспользует его между
    запросами — ровно то, что делает `AsyncClient` у `BotClient`.

    **Что адаптер не делает.** Во-первых, он отдаёт **сырые записи бота**, а не
    готовые представления: сборка `SubscriptionView` остаётся в
    `subscriptions.subscription_views`, где и была. Во-вторых, он отдаёт не
    `AccountStore` целиком, а ту форму, которую ждут роуты, вход и связывание:
    для связывания это «сколько строк изменилось» (`link_*`/`unlink_*`), а не
    объект, — см. `_changed_rows` и докстроку `linking.AccountStore`. Обе формы
    (`signin.AccountStore`, `linking.AccountStore`) адаптер закрывает: без них
    вход по почте — главная дверь кабинета — отвечал бы 500 (issue #47).

    Скачиванию `.ovpn` нужен ещё один метод — `get_ovpn`: его ищет на хранилище
    `_StoreConfigSource` в `cabinet/app.py`, без него боевое скачивание отвечало
    отказом на `200` и файла не было вовсе (issue #48). Он же держит код отказа
    бота (`subscription_expired`, `config_revoked`) — по нему `configs` выбирает
    экран, поэтому отказ не должен свернуться в общий.

    Конфигурацию адаптер не дублирует: адрес и секрет он берёт у того самого
    клиента, которого оборачивает. Второй копии секрета быть не должно — иначе
    развёртывание заполнило бы одну, а запрос ушёл бы с другой.
    """

    def __init__(self, bot: BotClient, *, http_client: httpx.Client | None = None) -> None:
        # Аргумент важнее умолчания: так тест подставляет транспорт и не зависит
        # от сети. Порядок тот же, что у `BotClient(client=...)`.
        self.bot = bot
        self._http = http_client or httpx.Client(
            base_url=bot.base_url,
            timeout=httpx.Timeout(
                DEFAULT_TOTAL_TIMEOUT,
                connect=DEFAULT_CONNECT_TIMEOUT,
                read=DEFAULT_READ_TIMEOUT,
            ),
        )

    # ---------- то, что зовут роуты ----------

    def find_by_id(self, account_id: int) -> Account | None:
        """Аккаунт по `id` (`GET /internal/accounts/{id}`) или `None`.

        `404 account_not_found` — это «аккаунта нет», а не сбой: экран на него
        гасит сессию и ведёт на вход. Отличать «не найден» от «бот сломался»
        обязательно — иначе расхождение состояния показывалось бы 500, а
        недоступность бота молча выглядела бы как «аккаунт удалён».
        """
        try:
            data = self._get(f"{BOT_PATH}/accounts/{account_id}")
        except BotError as exc:
            if exc.code == "account_not_found" or exc.status == 404:
                return None
            raise
        account = data.get("account")
        return _account_from_api(account) if account else None

    def subscriptions_for(self, account_id: int) -> list[Subscription]:
        """Подписки аккаунта в форме ответа бота.

        Возвращаются сырые записи, а не представления: «истекла ли» и «сколько
        осталось» решает `subscriptions`, и второй копии этих правил здесь быть
        не должно. Пустой список — норма: «подписок нет» это состояние экрана
        (W4.3), а не отказ.
        """
        data = self._get(f"{BOT_PATH}/accounts/{account_id}/subscriptions")
        return [Subscription.from_api(item) for item in data.get("subscriptions", [])]

    def get_ovpn(self, account_id: int, config_id: str) -> bytes:
        """Содержимое `.ovpn` (`GET /internal/accounts/{id}/configs/{config_id}`).

        Байтами, а не текстом: профиль отдаётся файлом, и превращать его в строку
        по дороге незачем — так же, как у `BotClient.get_ovpn`.

        Этого метода адаптеру и не хватало (issue #48): `_StoreConfigSource` в
        `cabinet/app.py` ищет `get_ovpn` на хранилище, а без него выложенный
        кабинет отвечал отказом на **каждое** скачивание — то есть главного своего
        действия не делал вовсе. Отказ был мягким (`200` с экраном, не `500`),
        поэтому и незаметным.

        `config_id` здесь равен id подписки: конфиг у подписки один, и адрес
        скачивания называет подписку (`subscription_config` в `cabinet/app.py`).

        Отказ по истёкшей подписке, чужому или отозванному конфигу приходит
        `BotError` с кодом контракта. Разбор общий (`_error_from_response`),
        поэтому код доезжает до `configs`: там по нему выбирается своё исключение,
        а у исключения — свой текст на экране. Свернуть отказ здесь в «не удалось»
        значило бы отнять у человека единственную подсказку.
        """
        return self._request_bytes("GET", f"{BOT_PATH}/accounts/{account_id}/configs/{config_id}")

    def get_balance(self, account_id: int) -> Balance:
        """Накопленный кредит. Отсутствие Telegram — `BotError` с кодом
        `telegram_required`, который вызывающий отличает от нуля."""
        return Balance.from_api(self._get(f"{BOT_PATH}/accounts/{account_id}/balance"))

    def get_referral(self, account_id: int) -> Referral:
        """Реферальная программа. Без Telegram — тот же `telegram_required`."""
        return Referral.from_api(self._get(f"{BOT_PATH}/accounts/{account_id}/referral"))

    def unlink_preview(self, account_id: int, method: str) -> UnlinkPreview:
        """Что потеряется при отвязке `method` (`mail` | `telegram`).

        Синхронный, в отличие от одноимённого метода `BotClient`: экран способов
        входа синхронный, а число затронутых конфигов знает только бот (§4а).
        """
        data = self._get(
            f"{BOT_PATH}/accounts/{account_id}/unlink-preview", params={"method": method}
        )
        return UnlinkPreview.from_api(data)

    # ---------- вход и связывание (signin, linking) ----------
    #
    # Семь методов ниже — то, чего адаптеру не хватало для входа и связывания:
    # без них выложенный кабинет отвечал 500 на входе по почте, а он и есть
    # главная дверь (issue #47). Формы — из протоколов `signin.AccountStore` и
    # `linking.AccountStore`, а не из удобства.

    def find_by_mail(self, mail: str) -> Account | None:
        """Аккаунт по почте (`GET /internal/accounts/by-mail`) или `None`.

        `404 account_not_found` — это «аккаунта нет», а не сбой: по нему вход
        решает «завести аккаунт», а по недоступности бота — «повторить».
        Отличать обязательно: иначе первая же регистрация падала бы 500, а
        недоступность бота выглядела бы как «такого адреса нет».
        """
        return self._find(f"{BOT_PATH}/accounts/by-mail", params={"mail": mail})

    def find_by_chat_id(self, chat_id: int) -> Account | None:
        """Аккаунт по Telegram id (`GET /internal/accounts/by-chat-id`) или `None`.

        Симметрично поиску по почте. Нужен Telegram-входу и связыванию: по нему
        отличают «место занято другим аккаунтом» от «заняло это же самое».
        """
        return self._find(f"{BOT_PATH}/accounts/by-chat-id", params={"chat_id": chat_id})

    def _find(self, path: str, **kwargs: Any) -> Account | None:
        """Общий разбор поиска: `404` — «не найден», а не ошибка.

        Тот же приём, что у `find_by_id`: «нет аккаунта» и «бот сломался» —
        разные состояния, и первое обязано быть `None`, а не исключением.
        """
        try:
            data = self._get(path, **kwargs)
        except BotError as exc:
            if exc.code == "account_not_found" or exc.status == 404:
                return None
            raise
        account = data.get("account")
        return _account_from_api(account) if account else None

    def create_account(
        self,
        *,
        chat_id: int | None = None,
        mail: str | None = None,
        mail_verified_at: datetime | None = None,
    ) -> Account:
        """Завести аккаунт (`POST /internal/accounts`) — регистрация или Telegram-вход.

        `mail_verified_at` уходит боту вместе с адресом: почта без подтверждения
        не существует (инвариант 2), и бот отвечает `400 mail_not_verified`, если
        отметки нет. Подставлять её здесь «сейчас» нельзя — вход передаёт момент,
        полученный от `AuthMail`, а отметка обязана совпадать с моментом проверки
        кода. Аккаунт без способа входа не заводим: он был бы недоступен навсегда
        (W3.5), и это отказ вызывающего, а не бота.
        """
        if chat_id is None and not mail:
            raise ValueError("аккаунт без способа входа недоступен навсегда")
        body: dict[str, Any] = {}
        if chat_id is not None:
            body["chat_id"] = chat_id
        if mail:
            body["mail"] = mail
            if mail_verified_at is not None:
                body["mail_verified_at"] = mail_verified_at.isoformat()
        data = self._request("POST", f"{BOT_PATH}/accounts", json=body)
        return _account_from_api(data["account"])

    def link_chat_id(self, account_id: int, chat_id: int) -> int:
        """Привязать Telegram (`POST /internal/accounts/{id}/link-telegram`).

        `1` — привязали, `0` — место занято. Условие `AND chat_id IS NULL` живёт
        у бота: занятый `chat_id` приходит `409 chat_id_taken`, и именно ради
        этого случая `0` отличим от `1` — место мог занять кто-то другой между
        чтением и записью. Перезаписи нет и быть не должно (§3.3).
        """
        return self._changed_rows(
            f"{BOT_PATH}/accounts/{account_id}/link-telegram", {"chat_id": chat_id}
        )

    def link_mail(self, account_id: int, mail: str, mail_verified_at: datetime) -> int:
        """Привязать почту (`POST /internal/accounts/{id}/link-mail`).

        `mail_verified_at` обязателен: владение адресом доказывает код, и
        `Linking` передаёт сюда момент проверки. Без него бот ответил бы
        `400 mail_not_verified`, а привязка стала бы заявлением браузера —
        pre-account hijacking (§3.3). Занятая почта — `409 mail_taken` → `0`.
        """
        body = {"mail": mail, "mail_verified_at": mail_verified_at.isoformat()}
        return self._changed_rows(f"{BOT_PATH}/accounts/{account_id}/link-mail", body)

    def unlink_chat_id(self, account_id: int) -> int:
        """Отвязать Telegram (`POST /internal/accounts/{id}/unlink-telegram`).

        `1` — отвязали, `0` — отвязка оставила бы аккаунт без способа входа. Бот
        отвечает на это `409 last_login_method` (§1), и `0` доносит запрет до
        `Linking.unlink_telegram`, где он становится `UnlinkRefused` — **своим**
        отказом, а не общим: аккаунт без входа недоступен навсегда (W3.5).
        """
        return self._changed_rows(f"{BOT_PATH}/accounts/{account_id}/unlink-telegram")

    def unlink_mail(self, account_id: int) -> int:
        """Отвязать почту (`POST /internal/accounts/{id}/unlink-mail`).

        Симметрично отвязке Telegram: `409 last_login_method` → `0` → отказ
        «нельзя отвязать последний способ входа», а не «не удалось» и не 500.
        """
        return self._changed_rows(f"{BOT_PATH}/accounts/{account_id}/unlink-mail")

    # ---------- транспорт ----------

    def _headers(self) -> dict[str, str]:
        # Тот же заголовок и тот же секрет, что у живого клиента, — берутся из
        # одного места (`BotClient._headers`), чтобы разойтись им было негде.
        return self.bot._headers()

    def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Синхронное чтение. Тонкая обёртка над `_request`: у роутов чтений
        большинство, и `_get` называет намерение, а общий транспорт — один."""
        return self._request("GET", path, **kwargs)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Синхронный запрос, ответ которого — объект JSON.

        Разбор ошибок общий с `BotClient` (`_error_from_response`): формат отказа
        — часть контракта бота, и вторая его копия разошлась бы с первой ровно
        там, где ошибку и надо различать по коду. Метод — параметр, а не
        `_get`/`_post` по отдельности: адрес, заголовок и разбор ошибки у чтения
        и записи те же, и разводить их значило бы держать две копии этого
        разбора.
        """
        response = self._send(method, path, **kwargs)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise BotError(
                "invalid_response", "бот вернул не-JSON", status=response.status_code
            ) from exc
        if not isinstance(payload, dict):
            raise BotError("invalid_response", "бот вернул не объект", status=response.status_code)
        return payload

    def _request_bytes(self, method: str, path: str, **kwargs: Any) -> bytes:
        """Синхронный запрос, ответ которого — байты (`.ovpn`).

        Тело не разбирается вовсе: профиль бот отдаёт файлом, а не объектом, и
        ветка «разбери тело как JSON» отвергла бы его как `invalid_response` —
        то есть отказ бота вместо файла. Проверка статуса и разбор отказа при
        этом те же, что у JSON-ответов (`_send`), — иначе код отказа терялся бы
        на байтовом пути.
        """
        return self._send(method, path, **kwargs).content

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Один синхронный запрос: сеть, статус, разбор отказа.

        Общая точка для JSON- и байтовых ответов — та же роль, что у
        `BotClient._send`: своя у каждого, обработка ошибок разъехалась бы ровно
        там, где отказ бота и надо различать по коду (issue #48: код отказа
        обязан дойти до экрана, а не свернуться в общий отказ).
        """
        try:
            response = self._http.request(method, path, headers=self._headers(), **kwargs)
        except _NETWORK_ERRORS as exc:
            raise BotUnreachable(
                f"запрос к боту не удался: {exc.__class__.__name__}", cause=exc
            ) from exc

        if response.status_code >= 400:
            raise _error_from_response(response)
        return response

    def _changed_rows(self, path: str, body: dict[str, Any] | None = None) -> int:
        """Запись, отвечающая «сколько строк изменилось»: `1` или `0`.

        Протокол связывания требует именно числа, а не объекта: только `0`
        отличает «место заняли между чтением и записью» от «привязал». Бот
        отвечает на это `409` с кодом (`chat_id_taken`, `mail_taken`,
        `last_login_method`), поэтому контрактный отказ переводится в `0`, а
        **прочие** отказы (например `404 account_not_found`) пробрасываются как
        есть: это не «место занято», а расхождение, и сводить его к нулю значило
        бы спрятать настоящий сбой за состоянием «занято».
        """
        try:
            self._request("POST", path, json=body)
        except BotError as exc:
            if exc.code in _REFUSAL_CODES:
                return 0
            raise
        return 1


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


# --- Вход по Telegram: публичный API бота (§5) -----------------------------


def public_code_and_secret() -> tuple[str, str]:
    """Новая пара «код челленджа + секрет» для входа.

    Источник — `secrets`, не `random`: угаданный `public_code` — половина входа,
    а угаданный секрет — весь он целиком. Оба возвращаются вместе, потому что
    порознь они смысла не имеют: секрет существует ровно ради этого кода.
    """
    return (
        secrets.token_urlsafe(PUBLIC_CODE_BYTES),
        secrets.token_urlsafe(SECRET_BYTES),
    )


def secret_hash(secret: str) -> str:
    """Хеш секрета — то, что кабинет отправляет боту при `/auth/link`.

    Бот хранит хеш и сверяет его при опросе: сам секрет у него не лежит. Форма
    (`sha256` в hex, без префикса) — та, что принимает `secret_matches` бота.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LoginLink:
    """Ответ `POST /api/v1/auth/link` — начатый челлендж.

    `deep_link` показывает человеку «Открыть Telegram»; `expires_at` задаёт срок
    челленджа. Срок берётся **у бота**, а не выдумывается кабинетом: челлендж
    живёт там, и свой таймер разошёлся бы с ним — кабинет считал бы вход живым,
    когда бот его уже погасил, и наоборот.
    """

    deep_link: str
    expires_at: datetime | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> LoginLink:
        """Собрать из ответа бота, не доверяя его форме: пропавшее поле не должно
        ронять начало входа, а незнакомый срок — трактоваться как «истёк»."""
        return cls(
            deep_link=str(raw.get("deep_link", "")),
            expires_at=_parse_datetime(raw.get("expires_at")),
        )


@dataclass(frozen=True)
class LoginPoll:
    """Ответ `POST /api/v1/auth/poll` — что случилось с челленджем.

    `status` — `pending`, `denied` или `confirmed` (§5). На `confirmed` бот
    отдаёт `account_id` и `chat_id`: аккаунт он уже нашёл или завёл для
    подтверждённого `chat_id`, и **это единственный источник личности**.
    `account_id` здесь `None`, если бот его не прислал, — вызывающий обязан
    отказать, а не догадываться по `chat_id` (§3.3).

    `retry_after_ms` приходит только с `pending`: сколько ждать до следующего
    опроса. Читается через `.get`, потому что ответ приходит из другого
    репозитория, и пропавшее поле не должно ронять разбор.
    """

    status: str
    retry_after_ms: int | None = None
    account_id: int | None = None
    chat_id: int | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> LoginPoll:
        """Собрать из ответа бота, не доверяя его форме.

        `account_id`/`chat_id` читаются как `int | None`, а не приводятся к нулю:
        ноль — валидный по типу, но ложный идентификатор, и подстановка его
        означала бы вход в чужой аккаунт вместо честного отказа.
        """
        return cls(
            status=str(raw.get("status", "")),
            retry_after_ms=_optional_int(raw.get("retry_after_ms")),
            account_id=_optional_int(raw.get("account_id")),
            chat_id=_optional_int(raw.get("chat_id")),
        )


def _optional_int(value: Any) -> int | None:
    """Целое из ответа бота или `None`. Нечисловое — `None`, а не исключение."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Challenge:
    """Начатый челлендж входа: секрет, ссылка и срок. Живёт только в памяти.

    Секрет тут потому, что опрос требует его вместе с кодом, а хранить его в
    браузере нельзя: он даёт завершить вход. Ссылка — чтобы экран мог предложить
    «открыть бота ещё раз» на повторном показе, не начиная новый челлендж.
    """

    secret: str
    deep_link: str
    expires_at: datetime | None = None


@dataclass
class TelegramLogin:
    """Состояние начатых челленджей входа в памяти процесса кабинета.

    **Почему в памяти.** Тот же выбор, что у кодов `AuthMail` (§3.4 спеки): кабинет
    не трогает базу, а челлендж живёт минуты и переживать перезапуск не должен —
    его срок задаёт бот. Перезапуск процесса просто теряет начатые входы, и это
    честное состояние: человек начинает заново, а не входит по протухшему.

    **Ключ — `public_code`.** Он не секрет: он и так у человека в `deep_link` и
    уходит в скрытое поле формы. Секрет по нему находится только здесь, на
    сервере, и в разметку не попадает никогда.

    **Опрос не забирает запись.** При `pending` человек опрашивает снова, и
    запись обязана пережить это; гасит её только исход (`forget`) — успех,
    отказ или истечение.
    """

    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _challenges: dict[str, Challenge] = field(default_factory=dict, init=False, repr=False)

    def remember(
        self,
        public_code: str,
        *,
        secret: str,
        deep_link: str,
        expires_at: datetime | None = None,
    ) -> None:
        """Запомнить начатый челлендж. Заодно убирает протухшие.

        Чистка здесь, а не по таймеру: таймеров в процессе кабинета нет, а
        растущий словарь — утечка. Свежий вход — естественная точка, где старые
        записи точно никому не нужны.
        """
        self._prune()
        self._challenges[public_code] = Challenge(
            secret=secret, deep_link=deep_link, expires_at=_as_utc(expires_at)
        )

    def get(self, public_code: str | None) -> Challenge | None:
        """Челлендж по коду или `None`. Протухший — `None` и забывается."""
        if not public_code:
            return None
        challenge = self._challenges.get(public_code)
        if challenge is None:
            return None
        if self._expired(challenge):
            del self._challenges[public_code]
            return None
        return challenge

    def secret_for(self, public_code: str) -> str:
        """Секрет челленджа. Нужен только своим кодом кабинета — не для экрана."""
        return self._challenges[public_code].secret

    def forget(self, public_code: str) -> None:
        """Погасить челлендж: он завершён, отказом или успехом."""
        self._challenges.pop(public_code, None)

    def _prune(self) -> None:
        for code in [c for c, ch in self._challenges.items() if self._expired(ch)]:
            del self._challenges[code]

    def _expired(self, challenge: Challenge) -> bool:
        """Истёк ли челлендж. Без срока — считаем живым: срок задаёт бот, и
        выдумывать свой значило бы гасить вход раньше, чем это делает он."""
        if challenge.expires_at is None:
            return False
        return self.now() >= challenge.expires_at


def _as_utc(value: datetime | None) -> datetime | None:
    """Привести срок к UTC. Наивное время бот пишет как UTC (тот же приём, что
    в `cabinet.sessions._as_utc`), и сравнивать его с зональным иначе нельзя."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class TelegramAuthClient:
    """Клиент публичного API бота: начало входа и опрос подтверждения (§5).

    **Синхронный, в отличие от `BotClient`.** Роуты кабинета синхронные, и
    синхронный клиент здесь — не упрощение, а отсутствие лишнего звена: у
    внутреннего клиента асинхронность нужна была потому, что он уже был
    асинхронным, и для роутов к нему пришлось писать адаптер `BotStore`. Здесь
    адаптера нет вовсе, и держать его незачем.

    **Секрета у него нет.** Публичный API его не требует — он для внешних
    клиентов. Общий секрет внутреннего роутера сюда не передаётся и не должен:
    иначе он уходил бы в сервис, который его не проверяет, и в логи той стороны.

    **Что клиент не делает.** Он не хранит секрет челленджа — это дело
    `TelegramLogin`; он не решает, что показать человеку; и он не заводит аккаунт
    сам. Ответ опроса отдаётся как есть, а трактовка (`pending`/`denied`/
    `confirmed`, отказ по коду) — дело вызывающего.
    """

    def __init__(self, base_url: str | None = None, *, client: httpx.Client | None = None) -> None:
        self.base_url = (base_url or os.getenv(BOT_PUBLIC_BASE_ENV) or DEFAULT_PUBLIC_BASE).rstrip(
            "/"
        )
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                DEFAULT_TOTAL_TIMEOUT,
                connect=DEFAULT_CONNECT_TIMEOUT,
                read=DEFAULT_READ_TIMEOUT,
            ),
        )

    @classmethod
    def from_env(cls, source: Mapping[str, str]) -> TelegramAuthClient:
        """Собрать из окружения, как `MailSender.from_env`.

        Адрес — настройка развёртывания, но пустая переменная здесь читается как
        «взять умолчание», а не как отказ: умолчание — loopback рядом с
        кабинетом, где клиентский API и слушает. В отличие от адреса внутреннего
        роутера, промахнуться тут некуда: за loopback другой сервис не отвечает.
        """
        return cls(base_url=source.get(BOT_PUBLIC_BASE_ENV, "").strip() or None)

    def start_login(self, public_code: str, secret_hash_value: str) -> LoginLink:
        """Начать челлендж: `public_code` + `secret_hash` → ссылка `deep_link`.

        Ссылку строит бот: именем бота владеет он, и собранная кабинетом ссылка
        однажды разошлась бы с настоящей. Возвращается ссылка и её срок — больше
        экрану показывать нечего, а срок нужен, чтобы гасить протухшие челленджи
        в памяти.
        """
        return LoginLink.from_api(
            self._request(
                "POST",
                AUTH_LINK_PATH,
                json={"public_code": public_code, "secret_hash": secret_hash_value},
            )
        )

    def poll_login(self, public_code: str, secret: str, device_nonce: str) -> LoginPoll:
        """Опрос подтверждения: `public_code` + `secret` (+ код из Telegram).

        `device_nonce` — код, который бот прислал человеку в чат; его вводит
        человек. Бот требует его лишь тогда, когда подтверждение уже произошло,
        но отправляем всегда: пустой код — это `nonce_mismatch` (человеку «код
        не подошёл»), а не спор с ботом о порядке проверок.

        Отказы по коду (`invalid_secret`, `operation_not_found`,
        `already_consumed`, `nonce_mismatch`) приходят сюда `BotError` — разбор
        общий с внутренним клиентом, и код доходит до вызывающего неизменным.
        """
        return LoginPoll.from_api(
            self._request(
                "POST",
                AUTH_POLL_PATH,
                json={"public_code": public_code, "secret": secret, "device_nonce": device_nonce},
            )
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Один запрос к публичному API: сеть, статус, разбор ответа.

        Заголовка с секретом здесь нет намеренно — у этого сервиса секрета нет.
        Обработка ошибок та же, что у внутреннего клиента, и это не дублирование
        ради красоты: формат отказа у бота один, и вторая его копия разошлась бы
        ровно там, где код отказа и надо различить.
        """
        try:
            response = self._client.request(method, path, **kwargs)
        except _NETWORK_ERRORS as exc:
            raise BotUnreachable(
                f"запрос к публичному API бота не удался: {exc.__class__.__name__}", cause=exc
            ) from exc

        if response.status_code >= 400:
            raise _error_from_response(response)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise BotError(
                PUBLIC_ERROR_CODE, "бот вернул не-JSON", status=response.status_code
            ) from exc
        if not isinstance(payload, dict):
            raise BotError(PUBLIC_ERROR_CODE, "бот вернул не объект", status=response.status_code)
        return payload
