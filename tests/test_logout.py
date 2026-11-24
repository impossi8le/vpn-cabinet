"""Выход из кабинета: кнопка, маршрут и настоящее гашение сессии.

Дефект, который здесь закрывается, нашёл обзор авторизации: `sign_out` и
`POST /internal/sessions/revoke` существовали, но **ни один роут кабинета их не
вызывал**, и кнопки «Выйти» не было ни на одном экране. Человек не мог закрыть
вход со своего устройства — токен жил до истечения срока.

Проверяется не «кнопка есть в разметке», а выполнение: после `POST /logout`
cookie погашена, токен не находится в хранилище, а защищённый экран снова ведёт
на вход. Кнопка без обработчика выглядит правильной и не работает — этот класс
дефекта в проекте уже ловили.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import BotUnreachable
from cabinet.mail_sender import MailSender
from cabinet.sessions import SingleSessionStore
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"


class _RefusingStore(SingleSessionStore):
    """Хранилище, которое не может отозвать сессию: бот недоступен.

    Выход обязан сработать и в этом случае: cookie гасится на месте, иначе
    человек остаётся в кабинете ровно тогда, когда просил выйти.
    """

    def revoke(self, token: str) -> None:
        raise BotUnreachable("бот недоступен")


def _build(session_store: object | None = None) -> tuple[TestClient, FakeMailSender]:
    """Клиент на подставных зависимостях и транспорт писем: ни сети, ни базы."""
    sender = FakeMailSender()
    app = build_app(
        store=FakeApi(),
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        now=lambda: NOW,
        session_store=session_store,  # type: ignore[arg-type]
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.app_state = app.state  # type: ignore[attr-defined]
    return client, sender


def _sign_in(client: TestClient, sender: FakeMailSender) -> str:
    """Войти тем же путём, что человек: запрос кода → код → cookie.

    Возвращает токен из cookie: тест не подставляет сессию и не отличает
    рабочий вход от сломанного.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    body = sender.sent_to(MAIL)[-1]["body"]
    code = "".join(ch for ch in body if ch.isdigit())[:6]
    client.post("/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False)
    token = client.cookies.get(COOKIE_NAME)
    assert token
    return token


def test_logout_clears_the_cookie_and_ends_the_session() -> None:
    """После выхода cookie нет, токен не находится, экран снова просит вход."""
    client, sender = _build()
    token = _sign_in(client, sender)
    assert client.get("/subscriptions").status_code == 200

    response = client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert COOKIE_NAME not in client.cookies
    assert client.app_state.sessions.resolve(token) is None  # type: ignore[attr-defined]
    assert client.get("/subscriptions", follow_redirects=False).status_code == 303


def test_logout_without_a_session_is_idempotent() -> None:
    """Повторный выход без cookie — тот же редирект, а не 500.

    Выход — дверь, которой человек закрывает доступ; отказ «сессии уже нет»
    оставлял бы ощущение, что выход не сработал.
    """
    client, _ = _build()

    response = client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_logout_is_post_only() -> None:
    """GET выхода не существует: переход по ссылке не выкидывает из кабинета.

    Ссылку могут открыть превью-бот или предзагрузка браузера. Выход — POST,
    как и остальные изменяющие действия.
    """
    client, _ = _build()

    assert client.get("/logout", follow_redirects=False).status_code == 405


def test_logout_clears_the_cookie_even_if_the_bot_is_unreachable() -> None:
    """Бот не ответил — cookie всё равно гаснет, а не остаётся у человека.

    Серверную запись в этом случае отозвать нечем: она остаётся живой, и это
    записано в журнал. Человеку токен больше не предъявлен — браузер его
    забыл. Оставлять cookie «на потом» было бы хуже: человек просил выйти.
    """
    client, sender = _build(session_store=_RefusingStore())
    token = _sign_in(client, sender)

    response = client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert COOKIE_NAME not in client.cookies
    assert client.app_state.sessions.resolve(token) is not None  # type: ignore[attr-defined]


def test_account_screen_has_a_logout_button_in_the_header() -> None:
    """Кнопка выхода — в шапке экрана способов входа, на виду.

    Внизу страницы её не находили: экран способов входа длинный, и выход
    оказывался за пределами первого экрана. Поэтому проверяется ещё и место —
    форма обязана стоять выше первой карточки.
    """
    client, sender = _build()
    _sign_in(client, sender)

    html = client.get("/account").text

    assert html.count('action="/logout"') == 1
    assert "Выйти" in html
    assert 'method="post"' in html
    assert html.index('action="/logout"') < html.index('class="card"')


def test_subscriptions_screen_has_no_logout_button() -> None:
    """Выход живёт на экране способов входа, а не на главном.

    Два места для одного действия разъехались бы: на главном выход стоял в
    шапке рядом со ссылкой на способы входа и спорил с ней за место.
    """
    client, sender = _build()
    _sign_in(client, sender)

    html = client.get("/subscriptions").text

    assert 'action="/logout"' not in html
