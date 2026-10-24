"""#55: тесты «от формы» — вход по почте, каким его отправляет браузер.

Почему отдельный файл и почему разбор разметки. Ранние тесты собирали тело
запроса **руками** (`data={"mail": MAIL, "code": code}`) и потому не видели
дефекта: форма на экране кода отправляла только код, адреса в ней не было, а
роут проверки читает адрес из формы — то есть всегда пустой. Любой верный код
отвергался, а форма повторной отправки не слала ничего и приводила на экран со
словами «Мы отправили код на .». Тест, собранный руками, это пропускает: он
кладёт в запрос поле, которого форма не шлёт.

Здесь поля берутся **из отрисованной разметки**: формы разбираются, из них
вытаскиваются поля, и отправляются ровно они. Такой тест краснеет на форме без
адреса и зеленеет на форме с ним — он и есть доказательство, что дефект был.

Путь проверяется целиком и всеми формами: адрес → код → подписки, отдельно
повторная отправка, и тем же способом — экран привязки почты.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.auth_mail import RESEND_INTERVAL_SECONDS
from cabinet.mail_sender import MailSender
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"
LINK_MAIL = "second@example.com"


class Clock:
    """Управляемые часы: пауза между отправками проверяется сдвигом, не сном."""

    def __init__(self, start: datetime) -> None:
        self._moment = start

    def __call__(self) -> datetime:
        return self._moment

    def advance(self, seconds: int = 0) -> None:
        self._moment += timedelta(seconds=seconds)


class _FormParser(HTMLParser):
    """Разбор форм разметки: адрес формы и её поля (включая скрытые).

    Это и есть «от формы»: тест не знает заранее, какие поля шлёт браузер, —
    он берёт их у самой разметки. Поле, которого форма не отдаёт, в запрос не
    попадёт, и тест это заметит.
    """

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self._current = {
                "action": values.get("action") or "",
                "method": (values.get("method") or "get").lower(),
                "fields": {},
            }
        elif tag == "input" and self._current is not None:
            name = values.get("name")
            if name:
                self._current["fields"][name] = values.get("value") or ""  # type: ignore[index]

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _forms(html: str) -> list[dict[str, object]]:
    parser = _FormParser()
    parser.feed(html)
    return parser.forms


def _form(html: str, action: str) -> dict[str, object]:
    """Форма по адресу действия. Нет такой — тест обязан упасть, а не угадать."""
    for form in _forms(html):
        if form["action"] == action:
            return form
    raise AssertionError(
        f"на странице нет формы с действием {action}: {[f['action'] for f in _forms(html)]}"
    )


def _code(sender: FakeMailSender, mail: str = MAIL) -> str:
    body = sender.sent_to(mail)[-1]["body"]
    return "".join(ch for ch in body if ch.isdigit())[:6]


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def sender() -> FakeMailSender:
    return FakeMailSender()


@pytest.fixture
def app(fake_api: FakeApi, sender: FakeMailSender, clock: Clock):
    return build_app(
        store=fake_api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        now=clock,
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# --- 1. весь путь входа по формам -----------------------------------------


def test_mail_login_from_the_forms_reaches_subscriptions(
    client: TestClient, sender: FakeMailSender
) -> None:
    """Адрес → код → подписки: каждая форма отправляется теми полями, что в ней есть.

    На экране кода адрес обязан ехать вместе с формой: без него сервер получает
    пустой адрес и отвергает **любой** код. Именно на этом владелец и получил
    «Код не подошёл» на верный код.
    """
    # Шаг 1 — форма входа. Поле адреса берём из разметки, значение — своё.
    mail_form = _form(client.get("/login").text, "/login")
    fields = dict(mail_form["fields"])  # type: ignore[arg-type]
    fields["mail"] = MAIL
    requested = client.post(mail_form["action"], data=fields, follow_redirects=False)

    assert requested.status_code == 303
    assert requested.headers["location"].startswith("/login/waiting")

    # Шаг 2 — экран кода. Здесь и проверяется, что адрес уехал с формой.
    waiting_html = client.get(requested.headers["location"]).text
    assert MAIL in waiting_html, "экран кода не назвал адрес — состояние потеряно"
    code_form = _form(waiting_html, "/login/verify")
    fields = dict(code_form["fields"])  # type: ignore[arg-type]
    fields["code"] = _code(sender)
    assert fields.get("mail") == MAIL, "форма кода не шлёт адрес — сервер отвергнет код"

    signed_in = client.post(code_form["action"], data=fields, follow_redirects=False)

    assert signed_in.status_code == 303, signed_in.text
    assert signed_in.headers["location"] == "/subscriptions"
    assert COOKIE_NAME in signed_in.cookies


def test_resend_from_the_form_keeps_the_address(
    client: TestClient, sender: FakeMailSender, clock: Clock
) -> None:
    """Повторная отправка сохраняет адрес и возвращает на экран кода с ним.

    Форма повтора обязана нести поле адреса: без него нажатие теряет адрес и
    приводит на экран, который не знает, куда слать, — владелец видел «Мы
    отправили код на .».
    """
    location = client.post("/login", data={"mail": MAIL}, follow_redirects=False).headers[
        "location"
    ]
    waiting_html = client.get(location).text
    resend_form = _form(waiting_html, "/login/resend")
    fields = dict(resend_form["fields"])  # type: ignore[arg-type]

    assert fields.get("mail") == MAIL, "форма повтора не шлёт адрес — он потеряется"

    # Пауза между отправками проходит: повтор обязан удаться, а не упереться в лимит.
    clock.advance(RESEND_INTERVAL_SECONDS + 1)
    resent = client.post(resend_form["action"], data=fields, follow_redirects=False)

    assert resent.status_code == 303
    assert resent.headers["location"].startswith("/login/waiting")
    screen = client.get(resent.headers["location"]).text
    assert MAIL in screen
    assert len(sender.sent_to(MAIL)) == 2


def test_code_form_carries_the_address_after_a_refusal(
    client: TestClient, sender: FakeMailSender
) -> None:
    """После отказа форма кода по-прежнему знает адрес — иначе шага не пройти.

    Отказ показывается тем же экраном, и адрес к нему обязан доехать: иначе
    человек с письмом на руках не смог бы повторить ввод.
    """
    requested = client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    waiting_html = client.get(requested.headers["location"]).text
    code_form = _form(waiting_html, "/login/verify")
    fields = dict(code_form["fields"])  # type: ignore[arg-type]
    fields["code"] = "000000" if _code(sender) != "000000" else "111111"
    refused = client.post(code_form["action"], data=fields, follow_redirects=False)

    assert refused.status_code == 303
    assert refused.headers["location"] == "/login/waiting"

    again = client.get(refused.headers["location"]).text
    assert MAIL in again
    assert _form(again, "/login/verify")["fields"].get("mail") == MAIL  # type: ignore[union-attr]


# --- 2. экран привязки почты — тем же способом ----------------------------


def test_mail_linking_from_the_forms_confirms_the_address(
    app, client: TestClient, sender: FakeMailSender, fake_api: FakeApi, clock: Clock
) -> None:
    """Привязка почты: адрес → код → привязано, все формы — из разметки.

    Состояние здесь не теряется: скрытое поле адреса есть и у формы кода, и у
    формы повтора (`linking.html`). Тест это закрепляет — тем же способом, что
    и вход, — чтобы правка одной формы не разошлась со второй.
    """
    # Аккаунт только с Telegram: у него есть куда привязать почту.
    account = fake_api.create_account(chat_id=987_654_321)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account.id).token)

    account_html = client.get("/account").text
    link_form = _form(account_html, "/account/link/mail")
    fields = dict(link_form["fields"])  # type: ignore[arg-type]
    fields["mail"] = LINK_MAIL
    requested = client.post(link_form["action"], data=fields, follow_redirects=False)
    assert requested.status_code == 200
    assert LINK_MAIL in requested.text

    # Экран держит шаг кода: адрес приехал скрытым полем.
    confirm_form = _form(requested.text, "/account/link/mail/confirm")
    fields = dict(confirm_form["fields"])  # type: ignore[arg-type]
    assert fields.get("mail") == LINK_MAIL, "форма кода не шлёт адрес — привязка не пройдёт"
    fields["code"] = _code(sender, LINK_MAIL)
    confirmed = client.post(confirm_form["action"], data=fields, follow_redirects=False)

    assert confirmed.status_code == 200
    assert "Почта привязана" in confirmed.text
    assert fake_api.find_by_id(account.id).mail == LINK_MAIL
