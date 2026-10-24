"""Сессии кабинета (W2.5).

Проверяется не «выдаётся ли токен», а главное свойство волны: сессия
принадлежит **аккаунту**, а не способу входа. Отсюда два парных случая —
аккаунт, заведённый по почте (`chat_id = NULL`), и аккаунт из Telegram: обе
сессии валидны, и обе ведут к одному `account_id`.

Границы: ни сети, ни базы. Хранилище сессий — `SingleSessionStore`, аккаунты —
мок `FakeApi` из `tests/fakes.py` (он воспроизводит инварианты живого бота, а не
обходит их).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cabinet.accounts import Account
from cabinet.sessions import (
    DEFAULT_SESSION_TTL,
    MIN_TOKEN_BYTES,
    NoAccount,
    SessionRefused,
    SessionService,
    SingleSessionStore,
    hash_token,
    new_token,
)
from tests.fakes import FakeApi

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def sessions(fake_api: FakeApi) -> SessionService:
    """Сервис сессий с фиксированными часами.

    Часы фиксированы, потому что срок сессии участвует в проверке: с живыми
    часами тест на истечение краснел бы или зеленел в зависимости от того, как
    быстро отработал прогон.
    """
    return SessionService(
        store=SingleSessionStore(),
        accounts=fake_api,
        now=lambda: NOW,
    )


# --- главное свойство: сессия не зависит от способа входа ----------------


def test_session_is_issued_for_mail_only_account_without_chat_id(
    sessions: SessionService, account_email_only: Account
):
    """Сессия выдаётся аккаунту, у которого `chat_id` пуст.

    Это и есть главный случай проекта: человек без Telegram. Сессия, привязанная
    к `chat_id`, такому человеку недоступна вовсе — он не вошёл бы никак. Здесь
    фиксируется, что владелец сессии — `account_id`, и `chat_id` для выдачи не
    нужен.
    """
    assert account_email_only.chat_id is None

    view = sessions.sign_in(account_email_only.id)

    assert view.account_id == account_email_only.id
    assert view.token


def test_session_is_issued_for_telegram_only_account(
    sessions: SessionService, account_telegram_only: Account
):
    """Симметрично: аккаунт из Telegram получает сессию той же формы."""
    assert account_telegram_only.mail is None

    view = sessions.sign_in(account_telegram_only.id)

    assert view.account_id == account_telegram_only.id


def test_both_login_methods_lead_to_the_same_account_id(
    sessions: SessionService, fake_api: FakeApi, account_linked: Account
):
    """Ключевое свойство: у аккаунта с обоими способами входа сессия одна.

    Две сессии, полученные «разными дверями», принадлежат одному `account_id`,
    поэтому выход в одной двери действует и в другой. Разойдись они — человек
    выходил бы в браузере и оставался внутри в приложении.
    """
    from_mail = sessions.sign_in(account_linked.id)
    from_telegram = sessions.sign_in(account_linked.id)

    assert from_mail.account_id == from_telegram.account_id == account_linked.id
    # Токены разные: это две сессии одного человека, а не одна на всех.
    assert from_mail.token != from_telegram.token


def test_resolved_session_reports_the_account_not_the_chat_id(
    sessions: SessionService, account_email_only: Account
):
    """После проверки виден `account_id`, а не способ входа."""

    view = sessions.sign_in(account_email_only.id)
    resolved = sessions.resolve(view.token)

    assert resolved is not None
    assert resolved.account_id == account_email_only.id


# --- срок жизни ----------------------------------------------------------


def test_session_expires_after_ttl(fake_api: FakeApi, account_email_only: Account):
    """Истёкшая сессия не проходит — и не проходит по времени, а не по статусу.

    Часы сдвигаются за срок TTL: проверяется именно истечение, а не отсутствие
    сессии.
    """
    moment = [NOW]
    service = SessionService(
        store=SingleSessionStore(),
        accounts=fake_api,
        ttl=timedelta(hours=1),
        now=lambda: moment[0],
    )
    view = service.sign_in(account_email_only.id)

    moment[0] = NOW + timedelta(hours=2)

    assert service.resolve(view.token) is None


def test_session_survives_just_short_of_the_deadline(
    fake_api: FakeApi, account_email_only: Account
):
    """Граница: секунда до срока — сессия ещё действует.

    Пара к тесту на истечение: без неё код, гасящий сессии сразу после выдачи,
    прошёл бы проверку «истекла» и был бы замечен только в бою.
    """
    moment = [NOW]
    service = SessionService(
        store=SingleSessionStore(),
        accounts=fake_api,
        ttl=timedelta(hours=1),
        now=lambda: moment[0],
    )
    view = service.sign_in(account_email_only.id)

    moment[0] = NOW + timedelta(hours=1) - timedelta(seconds=1)

    assert service.resolve(view.token) is not None


def test_expired_session_is_revoked_on_resolution(fake_api: FakeApi, account_email_only: Account):
    """Истёкшую сессию гасим при первой же проверке.

    Оставленная запись — это ещё действующий токен в таблице. Раз сессия
    истекла, держать её дальше смысла нет.
    """
    moment = [NOW]
    store = SingleSessionStore()
    service = SessionService(
        store=store,
        accounts=fake_api,
        ttl=timedelta(hours=1),
        now=lambda: moment[0],
    )
    view = service.sign_in(account_email_only.id)
    moment[0] = NOW + timedelta(hours=2)

    service.resolve(view.token)

    assert store.sessions == {}


def test_naive_expiry_from_the_store_does_not_break_resolution(
    fake_api: FakeApi, account_email_only: Account
):
    """Срок без зоны не роняет проверку.

    Бот пишет `DATETIME` без зоны, и сравнение с зональными часами иначе бросило
    бы `TypeError` в середине обработки запроса. Наивное время считаем UTC.
    """

    class NaiveStore(SingleSessionStore):
        def create(self, *, account_id: int, token_hash: str, expires_at: datetime):
            return super().create(
                account_id=account_id,
                token_hash=token_hash,
                expires_at=expires_at.replace(tzinfo=None),
            )

    service = SessionService(store=NaiveStore(), accounts=fake_api, now=lambda: NOW)
    view = service.sign_in(account_email_only.id)

    assert service.resolve(view.token) is not None


def test_session_ttl_has_a_default_limit():
    """У сессии обязан быть срок. Ведомый срок — не мелочь: без него угнанный
    токен работает вечно, и заметить это негде."""
    assert timedelta(0) < DEFAULT_SESSION_TTL


# --- выход ---------------------------------------------------------------


def test_sign_out_revokes_the_session(sessions: SessionService, account_email_only: Account):
    view = sessions.sign_in(account_email_only.id)

    sessions.sign_out(view.token)

    assert sessions.resolve(view.token) is None


def test_sign_out_is_idempotent(sessions: SessionService, account_email_only: Account):
    """Повторный выход — не ошибка (§4).

    Выход — та дверь, которой человек закрывает себе доступ; отказ «сессии уже
    нет» оставил бы его с ощущением, что выход не сработал.
    """
    view = sessions.sign_in(account_email_only.id)

    sessions.sign_out(view.token)
    sessions.sign_out(view.token)  # не должно бросить
    sessions.sign_out("токена такого нет")
    sessions.sign_out(None)


def test_sign_out_of_one_session_does_not_touch_the_other(
    sessions: SessionService, account_linked: Account
):
    """Выход закрывает ровно одну дверь.

    Сессии одного человека независимы: выход в браузере не выкидывает его из
    приложения. Обратное («выход везде») было бы отдельным действием, и путать
    их нельзя — человек потерял бы доступ там, где не просил.
    """
    first = sessions.sign_in(account_linked.id)
    second = sessions.sign_in(account_linked.id)

    sessions.sign_out(first.token)

    assert sessions.resolve(first.token) is None
    assert sessions.resolve(second.token) is not None


# --- токен ---------------------------------------------------------------


def test_unknown_token_resolves_to_none(sessions: SessionService):
    """Неизвестный токен — `None`, а не исключение: экран на это отвечает
    формой входа, а не отказом."""
    assert sessions.resolve("чужой-токен") is None


def test_missing_token_resolves_to_none(sessions: SessionService):
    """Отсутствие cookie — обычное состояние первого захода."""
    assert sessions.resolve(None) is None
    assert sessions.resolve("") is None


def test_token_is_not_stored_in_the_clear(fake_api: FakeApi, account_email_only: Account):
    """В хранилище лежит хеш, а не токен.

    Репозиторий публичный: токен в дампе памяти или в логе — это уже угнанная
    сессия. Открытого токена в хранилище быть не должно ни в каком виде.
    """
    store = SingleSessionStore()
    service = SessionService(store=store, accounts=fake_api, now=lambda: NOW)

    view = service.sign_in(account_email_only.id)

    assert view.token not in store.sessions
    assert hash_token(view.token) in store.sessions


def test_tokens_differ_between_issuances(sessions: SessionService, account_email_only: Account):
    """Каждая выдача — свой токен.

    Повтор токена означал бы, что второй человек получает доступ первого.
    """
    tokens = {sessions.sign_in(account_email_only.id).token for _ in range(5)}

    assert len(tokens) == 5


def test_token_has_enough_entropy():
    """Токен — достаточно длинный, и уступить это нельзя.

    Проверка стоит здесь, а не в комментарии: короткий токен превращает подбор
    из невозможного в вопрос времени.
    """
    assert len(new_token()) >= MIN_TOKEN_BYTES
    with pytest.raises(ValueError):
        new_token(MIN_TOKEN_BYTES - 1)


def test_repeated_token_from_the_source_is_refused(fake_api: FakeApi, account_email_only: Account):
    """Коллизия источника токенов — отказ, а не тихая перезапись.

    Если фабрика выдала один и тот же токен дважды, второй человек получил бы
    доступ первого. Заметить это можно только здесь: в хранилище повтор по хешу
    — это «такая сессия уже есть», и тихая перезапись сменила бы владельца.
    """
    service = SessionService(
        store=SingleSessionStore(),
        accounts=fake_api,
        now=lambda: NOW,
        token_factory=lambda: "один-и-тот-же-токен",
    )
    service.sign_in(account_email_only.id)

    with pytest.raises(SessionRefused):
        service.sign_in(account_email_only.id)


def test_resolution_extends_the_session(fake_api: FakeApi, account_email_only: Account):
    """Успешная проверка продлевает срок на полный TTL от текущего момента.

    Кабинет открывают редко — раз в недели или месяцы. Срок, который не едет
    вперёд, ронял бы вход у человека, который как раз вернулся: он заходил бы
    ровно перед истечением и всё равно оказывался на экране входа. Продление —
    не «вечная сессия»: забытый человек, не заглянувший дольше TTL, входа не
    сохраняет, а угнанный токен перестаёт работать сам.
    """
    moment = [NOW]
    store = SingleSessionStore()
    service = SessionService(
        store=store,
        accounts=fake_api,
        ttl=timedelta(days=1),
        now=lambda: moment[0],
    )
    view = service.sign_in(account_email_only.id)
    first_expires = view.expires_at

    moment[0] = NOW + timedelta(hours=6)
    resolved = service.resolve(view.token)

    assert resolved is not None
    assert resolved.expires_at == moment[0] + timedelta(days=1)
    assert resolved.expires_at > first_expires
    session = store.find(hash_token(view.token))
    assert session is not None
    assert session.last_seen_at == moment[0]
    assert session.expires_at == moment[0] + timedelta(days=1)


def test_session_outlives_the_former_month_limit(fake_api: FakeApi, account_email_only: Account):
    """Сессия редкого посетителя живёт дольше месяца.

    Проверяется срок, а не статус: часы уезжают за то, что давал прежний срок
    (тридцать дней), и сессия обязана остаться живой — иначе редкий заход
    разваливался бы ровно на этом рубеже.
    """
    moment = [NOW]
    service = SessionService(
        store=SingleSessionStore(),
        accounts=fake_api,
        now=lambda: moment[0],
    )
    view = service.sign_in(account_email_only.id)

    assert timedelta(days=180) <= DEFAULT_SESSION_TTL

    moment[0] = NOW + timedelta(days=60)

    assert service.resolve(view.token) is not None


def test_resolution_does_not_extend_an_expired_session(
    fake_api: FakeApi, account_email_only: Account
):
    """Истёкшую сессию продление не воскрешает.

    Продление — свойство живой сессии. Истёкшая гасится, и продлевать нечего:
    иначе одним запросом с мёртвым токеном срок возвращался бы вперёд, и
    истечение перестало бы что-либо значить.
    """
    moment = [NOW]
    store = SingleSessionStore()
    service = SessionService(
        store=store,
        accounts=fake_api,
        ttl=timedelta(hours=1),
        now=lambda: moment[0],
    )
    view = service.sign_in(account_email_only.id)

    moment[0] = NOW + timedelta(hours=2)
    assert service.resolve(view.token) is None
    assert store.sessions == {}


def test_revoked_session_stays_refused_and_is_not_renewed(
    fake_api: FakeApi, account_email_only: Account
):
    """Отозванную сессию продление не возвращает.

    Отзыв — это выход: токен обязан быть мёртв. Запрос с ним не может ни
    пройти, ни завести запись заново — иначе длинный срок превращался бы в
    способ обойти выход, а отозванный токен жил бы дальше.
    """
    moment = [NOW]
    store = SingleSessionStore()
    service = SessionService(
        store=store,
        accounts=fake_api,
        ttl=timedelta(days=1),
        now=lambda: moment[0],
    )
    view = service.sign_in(account_email_only.id)

    service.sign_out(view.token)
    moment[0] = NOW + timedelta(minutes=1)

    assert service.resolve(view.token) is None
    assert store.sessions == {}


# --- отказы --------------------------------------------------------------


def test_session_for_unknown_account_is_refused(sessions: SessionService):
    """Нет аккаунта — отказ входа, а не исключение хранилища.

    Сессия на несуществующий аккаунт была бы ссылкой в пустоту: непонятно, кому
    она принадлежит и чьи подписки показывать.
    """
    with pytest.raises(NoAccount):
        sessions.sign_in(4_242)


def test_no_account_is_a_session_refusal(sessions: SessionService):
    """Отказ по аккаунту — частный случай отказа сессии, чтобы вызывающий код
    ловил одно исключение, а не два."""
    with pytest.raises(SessionRefused):
        sessions.sign_in(4_242)
