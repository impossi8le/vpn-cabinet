"""Продающий кабинет: пункты дока по порядку «эффект / трудозатраты» (#39).

Владелец темы — [док продающего кабинета](../docs/2026-10-17-selling-cabinet-design.md).
Порядок проверок здесь тот же, что в доке:

1. микрокопия под кнопками (§6 п. 1);
4. экономия **в рублях**, а не в процентах (§6 п. 4): «−10%» требует счёта;
5. выделенный рекомендуемый тариф — 180 дней (§6 п. 5);
6. честный блок доверия — только то, что можно подтвердить (§4, §7).

**Бесплатный доступ — отдельно и сверху** (§1, §6 п. 2) проверяется не кнопкой, а
путём: кнопка, ведущая к оплате, была бы обещанием бесплатного, которого нет.
Поэтому вторая половина файла — про то, что бесплатный путь действительно выдаёт
конфиг без денег, и что получить его нельзя, когда право не подтверждено.

Ни одного цвета: продающий слой не заводит второй семантики (§5).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cabinet.app import build_app
from cabinet.mail_sender import MailSender
from cabinet.payments import Payment
from cabinet.render import TRIAL_REASON_TEXTS
from tests.fakes import FakeApi, FakeMailSender
from tests.markup import hrefs, link, link_containing

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "buyer@example.com"
CHAT_ID = 123_456_789
CONFIRM_URL = "https://pay.example.com/confirm"
FREE_CTA = "Начать бесплатно"


class _StubPayments:
    """Подставная ЮKassa: `created` пуст, если кабинет денег не просил."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    def return_url(self, order_id: str) -> str:
        return f"https://cabinet.example.com/buy/done?order_id={order_id}"

    def create_payment(
        self, *, order_id: str, amount_rub: int, description: str, return_url: str
    ) -> Payment:
        self.created.append({"order_id": order_id, "amount_rub": amount_rub})
        return Payment(
            id="pay-1", status="pending", amount_rub=amount_rub, confirmation_url=CONFIRM_URL
        )

    def get_payment(self, payment_id: str) -> Payment:  # pragma: no cover - оплата не нужна
        raise AssertionError("этот прогон оплату не подтверждает")


def _signed_in(
    api: FakeApi, *, chat_id: int | None = CHAT_ID, eligible: bool | None = None
) -> tuple[TestClient, FakeMailSender, _StubPayments]:
    account = api.create_account(mail=MAIL, mail_verified_at=NOW, chat_id=chat_id)
    if eligible is not None:
        api.set_trial(
            account.id,
            eligible=eligible,
            reason=None if eligible else "trial_used_by_chat_id",
        )
    api.add_server(code="nl-1", name="Нидерланды · Амстердам")
    sender = FakeMailSender()
    payments = _StubPayments()
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        payments=payments,
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("cabinet_session", app.state.sessions.sign_in(account.id).token)
    return client, sender, payments


# --- §6 п. 1: микрокопия под кнопками ---------------------------------------


def test_buy_screen_has_microcopy_under_the_main_button() -> None:
    """Под главной кнопкой — строка, снимающая трение.

    Самая дешёвая правка из списка дока: одна строка, а человек перестаёт
    бояться, что нажатие сразу спишет деньги или попросит карту.
    """
    client, _sender, _payments = _signed_in(FakeApi())

    html = client.get("/buy").text

    assert link(html, "Продолжить")
    assert "Без карты" in html


def test_servers_screen_has_microcopy_under_the_payment_button() -> None:
    """Под кнопкой оплаты — то, что человек хочет знать о платеже.

    Два честных факта: чем платим и что автопродления нет. Оба правдивы и оба
    снимают страх, который в этом месте сильнее всего.
    """
    client, _sender, _payments = _signed_in(FakeApi())

    html = client.get("/buy/servers?days=30&devices=1&pick=nl-1").text

    assert link(html, "Перейти к оплате")
    assert "ЮKassa" in html
    assert "Автопродления нет" in html


# --- §6 п. 4: экономия в рублях, а не в процентах ---------------------------


def test_device_options_show_savings_in_rubles() -> None:
    """Комплект описан экономией в рублях: «−10%» требует счёта, «28 ₽» — нет."""
    client, _sender, _payments = _signed_in(FakeApi())

    html = client.get("/buy").text

    assert "экономия 28" in html
    assert "экономия 63" in html


def test_device_options_do_not_make_the_person_do_arithmetic() -> None:
    """Процентов в описании комплекта нет — их место заняли рубли.

    Парная проверка к предыдущей: без неё тест зеленел бы и на строке, где
    рядом стоят и проценты, и рубли, то есть трение никуда не делось.
    """
    client, _sender, _payments = _signed_in(FakeApi())

    html = client.get("/buy").text

    assert "10%" not in html
    assert "15%" not in html


# --- §6 п. 5: выделенный тариф ----------------------------------------------


def test_recommended_tariff_is_marked_once() -> None:
    """Рекомендуемый тариф выделен, и он один: выбор из четырёх без подсказки — трение."""
    client, _sender, _payments = _signed_in(FakeApi())

    html = client.get("/buy").text

    assert html.count("is-best") == 1, "метка у одного варианта"
    assert "Рекомендуем" in html


def test_recommended_tariff_is_the_half_year_one() -> None:
    """Рекомендуем именно 180 дней — так записано в доке (§2)."""
    client, _sender, _payments = _signed_in(FakeApi())

    html = client.get("/buy").text

    assert "days=180" in link_containing(html, "Рекомендуем")


