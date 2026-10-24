"""#55: отказ входа — состояние экрана, а не текст в адресе.

Владелец попросил код дважды за минуту и на второй запрос получил
`303 → /login?error=<текст>&mail=<адрес>`. Оттуда два дефекта, и оба здесь
закрыты проверкой:

1. **Отказ уехал в адрес.** Текст отказа — состояние экрана, а не ссылка:
   перезагрузка показывала старый отказ снова, а адрес дублировался в строке
   браузера. Тесты требуют, чтобы ответ-отказ вёл на голый `/login` (и
   `/login/waiting` для кода) и не нёс `error=`, а перезагрузка отказ **не**
   повторяла.
2. **Ожидания не было видно.** Лимит сам по себе верен (`AuthMail`: 60 с между
   отправками, 5 в час), но экран не показывал, сколько ждать, — человек жал
   снова и получал тот же ответ. Тесты требуют, чтобы число секунд было на
   странице, а ожидание было **конечным**: по сдвигу поданных часов отправка
   снова проходит.

Часы — зависимость (`build_app(now=…)`), поэтому ни один тест не спит: время
идёт сдвигом `Clock`, как в `test_auth_mail`. Почта — `FakeMailSender`: письма
не уходят наружу, код читается из `outbox`.

Раздел 6 добавлен позже, по ревью (issue #58): у #55 остались три места, и
одно из них — того же класса, что сама #55. Читатель `error=` в адресе уцелел
без писателя (ссылка с чужим текстом показывала этот текст на нашей странице),
экран кода показывал отказ по лимиту дважды, а остаток меньше секунды давал
«Повторите через 0 с», хотя лимит ещё действовал.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cabinet.app import CODE_ERROR, COOKIE_NAME, LOGIN_MAIL_REQUIRED, build_app
from cabinet.auth_mail import MAX_ATTEMPTS, RESEND_INTERVAL_SECONDS
from cabinet.mail_sender import DEFAULT_CODE_TTL_MINUTES, MailSender
from cabinet.sessions import DEFAULT_SESSION_TTL
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"

# Начало текста отказа по лимиту. Число секунд живёт отдельной меткой отсчёта,
# поэтому целой фразы с числом на странице ждать нельзя.
RATE_LIMITED_PREFIX = "Слишком часто"


class Clock:
    """Управляемые часы. Ожидание проверяется сдвигом времени, а не сном."""

    def __init__(self, start: datetime) -> None:
        self._moment = start

    def __call__(self) -> datetime:
        return self._moment

    def advance(self, seconds: float = 0) -> None:
        self._moment += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def sender() -> FakeMailSender:
    return FakeMailSender()


@pytest.fixture
def client(fake_api: FakeApi, sender: FakeMailSender, clock: Clock) -> TestClient:
    app = build_app(
        store=fake_api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        now=clock,
    )
    # `raise_server_exceptions=False`: 500 обязан прийти как 500, а не как
    # исключение наружу, — иначе тест «отказ, а не 500» проверял бы стектрейс.
    return TestClient(app, raise_server_exceptions=False)


def _code(sender: FakeMailSender, mail: str = MAIL) -> str:
    body = sender.sent_to(mail)[-1]["body"]
    return "".join(ch for ch in body if ch.isdigit())[:6]


def _wrong_code(right: str) -> str:
    """Код, заведомо не совпадающий с выданным."""
    return "000000" if right != "000000" else "111111"


def _refusal_screen(client: TestClient, response) -> str:
    """Экран отказа: адрес без `error=`, текст — со страницы, а не из ссылки."""
    assert response.status_code == 303, "отказ — состояние экрана, а не сбой HTTP"
    location = response.headers["location"]
    assert "error=" not in location, f"текст отказа уехал в адрес: {location}"
    screen = client.get(location)
    assert screen.status_code == 200
    return screen.text


# --- 1. лимит виден и в ответе то же состояние ----------------------------


def test_second_rapid_request_is_refused_as_a_screen_state(
    client: TestClient, sender: FakeMailSender
) -> None:
    """Вторая отправка подряд — отказ экраном, а не 500, и число секунд видно.

    Пара к «первый запрос уходит»: без неё тест зеленел бы и на коде, который
    шлёт письмо на каждый запрос без всякого лимита.
    """
    first = client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    assert first.headers["location"].startswith("/login/waiting")
    assert len(sender.sent_to(MAIL)) == 1

    second = client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    screen = _refusal_screen(client, second)

    assert RATE_LIMITED_PREFIX in screen
    # Число секунд ожидания — на странице, а не только в ответе сервера.
    assert f'data-resend-wait="{RESEND_INTERVAL_SECONDS}"' in screen
    assert "data-resend-countdown" in screen
    # Письма не добавилось: отказ не отправил второе.
    assert len(sender.sent_to(MAIL)) == 1


def test_double_submit_behaves_the_same(client: TestClient) -> None:
    """Двойная отправка формы — тот же отказ, а не два письма и не сбой.

    Так выглядит нажатие кнопки дважды или перезагрузка формы POST-ом: оба
    запроса обязаны дать одно состояние экрана, и адрес у обоих — голый.
    """
    first = client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    second = client.post("/login", data={"mail": MAIL}, follow_redirects=False)

    assert first.status_code == second.status_code == 303
    assert second.headers["location"] == "/login"
    screen = _refusal_screen(client, second)
    assert RATE_LIMITED_PREFIX in screen


# --- 2. отказ не в адресе -------------------------------------------------


def test_refusal_location_is_a_bare_login(client: TestClient) -> None:
    """Адрес отказа — `/login`, без `error=` и без адреса почты.

    Это и есть дефект владельца: текст и адрес в ссылке. Здесь проверяется, что
    ссылка осталась ровно `/login` — перезагрузка не воспроизведёт отказ.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    refused = client.post("/login", data={"mail": MAIL}, follow_redirects=False)

    assert refused.headers["location"] == "/login"


