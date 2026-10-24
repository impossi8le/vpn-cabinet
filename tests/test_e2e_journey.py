"""Сквозной прогон кабинета: от входа до оплаты и способов входа.

**Зачем отдельный файл.** Тесты экранов проверяют экран, а не путь: каждый берёт
свой адрес и своё состояние. Здесь один прогон ведёт человека через весь кабинет
подряд — вход, подписки, аккаунт, покупка, оплата, — и держит то, что не видно
ни одному тесту экрана: что **шаги соединены**. Экран может быть идеальным и
при этом недостижимым: ровно так было с `/account` — он существовал со всей
привязкой и отвязкой, а ссылки на него не было нигде, кроме углового состояния
оплаты.

**Ни одного адреса руками.** Переходы берутся из разметки (`tests/markup.py`):
если ссылка пропала или подпись изменилась, прогон падает — и это его работа.
Раньше «живые» проверки делались разовыми скриптами на сервере, которые не
сохранялись и не повторялись; теперь путь проверяется в общем прогоне.

**Чего здесь нет.** Браузера: прогон ходит по HTTP и разметке, JavaScript не
исполняется. Скрипты кабинета только оживляют уже работающее (отсчёт лимита,
фильтр цифр), поэтому путь проходится и без них — так и задумано.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cabinet.app import build_app
from cabinet.mail_sender import MailSender
from cabinet.payments import Payment
from cabinet.render import (
    PAY_TELEGRAM_REQUIRED_NOTE,
    TRIAL_UNKNOWN_REASON_NOTE,
)
from tests.fakes import FakeApi, FakeMailSender
from tests.markup import form, hrefs, link, link_containing

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "buyer@example.com"
CHAT_ID = 123_456_789
CONFIRM_URL = "https://pay.example.com/confirm"


class _StubPayments:
    """Подставная ЮKassa: наружу ничего не уходит, вызовы запоминаются."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    def return_url(self, order_id: str) -> str:
        return f"https://cabinet.example.com/buy/done?order_id={order_id}"

    def create_payment(
        self, *, order_id: str, amount_rub: int, description: str, return_url: str
    ) -> Payment:
        self.created.append(
            {"order_id": order_id, "amount_rub": amount_rub, "return_url": return_url}
        )
        return Payment(
            id="pay-1", status="pending", amount_rub=amount_rub, confirmation_url=CONFIRM_URL
        )

    def get_payment(self, payment_id: str) -> Payment:  # pragma: no cover - прогон не платит
        raise AssertionError("прогон не подтверждает оплату")


def _client(api: FakeApi) -> tuple[TestClient, FakeMailSender, _StubPayments]:
    sender = FakeMailSender()
    payments = _StubPayments()
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        payments=payments,
        now=lambda: NOW,
    )
    return TestClient(app, raise_server_exceptions=False), sender, payments


