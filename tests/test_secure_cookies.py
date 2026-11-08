"""W6.7: cookie кабинета ставит `Secure`, когда запрос пришёл по https.

Флаг решает, отдаст ли браузер cookie по открытому http. На живом домене
кабинет стоит за nginx, который снимает TLS и сообщает схему заголовком
`X-Forwarded-Proto`; приложение видит её как `request.url.scheme`. Без флага
токен сессии уходил бы и по http — а значит, перехватывался бы на первом же
запросе без шифрования.

Пара к нему — локальный запуск: по `http://127.0.0.1` cookie с `Secure` браузер
не отправил бы вовсе, и вход перестал бы работать. Поэтому флаг обязан
зависеть от схемы, а не стоять всегда.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.auth_mail import CODE_LENGTH
from cabinet.mail_sender import MailSender
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"


@pytest.fixture
def fake_mail_sender() -> FakeMailSender:
    return FakeMailSender()


def _client(fake_api: FakeApi, sender: FakeMailSender, *, base_url: str) -> TestClient:
    app = build_app(
        store=fake_api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        now=lambda: NOW,
    )
    return TestClient(app, base_url=base_url, raise_server_exceptions=False)


def _code_from(mail: FakeMailSender) -> str:
    body = mail.sent_to(MAIL)[-1]["body"]
    return "".join(ch for ch in body if ch.isdigit())[:CODE_LENGTH]


def _cookie(response, name: str):  # noqa: ANN001 - тип ответа TestClient
    """Cookie по имени из jar ответа: у неё виден разобранный флаг `secure`."""
    for cookie in response.cookies.jar:
        if cookie.name == name:
            return cookie
    return None


def _assert_secure(response, name: str) -> None:  # noqa: ANN001
    cookie = _cookie(response, name)
    assert cookie is not None, f"cookie {name} не пришла"
    assert cookie.secure is True, f"cookie {name} без Secure"


def _assert_not_secure(response, name: str) -> None:  # noqa: ANN001
    cookie = _cookie(response, name)
    assert cookie is not None, f"cookie {name} не пришла"
    assert cookie.secure is False, f"cookie {name} с Secure на http"


def test_session_cookie_is_secure_behind_tls(
    fake_api: FakeApi, fake_mail_sender: FakeMailSender
) -> None:
    """Вход по https выдаёт сессию с флагом `Secure`."""
    client = _client(fake_api, fake_mail_sender, base_url="https://testserver")
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _code_from(fake_mail_sender)

    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )

    header = response.headers["set-cookie"]
    assert header.startswith(f"{COOKIE_NAME}=")
    _assert_secure(response, COOKIE_NAME)
    assert "HttpOnly" in header


def test_session_cookie_has_no_secure_on_local_http(
    fake_api: FakeApi, fake_mail_sender: FakeMailSender
) -> None:
    """`python -m cabinet` по http обязан работать: без флага, иначе cookie не
    вернётся браузером и вход не состоится."""
    client = _client(fake_api, fake_mail_sender, base_url="http://testserver")
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _code_from(fake_mail_sender)

    response = client.post(
        "/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False
    )

    header = response.headers["set-cookie"]
    assert header.startswith(f"{COOKIE_NAME}=")
    _assert_not_secure(response, COOKIE_NAME)


def test_login_notice_cookie_is_secure_behind_tls(
    fake_api: FakeApi, fake_mail_sender: FakeMailSender
) -> None:
    """Одноразовый отказ формы — тоже по https с флагом: в нём адрес человека."""
    client = _client(fake_api, fake_mail_sender, base_url="https://testserver")

    response = client.post("/login", data={"mail": "  "}, follow_redirects=False)

    header = response.headers["set-cookie"]
    assert header.startswith("cabinet_login_notice=")
    _assert_secure(response, "cabinet_login_notice")


def test_visitor_cookie_is_secure_behind_tls(
    fake_api: FakeApi, fake_mail_sender: FakeMailSender
) -> None:
    """Cookie посещения — с той же схемой, что и сессия."""
    client = _client(fake_api, fake_mail_sender, base_url="https://testserver")

    response = client.get("/login")

    header = response.headers.get("set-cookie", "")
    assert "visitor=" in header
    _assert_secure(response, "cabinet_visitor")