def test_reload_does_not_reshow_the_refusal(client: TestClient) -> None:
    """Перезагрузка не повторяет старый отказ — он одноразовый.

    Первый GET показывает отказ и гасит его; второй GET — чистая форма. Иначе
    человек, обновив страницу, читал бы отказ, которого уже нет.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)

    shown_once = client.get("/login")
    assert RATE_LIMITED_PREFIX in shown_once.text

    reloaded = client.get("/login")
    assert RATE_LIMITED_PREFIX not in reloaded.text


def test_empty_address_refusal_also_leaves_the_url(client: TestClient) -> None:
    """Пустой адрес — тот же путь: отказ состоянием, а не текстом в ссылке.

    Раньше этот отказ возвращался на `/login?error=…&mail=…`, и он же был
    единственным, что проверялось в `test_app`. Теперь текст общий для всех
    отказов экрана, и в адрес он не попадает — ни здесь, ни у лимита.
    """
    refused = client.post("/login", data={"mail": "   "}, follow_redirects=False)

    screen = _refusal_screen(client, refused)
    assert LOGIN_MAIL_REQUIRED in screen


# --- 3. один текст на три состояния кода ---------------------------------


def test_wrong_expired_and_exhausted_share_one_refusal(
    client: TestClient, sender: FakeMailSender, clock: Clock
) -> None:
    """Неверный, истёкший и исчерпанный код — один текст отказа.

    `AuthMail` различает эти состояния (и подсказка «осталось N попыток» у
    неверного), но наружу из кабинета уходит **одно** сообщение: иначе по
    разнице ответов выяснялось бы состояние чужого ящика (§3.3). Проверяются
    все три, и текст у них обязан совпасть.
    """
    # Неверный код: попытки ещё остались.
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    wrong = _wrong_code(_code(sender))
    rejected = client.post(
        "/login/waiting", data={"mail": MAIL, "code": wrong}, follow_redirects=False
    )
    wrong_text = _refusal_screen(client, rejected)

    # Истёкший код: срок вышел, цифры уже не значат ничего.
    clock.advance(RESEND_INTERVAL_SECONDS + 1)
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    expired_code = _code(sender)
    clock.advance(DEFAULT_CODE_TTL_MINUTES * 60 + 1)
    expired = client.post(
        "/login/waiting", data={"mail": MAIL, "code": expired_code}, follow_redirects=False
    )
    expired_text = _refusal_screen(client, expired)

    # Исчерпанный код: N-я неудачная попытка гасит его целиком.
    clock.advance(RESEND_INTERVAL_SECONDS + 1)
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    exhausted_code = _wrong_code(_code(sender))
    exhausted = None
    for _ in range(MAX_ATTEMPTS):
        exhausted = client.post(
            "/login/waiting",
            data={"mail": MAIL, "code": exhausted_code},
            follow_redirects=False,
        )
    exhausted_text = _refusal_screen(client, exhausted)

    for text in (wrong_text, expired_text, exhausted_text):
        assert CODE_ERROR in text
    assert wrong_text == expired_text == exhausted_text


# --- 4. поддельная и истёкшая сессия --------------------------------------


def test_forged_and_expired_session_cookie_redirect_to_login(
    client: TestClient, sender: FakeMailSender, clock: Clock
) -> None:
    """Поддельный и истёкший токен ведут на вход, а не роняют экран.

    Оба состояния — обычный первый заход и истёкший вход, а не сбой сервера:
    поддельный токен не найден в хранилище, истёкший погашен. Оба обязаны
    ответить редиректом на `/login`.
    """
    client.cookies.set(COOKIE_NAME, "not-a-real-token")
    forged = client.get("/subscriptions", follow_redirects=False)
    assert forged.status_code == 303
    assert forged.headers["location"] == "/login"

    # Живая сессия, которой дали истечь: срок двигаем часами, а не ожиданием.
    client.cookies.clear()
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    client.post(
        "/login/waiting",
        data={"mail": MAIL, "code": _code(sender)},
        follow_redirects=False,
    )
    clock.advance(int(DEFAULT_SESSION_TTL.total_seconds()) + 1)

    expired = client.get("/subscriptions", follow_redirects=False)
    assert expired.status_code == 303
    assert expired.headers["location"] == "/login"


# --- 5. ожидание конечно --------------------------------------------------


def test_send_works_again_once_the_wait_elapses(
    client: TestClient, sender: FakeMailSender, clock: Clock
) -> None:
    """Через паузу отправка снова проходит — ожидание не вечное.

    Число секунд на экране конечно, и это проверяется его же часами: сдвиг на
    `RESEND_INTERVAL_SECONDS + 1` превращает отказ в успех. Без этого теста
    экран мог бы показывать отсчёт, за которым ничего не меняется.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    refused = client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    assert refused.headers["location"] == "/login"

    clock.advance(RESEND_INTERVAL_SECONDS + 1)
    again = client.post("/login", data={"mail": MAIL}, follow_redirects=False)

    assert again.status_code == 303
    assert again.headers["location"].startswith("/login/waiting")
    assert len(sender.sent_to(MAIL)) == 2


