"""issue #44: привязка почты в кабинете доводится до конца, а не до письма.

Дефект был не в разметке и не в связывании: `Linking.link_mail` умел всё —
проверить код, привязать только на пустое место (§3.3), — но **шага ввода кода
на экране не существовало**. `POST /account/link/mail` отправлял код и отвечал
текстом «ввод кода ещё не подключён, подтверждение завершается в боте», а
завершать его некому: код выдаёт и проверяет `cabinet/auth_mail.py`, хранилище
кодов — память процесса кабинета. Человек вводил адрес, получал письмо и
оставался с ним.

Поэтому проверяется **выполнение привязки**, а не наличие поля в разметке: тест
«в HTML есть поле кода» этот дефект как раз и пропустил бы — так же, как дефект
#43 пропустил тест «кнопка есть».

Отдельно проверяются два свойства, ради которых шаг держится на сервере:

- **Код, выданный на один адрес, не привязывает другой.** Адрес едет в форме
  скрытым полем, и подменить его может кто угодно. Спасает то, что `AuthMail`
  хранит код **по адресу**: подменённый адрес не найдёт кода у себя.
- **Занятая почта не перезаписывается.** Привязка — только на пустое место,
  даже когда код верный.

Зависимости подставные: ни сети, ни базы, всё на моке (W0–W5).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from cabinet.accounts import Account
from cabinet.app import LINK_REFUSED_ERROR, build_app
from cabinet.mail_sender import MailSender
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
CHAT_ID = 123_456_789
# Адрес, который привязывается в большинстве тестов. Он не совпадает ни с одним
# адресом из фикстур: привязка «своего» адреса проверяла бы меньше.
NEW_MAIL = "new@example.com"

# Адрес подтверждения и текст успеха записаны здесь строками, а не взяты из
# `cabinet.app`: на момент RED этих имён ещё нет, и импорт уронил бы **весь файл**
# на сборе — вместе с теми, что к делу не относятся. Прогон, который не смог
# собраться, не говорит ничего ни об одном тесте.
_CONFIRM_PATH = "/account/link/mail/confirm"
LINKED_TEXT = "Почта привязана"
_COOKIE = "cabinet_session"


@pytest.fixture
def sender() -> FakeMailSender:
    return FakeMailSender()


@pytest.fixture
def client(fake_api: FakeApi, sender: FakeMailSender) -> TestClient:
    """Клиент на подставных зависимостях.

    `raise_server_exceptions=False`: необработанный отказ обязан прийти как 500, а
    не как исключение наружу, — иначе тест «не 500» на повторе отправки видел бы
    стектрейс и не отличил бы сбой от ответа.
    """
    app = build_app(
        store=fake_api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.app_state = app.state  # type: ignore[attr-defined]
    return client


def _signed_in_telegram_only(client: TestClient, fake_api: FakeApi) -> Account:
    """Аккаунт из Telegram и его сессия — состояние, ради которого почту привязывают.

    Токен берётся у самого приложения, а не подставляется строкой: подставленный
    cookie был бы неотличим от поддельного, и тест не сказал бы, что нашёл аккаунт
    по сессии.
    """
    account = fake_api.create_account(chat_id=CHAT_ID)
    client.cookies.set(_COOKIE, client.app_state.sessions.sign_in(account.id).token)  # type: ignore[attr-defined]
    return account


def _code_for(client: TestClient, sender: FakeMailSender, mail: str) -> str:
    """Запросить код на адрес тем же путём, что человек, и прочитать его в письме.

    Код вынимается из письма, а не придумывается: тест не должен знать его
    заранее — иначе он проверял бы согласие теста с тестом.
    """
    client.post("/account/link/mail", data={"mail": mail}, follow_redirects=False)
    body = sender.sent_to(mail)[-1]["body"]
    return "".join(ch for ch in body if ch.isdigit())[:6]


def _wrong_code(real: str) -> str:
    """Отличающийся код той же формы: «000000» мог бы случайно оказаться верным."""
    return "".join("9" if digit != "9" else "1" for digit in real)


def _confirm(client: TestClient, mail: str, code: str):
    return client.post(
        _CONFIRM_PATH,
        data={"mail": mail, "code": code},
        follow_redirects=False,
    )


# --- 1. код отправлен — код принимается ------------------------------------


def test_sending_the_code_opens_the_code_step(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """После отправки на экране есть поле кода и адрес, на который он ушёл.

    Письмо без поля для кода — тупик: именно это и было дефектом. Проверяется и
    адрес: человек, запросивший код на один ящик, должен видеть, на какой.
    """
    _signed_in_telegram_only(client, fake_api)

    response = client.post("/account/link/mail", data={"mail": NEW_MAIL}, follow_redirects=False)

    assert response.status_code == 200
    assert 'name="code"' in response.text
    assert NEW_MAIL in response.text


def test_sending_the_code_does_not_promise_the_bot_finishes_it(
    client: TestClient, fake_api: FakeApi
) -> None:
    """Экран не обещает шаг, которого нет ни здесь, ни в боте.

    Бывший текст («ввод кода ещё не подключён — подтверждение адреса завершается
    в боте») отправлял человека ждать в бота то, что бот сделать не может: код
    выдаёт и проверяет **кабинет**. Строка удаляется вместе с шагом, и тест
    держит именно это: обещание не должно вернуться вместе с текстом.
    """
    _signed_in_telegram_only(client, fake_api)

    response = client.post("/account/link/mail", data={"mail": NEW_MAIL}, follow_redirects=False)

    assert "завершается в боте" not in response.text
    assert "ещё не подключён" not in response.text


def test_confirming_with_the_code_links_the_address(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """Верный код **привязывает почту** — проверяется запись, а не ответ 200.

    Тест, довольный кодом ответа, зеленел бы и на роуте, который рисует экран и
    ничего не меняет. Поэтому сверяется аккаунт в хранилище, а Telegram остаётся
    нетронутым: привязка добавляет способ входа, а не заменяет его.
    """
    account = _signed_in_telegram_only(client, fake_api)
    code = _code_for(client, sender, NEW_MAIL)

    response = _confirm(client, NEW_MAIL, code)

    assert response.status_code == 200
    after = fake_api.find_by_id(account.id)
    assert after.mail == NEW_MAIL
    assert after.mail_verified_at is not None  # инвариант 2: почта без отметки не существует
    assert after.chat_id == CHAT_ID
    assert LINKED_TEXT in response.text


def test_confirming_links_the_normalised_address(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """Адрес с большой буквы привязывается строчными — один ящик, одна запись.

    Иначе `User@Example.com` в записи и `user@example.com` при входе разошлись бы,
    и человек завёл бы себе второй аккаунт на тот же ящик (`signin._normalize_mail`).
    """
    account = _signed_in_telegram_only(client, fake_api)
    code = _code_for(client, sender, NEW_MAIL)

    _confirm(client, "New@Example.COM", code)

    assert fake_api.find_by_id(account.id).mail == NEW_MAIL


# --- 2. код не принят — почта не привязана ---------------------------------


def test_wrong_code_does_not_link_the_address(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """Неверный код — отказ на экране, и поле кода остаётся на месте.

    Пара к тесту выше: без неё «привязка работает» ничего не говорит о том, что
    она не срабатывает на любом коде. Поле кода обязано остаться — иначе отказ
    выглядел бы тупиком, а человек с письмом на руках остался бы без шага.
    """
    account = _signed_in_telegram_only(client, fake_api)
    real = _code_for(client, sender, NEW_MAIL)

    response = _confirm(client, NEW_MAIL, _wrong_code(real))

    assert response.status_code == 200
    assert fake_api.find_by_id(account.id).mail is None
    assert 'name="code"' in response.text


def test_code_issued_for_another_address_does_not_link_this_one(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """Код, выданный на чужой адрес, не привязывает подставленный в форму.

    Форма несёт адрес скрытым полем, и подменить его может кто угодно. Привязки
    здесь нет потому, что код хранится **по адресу**: у подставленного адреса
    кода нет вовсе. Это тот же разбор, что у W3.4, но на уровне HTTP-слоя: шаг
    должен быть защищён не тем, что поле не видно, а тем, что проверяется.
    """
    account = _signed_in_telegram_only(client, fake_api)
    other = "attacker@example.com"
    code = _code_for(client, sender, other)

    response = _confirm(client, NEW_MAIL, code)

    assert response.status_code == 200
    assert fake_api.find_by_id(account.id).mail is None


def test_confirming_without_requesting_a_code_is_refused(
    client: TestClient, fake_api: FakeApi
) -> None:
    """Кода не запрашивали — привязывать нечем, и это не 500.

    Заявка на привязку без письма: адрес назван, кода у сервера нет. Отказ, а не
    «привязали по заявлению» — иначе шаг стал бы проще входа (§3.3).
    """
    account = _signed_in_telegram_only(client, fake_api)

    response = _confirm(client, NEW_MAIL, "123456")

    assert response.status_code == 200
    assert fake_api.find_by_id(account.id).mail is None
    # И отказ виден на экране: без этой строки подошёл бы и роут, который
    # отвечает 200 и молчит, — а молчание человек читает как сбой страницы,
    # а не как «код не подошёл».
    assert "Код не подошёл" in response.text


def test_confirming_does_not_overwrite_an_existing_address(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """Занятая почта не перезаписывается, даже когда код верный (инвариант 3).

    Обратный случай к «привязка работает»: правило «только на пустое место» —
    то, что не даёт знанию чужого адреса сменить вход в аккаунт. Проверяется при
    верном коде: перезапись обязана упереться не в проверку кода, а в занятость.
    """
    account = fake_api.create_account(
        chat_id=CHAT_ID, mail="first@example.com", mail_verified_at=NOW
    )
    client.cookies.set(_COOKIE, client.app_state.sessions.sign_in(account.id).token)  # type: ignore[attr-defined]
    code = _code_for(client, sender, NEW_MAIL)

    response = _confirm(client, NEW_MAIL, code)

    assert response.status_code == 200
    assert fake_api.find_by_id(account.id).mail == "first@example.com"
    assert LINK_REFUSED_ERROR in response.text


# --- 3. повторная отправка: подсказка, а не 500 ----------------------------


def test_repeat_sending_shows_the_wait_and_keeps_the_code_field(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """Вторая отправка подряд — ожидание N секунд, а не сбой сервера.

    `AuthMail` отвергает слишком частую отправку типом, который HTTP-слой не
    ловил: `RateLimited` — не `SignInError`, и запрос падал с 500. Человек,
    нажавший «ещё раз» (обычное дело — письмо не пришло за десять секунд),
    видел страницу сбоя вместо подсказки. Поле кода при этом остаётся: код из
    первого письма всё ещё действует, и терять шаг нельзя.
    """
    _signed_in_telegram_only(client, fake_api)
    client.post("/account/link/mail", data={"mail": NEW_MAIL}, follow_redirects=False)

    response = client.post("/account/link/mail", data={"mail": NEW_MAIL}, follow_redirects=False)

    assert response.status_code == 200
    assert "Повторите через" in response.text
    assert 'name="code"' in response.text


# --- 4. без сессии — на вход ----------------------------------------------


def test_confirming_without_session_redirects_to_login(
    client: TestClient, sender: FakeMailSender, fake_api: FakeApi
) -> None:
    """Привязка без сессии — редирект на вход, а не привязка чужому аккаунту.

    Шаг привязывает к **вошедшему** аккаунту, и аккаунт берётся из сессии, а не
    из формы. Без сессии привязывать не к кому.

    Код берётся **до** выхода из аккаунта: письмо просят у того же кабинета, а
    экран отправки без сессии уводит на вход и письма не отправляет. Иначе тест
    падал бы на пустом ящике — то есть проверял бы отправку письма, а не
    привязку. Cookie снимается после: человек вышел (или закрыл браузер), а
    форма осталась открытой — ровно тот случай, ради которого сессия
    проверяется на сервере, а не в разметке.
    """
    account = _signed_in_telegram_only(client, fake_api)
    code = _code_for(client, sender, NEW_MAIL)
    client.cookies.clear()

    response = _confirm(client, NEW_MAIL, code)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    # И адрес не привязался: редирект без записи — это не «привязали, но не
    # показали». Без этой строки тест зеленел бы и на роуте, который успевает
    # записать почту, а потом уводит на вход.
    assert fake_api.find_by_id(account.id).mail is None