# --- §1 и §6 п. 2: бесплатный доступ отдельно и сверху ----------------------


def test_free_access_is_named_apart_from_the_paid_tariffs() -> None:
    """Бесплатный доступ назван отдельным блоком и стоит выше платных тарифов.

    Док §1: бесплатный доступ — основная точка входа у большинства продуктов, и
    он должен быть первым, что видит человек, а не одним из четырёх вариантов.
    """
    client, _sender, _payments = _signed_in(FakeApi())

    html = client.get("/buy").text

    assert html.index("30 дней бесплатно") < html.index("90 дней")
    assert link(html, FREE_CTA)


def test_free_access_leads_into_the_flow_with_the_free_pair() -> None:
    """Кнопка ведёт в тот же путь, но с бесплатной парой: 30 дней, одно устройство."""
    client, _sender, _payments = _signed_in(FakeApi())

    free = link(client.get("/buy").text, FREE_CTA)

    assert "days=30" in free and "devices=1" in free
    assert "free=1" in free, "признак бесплатного пути едет дальше"


def test_free_path_issues_configs_without_payment() -> None:
    """Бесплатный путь выдаёт конфиг, не трогая платёжный сервис и не беря денег."""
    api = FakeApi()
    client, sender, payments = _signed_in(api, eligible=True)

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1&free=1", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/buy/done?order_id=")
    assert payments.created == [], "бесплатно — значит без ЮKassa"
    order_id = response.headers["location"].split("order_id=")[1]
    order = api.get_order(order_id)
    assert order.status == "completed" and order.amount_rub == 0
    assert sender.sent_to(MAIL), "человек получает конфиг письмом, как после оплаты"


def test_free_path_refuses_when_the_right_is_not_confirmed() -> None:
    """Право не подтверждено — конфига нет, и причина названа человеческим текстом.

    Бесплатный доступ выдаётся один раз, и решает это бот. Проверка стоит на
    самом пути, а не только на экране: `free=1` в адресе не должен обходить право.
    """
    api = FakeApi()
    client, sender, payments = _signed_in(api, eligible=False)

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1&free=1")

    assert response.status_code == 200
    assert TRIAL_REASON_TEXTS["trial_used_by_chat_id"] in response.text
    assert payments.created == []
    assert sender.outbox == []
    assert [kw for name, kw in api.calls if name == "create_order"] == [], "заказ не создаётся"


def test_free_flag_cannot_buy_other_than_the_free_pair() -> None:
    """Признак `free` не даёт чужую пару: год на три устройства так не получить.

    Признак приходит из адреса, а адрес набирается руками. Если бы бесплатный
    путь брал `days`/`devices` из адреса, `free=1&days=360&devices=3` выдал бы
    самый дорогой комплект даром — и это была бы не «дыра на будущее», а готовая
    ссылка. Пара бесплатного пути берётся из констант, и тест держит именно это.
    """
    api = FakeApi()
    client, _sender, payments = _signed_in(api, eligible=True)

    response = client.get(
        "/buy/pay?days=360&devices=3&pick=nl-1&pick=nl-1&pick=nl-1&free=1",
        follow_redirects=False,
    )

    assert response.status_code == 303
    order_id = response.headers["location"].split("order_id=")[1]
    order = api.get_order(order_id)
    assert (order.days, order.devices, order.amount_rub) == (30, 1, 0)
    assert order.servers == ("nl-1",)
    assert payments.created == []


def test_free_path_is_walkable_by_links() -> None:
    """Бесплатный путь проходится по ссылкам разметки — от тарифов до выдачи.

    Проверка адреса вручную тут не годится: она не заметит, что признак `free`
    потерялся на одном из шагов, и человек с бесплатной кнопки попадёт на оплату.
    Прогон идёт по разметке, как браузер.
    """
    api = FakeApi()
    client, sender, payments = _signed_in(api, eligible=True)

    free = link(client.get("/buy").text, FREE_CTA)
    confirm = client.get(free).text
    assert "free=1" in link(confirm, "Выбрать серверы")

    servers = client.get(link(confirm, "Выбрать серверы")).text
    servers = client.get(hrefs(servers, "Добавить")[0]).text

    pay_url = link(servers, "Получить бесплатно")
    assert "free=1" in pay_url, "кнопка бесплатного пути не должна вести к оплате"
    response = client.get(pay_url, follow_redirects=False)

    assert response.status_code == 303
    assert payments.created == []
    assert sender.sent_to(MAIL), "конфиг ушёл письмом"


def test_free_access_is_refused_when_there_is_no_telegram() -> None:
    """Аккаунту без Telegram бесплатный доступ недоступен — как и покупка.

    Триал живёт на `chat_id`: без него выдавать нечего, и экран говорит это
    прямо, а не уводит к оплате.
    """
    api = FakeApi()
    # Право не подменяем: подставной бот сам отвечает `telegram_required`
    # аккаунту без `chat_id` — так же, как живой.
    client, sender, payments = _signed_in(api, chat_id=None)

    response = client.get("/buy/pay?days=30&devices=1&pick=nl-1&free=1")

    assert response.status_code == 200
    assert payments.created == []
    assert sender.outbox == []
    assert hrefs(response.text, "Привязать Telegram") == ["/account"]
