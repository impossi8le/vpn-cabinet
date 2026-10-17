"""Коды на почту: выдача, TTL, лимиты (W2.2).

Тест написан раньше кода. Проверяется ровно то, ради чего модуль существует:
истёкший код не пускает, повторное использование кода не проходит, исчерпание
попыток гасит операцию, частота отправки ограничена.

Письма не уходят наружу: транспорт — `FakeMailSender`, код читается из
`outbox`. Живой почты и сети здесь нет.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fakes import FakeMailSender

from cabinet.auth_mail import (
    CODE_LENGTH,
    MAX_ATTEMPTS,
    MAX_SENDS_PER_WINDOW,
    RESEND_INTERVAL_SECONDS,
    AuthMail,
    CodeExhausted,
    CodeExpired,
    CodeRejected,
    NoPendingCode,
    RateLimited,
    generate_code,
)
from cabinet.mail_sender import MailSender, MailTransportError

MAIL = "user@example.com"


class Clock:
    """Управляемые часы. TTL проверяется сдвигом времени, а не ожиданием:
    иначе тест либо тормозит прогон, либо становится недетерминированным."""

    def __init__(self, start):
        self._moment = start

    def __call__(self):
        return self._moment

    def advance(self, seconds: int = 0, *, minutes: int = 0) -> None:
        self._moment += timedelta(seconds=seconds, minutes=minutes)


def build(fake_mail: FakeMailSender, now) -> tuple[AuthMail, Clock]:
    clock = Clock(now)
    sender = MailSender(sender="cabinet@example.com", transport=fake_mail)
    return AuthMail(mail_sender=sender, now=clock), clock


def last_code(fake_mail: FakeMailSender, mail: str = MAIL) -> str:
    """Код из последнего письма. В бою код видит только человек в ящике —
    тест читает его оттуда же, из `outbox`, а не из внутренностей модуля."""
    body = fake_mail.sent_to(mail)[-1]["body"]
    digits = "".join(ch for ch in body if ch.isdigit())
    return digits[:CODE_LENGTH]


# --- выдача и успешная проверка ------------------------------------------


def test_code_is_sent_and_accepted(fake_mail: FakeMailSender, now):
    auth, _ = build(fake_mail, now)

    auth.request_code(MAIL)

    assert len(fake_mail.sent_to(MAIL)) == 1
    auth.verify(MAIL, last_code(fake_mail))  # не бросает


def test_generated_code_is_digits_of_expected_length():
    """Код собирается по цифре, а не из числа: ведущий ноль не теряется."""
    code = generate_code()
    assert len(code) == CODE_LENGTH
    assert code.isdigit()


# --- истёкший код --------------------------------------------------------


def test_expired_code_does_not_let_in(fake_mail: FakeMailSender, now):
    auth, clock = build(fake_mail, now)
    auth.request_code(MAIL)
    code = last_code(fake_mail)

    clock.advance(minutes=11)  # TTL по умолчанию — 10 минут

    with pytest.raises(CodeExpired):
        auth.verify(MAIL, code)


def test_expired_code_is_gone_not_merely_refused(fake_mail: FakeMailSender, now):
    """После истечения код не «принимается позже»: запись снята."""
    auth, clock = build(fake_mail, now)
    auth.request_code(MAIL)
    code = last_code(fake_mail)
    clock.advance(minutes=11)

    with pytest.raises(CodeExpired):
        auth.verify(MAIL, code)
    with pytest.raises(NoPendingCode):
        auth.verify(MAIL, code)


def test_code_is_alive_just_before_it_expires(fake_mail: FakeMailSender, now):
    """Граница с другой стороны: за секунду до срока код ещё рабочий."""
    auth, clock = build(fake_mail, now)
    auth.request_code(MAIL)
    code = last_code(fake_mail)

    clock.advance(seconds=9 * 60 + 59)
    auth.verify(MAIL, code)


# --- одноразовость -------------------------------------------------------


def test_code_cannot_be_used_twice(fake_mail: FakeMailSender, now):
    """Повторное использование кода не проходит: после успеха записи нет."""
    auth, _ = build(fake_mail, now)
    auth.request_code(MAIL)
    code = last_code(fake_mail)

    auth.verify(MAIL, code)
    with pytest.raises(NoPendingCode):
        auth.verify(MAIL, code)


def test_new_code_replaces_the_old_one(fake_mail: FakeMailSender, now):
    """Новый запрос гасит прежний код: иначе на адресе копились бы действующие
    коды, и старые письма оставались бы ключами ко входу."""
    auth, clock = build(fake_mail, now)
    auth.request_code(MAIL)
    first = last_code(fake_mail)
    clock.advance(seconds=RESEND_INTERVAL_SECONDS + 1)

    auth.request_code(MAIL)
    second = last_code(fake_mail)

    assert first != second
    with pytest.raises(CodeRejected):
        auth.verify(MAIL, first)
    auth.verify(MAIL, second)


# --- неверный код и лимит попыток ----------------------------------------


def test_wrong_code_is_refused_with_attempts_left(fake_mail: FakeMailSender, now):
    auth, _ = build(fake_mail, now)
    auth.request_code(MAIL)

    with pytest.raises(CodeRejected) as exc:
        auth.verify(MAIL, "000000" if last_code(fake_mail) != "000000" else "111111")
    assert exc.value.attempts_left == MAX_ATTEMPTS - 1


def test_nth_wrong_attempt_exhausts_the_code(fake_mail: FakeMailSender, now):
    """N-я неудачная попытка гасит операцию — даже верный код после неё не
    принимается. Это и есть предел перебора."""
    auth, _ = build(fake_mail, now)
    auth.request_code(MAIL)
    right = last_code(fake_mail)
    wrong = "000000" if right != "000000" else "111111"

    for _ in range(MAX_ATTEMPTS - 1):
        with pytest.raises(CodeRejected):
            auth.verify(MAIL, wrong)

    with pytest.raises(CodeExhausted):
        auth.verify(MAIL, wrong)

    # Код погашен: правильные цифры уже не пускают.
    with pytest.raises(NoPendingCode):
        auth.verify(MAIL, right)


def test_verifying_without_requesting_is_refused(fake_mail: FakeMailSender, now):
    auth, _ = build(fake_mail, now)
    with pytest.raises(NoPendingCode):
        auth.verify(MAIL, "123456")


# --- лимит частоты отправки ----------------------------------------------


def test_resend_within_interval_is_rate_limited(fake_mail: FakeMailSender, now):
    """Частая отправка на один адрес ограничена: «нажал ещё раз, потому что не
    пришло» не должно превращаться в поток писем чужому ящику."""
    auth, _ = build(fake_mail, now)
    auth.request_code(MAIL)

    with pytest.raises(RateLimited) as exc:
        auth.request_code(MAIL)
    assert exc.value.retry_after_seconds > 0
    assert len(fake_mail.sent_to(MAIL)) == 1


def test_send_limit_per_window_is_enforced(fake_mail: FakeMailSender, now):
    """Потолок писем за окно: даже с паузами больше нельзя, чем разрешено."""
    auth, clock = build(fake_mail, now)

    for _ in range(MAX_SENDS_PER_WINDOW):
        auth.request_code(MAIL)
        clock.advance(seconds=RESEND_INTERVAL_SECONDS + 1)

    with pytest.raises(RateLimited):
        auth.request_code(MAIL)
    assert len(fake_mail.sent_to(MAIL)) == MAX_SENDS_PER_WINDOW


def test_window_slides_so_limit_is_not_permanent(fake_mail: FakeMailSender, now):
    """Окно скользящее: спустя час адрес снова может получить письмо."""
    auth, clock = build(fake_mail, now)
    for _ in range(MAX_SENDS_PER_WINDOW):
        auth.request_code(MAIL)
        clock.advance(seconds=RESEND_INTERVAL_SECONDS + 1)

    clock.advance(seconds=3600)
    auth.request_code(MAIL)
    assert len(fake_mail.sent_to(MAIL)) == MAX_SENDS_PER_WINDOW + 1


def test_limit_is_per_address_not_global(fake_mail: FakeMailSender, now):
    """Лимит считается по адресу: чужой поток запросов не запирает вход
    остальным."""
    auth, _ = build(fake_mail, now)
    auth.request_code(MAIL)

    auth.request_code("other@example.com")  # не бросает


def test_address_is_normalized_for_limits_and_verification(fake_mail: FakeMailSender, now):
    """`User@Example.com` и `user@example.com` — один ящик: иначе лимит
    обходился бы сменой регистра, а код не принимался бы."""
    auth, _ = build(fake_mail, now)
    auth.request_code("User@Example.com")
    code = last_code(fake_mail, "user@example.com")

    auth.verify("user@example.com", code)


# --- сбой транспорта не притворяется доставкой ----------------------------


def test_undelivered_mail_leaves_no_active_code(fake_mail: FakeMailSender, now):
    """Если письмо не ушло — кода нет и попытка не засчитана: человек не должен
    оказаться у формы, код к которой лежит в неотправленном письме."""

    class BrokenTransport:
        def send(self, *, to, subject, body):
            raise MailTransportError("smtp: отправка не удалась")

    clock = Clock(now)
    auth = AuthMail(
        mail_sender=MailSender(sender="cabinet@example.com", transport=BrokenTransport()),
        now=clock,
    )

    with pytest.raises(MailTransportError):
        auth.request_code(MAIL)

    with pytest.raises(NoPendingCode):
        auth.verify(MAIL, "123456")
    # Квота не сожжена: следующий запрос проходит сразу, без паузы.
    auth.mail_sender = MailSender(sender="cabinet@example.com", transport=fake_mail)
    auth.request_code(MAIL)
    assert len(fake_mail.sent_to(MAIL)) == 1


# --- has_pending ---------------------------------------------------------


def test_has_pending_reflects_state(fake_mail: FakeMailSender, now):
    auth, clock = build(fake_mail, now)
    assert auth.has_pending(MAIL) is False

    auth.request_code(MAIL)
    assert auth.has_pending(MAIL) is True

    clock.advance(minutes=11)
    assert auth.has_pending(MAIL) is False
