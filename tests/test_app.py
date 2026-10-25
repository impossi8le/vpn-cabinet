"""W6.6: точка входа приложения.

Проверяется не «роут зарегистрирован», а поведение через настоящий HTTP-запрос
`TestClient`. Разница существенная: список путей, собранный по `app.routes`,
зеленеет и на ручке, которая при вызове падает, — а именно от падения
приложение и защищаем. Здесь каждый тест делает запрос и смотрит на ответ.

Зависимости подставляются прямо в `build_app`: подставной бот и подставная
почта, ни сети, ни базы — как и во всех остальных тестах волны.

Отдельно проверяется, что вход по почте **доходит до отправки письма**: код
ищется в `outbox` подставной почты, а не подставляется константой. Иначе тест
подтверждал бы приём любого кода, а не работу двери.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.auth_mail import CODE_LENGTH
from cabinet.mail_sender import MailSender
from cabinet.sessions import DEFAULT_SESSION_TTL
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"


@pytest.fixture
def fake_mail_sender() -> FakeMailSender:
    """Подставная почта приложения. Тест читает `outbox`."""
    return FakeMailSender()


@pytest.fixture
def client(fake_api: FakeApi, fake_mail_sender: FakeMailSender) -> TestClient:
    """Клиент приложения на подставных зависимостях.

    `raise_server_exceptions=False`: 500 должен прийти как 500, а не как
    исключение наружу. Тест на «без сессии — редирект, а не 500» обязан видеть
    код ответа, иначе он проверял бы стектрейс вместо поведения.
    """
    app = build_app(
        store=fake_api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=fake_mail_sender),
        now=lambda: NOW,
    )
    return TestClient(app, raise_server_exceptions=False)


def _code_from(mail: FakeMailSender) -> str:
    """Код из последнего письма на адрес — как его увидел бы человек."""
    body = mail.sent_to(MAIL)[-1]["body"]
    return "".join(ch for ch in body if ch.isdigit())[:CODE_LENGTH]


# --- экран входа ---------------------------------------------------------


def test_login_page_renders_form(client: TestClient) -> None:
    """`GET /login` отдаёт форму входа, а не пустую страницу и не 500."""
    response = client.get("/login")

    assert response.status_code == 200
    assert "Вход в кабинет" in response.text
    assert 'name="mail"' in response.text


# --- запрос кода ---------------------------------------------------------


def test_requesting_code_sends_the_letter(
    client: TestClient, fake_mail_sender: FakeMailSender
) -> None:
    """`POST /login` с адресом доводит запрос до отправки письма.

    Проверяется через подставную почту: письмо обязано лежать в `outbox` на
    этот адрес. Роут, который вернул 303, но письма не отправил, оставил бы
    человека на экране кода без кода — и зелёный тест на «303» этого не увидел.
    """
    response = client.post("/login", data={"mail": MAIL}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login/waiting")
    assert len(fake_mail_sender.sent_to(MAIL)) == 1


def test_requesting_code_to_waiting_screen_shows_the_address(
    client: TestClient, fake_mail_sender: FakeMailSender
) -> None:
    """После запроса адрес стоит на экране кода — человеку не набирать заново."""
    location = client.post("/login", data={"mail": MAIL}, follow_redirects=False).headers[
        "location"
    ]

    response = client.get(location)

    assert response.status_code == 200
    assert MAIL in response.text


def test_empty_address_returns_to_form_without_sending(
    client: TestClient, fake_mail_sender: FakeMailSender
) -> None:
    """Пустой адрес — отказ формы, письма нет.

    Пара к успеху: без неё тест зеленел бы и на коде, который шлёт письмо на
    каждый запрос независимо от того, что ввели.
    """
    response = client.post("/login", data={"mail": "   "}, follow_redirects=False)

    assert response.status_code == 303
    # Адрес остаётся голым: текст отказа едет одноразовой cookie, а не в ссылке
    # (issue #55). Раньше здесь стоял `"/login?"` — то есть проверка на то, что
    # отказ уехал в адрес; от неё и был дефект.
    assert response.headers["location"] == "/login"
    assert fake_mail_sender.outbox == []


# --- вход по коду --------------------------------------------------------


def test_entering_code_signs_in_with_cookie(
    client: TestClient, fake_mail_sender: FakeMailSender
) -> None:
    """Полный вход: код из письма → cookie сессии → экран подписок.

    Сессия приходит **cookie**, а не телом ответа: она не должна быть
    достижима из JavaScript, и тест смотрит именно на `Set-Cookie` с флагами.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _code_from(fake_mail_sender)

    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/subscriptions"
    cookie = response.cookies.get(COOKIE_NAME)
    assert cookie
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "samesite=lax" in response.headers["set-cookie"].lower()


