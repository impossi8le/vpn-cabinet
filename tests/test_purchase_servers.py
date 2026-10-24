"""Шаг 4 покупки: выбор серверов ровно по числу устройств (#37).

Главное правило шага (спека §1): число выбранных серверов должно **совпасть** с
числом устройств, и это условие завершения, а не лимит — пока выбрано меньше,
шаг не закончен. Один и тот же сервер можно взять несколько раз.

Ссылки проверяются «от разметки»: тест не собирает адреса руками, а берёт
отрисованные `href` и идёт по ним — как это сделал бы браузер. Раньше в проекте
тест, собранный руками, пропустил форму без адреса; здесь адрес выбора живёт
только в ссылках, и брать его из разметки — единственный честный способ.

Экран работает без JavaScript: выбор и переходы — ссылки, а не скрипт.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import BotUnreachable
from cabinet.mail_sender import MailSender
from cabinet.theme import COLORS
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "buyer@example.com"


class _LinkParser(HTMLParser):
    """Ссылки разметки: адрес и подпись. Это и есть «от разметки» для шага 4."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, self._text.strip()))
            self._href = None
            self._text = ""


def _links(html: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    parser.feed(html)
    return parser.links


def _hrefs(html: str, label: str) -> list[str]:
    """Адреса ссылок с подписью `label`, **в порядке разметки**.

    Порядок — это порядок серверов от бота: строки рисуются как пришли, поэтому
    первая «Добавить» относится к первому серверу. Так тест берёт адрес у
    страницы, а не строит сам.
    """
    return [href for href, text in _links(html) if text == label]


def _client_for(api: FakeApi, account_id: int) -> TestClient:
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account_id).token)
    return client


def _api_with_servers() -> FakeApi:
    """Подставной бот с двумя серверами — имена как у бота, коды — идентификаторы."""
    api = FakeApi()
    api.add_server("nl-1", "Нидерланды · Амстердам")
    api.add_server("de-1", "Германия · Франкфурт")
    return api


def _signed_in(api: FakeApi | None = None) -> TestClient:
    api = api if api is not None else _api_with_servers()
    account = api.create_account(mail=MAIL, mail_verified_at=NOW)
    return _client_for(api, account.id)


class _NoServers(FakeApi):
    """Подставной бот, не отвечающий на список серверов — сбой связи."""

    def list_servers(self):  # type: ignore[override]
        raise BotUnreachable("нет связи")


def _markup_without_palette(html: str) -> str:
    start = html.find(":root {")
    end = html.find("}", start)
    return html[:start] + html[end:]


# --- без сессии — на вход -------------------------------------------------