# --- 6. остатки #55 (issue #58) -------------------------------------------


def test_refusal_text_from_the_url_is_not_shown(client: TestClient) -> None:
    """Текст отказа из адреса не показывается: у читателя нет писателя.

    До #55 отказ ездил ссылкой, и параметр `error` был писателем. После #55
    писателя убрали, а читателя оставили — и `GET /login?error=<чужой текст>`
    показывал этот текст на нашей странице. Автоэкранирование от XSS спасало,
    но не от подделки: ссылку с чужим текстом можно выдать за нашу.

    Проверка именно на **чтение**: параметр в адресе не должен ничего менять.
    Текст взят заведомо чужой — такой, какого кабинет сам не пишет никогда.
    """
    forged = "Свяжитесь с поддержкой по телефону +7 000 000 00 00"

    assert forged not in client.get("/login", params={"error": forged}).text
    assert forged not in client.get("/login/waiting", params={"error": forged}).text


def test_rate_limit_text_appears_once_on_the_code_screen(
    client: TestClient, sender: FakeMailSender
) -> None:
    """Отказ по лимиту на экране кода читается **один** раз, а не дважды.

    `_waiting_refusal` кладёт и готовую фразу, и число секунд. Экран показывал
    фразу замороженной (в отказе кода) **и** ту же фразу живым отсчётом — рядом,
    и они расходились числом. Фраза должна остаться в одном месте: там, где
    рядом отсчёт, то есть у формы повторной отправки.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    refused = client.post("/login/resend", data={"mail": MAIL}, follow_redirects=False)

    screen = _refusal_screen(client, refused)
    assert screen.count(RATE_LIMITED_PREFIX) == 1
    assert screen.count("Повторите через") == 1
    # Отсчёт ровно один. Считается открывающая метка самого числа, а не имя
    # атрибута: строка `data-resend-countdown` встречается ещё и в скрипте,
    # который по нему ищет метку, и по ней две штуки получились бы и на верной
    # странице. Два отсчёта — это два расходящихся числа, и их быть не должно.
    assert screen.count('<b class="mono" data-resend-countdown') == 1


def test_wait_is_never_zero_seconds(
    client: TestClient, sender: FakeMailSender, clock: Clock
) -> None:
    """Остаток меньше секунды показывается как «1 с», а не как «0 с».

    `int()` от остатка 0.5 давал ноль, а ноль — это уже «ждать не надо»,
    которого на экране не должно быть, пока отправка отвергается: человек либо
    читает «Повторите через 0 с» и не понимает, почему снова отказ, либо (с
    `_notice_int`, который ноль считает отсутствием отсчёта) теряет отсчёт
    вовсе. Округление вверх оставляет ожидание и видимым, и конечным.
    """
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    # Тот край окна, где `int` отдавал ноль: до конца интервала — полсекунды.
    clock.advance(RESEND_INTERVAL_SECONDS - 0.5)

    refused = client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    screen = client.get(refused.headers["location"]).text

    assert 'data-resend-wait="1"' in screen
    assert "Повторите через 0" not in screen
    # Письма не прибавилось: отказ остался отказом, а не превратился в отправку.
    assert len(sender.sent_to(MAIL)) == 1
