"""Клиент ЮKassa: сумма из заказа, чек не передан, ключ не в логах (шаг 5, #61).

Модуль — про деньги, и три его правила проверяются здесь прямо:

- **сумма приходит аргументом**, а не считается и не читается откуда-то: её
  посчитал `pricing` и записал заказ, и платёж обязан совпасть с записью;
- **блока `receipt` в запросе нет вовсе** — решение владельца (спека §3а): чеком
  занимается страница оплаты ЮKassa, а адрес покупателя кабинет не знает. В боте
  в чек подставляются константы сервиса (`handlers/payment_handler.py:137`) — это
  #42, и повторять это нельзя;
- **ни ключа, ни секрета в текстах отказов**: репозиторий публичный.

Сети нет: транспорт подставлен (`httpx.MockTransport`), как у `ApiTransport` в
тестах почты. Наружу при прогоне не уходит ничего.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from cabinet.payments import (
    DEFAULT_API_BASE,
    PUBLIC_BASE_ENV,
    SECRET_KEY_ENV,
    SHOP_ID_ENV,
    Payment,
    PaymentClient,
    PaymentsConfigError,
    PaymentsProviderError,
    PaymentsSettings,
    PaymentsTransportError,
    UnconfiguredPayments,
    build_payments,
    return_url,
)

SHOP_ID = "shop-111"
SECRET = "test-secret-key"
PUBLIC_BASE = "https://cabinet.example.com"
ORDER_ID = "ord-1"


def _settings(**overrides: str) -> dict[str, str]:
    env = {
        SHOP_ID_ENV: SHOP_ID,
        SECRET_KEY_ENV: SECRET,
        PUBLIC_BASE_ENV: PUBLIC_BASE,
    }
    env.update(overrides)
    return env


def _client(handler) -> PaymentClient:
    """Клиент с подставным транспортом. Сети нет — как у почтового теста."""
    return PaymentClient(
        shop_id=SHOP_ID,
        secret_key=SECRET,
        public_base=PUBLIC_BASE,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _payment_body(*, amount: str = "140.00", status: str = "pending") -> dict:
    return {
        "id": "pay-1",
        "status": status,
        "paid": status == "succeeded",
        "amount": {"value": amount, "currency": "RUB"},
        "confirmation": {"type": "redirect", "confirmation_url": "https://pay.example.com/c"},
    }


# --- конфиг из окружения --------------------------------------------------


def test_settings_from_env_reads_all_three_values() -> None:
    """Магазин, секрет и публичный адрес — из окружения, а не из кода."""
    settings = PaymentsSettings.from_env(_settings())

    assert settings.shop_id == SHOP_ID
    assert settings.secret_key == SECRET
    assert settings.public_base == PUBLIC_BASE
    assert settings.api_base == DEFAULT_API_BASE


def test_missing_shop_refuses_to_configure() -> None:
    """Без магазина конфиг неполон — отказ, а не «работаем без него»."""
    with pytest.raises(PaymentsConfigError) as exc:
        PaymentsSettings.from_env(_settings(**{SHOP_ID_ENV: ""}))

    assert SHOP_ID_ENV in str(exc.value)


def test_missing_secret_refusal_does_not_echo_other_secret() -> None:
    """В отказе назван только **имя** переменной, а не значение.

    Секрет живёт в окружении, и напечатать его в тексте отказа — то же, что
    положить в публичный лог. Здесь значение подставлено и проверяется, что в
    сообщении его нет.
    """
    with pytest.raises(PaymentsConfigError) as exc:
        PaymentsSettings.from_env(_settings(**{SECRET_KEY_ENV: ""}))

    assert SECRET_KEY_ENV in str(exc.value)
    assert SECRET not in str(exc.value)


def test_missing_public_base_refuses_to_configure() -> None:
    """Без публичного адреса не собрать `return_url` — это тоже отказ."""
    with pytest.raises(PaymentsConfigError) as exc:
        PaymentsSettings.from_env(_settings(**{PUBLIC_BASE_ENV: ""}))

    assert PUBLIC_BASE_ENV in str(exc.value)


# --- адрес возврата -------------------------------------------------------


def test_return_url_points_back_to_the_cabinet_with_the_order() -> None:
    """Адрес возврата ведёт на `/buy/done?order_id=…` того же кабинета."""
    client = _client(lambda request: httpx.Response(200, json={}))

    assert client.return_url(ORDER_ID) == f"{PUBLIC_BASE}/buy/done?order_id={ORDER_ID}"


def test_return_url_does_not_double_the_slash() -> None:
    """Хвостовой слэш в адресе кабинета не даёт `//buy/done`."""
    assert return_url(f"{PUBLIC_BASE}/", ORDER_ID) == f"{PUBLIC_BASE}/buy/done?order_id={ORDER_ID}"


# --- создание платежа -----------------------------------------------------


def test_create_payment_sends_amount_from_the_order() -> None:
    """Сумма берётся из аргумента и уходит как есть — это сумма заказа.

    Платёж не считает денег: `amount_rub` пришёл из заказа, и запрос обязан
    нести именно его. Взятая из браузера сумма продала бы подписку за рубль.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_payment_body(amount="140.00"))

    payment = _client(handler).create_payment(
        order_id=ORDER_ID,
        amount_rub=140,
        description="Подписка VPN",
        return_url=f"{PUBLIC_BASE}/buy/done?order_id={ORDER_ID}",
    )

    assert seen["body"]["amount"] == {"value": "140.00", "currency": "RUB"}
    assert payment.amount_rub == 140
    assert payment.confirmation_url == "https://pay.example.com/c"