def test_servers_without_session_redirects_to_login() -> None:
    """Без cookie — редирект на вход, а не 500 и не чужой экран."""
    client = TestClient(build_app(store=FakeApi()), raise_server_exceptions=False)

    response = client.get("/buy/servers", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- список серверов от бота ----------------------------------------------


def test_servers_come_from_the_bot() -> None:
    """Имена серверов — те, что отдал бот, а не выдуманные на экране."""
    html = _signed_in().get("/buy/servers?days=30&devices=2").text

    assert "Нидерланды · Амстердам" in html
    assert "Германия · Франкфурт" in html


def test_empty_server_list_is_honest_not_an_error() -> None:
    """У бота нет серверов — экран говорит «нет», а не показывает пустоту молча."""
    html = _signed_in(FakeApi()).get("/buy/servers").text

    assert "Серверов сейчас нет" in html


def test_unreachable_bot_is_not_an_empty_list() -> None:
    """Бот не ответил — это отказ, а не «серверов нет»: списка кабинет не видел."""
    html = _signed_in(_NoServers()).get("/buy/servers").text

    assert "Список серверов не получен" in html
    assert "Серверов сейчас нет" not in html


# --- выбор: условие завершения, а не лимит --------------------------------


def test_step_is_not_finished_until_enough_servers_are_chosen() -> None:
    """Выбрано меньше, чем устройств, — шаг не закончен, и сказано, сколько осталось."""
    html = _signed_in().get("/buy/servers?days=30&devices=2&pick=nl-1").text

    assert "Выбрано 1 из 2" in html
    assert "Шаг не закончен" in html


def test_step_is_finished_when_selection_matches_devices() -> None:
    """Выбрано ровно столько, сколько устройств, — шаг закончен."""
    html = _signed_in().get("/buy/servers?days=30&devices=2&pick=nl-1&pick=de-1").text

    assert "Выбрано 2 из 2" in html
    assert "Готово" in html


def test_the_same_server_can_be_chosen_twice() -> None:
    """Один сервер можно выбрать несколько раз — комплект из одинаковых (спека §1)."""
    html = _signed_in().get("/buy/servers?days=30&devices=2&pick=nl-1&pick=nl-1").text

    assert "Выбрано 2 из 2" in html
    assert "2 сервера" in html


def test_no_add_link_once_enough_servers_are_chosen() -> None:
    """Сверх нужного добавить нельзя: «ровно столько» держится в разметке, не в тексте."""
    html = _signed_in().get("/buy/servers?days=30&devices=1&pick=nl-1").text

    assert _hrefs(html, "Добавить") == []


# --- ссылки: тест идёт по нарисованным адресам ----------------------------


def test_add_link_from_the_page_adds_the_server() -> None:
    """«Добавить» из разметки добавляет сервер на следующем экране.

    Адрес берётся у страницы, а не собирается руками: так тест краснеет, если
    ссылка перестала нести текущий выбор.
    """
    client = _signed_in()
    page = client.get("/buy/servers?days=30&devices=2").text
    first_add = _hrefs(page, "Добавить")[0]

    after = client.get(first_add).text

    assert "Выбрано 1 из 2" in after
    assert "1 сервер" in after


def test_two_adds_from_the_pages_reach_the_required_count() -> None:
    """Две «Добавить» подряд доводят выбор до нужного числа — по нарисованным ссылкам."""
    client = _signed_in()
    page = client.get("/buy/servers?days=30&devices=2").text

    after_first = client.get(_hrefs(page, "Добавить")[0]).text
    after_second = client.get(_hrefs(after_first, "Добавить")[1]).text

    assert "Выбрано 2 из 2" in after_second
    assert "Готово" in after_second


def test_remove_link_drops_one_occurrence_only() -> None:
    """«Убрать» снимает **одно** вхождение, а не все: комплект из одинаковых цел.

    Сервер выбран дважды; одна «Убрать» обязана оставить один, а не обнулить оба.
    """
    client = _signed_in()
    page = client.get("/buy/servers?days=30&devices=2&pick=nl-1&pick=nl-1").text
    first_remove = _hrefs(page, "Убрать")[0]

    after = client.get(first_remove).text

    assert "Выбрано 1 из 2" in after
    assert "1 сервер" in after


# --- переходы и цвет ------------------------------------------------------


def test_servers_screen_links_back_to_confirmation() -> None:
    """Возврат к подтверждению с той же парой — шаг адресуем, а не тупик."""
    html = _signed_in().get("/buy/servers?days=90&devices=2").text

    assert 'href="/buy/confirm?days=90&amp;devices=2"' in html


def test_servers_screen_renders_through_the_real_route() -> None:
    """Экран идёт через настоящий роут `TestClient`, а не прямой вызов рендера."""
    response = _signed_in().get("/buy/servers")

    assert response.status_code == 200
    assert "Выберите серверы" in response.text


def test_servers_screen_has_no_green() -> None:
    """Выбор сервера — не защита (§7): зелёного на экране нет."""
    html = _signed_in().get("/buy/servers").text

    assert COLORS.green.upper() not in _markup_without_palette(html).upper()


# --- чужой выбор в адресе -------------------------------------------------


def test_unknown_server_code_in_the_url_is_not_counted_as_chosen() -> None:
    """Чужой код сервера в адресе не считается выбором: состояние не выдумывается.

    Выбор живёт **только** в адресе, поэтому адрес же — единственное, что можно
    подделать. `?devices=2&pick=<чужой>&pick=<чужой>` давал «Готово: выбрано
    ровно столько» при пустом экране: число повторов считалось выбором, а коды
    со списком, который кабинет сам получил, не сверялись.

    Адрес берётся из разметки — ссылка «Добавить», — а меняется в нём только код
    сервера. Так проверяется сверка со списком, а не формат ссылки: собери тест
    адрес руками, он зеленел бы и на выдуманном формате.
    """
    client = _signed_in()
    real_add = _hrefs(client.get("/buy/servers?days=30&devices=2").text, "Добавить")[0]
    params = real_add.split("pick=")[0]

    # Оба вхождения — чужой код: вдвоём они всё ещё не выбор.
    forged = client.get(f"{params}pick=нет-такого&pick=нет-такого").text

    assert "Готово" not in forged
    assert "Выбрано 0 из 2" in forged

    # Один настоящий и один чужой — ровно один выбор, а не два.
    real_code = real_add.split("pick=")[1]
    mixed = client.get(f"{params}pick={real_code}&pick=нет-такого").text

    assert "Готово" not in mixed
    assert "Выбрано 1 из 2" in mixed
