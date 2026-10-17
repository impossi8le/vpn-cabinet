"""Сценарии входа: почта и Telegram (W2.3, W2.4, W3.1).

Главное, что здесь проверяется, — не «пускает ли по коду», а то, что **все двери
ведут к одному `account_id`**. Аккаунт, заведённый по почте (без `chat_id`), и
аккаунт из Telegram дают валидные сессии одного владельца: человек, вошедший
разными способами, остаётся одним человеком, и его подписки не раздваиваются.

На каждый успешный путь есть парный отказ. Так тест на успех не зеленеет от кода,
который пускает вообще всех.

Границы: мок (`tests/fakes.py`) и подставная почта, ни сети, ни базы.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cabinet.accounts import Account
from cabinet.auth_mail import CODE_LENGTH, RESEND_INTERVAL_SECONDS, AuthMail, RateLimited
from cabinet.linking import Linking
from cabinet.mail_sender import MailSender, MailTransportError
from cabinet.sessions import SessionService, SingleSessionStore
from cabinet.signin import MailSignInError, SignInError, SignInService
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"


class Clock:
    """Подвижные часы.

    Одному тесту иногда нужно **два** кода на один адрес (второй вход по почте), а
    `AuthMail` не даёт запросить их подряд: между отправками стоит пауза против
    рассылки. Ждать её в тесте нельзя, поэтому время двигается само — так же, как
    у человека, который просто переждал минуту.
    """

    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def auth_mail(fake_mail: FakeMailSender, clock: Clock) -> AuthMail:
    sender = MailSender(sender="cabinet@example.com", transport=fake_mail)
    return AuthMail(mail_sender=sender, now=clock)


@pytest.fixture
def sessions(fake_api: FakeApi, clock: Clock) -> SessionService:
    return SessionService(store=SingleSessionStore(), accounts=fake_api, now=clock)


@pytest.fixture
def signin(fake_api: FakeApi, auth_mail: AuthMail, sessions: SessionService) -> SignInService:
    return SignInService(
        store=fake_api,
        auth_mail=auth_mail,
        sessions=sessions,
        linking=Linking(store=fake_api, auth_mail=auth_mail),
    )


@pytest.fixture
def mail_code(auth_mail: AuthMail, fake_mail: FakeMailSender, clock: Clock):
    """Запросить код и достать его из подставной почты.

    Код берётся из письма, а не подставляется константой: тест обязан пройти тот
    же путь, что человек, — иначе он проверял бы приём любого кода.

    Перед повторным запросом на тот же адрес время сдвигается за паузу между
    отправками: без этого `AuthMail` откажет `RateLimited`, и тест проверял бы
    лимит вместо входа. Считаем отправки сами, а не спрашиваем `has_pending`:
    удачно использованный код из ожидания исчезает, а пауза на адрес остаётся.
    """
    sent_to: set[str] = set()

    def request(mail: str = MAIL) -> str:
        address = mail.strip().lower()
        if address in sent_to:
            clock.advance(RESEND_INTERVAL_SECONDS)
        sent_to.add(address)
        auth_mail.request_code(mail)
        body = fake_mail.sent_to(mail)[-1]["body"]
        return "".join(ch for ch in body if ch.isdigit())[:CODE_LENGTH]

    return request


# --- W2.3: регистрация по почте от начала до конца ------------------------


def test_registration_by_mail_creates_account_without_chat_id(
    signin: SignInService, fake_api: FakeApi, mail_code
):
    """Полный путь: почта → код → аккаунт с `chat_id = NULL`.

    Это главный случай проекта: человек за границей, Telegram недоступен, почта —
    единственная дверь. Аккаунт обязан завестись без `chat_id`, иначе войти
    некому.
    """
    code = mail_code()

    result = signin.sign_in_with_mail(MAIL, code)

    assert result.created is True
    assert result.account.mail == MAIL
    assert result.account.mail_verified_at is not None
    assert result.account.chat_id is None
    assert result.session.account_id == result.account.id
    assert fake_api.find_by_mail(MAIL).id == result.account.id


def test_registration_marks_the_account_as_verified(signin: SignInService, mail_code):
    """Почта заводится только с отметкой подтверждения (инвариант 2).

    Отметка берётся у тех же часов, что проверяли код: иначе подтверждение
    датировалось бы моментом, который разошёлся с моментом проверки.
    """
    code = mail_code()

    result = signin.sign_in_with_mail(MAIL, code)

    assert result.account.mail_verified_at == NOW


def test_wrong_code_does_not_create_an_account(signin: SignInService, fake_api: FakeApi, mail_code):
    """Пара к успешной регистрации: неверный код — аккаунта нет.

    Иначе заявленный в браузере адрес создавал бы запись, и pre-account
    hijacking (§3.3) открылся бы на шаге, где раньше его не было.
    """
    real = mail_code()
    wrong = "".join("9" if digit != "9" else "1" for digit in real)

    with pytest.raises(MailSignInError):
        signin.sign_in_with_mail(MAIL, wrong)

    assert fake_api.find_by_mail(MAIL) is None


def test_code_for_one_address_does_not_open_another(
    signin: SignInService, fake_api: FakeApi, mail_code
):
    """Код подтверждает **тот адрес**, на который отправлен.

    Код берётся из письма жертве и предъявляется со своим адресом — это и есть
    проверка владения ящиком. Без неё код был бы просто пропуском.
    """
    code = mail_code("victim@example.com")

    with pytest.raises(MailSignInError):
        signin.sign_in_with_mail("attacker@example.com", code)

    assert fake_api.find_by_mail("attacker@example.com") is None


def test_used_code_does_not_work_twice(signin: SignInService, mail_code):
    """Код одноразовый: повтор того же кода — отказ."""
    code = mail_code()
    signin.sign_in_with_mail(MAIL, code)

    with pytest.raises(MailSignInError):
        signin.sign_in_with_mail(MAIL, code)


def test_verification_failure_keeps_attempts_left(signin: SignInService, mail_code):
    """Подсказка «осталось N попыток» не теряется по дороге наверх.

    `AuthMail` её знает, экран показывает по ней — на пути между ними она не
    должна пропасть.
    """
    real = mail_code()
    wrong = "".join("9" if digit != "9" else "1" for digit in real)

    with pytest.raises(MailSignInError) as excinfo:
        signin.sign_in_with_mail(MAIL, wrong)

    assert excinfo.value.attempts_left is not None
    assert excinfo.value.attempts_left > 0


# --- W2.4: вход по почте не заводит второй аккаунт ------------------------


def test_second_mail_login_does_not_create_another_account(
    signin: SignInService, fake_api: FakeApi, mail_code
):
    """Повторный вход — вход, а не регистрация.

    У человека с одной почтой аккаунт обязан быть один: иначе подписки,
    привязанные к первому, исчезали бы при втором входе.
    """
    first = signin.sign_in_with_mail(MAIL, mail_code())

    second = signin.sign_in_with_mail(MAIL, mail_code())

    assert second.created is False
    assert second.account.id == first.account.id
    assert len(fake_api.accounts) == 1


def test_second_login_issues_a_new_session(signin: SignInService, mail_code):
    """Каждый вход — своя сессия: повторный вход не переиспользует прошлый токен."""
    first = signin.sign_in_with_mail(MAIL, mail_code())

    second = signin.sign_in_with_mail(MAIL, mail_code())

    assert first.session.token != second.session.token


def test_mail_is_matched_case_insensitively(signin: SignInService, fake_api: FakeApi, mail_code):
    """`User@Example.COM` и `user@example.com` — один аккаунт.

    Иначе регистр в форме заводил бы второй аккаунт на тот же ящик, и человек
    видел бы то одни подписки, то другие — в зависимости от того, как набрал
    адрес.
    """
    first = signin.sign_in_with_mail(MAIL, mail_code())

    second = signin.sign_in_with_mail("User@Example.COM", mail_code("user@example.com"))

    assert second.account.id == first.account.id
    assert len(fake_api.accounts) == 1


# --- W3.1: вход по Telegram ----------------------------------------------


def test_telegram_login_uses_the_account_id_from_the_bot(
    signin: SignInService, fake_api: FakeApi, account_telegram_only: Account
):
    """Обычный путь: бот вернул `account_id` рядом с токеном (§5) — доверяем ему.

    Решение «нашли или завели» принято ботом там, где есть подтверждение
    `chat_id`; кабинет его не перепроверяет и не заводит второй аккаунт.
    """
    result = signin.sign_in_with_telegram(account_telegram_only.chat_id, account_id=1)

    assert result.account.id == account_telegram_only.id
    assert result.session.account_id == account_telegram_only.id
    assert len(fake_api.accounts) == 1


def test_telegram_login_finds_the_account_when_the_bot_did_not_say(
    signin: SignInService, fake_api: FakeApi, account_telegram_only: Account
):
    """Если бот не отдал `account_id` — ищем по `chat_id` сами."""
    result = signin.sign_in_with_telegram(account_telegram_only.chat_id)

    assert result.account.id == account_telegram_only.id
    assert result.created is False
    assert len(fake_api.accounts) == 1


def test_telegram_login_creates_an_account_when_none_exists(
    signin: SignInService, fake_api: FakeApi
):
    """Нет аккаунта — заводим. При подтверждённом `chat_id` это безопасно:
    подтверждение уже произошло, и `chat_id` доказан человеком."""
    result = signin.sign_in_with_telegram(987_654_321)

    assert result.created is True
    assert result.account.chat_id == 987_654_321
    assert result.account.mail is None


def test_telegram_login_for_unknown_account_is_refused(signin: SignInService):
    """`account_id` от бота указывает в пустоту — отказ, а не сессия «в никуда»."""
    with pytest.raises(SignInError):
        signin.sign_in_with_telegram(123_456_789, account_id=4_242)


# --- главное свойство: обе двери — один account_id ------------------------


def test_both_doors_lead_to_the_same_account(
    signin: SignInService,
    fake_api: FakeApi,
    sessions: SessionService,
    auth_mail: AuthMail,
    mail_code,
):
    """Ключевое свойство волны.

    Человек регистрируется по почте, получает сессию, привязывает Telegram — и
    входит через Telegram. Обе сессии обязаны вести к **одному** `account_id`:
    иначе подписки раздвоятся, а выход в одной двери не выкинет из другой.
    """
    by_mail = signin.sign_in_with_mail(MAIL, mail_code())

    # Привязка Telegram к этому же аккаунту — как после подтверждения в боте.
    Linking(store=fake_api, auth_mail=auth_mail).link_telegram(
        by_mail.account_id, chat_id=555_000_111, confirmed=True
    )

    by_telegram = signin.sign_in_with_telegram(555_000_111)

    assert by_mail.account_id == by_telegram.account_id
    assert len(fake_api.accounts) == 1
    # Обе сессии живы и указывают на один аккаунт.
    assert sessions.resolve(by_mail.session.token).account_id == by_telegram.account_id
    assert sessions.resolve(by_telegram.session.token).account_id == by_mail.account_id


def test_session_of_mail_account_without_telegram_is_valid_and_alone(
    signin: SignInService, sessions: SessionService, mail_code
):
    """Сессия для аккаунта без `chat_id` валидна сама по себе.

    Без Telegram аккаунт не становится «неполным»: сессия выдана, действует и
    ведёт к тому же `account_id`, что и любая другая.
    """
    result = signin.sign_in_with_mail(MAIL, mail_code())

    resolved = sessions.resolve(result.session.token)

    assert resolved is not None
    assert resolved.account_id == result.account.id
    assert result.account.chat_id is None


# --- привязка почты к вошедшему аккаунту ---------------------------------


def test_link_mail_delegates_to_linking(
    signin: SignInService, account_telegram_only: Account, mail_code
):
    """Привязка почты — тот же сценарий с кодом, и он не переписан здесь заново.

    Модуль вызывает `Linking`, а не повторяет его проверки: «привязка не проще
    входа» остаётся правдой в одном месте (`linking.py`), а не в двух.
    """
    code = mail_code()

    result = signin.link_mail(account_telegram_only.id, MAIL, code)

    assert result.account.mail == MAIL
    assert result.account.chat_id == account_telegram_only.chat_id


def test_link_mail_without_linking_is_refused(
    fake_api: FakeApi, auth_mail: AuthMail, sessions: SessionService
):
    """Без связывания вызов — ошибка развёртывания, а не состояние сценария."""
    service = SignInService(store=fake_api, auth_mail=auth_mail, sessions=sessions)

    with pytest.raises(SignInError):
        service.link_mail(1, MAIL, "123456")


# --- запрос кода ---------------------------------------------------------


def test_requesting_the_code_sends_a_letter(
    signin: SignInService, fake_mail: FakeMailSender, auth_mail: AuthMail
):
    """Запрос кода доставляет письмо: без него форма входа нерабочая."""
    signin.request_mail_code(MAIL)

    assert auth_mail.has_pending(MAIL)
    assert fake_mail.sent_to(MAIL)


def test_rate_limit_keeps_its_retry_hint(signin: SignInService):
    """Слишком частый запрос проходит как `RateLimited`, а не как общий отказ.

    У него есть `retry_after_seconds`, который экран показывает («попробуйте
    через минуту»); свести его к `SignInError` значило бы потерять именно эту
    подсказку.
    """
    signin.request_mail_code(MAIL)

    with pytest.raises(RateLimited) as excinfo:
        signin.request_mail_code(MAIL)

    assert excinfo.value.retry_after_seconds > 0


def test_transport_failure_is_our_refusal_not_a_mail_error(
    fake_api: FakeApi, sessions: SessionService
):
    """Сбой транспорта — отказ операции, а не тип почтового слоя наружу.

    Вызывающий код кабинета не должен зависеть от того, что под ним: иначе
    подмена `MailSender` на другой транспорт ломала бы обработку на экране.
    """

    class BrokenMail(FakeMailSender):
        def send(self, *, to: str, subject: str, body: str) -> None:
            raise MailTransportError("транспорт недоступен")

    auth_mail = AuthMail(mail_sender=MailSender(sender="c@example.com", transport=BrokenMail()))
    service = SignInService(store=fake_api, auth_mail=auth_mail, sessions=sessions)

    with pytest.raises(SignInError) as excinfo:
        service.request_mail_code(MAIL)

    assert not isinstance(excinfo.value, MailSignInError)
