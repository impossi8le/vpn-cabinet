"""Дружелюбность экранов (§5 разбора): проверяемые свойства, а не «красота».

Четыре правки, у каждой — своя проверка:

- **подписки**: срок и кнопка скачивания идут первыми, «Сменить регион» —
  тихой ссылкой внизу, а не второй главной кнопкой;
- **покупка**: подпись-лид на каждом шаге укладывается в одну строку;
- **смена региона**: активный ключ виден сразу, остальные спрятаны в
  `<details>` — раскрытие работает без JavaScript;
- **экран кода**: сказано, сколько ждать письмо. Строка про «Спам» и смена
  адреса уже есть, и второй копии здесь быть не должно.

Порядок блоков проверяется по индексам в готовом HTML: это и есть иерархия,
которую видит человек, а не замысел в комментарии.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import unescape

from fastapi.testclient import TestClient

from cabinet import render
from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import Balance, Referral, Subscription
from cabinet.render import render_login_waiting, render_subscriptions
from cabinet.subscriptions import STATUS_ACTIVE, subscription_views
from tests.fakes import FakeApi

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _strip_tags(fragment: str) -> str:
    """Текст абзаца: без тегов, сущностей и лишних пробелов."""
    text = unescape(re.sub(r"<[^>]+>", " ", fragment))
    return " ".join(text.split())


def _leads(html: str) -> list[str]:
    return re.findall(r'<p class="lead">(.*?)</p>', html, re.S)


# --- подписки: главное первым --------------------------------------------


def _subscriptions_html() -> str:
    """Экран с подпиской и деньгами: у всех блоков есть за что зацепиться."""
    views = subscription_views(
        [
            Subscription(
                id="1",
                name="Подключение",
                status=STATUS_ACTIVE,
                end_date=NOW + timedelta(days=10),
            )
        ],
        now=NOW,
    )
    return render_subscriptions(
        views,
        balance=Balance(credit_rub=450),
        referral=Referral(referral_code="ABC123", invited_count=2, rub_earned=100),
    )


def test_subscription_term_and_download_come_before_other_blocks() -> None:
    """Срок и «Скачать конфиг» — раньше покупки, баланса и рефералки.

    Главный вопрос экрана — «что у меня есть?». Кнопка «Купить подписку»,
    баланс и рефералка — второстепенные ответы, и стоять выше срока им незачем:
    человек сначала видит, что куплено, и только потом — что можно купить.
    """
    html = _subscriptions_html()

    download = html.index("Скачать конфиг")
    assert download < html.index("Купить подписку")
    assert download < html.index("Баланс")
    assert download < html.index("Ваш код")


def test_term_is_read_before_the_explaining_note() -> None:
    """В карточке сперва срок, потом пояснение к состоянию.

    Пояснение «Подписка действует до указанной даты» дублирует дату словами;
    пока оно стоит выше, взгляд сначала читает оговорку, а не сам срок.
    """
    html = _subscriptions_html()

    assert html.index("<dt>Срок</dt>") < html.index("Подписка действует")


def test_region_change_is_a_quiet_link_at_the_bottom() -> None:
    """«Сменить регион» — тише и ниже: не вторая главная кнопка.

    Регион меняют редко, и он не отвечает на вопрос «что у меня есть?».
    Ссылка остаётся, но перестаёт спорить за внимание с подпиской.
    """
    html = _subscriptions_html()

    assert 'href="/region"' in html
    # Прежняя кнопка-«призрак» того же веса, что «Купить подписку», убрана.
    assert 'class="btn btn-g" href="/region"' not in html
    # Блок региона ниже покупки: сначала то, что человек делает чаще.
    assert html.index("Сменить регион") > html.index("Купить подписку")


# --- покупка: лиды в одну строку -----------------------------------------


def test_each_purchase_lead_is_one_short_line() -> None:
    """Лид каждого шага — одно предложение, а не абзац объяснений.

    На шагах стояли по два-три предложения: страница читалась как инструкция.
    Смысл сохранён, вода убрана: одна мысль на строку. Считаем точки — второе
    предложение в лиде и есть та вода.
    """
    pages = [
        render.render_buy(),
        render.render_buy_confirm(),
        render.render_buy_servers(days=30, devices=2),
    ]

    for html in pages:
        (lead,) = _leads(html)
        text = _strip_tags(lead)
        assert text.count(".") == 1, f"лид не в одну строку: {text!r}"
        assert len(text) <= 80, f"лид длинноват: {text!r}"


# --- смена региона: активный ключ и <details> -----------------------------


def _region_client(profiles: list[dict]) -> tuple[TestClient, FakeApi]:
    api = FakeApi()
    account = api.create_account(chat_id=555_000_777)
    api.reissue_payload["profiles"] = profiles
    app = build_app(store=api, now=lambda: NOW)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account.id).token)
    return client, api


def test_active_key_visible_and_the_rest_inside_details() -> None:
    """На 50 профилях: активный виден, остальные спрятаны, форма работает.

    Активный ключ (первый в списке бота — так платформа отдаёт действующий)
    показывается сразу. Остальные идут в `<details>`: это раскрытие средствами
    браузера, без JavaScript. Выбор ключа остаётся в форме и отправляется даже
    при закрытом `<details>`.
    """
    active = {"id": 100, "filename": "NLclient285.ovpn", "expiration_date": "2026-12-01"}
    rest = [
        {"id": 100 + i, "filename": f"K{i}.ovpn", "expiration_date": "2025-01-01"}
        for i in range(1, 50)
    ]
    client, _ = _region_client([active, *rest])

    html = client.get("/region").text
    visible, hidden = html.split("<details", 1)

    assert active["filename"] in visible
    assert 'name="profile_id"' in hidden
    for item in rest:
        assert item["filename"] not in visible
        assert item["filename"] in hidden
    assert "Показать все" in hidden

    response = client.post(
        "/region",
        data={"target": "GE_CLUSTER", "profile_id": str(active["id"])},
        follow_redirects=False,
    )
    assert response.status_code == 303


# --- экран кода: сколько ждать -------------------------------------------


def test_waiting_screen_says_how_long_the_letter_takes() -> None:
    """Одна строка «сколько ждать» — рядом с адресом, до формы.

    «Письма нет» без ожидания читается как «не придёт». Строка говорит, что
    минута — обычный срок, и не обещает мгновенности.
    """
    html = render_login_waiting(mail="user@example.com")

    assert "в течение минуты" in html


def test_waiting_screen_does_not_repeat_the_spam_hint() -> None:
    """Подсказка про «Спам» и смену адреса остаётся одна — не дублируется.

    Прошлая задача уже добавила её; новая строка говорит только о времени,
    а не пересказывает тот же совет второй раз.
    """
    html = render_login_waiting(mail="user@example.com")

    assert html.count("Спам") == 1
    assert html.count('<a href="/login">') == 1
