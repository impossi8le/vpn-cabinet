"""Главный экран: действия сверху, подключения списком, история и конфиги — ниже.

Раскладка — это иерархия, которую человек видит. Поэтому она проверяется по
порядку блоков в готовом HTML, а не по комментариям в шаблоне: комментарий
переживёт перестановку, а индекс в разметке — нет.

Что здесь закреплено:

- сверху — состояние и действия («Скачать конфиг», «Купить подписку»);
- ниже — список подключений, раскрытый по умолчанию;
- истёкшие уезжают в «Историю», свёрнутую: они не мешают, но и не пропадают;
- порядок списка переключается ссылками (`?sort=`), без JavaScript;
- конфиги собраны отдельным свёрнутым блоком внизу.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import Balance, Referral, Subscription
from cabinet.render import render_subscriptions
from cabinet.subscriptions import SORT_EXPIRY, SORT_LATEST, SORT_NAME, subscription_views
from tests.fakes import FakeApi

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _sub(
    id_: str,
    name: str,
    status: str = "active",
    days: int | None = 30,
) -> Subscription:
    return Subscription(
        id=id_,
        name=name,
        status=status,
        end_date=NOW + timedelta(days=days) if days is not None else None,
    )


def _html(*subs: Subscription, sort: str = SORT_EXPIRY) -> str:
    return render_subscriptions(
        subscription_views(subs, now=NOW),
        sort=sort,
    )


def _details_before(html: str, title: str) -> str:
    """Кусок от открывающего `<details>` до заголовка панели.

    Так проверяется, раскрыта панель или свёрнута: атрибут `open` живёт именно
    здесь, и никакая другая часть страницы на ответ не влияет.
    """
    marker = f'<span class="panel-t">{title}</span>'
    end = html.index(marker)
    return html[html.rindex("<details", 0, end) : end]


# --- верх экрана: состояние и действия -----------------------------------


def test_actions_sit_above_the_connections_list() -> None:
    """«Скачать конфиг» и «Купить подписку» — выше списка подключений.

    Экран открывают ради действия: скачать файл или продлить. Список — это
    справка о том, что уже есть, и стоять выше действий он не должен.
    """
    html = _html(_sub("1", "Основное"))

    # Якорь — заголовок панели, а не `<title>`: «Мои подключения» стоит и там,
    # и сравнение с ним ничего бы не проверяло.
    list_start = html.index('<span class="panel-t">Мои подключения</span>')
    assert html.index("Скачать конфиг") < list_start
    assert html.index("Купить подписку") < list_start


def test_empty_screen_is_one_card_with_the_buy_button() -> None:
    """Без подписок — одна карточка: «Подписок нет» и кнопка покупки в ней же.

    Раньше сверху стояла строка «Подключений пока нет», а под ней карточка
    «Подписок нет»: два блока об одном и том же, и человек перечитывал экран,
    чтобы понять, различаются ли они. У пустого экрана одна мысль — покупка.
    """
    html = _html()

    assert html.count("Подписок нет") == 1
    # Кнопка — главная и внутри той же карточки: другого действия на экране нет.
    assert '<a class="btn btn-p btn-sm" href="/buy">Купить подписку</a>' in html


def test_state_line_shows_the_term_without_stealing_the_download_button() -> None:
    """Состояние сказано строкой, а не отдельной карточкой на пол-экрана."""
    html = _html(_sub("1", "Основное", days=9))

    assert "Действует до" in html
    assert "ещё 9 дней" in html


def test_money_is_shown_after_the_actions() -> None:
    """Баланс и рефералка — под действиями: они не отвечают «что мне сделать»."""
    html = render_subscriptions(
        subscription_views([_sub("1", "Основное")], now=NOW),
        balance=Balance(credit_rub=450),
        referral=Referral(referral_code="ABC123", invited_count=2, rub_earned=100),
    )

    assert html.index("Купить подписку") < html.index("Баланс")
    assert html.index("Купить подписку") < html.index("Ваш код")


# --- список подключений и история ----------------------------------------


def test_main_list_is_expanded_and_history_is_collapsed() -> None:
    """Текущее видно сразу, история — за раскрытием.

    «Скрыть истёкшие» — это не удалить: человек должен найти старую подписку,
    когда она понадобится, но не спотыкаться о неё каждый раз.
    """
    html = _html(_sub("1", "Живое"), _sub("2", "Старое", status="expired", days=-5))

    assert " open" in _details_before(html, "Мои подключения")
    assert " open" not in _details_before(html, "История")


def test_expired_moves_out_of_the_main_list_into_history() -> None:
    """Истёкшая — ниже отметки «История», живая — выше неё."""
    html = _html(_sub("1", "Живое"), _sub("2", "Старое", status="expired", days=-5))

    history = html.index("История")
    assert html.index("Живое") < history
    assert html.index("Старое") > history


def test_blocked_stays_in_the_main_list() -> None:
    """Заблокированная не истекла: её разбирают, а не прячут в архив."""
    html = _html(_sub("1", "Закрытое", status="blocked", days=5))

    assert "Заблокирована" in html
    if "История" in html:
        assert html.index("Закрытое") < html.index("История")


def test_history_disappears_when_there_is_nothing_to_archive() -> None:
    """Без истёкших панель истории не рисуется вовсе — не пустая коробка."""
    html = _html(_sub("1", "Живое"))

    assert '<span class="panel-t">История</span>' not in html


# --- сортировки ----------------------------------------------------------


def test_default_order_starts_with_the_soonest_expiry() -> None:
    """По умолчанию — «по остатку»: то, что кончится раньше, выше.

    Так подписку, до конца которой три дня, не прячут под годовой: продлить
    нужно именно её.
    """
    html = _html(_sub("1", "Долгое", days=90), _sub("2", "Скоро", days=5))

    assert html.index("Скоро") < html.index("Долгое")


def test_latest_order_starts_with_the_farthest_expiry() -> None:
    """«По новизне» — свежая покупка выше: у неё самый дальний срок."""
    html = _html(_sub("1", "Долгое", days=90), _sub("2", "Скоро", days=5), sort=SORT_LATEST)

    assert html.index("Долгое") < html.index("Скоро")


def test_name_order_is_alphabetical() -> None:
    html = _html(_sub("1", "Яблоко", days=5), _sub("2", "Арбуз", days=90), sort=SORT_NAME)

    assert html.index("Арбуз") < html.index("Яблоко")


def test_subscription_without_a_term_sorts_last() -> None:
    """Подписка без срока — в конце любого порядка по сроку.

    `None` о сроке не говорит ничего, и ставить её рядом с «сгорает завтра»
    значило бы придумать дату.
    """
    for sort in (SORT_EXPIRY, SORT_LATEST):
        html = _html(_sub("1", "Бессрочное", days=None), _sub("2", "С датой", days=30), sort=sort)
        assert html.index("С датой") < html.index("Бессрочное")


def test_sort_is_a_link_not_a_hidden_setting() -> None:
    """Все три порядка доступны ссылками: работает без JavaScript и без меню."""
    html = _html(_sub("1", "Основное"))

    for value in (SORT_EXPIRY, SORT_LATEST, SORT_NAME):
        assert f"/subscriptions?sort={value}" in html


# --- конфиги внизу -------------------------------------------------------


def test_configs_are_collected_at_the_bottom() -> None:
    """Файлы — отдельным блоком ниже списка: одно место вместо кнопки на карточке."""
    html = _html(_sub("1", "Домашнее"), _sub("2", "Телефон"))

    files = html.index("Файлы конфигурации")
    assert html.index("Мои подключения") < files
    tail = html[files:]
    assert "Домашнее" in tail
    assert "Телефон" in tail
    assert "/config" in tail


def test_configs_panel_is_collapsed() -> None:
    """К файлам приходят редко — панель свёрнута, чтобы не удлинять экран."""
    html = _html(_sub("1", "Домашнее"))

    assert " open" not in _details_before(html, "Файлы конфигурации")


def test_expired_subscription_has_no_config_link_anywhere() -> None:
    """У истёкшей конфига нет ни сверху, ни в списке, ни в файлах."""
    html = _html(_sub("1", "Старое", status="expired", days=-5))

    assert "/config" not in html
    assert "Скачивание недоступно" in html


# --- адрес страницы ------------------------------------------------------


def _client() -> TestClient:
    api = FakeApi()
    account = api.create_account(chat_id=555_000_111)
    api.add_subscription(
        account.id,
        name="Основное",
        status="active",
        expires_at=NOW + timedelta(days=20),
    )
    app = build_app(store=api, now=lambda: NOW)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account.id).token)
    return client


def test_route_accepts_every_known_sort() -> None:
    client = _client()

    for value in (SORT_EXPIRY, SORT_LATEST, SORT_NAME):
        response = client.get(f"/subscriptions?sort={value}")
        assert response.status_code == 200


@pytest.mark.parametrize("value", ["", "zzz", "None", "1"])
def test_unknown_sort_falls_back_to_the_default(value: str) -> None:
    """Чужая опечатка в адресе не валит страницу и не меняет порядок."""
    client = _client()

    response = client.get(f"/subscriptions?sort={value}")

    assert response.status_code == 200
    assert re.search(r"\?sort=expiry", response.text)