def test_create_payment_carries_idempotence_key_and_metadata() -> None:
    """Ключ идемпотентности — `order_id`, и он же в `metadata`.

    Один заказ — один платёж, сколько бы раз экран ни перезагрузили: ЮKassa
    отдаёт ту же страницу оплаты на повтор с тем же ключом.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_payment_body())

    _client(handler).create_payment(
        order_id=ORDER_ID,
        amount_rub=140,
        description="Подписка VPN",
        return_url=f"{PUBLIC_BASE}/buy/done?order_id={ORDER_ID}",
    )

    assert seen["headers"]["Idempotence-Key"] == ORDER_ID
    assert seen["body"]["metadata"]["order_id"] == ORDER_ID


def test_create_payment_uses_shop_basic_auth() -> None:
    """Аутентификация — пара «магазин + секрет» в HTTP Basic, как её ждёт ЮKassa."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_payment_body())

    _client(handler).create_payment(
        order_id=ORDER_ID,
        amount_rub=140,
        description="Подписка VPN",
        return_url=f"{PUBLIC_BASE}/buy/done?order_id={ORDER_ID}",
    )

    expected = base64.b64encode(f"{SHOP_ID}:{SECRET}".encode()).decode()
    assert seen["auth"] == f"Basic {expected}"


def test_create_payment_never_sends_a_receipt() -> None:
    """Блока `receipt` в запросе нет вовсе — ни `customer`, ни позиций.

    Чеком занимается страница оплаты ЮKassa (спека §3а). Подстановка в чек
    чужого адреса — это #42, и повторять её нельзя: чек с чужим адресом хуже
    отсутствующего, потому что выглядит правильным.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_payment_body())

    _client(handler).create_payment(
        order_id=ORDER_ID,
        amount_rub=140,
        description="Подписка VPN",
        return_url=f"{PUBLIC_BASE}/buy/done?order_id={ORDER_ID}",
    )

    assert "receipt" not in seen["body"]
    assert "customer" not in json.dumps(seen["body"])


def test_create_payment_redirects_to_the_provided_return_url() -> None:
    """В `confirmation.return_url` уходит адрес возврата, названный вызывающим."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_payment_body())

    _client(handler).create_payment(
        order_id=ORDER_ID,
        amount_rub=140,
        description="Подписка VPN",
        return_url=f"{PUBLIC_BASE}/buy/done?order_id={ORDER_ID}",
    )

    confirmation = seen["body"]["confirmation"]
    assert confirmation["type"] == "redirect"
    assert confirmation["return_url"] == f"{PUBLIC_BASE}/buy/done?order_id={ORDER_ID}"


# --- чтение платежа (его зовёт вебхук) ------------------------------------


def test_get_payment_reads_status_and_amount() -> None:
    """Перезапрос платежа отдаёт статус и сумму — по ним вебхук и решает."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json=_payment_body(amount="1913.00", status="succeeded"))

    payment = _client(handler).get_payment("pay-1")

    assert seen["method"] == "GET"
    assert seen["path"].endswith("/payments/pay-1")
    assert payment.status == "succeeded"
    assert payment.amount_rub == 1913
    assert payment.paid is True


# --- отказы: транспорт и провайдер разведены ------------------------------


def test_provider_error_is_a_refusal_not_a_network_error() -> None:
    """401 от ЮKassa — осмысленный отказ провайдера, а не недоступность сети."""
    client = _client(lambda request: httpx.Response(401, json={"error": "unauthorized"}))

    with pytest.raises(PaymentsProviderError):
        client.get_payment("pay-1")


def test_network_failure_is_its_own_error() -> None:
    """Сеть/таймаут — `PaymentsTransportError`: платежа могло и не быть, повтор уместен."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи")

    client = _client(handler)

    with pytest.raises(PaymentsTransportError):
        client.get_payment("pay-1")


def test_non_json_success_body_is_a_provider_error() -> None:
    """200 с не-JSON — расхождение контракта; это отказ, не сеть."""
    client = _client(lambda request: httpx.Response(200, text="<html>не json</html>"))

    with pytest.raises(PaymentsProviderError):
        client.get_payment("pay-1")


# --- заглушка и сборка из окружения ---------------------------------------


def test_unconfigured_payments_refuses_instead_of_pretending() -> None:
    """Заглушка **отказывает**, а не изображает успешный платёж."""
    payments = UnconfiguredPayments()

    with pytest.raises(PaymentsConfigError):
        payments.create_payment(order_id=ORDER_ID, amount_rub=1, description="x", return_url="u")
    with pytest.raises(PaymentsConfigError):
        payments.get_payment("pay-1")


def test_build_payments_without_environment_returns_stub() -> None:
    """Пустое окружение — отказывающая заглушка, а не падение на старте."""
    payments = build_payments({})

    assert isinstance(payments, UnconfiguredPayments)
    with pytest.raises(PaymentsConfigError):
        payments.get_payment("pay-1")


def test_build_payments_with_environment_returns_client() -> None:
    """Полное окружение — настоящий клиент с адресом API по умолчанию."""
    payments = build_payments(_settings())

    assert isinstance(payments, PaymentClient)
    assert payments.shop_id == SHOP_ID
    assert payments.return_url(ORDER_ID) == f"{PUBLIC_BASE}/buy/done?order_id={ORDER_ID}"


def test_payment_dataclass_parses_amount_with_kopeks() -> None:
    """Сумма из строки провайдера (`"140.00"`) читается целыми рублями."""
    payment = Payment.from_api(_payment_body(amount="140.00"))

    assert payment.amount_rub == 140
