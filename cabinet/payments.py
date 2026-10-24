"""Приём оплаты через ЮKassa: создание платежа и чтение его состояния.

Кабинет принимает деньги **сам**, а не через платёжный сервис бота (решение
владельца, спека §3): у кабинета свой магазин и свои ключи. Это второй источник
правды о платежах рядом с ботом, и потому у модуля две обязанности, которые
нельзя разнести. Создать платёж — начать покупку; **прочитать** платёж у
провайдера — единственная настоящая проверка «деньги пришли». Подписи у
уведомлений современного API ЮKassa нет (issue #61), поэтому «оплачено»
подтверждается перезапросом платежа, а не телом уведомления; читающая сторона
нужна вебхуку, и живёт она здесь же, где запись.

Правила, которые модуль держит сам:

- **Чек не передаём.** Решение владельца (спека §3а): адрес покупателя кабинет не
  спрашивает, чеком занимается страница оплаты ЮKassa. Блока `receipt` в запросе
  нет вовсе — ни `customer`, ни позиций. В боте в чек подставляются константы
  сервиса (`handlers/payment_handler.py:137`) — это #42, и повторять это здесь
  нельзя: чек с чужим адресом хуже отсутствующего, потому что выглядит правильным.

- **Сумма — аргумент, а не расчёт.** `create_payment` не считает денег: сумма
  приходит из заказа. Считать её здесь значило бы завести вторую цену рядом с
  `cabinet.pricing`, а две реализации цены разойдутся при первой правке скидок.

- **Ключ и секрет не логируются.** Ни в сообщениях об ошибках, ни в текстах для
  человека: репозиторий публичный, а лог читают шире, чем задумано.

- **Транспорт подставляется.** Живую сеть тесты не трогают: `client` — шов, как у
  `ApiTransport` в `mail_sender`. Наружу при прогоне не уходит ничего.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import quote

import httpx

__all__ = [
    "API_BASE_ENV",
    "DEFAULT_API_BASE",
    "DEFAULT_TIMEOUT_SECONDS",
    "PAYMENT_STATUS_CANCELED",
    "PAYMENT_STATUS_PENDING",
    "PAYMENT_STATUS_SUCCEEDED",
    "PUBLIC_BASE_ENV",
    "RETURN_PATH",
    "SECRET_KEY_ENV",
    "SHOP_ID_ENV",
    "Payment",
    "PaymentClient",
    "Payments",
    "PaymentsConfigError",
    "PaymentsError",
    "PaymentsProviderError",
    "PaymentsSettings",
    "PaymentsTransportError",
    "UnconfiguredPayments",
    "build_payments",
    "return_url",
]

# Адрес API ЮKassa и таймаут одной попытки. Это не деталь развёртывания, а
# контракт сервиса: адрес API один для всех магазинов, и «настраивать» его
# незачем. Значение вынесено константой, чтобы стенд и тест могли его не
# повторять.
DEFAULT_API_BASE = "https://api.yookassa.ru/v3"
DEFAULT_TIMEOUT_SECONDS = 10.0

# Куда ЮKassa возвращает человека после оплаты. Путь — часть кабинета, а полный
# адрес собирается из настройки публичного адреса (`PUBLIC_BASE_ENV`): в
# публичном репозитории настоящего домена быть не может.
RETURN_PATH = "/buy/done"

SHOP_ID_ENV = "YOOKASSA_SHOP_ID"
SECRET_KEY_ENV = "YOOKASSA_SECRET_KEY"
# Публичный адрес кабинета — тот, по которому до него дотягивается браузер
# покупателя. Из него собирается `return_url`: ЮKassa должна вернуть человека
# туда, а относительный адрес уведомлению не годится.
PUBLIC_BASE_ENV = "CABINET_PUBLIC_BASE"
API_BASE_ENV = "YOOKASSA_API_BASE"

# Статусы платежа (имена контракта ЮKassa, а не подписи: ветвиться по ним, а не
# по локализованному тексту). `succeeded` — деньги списаны; это единственное
# состояние, из которого следует выдача.
PAYMENT_STATUS_SUCCEEDED = "succeeded"
PAYMENT_STATUS_PENDING = "pending"
PAYMENT_STATUS_CANCELED = "canceled"

# Сетевые сбои httpx, которые для модуля означают одно: ЮKassa не ответила.
_NETWORK_ERRORS = (httpx.HTTPError, OSError)


class PaymentsError(Exception):
    """Оплата не прошла. Сообщение — для оператора: без ключа и без секрета."""


class PaymentsConfigError(PaymentsError):
    """Магазин не настроен или настроен неполно.

    Отказ конфигурации, а не конкретного платежа: неверная настройка должна
    называться сразу, а не выясняться на первой оплате человека.
    """


class PaymentsTransportError(PaymentsError):
    """ЮKassa не ответила: сеть, таймаут, обрыв соединения.

    Отличается от отказа провайдера тем, что платежа могло и не быть: повтор
    здесь допустим, а «провайдер отверг» — нет.
    """


class PaymentsProviderError(PaymentsError):
    """ЮKassa ответила отказом или телом не в формате контракта.

    Это осмысленный отказ сервиса (неверный ключ, мусор в запросе): повторять
    его вслепую бессмысленно.
    """


@dataclass(frozen=True)
class PaymentsSettings:
    """Конфиг магазина, собранный из окружения.

    Отдельная функция сборки — по образцу `MailSettings.from_env`: тогда и
    кабинет, и тест собирают клиента одним и тем же путём, и «какие переменные
    обязательны» видно в одном месте.
    """

    shop_id: str
    secret_key: str
    public_base: str
    api_base: str = DEFAULT_API_BASE
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PaymentsSettings:
        """Собрать из окружения. Пустая обязательная переменная — отказ.

        В тексте отказа называется **имя** переменной, но не её значение:
        секрет не печатается нигде — репозиторий публичный.
        """
        source = os.environ if env is None else env
        shop_id = source.get(SHOP_ID_ENV, "").strip()
        if not shop_id:
            raise PaymentsConfigError(f"не задан магазин: переменная окружения {SHOP_ID_ENV}")
        secret_key = source.get(SECRET_KEY_ENV, "").strip()
        if not secret_key:
            raise PaymentsConfigError(f"не задан секрет: переменная окружения {SECRET_KEY_ENV}")
        public_base = source.get(PUBLIC_BASE_ENV, "").strip()
        if not public_base:
            raise PaymentsConfigError(
                f"не задан публичный адрес кабинета: переменная окружения {PUBLIC_BASE_ENV}"
            )
        api_base = source.get(API_BASE_ENV, "").strip() or DEFAULT_API_BASE
        return cls(
            shop_id=shop_id,
            secret_key=secret_key,
            public_base=public_base,
            api_base=api_base,
        )


def return_url(public_base: str, order_id: str) -> str:
    """Куда ЮKassa вернёт человека — на экран возврата кабинета с номером заказа.

    Номер заказа едет в query: экран возврата показывает состояние **этого**
    заказа, и без него показать нечего. Адрес абсолютный — уведомлению ЮKassa
    относительный путь не годится.
    """
    base = public_base.rstrip("/")
    return f"{base}{RETURN_PATH}?order_id={quote(order_id)}"


@dataclass(frozen=True)
class Payment:
    """Платёж, каким его отдаёт ЮKassa.

    Поля читаются через `.get`: ответ приходит извне этого репозитория, и
    пропавшее поле не должно ронять разбор. `amount_rub` — целые рубли, как их
    считает заказ: суммы в копейках в проекте нет, а сравнивать «сколько
    просили» с «сколько пришло» надо в одной единице.

    `confirmation_url` — адрес страницы оплаты, куда кабинет уводит человека;
    `None` означает, что страница не выдана, и уводить некуда.
    """

    id: str
    status: str
    amount_rub: int
    confirmation_url: str | None = None
    paid: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Payment:
        amount = raw.get("amount")
        amount_rub = _amount_rub(amount.get("value")) if isinstance(amount, dict) else 0
        confirmation = raw.get("confirmation")
        confirmation_url = (
            confirmation.get("confirmation_url") if isinstance(confirmation, dict) else None
        )
        return cls(
            id=str(raw.get("id", "")),
            status=str(raw.get("status", "")),
            amount_rub=amount_rub,
            confirmation_url=str(confirmation_url) if confirmation_url else None,
            paid=bool(raw.get("paid", False)),
        )


def _amount_rub(value: Any) -> int:
    """Сумма из строки ЮKassa (`"140.00"`) в целые рубли.

    Непонятное значение — ноль: сумма, которую не удалось прочитать, не должна
    выглядеть как оплата. Сравнение с суммой заказа всё равно отсеет расхождение.
    """
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _amount_value(amount_rub: int) -> str:
    """Целые рубли в строку ЮKassa (`140` → `"140.00"`).

    Формат провайдера, а не украшение: ЮKassa ждёт строку с двумя знаками после
    точки. Своё форматирование здесь — потому что сумма приходит числом.
    """
    return f"{int(amount_rub)}.00"


class Payments(Protocol):
    """Шов, ради которого модуль разделён: экран зовёт `create_payment`, а уходит
    ли запрос к ЮKassa — дело подставленного клиента.

    Форма — ровно то, что нужно роуту: создать платёж, прочитать его и собрать
    адрес возврата. Вебхук зовёт `get_payment`; экран оплаты — `create_payment`.
    """

    def create_payment(
        self, *, order_id: str, amount_rub: int, description: str, return_url: str
    ) -> Payment: ...

    def get_payment(self, payment_id: str) -> Payment: ...

    def return_url(self, order_id: str) -> str: ...


class PaymentClient:
    """Клиент ЮKassa: создание платежа и чтение его состояния.

    Один экземпляр держит один `httpx.Client` и переиспользует соединения.
    Создаётся один раз на процесс, а не на запрос пользователя. Закрывается
    через `close()` на остановке приложения.
    """

    def __init__(
        self,
        *,
        shop_id: str,
        secret_key: str,
        public_base: str = "",
        api_base: str = DEFAULT_API_BASE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        # Аргументы важнее окружения: так тест подставляет мок и не зависит от
        # переменных процесса. Порядок «аргумент → окружение → умолчание»
        # повторяет то, как настраивается развёртывание.
        self.shop_id = shop_id
        self._secret_key = secret_key
        self.public_base = public_base
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    @classmethod
    def from_settings(
        cls, settings: PaymentsSettings, *, client: httpx.Client | None = None
    ) -> PaymentClient:
        """Собрать клиента по разобранному конфигу. Транспорт — шов теста."""
        return cls(
            shop_id=settings.shop_id,
            secret_key=settings.secret_key,
            public_base=settings.public_base,
            api_base=settings.api_base,
            timeout=settings.timeout,
            client=client,
        )

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, client: httpx.Client | None = None
    ) -> PaymentClient:
        return cls.from_settings(PaymentsSettings.from_env(env), client=client)

    def close(self) -> None:
        self._client.close()

    def return_url(self, order_id: str) -> str:
        """Адрес возврата для этого кабинета. Собирается из публичного адреса."""
        return return_url(self.public_base, order_id)

    # ---------- запись ----------

    def create_payment(
        self, *, order_id: str, amount_rub: int, description: str, return_url: str
    ) -> Payment:
        """Создать платёж на сумму **заказа**.

        Сумма приходит аргументом и не считается здесь: её посчитал `pricing` и
        записал заказ, и платёж обязан совпасть с записью. Взятая из запроса
        браузера сумма продала бы подписку за рубль.

        Ключ идемпотентности — `order_id`, и он же уезжает в `metadata`: один
        заказ — один платёж, сколько бы раз экран ни перезагрузили. Провайдер
        отдаёт одну и ту же страницу оплаты на повтор с тем же ключом.

        Блока `receipt` здесь нет намеренно (см. модульный docstring): чеком
        занимается страница оплаты ЮKassa, а адрес покупателя кабинет не знает.
        """
        body: dict[str, Any] = {
            "amount": {"value": _amount_value(amount_rub), "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description,
            "metadata": {"order_id": order_id},
        }
        return Payment.from_api(
            self._request(
                "POST",
                "/payments",
                json=body,
                headers={"Idempotence-Key": order_id},
            )
        )

    # ---------- чтение ----------

    def get_payment(self, payment_id: str) -> Payment:
        """Прочитать платёж у провайдера.

        Это и есть настоящая проверка «оплачено»: тело уведомления не
        авторитетно (подписи у него нет, issue #61), а ответ ЮKassa — да.
        Разбор отказов общий с записью (`_request`), поэтому «провайдер
        недоступен» и «провайдер отверг» не смешиваются.
        """
        return Payment.from_api(self._request("GET", f"/payments/{quote(payment_id)}"))

    # ---------- транспорт ----------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Один запрос: сеть, статус, разбор ответа.

        Аутентификация — HTTP Basic с парой «магазин + секрет»: так её ждёт
        ЮKassa, и заголовок собирает транспорт `httpx` из `auth`, а не вручную,
        чтобы секрет не проходил через наши строки.

        Адрес склеивается из `api_base` целиком, а не берётся относительным:
        подставной транспорт в тесте не обязан знать базовый адрес, и запрос
        уходит на тот же путь, что и в бою.
        """
        headers = dict(kwargs.pop("headers", {}))
        try:
            response = self._client.request(
                method,
                f"{self.api_base}{path}",
                auth=(self.shop_id, self._secret_key),
                headers=headers,
                **kwargs,
            )
        except _NETWORK_ERRORS as exc:
            raise PaymentsTransportError("ЮKassa недоступна") from exc

        if response.status_code >= 400:
            # Ключ и тело запроса в текст не попадают: репозиторий публичный.
            raise PaymentsProviderError(f"ЮKassa ответила статусом {response.status_code}")
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentsProviderError("ЮKassa вернула не-JSON") from exc
        if not isinstance(payload, dict):
            raise PaymentsProviderError("ЮKassa вернула не объект")
        return payload


class UnconfiguredPayments:
    """Магазин не настроен: любой вызов — отказ.

    Умолчание `build_app` (тесты и стенд) и запасной вариант `build_payments`,
    когда переменных ЮKassa нет. Запуск без магазина не должен ронять кабинет
    целиком — вход и подписки работают и без оплаты, — но и изображать оплату
    заглушка не имеет права: она **отказывает**, а не «успешно создаёт платёж».
    Экран оплаты показывает это отдельным состоянием, а не выдуманной страницей.
    """

    def create_payment(
        self, *, order_id: str, amount_rub: int, description: str, return_url: str
    ) -> Payment:
        raise PaymentsConfigError(
            f"магазин ЮKassa не настроен: задайте {SHOP_ID_ENV} и {SECRET_KEY_ENV}"
        )

    def get_payment(self, payment_id: str) -> Payment:
        raise PaymentsConfigError(
            f"магазин ЮKassa не настроен: задайте {SHOP_ID_ENV} и {SECRET_KEY_ENV}"
        )

    def return_url(self, order_id: str) -> str:
        raise PaymentsConfigError(f"магазин ЮKassa не настроен: задайте {PUBLIC_BASE_ENV}")


def build_payments(env: Mapping[str, str] | None = None) -> Payments:
    """Клиент ЮKassa из окружения или отказывающая заглушка.

    Неполный конфиг магазина **не** роняет запуск кабинета: оплата — одна из
    возможностей, и её отсутствие не должно закрывать вход и подписки. Но это и
    не тихий откат на выдуманный платёж: заглушка отказывает на использовании, и
    человек на экране оплаты видит, что оплата недоступна, а оператор — что
    настройка неполна (имя переменной в тексте отказа).
    """
    try:
        return PaymentClient.from_env(env)
    except PaymentsConfigError:
        return UnconfiguredPayments()
