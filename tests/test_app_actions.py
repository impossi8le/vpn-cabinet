"""issue #43: действия кабинета отвечают, а не 404.

Дефект был не в разметке, а в HTTP-слое: шаблон `linking.html` и
`render.render_linking` существовали, но экран не был подключён, а формы слали
на адреса (`/account/link/*`, `/account/unlink/*`), для которых обработчиков не
было вовсе — запрос отвечал `404`. Поэтому здесь проверяется **выполнение
запроса**, а не наличие кнопки в HTML: тест «в разметке есть кнопка» этот дефект
как раз и пропустил. Кнопка без обработчика выглядит правильной и не работает.

Проверяется пять вещей, названных в задаче:

1. `GET /account` с сессией — 200 и на экране есть кнопки привязки/отвязки;
2. `GET /account` без сессии — редирект на `/login`, а не 500 и не пустая форма;
3. каждое действие отвечает **не** 404 — это и есть текущий дефект;
4. отвязка единственного способа входа — на экране текст отказа (W3.5);
5. скачивание по истёкшей подписке — отказ на карточке, а не выдача конфига.

Зависимости подставляются в `build_app`: подставной бот и подставная почта, ни
сети, ни базы — как и во всей волне. Сессия вошедшего аккаунта берётся у самого
приложения (`app.state.sessions`), а не выдумывается: подставленный cookie был бы
не отличим от поддельного.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cabinet.app import (
    COOKIE_NAME,
    SUBSCRIPTIONS_UNKNOWN_CONFIG,
    UNLINK_LAST_METHOD_ERROR,
    build_app,
)
from cabinet.mail_sender import MailSender
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"
CHAT_ID = 123_456_789
_COOKIE = "cabinet_session"


def _build(fake_api: FakeApi, sender: FakeMailSender) -> TestClient:
    """Клиент на подставных зависимостях.

    `raise_server_exceptions=False`: 500 обязан прийти как 500, а не как
    исключение наружу, — иначе тест «не 404» видел бы стектрейс вместо кода
    ответа и не отличил бы сбой от дефекта.
    """
    app = build_app(
        store=fake_api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.app_state = app.state  # сессии приложения — для выдачи сессии в тесте
    return client


@pytest.fixture
def sender() -> FakeMailSender:
    return FakeMailSender()


@pytest.fixture
def client(fake_api: FakeApi, sender: FakeMailSender) -> TestClient:
    return _build(fake_api, sender)


def fake_api_of(client: TestClient) -> FakeApi:
    """Подставной бот, на котором собран клиент. Хранится в состоянии приложения."""
    return client.app_state.store  # type: ignore[attr-defined]


def _sign_in(client: TestClient, sender: FakeMailSender, mail: str = MAIL) -> None:
    """Войти тем же путём, что у человека: запрос кода → код → cookie.

    Cookie не подставляется напрямую: тест не должен отличать рабочий вход от
    сломанного.
    """
    client.post("/login", data={"mail": mail}, follow_redirects=False)
    body = sender.sent_to(mail)[-1]["body"]
    code = "".join(ch for ch in body if ch.isdigit())[:6]
    client.post("/login/waiting", data={"mail": mail, "code": code}, follow_redirects=False)


def _session_cookie(app_state: object, account_id: int) -> str:
    """Токен сессии вошедшего аккаунта — у самого приложения, а не выдуманный.

    Нужен, когда вход через `/login` дал бы не то состояние аккаунта, которое
    требуется (вход по почте всегда проставляет почту, а нам нужен аккаунт
    только с Telegram).
    """
    return app_state.sessions.sign_in(account_id).token  # type: ignore[attr-defined]


# --- 1. экран способов входа подключён ------------------------------------


def test_account_page_renders_for_signed_in_user(
    client: TestClient, sender: FakeMailSender
) -> None:
    """`GET /account` с сессией — 200, а не 404 из-за отсутствующего роута.

    Это и есть подключение экрана: до правки адреса `/account` не было, и
    страница не открывалась вовсе.
    """
    _sign_in(client, sender)

    response = client.get("/account")

    assert response.status_code == 200
    assert "Способы входа" in response.text


def test_account_page_offers_attach_and_detach_buttons(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """На экране есть кнопки: у недостающего способа — привязать, у обоих — отвязать.

    Аккаунт с почтой и Telegram: привязывать нечего, зато оба способа можно
    отвязать. Кнопки обязаны быть на странице, которую отдаёт роут, а не только
    в шаблоне.
    """
    _sign_in(client, sender)
    account = fake_api.find_by_mail(MAIL)
    fake_api.link_chat_id(account.id, CHAT_ID)

    html = client.get("/account").text

    assert "Отвязать почту" in html
    assert "Отвязать Telegram" in html


def test_account_page_shows_attach_form_for_missing_method(
    client: TestClient, fake_api: FakeApi
) -> None:
    """У недостающего способа — форма привязки, а не тупик.

    Аккаунт только с Telegram: почту можно привязать, и форма для этого стоит
    на странице роута.
    """
    account = fake_api.create_account(chat_id=CHAT_ID)
    client.cookies.set(_COOKIE, _session_cookie(client.app_state, account.id))

    html = client.get("/account").text

    assert "Получить код" in html
    assert 'name="mail"' in html


# --- 2. без сессии — на вход ----------------------------------------------


def test_account_page_without_session_redirects_to_login(client: TestClient) -> None:
    """Без cookie — редирект на вход, а не 500 и не форма чужого аккаунта.

    Обычное состояние первого захода. Ответ 500 здесь означал бы, что открытие
    адреса без сессии считается сбоем сервера.
    """
    response = client.get("/account", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_forged_session_does_not_open_account_page(client: TestClient) -> None:
    """Поддельный токен экран не открывает — пара к «после входа открывается».

    Без этой пары роут зеленел бы и на коде, который пускает по любому непустому
    cookie.
    """
    client.cookies.set(COOKIE_NAME, "not-a-real-token")

    response = client.get("/account", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- 3. каждое действие отвечает, а не 404 --------------------------------


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/account/link/telegram", {}),
        ("/account/link/mail", {"mail": MAIL}),
        ("/account/unlink/telegram", {}),
        ("/account/unlink/mail", {}),
    ],
)
def test_each_action_has_a_handler_not_404(
    client: TestClient, sender: FakeMailSender, path: str, data: dict
) -> None:
    """Ни одно действие не отвечает 404 — ровно этот дефект и был.

    Проверяется код ответа на реальный POST. Список путей, собранный по
    `app.routes`, тоже зеленел бы на ручке, которая при вызове падает; здесь
    запрос действительно выполняется.

    Аккаунт с обоими способами: тогда отвязка обоих разрешена, и 404 нельзя
    спутать с отказом по правилу «последний способ входа».
    """
    _sign_in(client, sender)
    account = fake_api_of(client).find_by_mail(MAIL)
    fake_api_of(client).link_chat_id(account.id, CHAT_ID)

    response = client.post(path, data=data, follow_redirects=False)

    assert response.status_code != 404, f"{path} без обработчика"


def test_unknown_action_still_404_for_contrast(client: TestClient) -> None:
    """Несуществующий адрес по-прежнему 404 — иначе проверка «не 404» пуста.

    Пара к предыдущему тесту: если бы приложение отвечало 200 на что угодно,
    тест «действие не 404» ничего не доказывал бы.
    """
    response = client.post("/account/no-such-action", data={}, follow_redirects=False)

    assert response.status_code == 404


# --- 4. отвязка единственного способа входа — отказ, а не тихий пропуск ---


def test_unlinking_the_only_method_shows_the_refusal(
    client: TestClient, sender: FakeMailSender
) -> None:
    """Отвязка единственного способа — на экране текст отказа (W3.5).

    Аккаунт с одной почтой. Запрет держится в `Linking` и в хранилище; роут
    обязан показать его человеку, а не ответить 500 или сделать вид, что отвязал.
    Отсутствие кнопки в разметке защитой не является: POST можно отправить и без
    неё, поэтому отказ обязан быть видимым на экране.
    """
    _sign_in(client, sender)

    response = client.post("/account/unlink/mail", data={}, follow_redirects=False)

    assert response.status_code == 200
    # Сверяется текст самого отказа, а не общая фраза шаблона «Это единственный
    # способ входа» у спрятанной кнопки: иначе тест зеленел бы и на роуте,
    # который отказ не показал.
    assert UNLINK_LAST_METHOD_ERROR in response.text


def test_unlinking_the_only_telegram_method_shows_the_refusal(
    client: TestClient, fake_api: FakeApi
) -> None:
    """Симметрично: аккаунт только с Telegram — отвязка Telegram отвергнута.

    Пара к отказу по почте: у способа, за которым стоят конфиги, запрет тот же,
    и он виден тем же текстом.
    """
    account = fake_api.create_account(chat_id=CHAT_ID)
    client.cookies.set(_COOKIE, _session_cookie(client.app_state, account.id))

    response = client.post("/account/unlink/telegram", data={}, follow_redirects=False)

    assert response.status_code == 200
    assert UNLINK_LAST_METHOD_ERROR in response.text


def test_unlink_succeeds_when_another_method_remains(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """Отвязка при втором способе входа **действительно** отвязывает.

    Это проверка выполнения действия, а не ответа 200: после запроса у аккаунта
    не должно остаться почты, а способ входа сохраниться должен — иначе тест
    зеленел бы и на роуте, который отвечает страницей, ничего не меняя.
    """
    _sign_in(client, sender)
    account = fake_api.find_by_mail(MAIL)
    fake_api.link_chat_id(account.id, CHAT_ID)

    response = client.post("/account/unlink/mail", data={}, follow_redirects=False)

    assert response.status_code == 200
    after = fake_api.find_by_id(account.id)
    assert after.mail is None  # почта отвязана
    assert after.chat_id == CHAT_ID  # Telegram остался — вход не потерян


# --- 5. скачивание по истёкшей подписке — отказ на карточке ---------------


def test_downloading_by_expired_subscription_refuses_on_the_card(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """Истёкшая подписка — отказ на карточке, а не выдача конфига (W4.2).

    Конфиг по такой подписке не отдаётся вовсе: право решает подписка, а не
    запрос. Ответ приходит тем же экраном подписок с текстом на карточке — так
    человек остаётся там, откуда нажал (W5.4).
    """
    _sign_in(client, sender)
    account = fake_api.find_by_mail(MAIL)
    config = fake_api.add_config(account.id, body="# fake .ovpn")
    subscription = fake_api.add_subscription(
        account.id,
        status="expired",
        expires_at=NOW - timedelta(days=1),
        config_id=config.id,
    )

    response = client.get(f"/subscriptions/{subscription.id}/config", follow_redirects=False)

    assert response.status_code == 200
    assert "Конфиг недоступен" in response.text
    # И содержимого профиля в ответе нет — иначе «отказ» отдал бы конфиг.
    assert "# fake .ovpn" not in response.text


def test_downloading_unknown_subscription_still_explains_the_refusal(
    client: TestClient, sender: FakeMailSender
) -> None:
    """Подписки с таким id нет — отказ всё равно виден, а не проглочен.

    Адрес назвал подписку, которой у аккаунта нет: карточки для неё на экране
    нет, и отказ, положенный «на карточку», не показался бы нигде — человек
    увидел бы обычный список и не понял, почему скачивание не началось. Поэтому
    здесь отказ уходит в общий текст экрана.
    """
    _sign_in(client, sender)

    response = client.get("/subscriptions/424242/config", follow_redirects=False)

    assert response.status_code == 200
    assert SUBSCRIPTIONS_UNKNOWN_CONFIG in response.text


def test_downloading_config_without_session_redirects_to_login(client: TestClient) -> None:
    """Без сессии скачивание уводит на вход, а не отдаёт конфиг анонимно."""
    response = client.get("/subscriptions/1/config", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
