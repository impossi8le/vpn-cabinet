"""Обход всех внутренних ссылок кабинета: каждая обязана открываться.

**Зачем, если есть тесты экранов.** Тесты экранов проверяют экран, сквозной
прогон — один путь. Оба класса дефектов, которые я находил руками, лежат **между**
экранами и не видны ни одному из них:

- `/account` со всей привязкой и отвязкой существовал, а ссылки на него не было
  нигде — попасть можно было только зная адрес;
- ссылки со входа на «условия» и «приватность» вели в **404**: разметка
  ссылалась, страниц не было.

Оба находила живая проверка, потому что тесты разметки проверяли наличие ссылки,
а не то, что **по ссылке что-то открывается**. Здесь это проверяется обходом: с
каждой открытой страницы берутся внутренние ссылки, и каждая обязана ответить.

Обход идёт **по разметке**, а не по списку роутов из кода: список роутов
проверил бы сервер, а не то, что человек может найти.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cabinet.app import build_app
from cabinet.mail_sender import MailSender
from tests.fakes import FakeApi, FakeMailSender
from tests.markup import links

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "buyer@example.com"
CHAT_ID = 123_456_789

# Сколько страниц обходить. Кабинет маленький: сорока хватает с запасом, а предел
# нужен, чтобы опечатка в ссылке не превратила обход в бесконечность.
MAX_PAGES = 40


def _client() -> tuple[TestClient, FakeApi]:
    """Клиент с сессией: так видны и экраны без входа, и экраны внутри."""
    api = FakeApi()
    account = api.create_account(mail=MAIL, mail_verified_at=NOW, chat_id=CHAT_ID)
    api.add_server(code="nl-1", name="Нидерланды · Амстердам")
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("cabinet_session", app.state.sessions.sign_in(account.id).token)
    return client, api


def _internal(href: str) -> bool:
    """Ссылка внутри кабинета: внешние и служебные не обходим.

    Внешние (почта, Telegram) принадлежат другим сервисам, и требовать от них
    ответа значило бы проверять чужую доступность. Служебные схемы (`mailto:`,
    `tel:`) браузер обрабатывает сам.
    """
    return href.startswith("/") and not href.startswith("//")


def _crawl(client: TestClient, *starts: str) -> dict[str, int]:
    """Обойти кабинет от заданных входов и вернуть «путь → код ответа».

    Обход идёт **по путям**, а не по полным адресам: у экранов покупки адрес
    несёт выбор (`?days=&devices=&pick=`), и комбинаций там столько, что обход по
    полным адресам упирается в предел, не дойдя до половины экранов. Проверяем мы
    «открывается ли экран», а не «открывается ли каждая комбинация».

    Входов несколько, потому что часть ссылок видна только в определённом
    состоянии экрана: например, ссылка на условия стоит рядом с кнопкой оплаты,
    а кнопка появляется лишь когда набор серверов собран. Обход от одной
    страницы до неё не дойдёт — и это свойство экрана, а не дефект.
    """
    seen: dict[str, int] = {}
    queue = list(starts)
    while queue and len(seen) < MAX_PAGES:
        url = queue.pop(0)
        path = url.split("?")[0].split("#")[0]
        if path in seen:
            continue
        response = client.get(url, follow_redirects=False)
        seen[path] = response.status_code
        # Редирект — тоже ответ; идём дальше по его адресу, чтобы обход не
        # обрывался на первом же «переехало сюда».
        location = response.headers.get("location", "")
        if response.status_code in (301, 302, 303, 307) and location.startswith("/"):
            queue.append(location)
        if response.status_code != 200 or "text/html" not in response.headers.get(
            "content-type", ""
        ):
            continue
        for href, _label in links(response.text):
            if _internal(href):
                queue.append(href)
    return seen


def test_every_internal_link_of_the_cabinet_resolves() -> None:
    """Каждая внутренняя ссылка, которую видит человек, открывается.

    Это проверка достижимости целиком: не «ссылка есть», а «по ней открывается
    страница». Именно её отсутствие стоило нам двух дефектов — 404 на условиях и
    экрана аккаунта, на который не было входа.
    """
    client, _api = _client()

    pages = _crawl(
        client,
        "/subscriptions",
        # Собранный набор: только в этом состоянии на экране серверов появляются
        # кнопка оплаты и ссылка на условия.
        "/buy/servers?days=30&devices=1&pick=nl-1",
    )

    broken = {url: code for url, code in pages.items() if code not in (200, 303)}
    assert broken == {}, f"ссылки, которые не открываются: {broken}"
    # Обход обязан был дойти до ключевых экранов, а не остановиться на первом.
    # Приватности здесь нет и быть не может: ссылка на неё стоит на экране
    # входа, а этот обход идёт со сессией и на вход не возвращается — за неё
    # отвечает второй обход, ниже.
    for expected in ("/subscriptions", "/account", "/buy", "/terms"):
        assert expected in pages, f"обход не дошёл до {expected}"


def test_login_page_links_all_resolve() -> None:
    """Страницы вокруг входа достижимы и без сессии — с них человек и начинает."""
    client, _api = _client()
    client.cookies.clear()

    pages = _crawl(client, "/login")

    broken = {url: code for url, code in pages.items() if code not in (200, 303)}
    assert broken == {}, f"ссылки со входа, которые не открываются: {broken}"
    for expected in ("/terms", "/privacy", "/login"):
        assert expected in pages, f"обход не дошёл до {expected}"
