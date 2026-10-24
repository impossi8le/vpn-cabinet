"""Условия, приватность и гарантия возврата (док продающего кабинета, §4).

**Почему отдельный файл.** Экран входа обещает: «нажимая кнопку, вы соглашаетесь
с условиями». Обещание выполнимо только если условия открываются, а на боевом
кабинете ссылки вели в **404** — это нашла живая проверка, когда рядом появилась
гарантия возврата. Обещать возврат и не показывать его условия значит обещать
чужое.

**Главный тест здесь — совпадение формулировок.** Слова гарантии стоят на экране
оплаты и в документе условий: две копии одного обещания разойдутся при первой
правке, и человек прочитает на экране одно, а в документе — другое. Поэтому тест
берёт фразу с экрана и требует её же в тексте условий, а не сверяет с копией,
вписанной в тест.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cabinet.app import build_app
from cabinet.mail_sender import MailSender
from cabinet.render import PAY_GUARANTEE_NOTE, PAY_GUARANTEE_TITLE
from tests.fakes import FakeApi, FakeMailSender
from tests.markup import hrefs, link

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "buyer@example.com"
CHAT_ID = 123_456_789


def _signed_in(*, chat_id: int | None = CHAT_ID) -> TestClient:
    api = FakeApi()
    account = api.create_account(mail=MAIL, mail_verified_at=NOW, chat_id=chat_id)
    api.add_server(code="nl-1", name="Нидерланды · Амстердам")
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("cabinet_session", app.state.sessions.sign_in(account.id).token)
    return client


def _anonymous() -> TestClient:
    return TestClient(build_app(store=FakeApi()), raise_server_exceptions=False)


def _flat(text: str) -> str:
    """Текст без переносов и лишних пробелов — для сравнения формулировок."""
    return re.sub(r"\s+", " ", text)


# --- документы открываются --------------------------------------------------


def test_terms_page_is_served() -> None:
    """Условия открываются: это документ, с которым человек соглашается на входе."""
    response = _anonymous().get("/terms")

    assert response.status_code == 200
    assert "Пользовательское соглашение" in response.text
    assert "Возврат средств" in response.text


def test_privacy_page_is_served() -> None:
    """Политика конфиденциальности открывается — вторая ссылка со входа."""
    response = _anonymous().get("/privacy")

    assert response.status_code == 200
    assert "Политика конфиденциальности" in response.text


def test_login_screen_legal_links_resolve() -> None:
    """Обе ссылки со входа ведут на страницы, которые действительно открываются.

    Проверка не «ссылка есть в разметке», а «по ссылке открывается документ»:
    разметка с обеими ссылками была и раньше, а страниц не было — 404 находился
    только живой проверкой.
    """
    client = _anonymous()
    html = client.get("/login").text

    assert hrefs(html, "политикой конфиденциальности") == ["/privacy"]
    assert hrefs(html, "условиями") == ["/terms"]
    for url in ("/privacy", "/terms"):
        assert client.get(url).status_code == 200, url


# --- гарантия рядом с кнопкой ----------------------------------------------


def test_guarantee_stands_next_to_the_payment_button() -> None:
    """Гарантия возврата — на экране оплаты, рядом с кнопкой (§4 дока).

    Именно здесь возникает страх «заплачу и не заработает», и именно здесь
    обещание возврата что-то значит.
    """
    html = _signed_in().get("/buy/servers?days=30&devices=1&pick=nl-1").text

    assert link(html, "Перейти к оплате"), "гарантия должна стоять у самой кнопки"
    assert PAY_GUARANTEE_TITLE in html
    assert PAY_GUARANTEE_NOTE in html
    assert link(html, "Условия") == "/terms"


def test_guarantee_wording_matches_the_terms_document() -> None:
    """Слова гарантии на экране — те же, что в условиях.

    Единственная проверка, которая ловит расхождение двух копий обещания. Тест
    берёт фразу **с экрана** и требует её в документе: если завтра поправят одну
    копию, он покраснеет, а не пропустит два разных обещания.
    """
    client = _signed_in()
    screen = client.get("/buy/servers?days=30&devices=1&pick=nl-1").text
    terms = client.get("/terms").text

    assert PAY_GUARANTEE_NOTE in screen
    # Сравниваются **слова**, а не переносы строк: в документе фраза разбита
    # разметкой на строки, и требовать совпадения пробелов значило бы проверять
    # вёрстку исходного файла, а не обещание.
    assert _flat(PAY_GUARANTEE_NOTE) in _flat(terms), "гарантия разошлась с условиями"


def test_free_path_does_not_promise_a_refund() -> None:
    """На бесплатном пути обещания возврата нет: возвращать нечего.

    Обещание возврата в месте, где денег не берут, читалось бы как условие
    бесплатности — то есть как то, чего документ не говорит.
    """
    html = _signed_in().get("/buy/servers?days=30&devices=1&pick=nl-1&free=1").text

    assert link(html, "Получить бесплатно")
    assert PAY_GUARANTEE_TITLE not in html
    assert PAY_GUARANTEE_NOTE not in html