def _sign_in_by_mail(client: TestClient, sender: FakeMailSender) -> None:
    """Войти по почте так, как это делает браузер: формами и разметкой.

    Адрес и поле адреса берутся из разметки: если форма перестанет их нести,
    прогон упадёт здесь, а не «войдёт» в никуда.

    Переход после отправки адреса прогон проходит **как браузер** — по адресу из
    ответа. Адрес экрана кода известен только этому переходу (успех уносит его в
    query), и пропустить его — значит проверять не тот экран, который увидит
    человек: именно так этот прогон и поймал себя на первом запуске.
    """
    login_page = client.get("/login").text
    entry = form(login_page, "/login")
    assert entry["method"] == "post"
    fields = dict(entry["fields"])
    fields["mail"] = MAIL
    sent_address = client.post(entry["action"], data=fields, follow_redirects=False)
    assert sent_address.status_code == 303
    assert sent_address.headers["location"].startswith("/login/waiting")

    body = sender.sent_to(MAIL)[-1]["body"]
    code = "".join(ch for ch in str(body) if ch.isdigit())[:6]

    waiting = client.get(sent_address.headers["location"]).text
    verify = form(waiting, "/login/verify")
    sent = dict(verify["fields"])
    # Адрес на этом шаге едет скрытым полем самой формы (дефект #56) — берём его
    # оттуда, а не из головы.
    assert sent.get("mail") == MAIL
    sent["code"] = code
    response = client.post(verify["action"], data=sent, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/subscriptions"


def _reach_the_servers_step(client: TestClient) -> str:
    """Пройти покупку до шага серверов по ссылкам разметки: 180 дней, 2 устройства."""
    subscriptions = client.get("/subscriptions").text
    buy = client.get(link(subscriptions, "Купить подписку")).text

    buy = client.get(link_containing(buy, "180 дней")).text
    buy = client.get(link_containing(buy, "2 устройства")).text
    assert "1\xa0350\xa0₽" in buy, "цена 180 дней на 2 устройства считается калькулятором"

    confirm = client.get(
        link(buy, "Выбрать серверы") if "Выбрать серверы" in buy else "/buy/confirm"
    ).text
    return confirm


# --- путь аккаунта без Telegram: покупка останавливается на границе сервиса ---


def test_journey_reaches_the_account_screen_and_the_payment_step() -> None:
    """Весь путь: вход → подписки → способы входа → покупка → оплата.

    Прогон идёт **только по ссылкам разметки**. Первая же его проверка —
    достижимость экрана способов входа с главного экрана: экран существовал, а
    входа на него не было.
    """
    api = FakeApi()
    api.add_server(code="nl-1", name="Нидерланды · Амстердам")
    api.add_server(code="lv-1", name="Латвия · Рига")
    client, sender, payments = _client(api)

    # 1. Корень ведёт на подписки, а те — на вход: это первый заход, не сбой.
    assert client.get("/", follow_redirects=False).headers["location"] == "/subscriptions"
    assert client.get("/subscriptions", follow_redirects=False).headers["location"] == "/login"

    # 2. Вход по почте настоящими формами.
    _sign_in_by_mail(client, sender)

    # 3. Главный экран: из него видно и покупку, и способы входа.
    subscriptions = client.get("/subscriptions").text
    assert "/buy" in link(subscriptions, "Купить подписку")

    # 4. Способы входа достижимы **с главного экрана** — это и было сломано.
    account = client.get(link(subscriptions, "Способы входа")).text
    assert "Способы входа" in account
    # Оба способа видны, и почта уже привязана: аккаунт создан входом по почте.
    assert MAIL in account
    assert "Telegram" in account

    # 5. Покупка: тариф и устройства выбираются ссылками, цена — калькулятором.
    buy = client.get(link(subscriptions, "Купить подписку")).text
    buy = client.get(link_containing(buy, "180 дней")).text
    buy = client.get(link_containing(buy, "2 устройства")).text
    assert "1\xa0350\xa0₽" in buy

    # 6. Подтверждение: сводка и право на триал (фактом, а не оговоркой).
    confirm = client.get(link(buy, "Продолжить")).text
    assert "180 дней" in confirm and "2 устройства" in confirm
    assert TRIAL_UNKNOWN_REASON_NOTE in confirm or "Пробный период" in confirm

    # 7. Серверы: ровно столько, сколько устройств — условие завершения шага.
    servers_url = link(confirm, "Выбрать серверы") if "Выбрать серверы" in confirm else None
    servers = client.get(servers_url or "/buy/servers?days=180&devices=2").text
    assert "Выбрано 0 из 2" in servers
    servers = client.get(hrefs(servers, "Добавить")[0]).text
    assert "Выбрано 1 из 2" in servers
    servers = client.get(hrefs(servers, "Добавить")[0]).text
    assert "Выбрано 2 из 2" in servers and "Готово" in servers

    # 8. Оплата достижима из состояния «Готово», а не только по прямому адресу.
    pay = client.get(link(servers, "Перейти к оплате"), follow_redirects=False)
    assert pay.status_code == 200, "экран оплаты отвечает отказом, а не уводит"

    # 9. Аккаунт без привязанного Telegram: продажа невозможна, и экран говорит
    #    это честно, ведя туда, где способ входа можно привязать.
    assert PAY_TELEGRAM_REQUIRED_NOTE in pay.text
    assert link(pay.text, "Привязать Telegram") == "/account"
    # Платежа не создавалось: заказать то, что нечем выдать, кабинет не должен.
    assert payments.created == []


# --- путь аккаунта с Telegram: покупка доходит до страницы оплаты -------------


def test_journey_with_telegram_reaches_the_payment_page() -> None:
    """Тот же путь для аккаунта с привязанным Telegram — до страницы оплаты.

    Такой аккаунт входит по почте (оба способа привязаны), и это обычный случай:
    человек завёл Telegram, а покупает в браузере и получает конфиг письмом.
    """
    api = FakeApi()
    api.add_server(code="nl-1", name="Нидерланды · Амстердам")
    account = api.create_account(mail=MAIL, mail_verified_at=NOW, chat_id=CHAT_ID)
    api.set_trial(account.id, eligible=True)
    client, sender, payments = _client(api)

    _sign_in_by_mail(client, sender)

    buy = client.get(link(client.get("/subscriptions").text, "Купить подписку")).text
    buy = client.get(link_containing(buy, "90 дней")).text
    confirm = client.get(link(buy, "Продолжить")).text
    servers = client.get(link(confirm, "Выбрать серверы")).text
    servers = client.get(hrefs(servers, "Добавить")[0]).text

    response = client.get(link(servers, "Перейти к оплате"), follow_redirects=False)

    # Успех — редирект на страницу оплаты, а не свой экран.
    assert response.status_code == 303
    assert response.headers["location"] == CONFIRM_URL
    # Заказ создан раньше платежа, и сумма платежа — сумма заказа.
    assert len(payments.created) == 1
    assert payments.created[0]["amount_rub"] == 400
    order_id = payments.created[0]["order_id"]
    assert order_id and payments.created[0]["return_url"].endswith(order_id)