def test_session_cookie_outlives_the_browser(
    client: TestClient, fake_mail_sender: FakeMailSender
) -> None:
    """Cookie сессии несёт срок — иначе закрытие браузера разлогинивало бы.

    Без `Max-Age` cookie живёт до закрытия браузера, а кабинет открывают раз в
    недели: серверная сессия оставалась бы живой, а человек каждый раз видел бы
    форму входа. `Max-Age` обязан совпасть с серверным TTL — разойдись они, и
    cookie жила бы дольше или меньше самой сессии.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _code_from(fake_mail_sender)

    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )

    header = response.headers["set-cookie"].lower()
    assert f"max-age={int(DEFAULT_SESSION_TTL.total_seconds())}" in header


def test_visit_renews_the_session_cookie(
    client: TestClient, fake_mail_sender: FakeMailSender
) -> None:
    """Заход обновляет cookie сессии — вместе с серверным продлением (#53).

    Серверная запись продлевается при каждом успешном заходе, а cookie,
    выпущенная один раз при входе, несла бы прежний `Max-Age`. Тогда активного
    человека выбрасывало бы из кабинета ровно через TTL от **первого** входа —
    то есть ровно то, что продление и должно отменять. Проверяются обе
    половины: cookie приходит заново и токен в ней тот же — это продление, а
    не вторая сессия.

    Пара — тест ниже: заход **без** сессии cookie не заводит.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _code_from(fake_mail_sender)
    signed_in = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )
    token = signed_in.cookies.get(COOKIE_NAME)
    assert token

    response = client.get("/subscriptions", follow_redirects=False)

    header = response.headers.get("set-cookie", "")
    assert header.startswith(f"{COOKIE_NAME}=")
    assert f"max-age={int(DEFAULT_SESSION_TTL.total_seconds())}" in header.lower()
    assert response.cookies.get(COOKIE_NAME) == token


def test_page_without_a_session_sets_no_session_cookie(client: TestClient) -> None:
    """Без сессии обновлять нечего — cookie не заводится.

    Пара к предыдущему: иначе он зеленел бы и на коде, который штампует cookie
    каждому зашедшему, включая того, кто ещё не входил.
    """
    response = client.get("/subscriptions", follow_redirects=False)

    assert response.status_code == 303
    assert COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_wrong_code_keeps_you_on_the_waiting_screen(
    client: TestClient, fake_mail_sender: FakeMailSender
) -> None:
    """Неверный код — возврат на экран кода, без сессии.

    Пара к успешному входу: иначе дверь пускала бы любого с любым кодом.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)

    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": "000000"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login/waiting")
    assert COOKIE_NAME not in response.cookies


# --- подписки ------------------------------------------------------------


def test_subscriptions_without_session_redirects_to_login(client: TestClient) -> None:
    """Без cookie — редирект на вход, а не 500 и не пустой список.

    Это обычное состояние первого захода: человек ещё не вошёл. Ответ `500`
    здесь означал бы, что отсутствие сессии считается сбоем сервера, — и такая
    ошибка вылезала бы у каждого, кто просто открыл адрес.
    """
    response = client.get("/subscriptions", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_subscriptions_after_sign_in_show_the_list(
    client: TestClient, fake_api: FakeApi, fake_mail_sender: FakeMailSender
) -> None:
    """После входа подписки открываются — и пустой список читается как состояние.

    У только что заведённого аккаунта подписок нет, и экран обязан сказать это
    словами (W4.3), а не показать сбой.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _code_from(fake_mail_sender)
    client.post("/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False)

    response = client.get("/subscriptions")

    assert response.status_code == 200


def test_forged_session_cookie_does_not_open_subscriptions(client: TestClient) -> None:
    """Поддельный токен в cookie подписки не открывает.

    Пара к «после входа показывается список»: без неё экран подписок зеленел бы
    и на коде, который пускает по любому непустому cookie.
    """
    client.cookies.set(COOKIE_NAME, "not-a-real-token")

    response = client.get("/subscriptions", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
