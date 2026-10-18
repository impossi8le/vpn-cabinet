"""Связывание способов входа (W3.2–W3.5).

Проверяется не «работает ли привязка», а «проходит ли атака». Разница
существенная: функциональный тест на успешную привязку зеленеет и от кода,
который принимает заявленный в браузере адрес без подтверждения. Поэтому здесь
на каждый успешный случай есть парный отказ, а сценарий атаки вынесен в
отдельный тест с прямым названием.

Класс ошибки — CVE-2026-53516 (Better Auth), nOAuth (Microsoft, 2023):
авто-связывание по заявлению одной стороны без проверки владения другой.
Разбор — §3.3 спеки.

Границы. Работа идёт против мок-слоя (`tests/fakes.py`), который воспроизводит
инварианты хранилища, а не обходит их. Живой `BotClient` пока не умеет отвязку
(#33), поэтому отвязка проверена здесь, а подключение к нему — отдельная работа;
в `cabinet/linking.py` для этого оставлен протокол хранилища.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from cabinet.accounts import Account, LinkError
from cabinet.auth_mail import CODE_LENGTH, AuthMail, AuthMailError
from cabinet.bot_client import BotClient, BotError
from cabinet.linking import (
    Linking,
    LinkRefused,
    UnlinkRefused,
)
from cabinet.mail_sender import MailSender
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(fake_api: FakeApi) -> FakeApi:
    """Хранилище аккаунтов для связывания. Мок целиком, без обёрток.

    Не адаптер: если связыванию понадобится адаптер, это будет видно по
    несоответствию протоколу `AccountStore`, а не спрятано за обёрткой в тесте.
    """
    return fake_api


@pytest.fixture
def auth_mail(fake_mail: FakeMailSender) -> AuthMail:
    """Коды на почту с фиксированными часами.

    Часы фиксированы, потому что срок кода участвует в проверке: с живыми
    часами тест на «код подтверждает адрес» мог бы позеленеть или покраснеть в
    зависимости от того, как быстро он отработал. Транспорт — подставной, как и
    требуют границы волны: письмо не уходит наружу.
    """
    sender = MailSender(sender="cabinet@example.com", transport=fake_mail)
    return AuthMail(mail_sender=sender, now=lambda: NOW)


@pytest.fixture
def linking(store: FakeApi, auth_mail: AuthMail) -> Linking:
    return Linking(store=store, auth_mail=auth_mail)


def _code_for(auth_mail: AuthMail, fake_mail: FakeMailSender, mail: str) -> str:
    """Запросить код и достать его из подставной почты.

    Код берётся из письма, а не подставляется константой: тест обязан пройти
    тот же путь, что человек, — иначе он проверял бы приём любого кода.
    """
    auth_mail.request_code(mail)
    body = fake_mail.sent_to(mail)[-1]["body"]
    return "".join(ch for ch in body if ch.isdigit())[:CODE_LENGTH]


# --- W3.4: сценарии атаки, которые обязаны не проходить -------------------


def test_pre_account_hijacking_is_stopped(linking: Linking, store: FakeApi):
    """Атака pre-account hijacking, полный сценарий.

    Шаг 1 — злоумышленник заводит аккаунт на чужой адрес: `victim@example.com`
    без подтверждения. Шаг 2 — жертва входит через Telegram. Шаг 3 — если
    система связывает по заявленному адресу, личность жертвы прилипает к записи
    злоумышленника, и он сохраняет доступ.

    Шаг 1 здесь **невозможен**: мок, как и живой бот, отвергает почту без
    `mail_verified_at` (§4, инвариант 2). Прилипать не к чему — именно так эта
    атака и закрывается: не проверкой на шаге 3, а тем, что шага 1 не
    существует.
    """
    with pytest.raises(LinkError, match="без подтверждения"):
        store.create_account(mail="victim@example.com")


def test_mail_cannot_be_linked_without_holding_the_mailbox(linking: Linking, fake_api: FakeApi):
    """Владелец адреса доказывается кодом. Без него привязки нет.

    Это второй вектор той же ошибки: аккаунт заведён по Telegram, а к нему
    пытаются привязать чужую почту. Код на этот адрес не запрашивали — значит,
    ящик не у жертвы, и привязка обязана упасть.
    """
    account = fake_api.create_account(chat_id=123_456_789)

    with pytest.raises(AuthMailError):
        linking.link_mail(account.id, mail="victim@example.com", code="000000")

    assert fake_api.find_by_id(account.id).mail is None


def test_wrong_code_does_not_link_the_mail(
    linking: Linking, fake_api: FakeApi, auth_mail, fake_mail
):
    """Код выдан, но введён неверный — привязки нет.

    Отделено от «кода не было»: здесь операция запрашивалась, письмо ушло, а
    совпадения не произошло. Привязка обязана требовать именно **совпадения**,
    а не факта запроса кода.
    """
    account = fake_api.create_account(chat_id=123_456_789)
    real = _code_for(auth_mail, fake_mail, "victim@example.com")
    wrong = "".join("9" if digit != "9" else "1" for digit in real)

    with pytest.raises(AuthMailError):
        linking.link_mail(account.id, mail="victim@example.com", code=wrong)

    assert fake_api.find_by_id(account.id).mail is None


def test_claimed_chat_id_without_telegram_confirmation_is_refused(
    linking: Linking, fake_api: FakeApi
):
    """`chat_id`, заявленный без подтверждения ботом, не привязывается.

    §3.3 спеки: `chat_id` присваивается только после подтверждения человеком в
    Telegram. Умолчание здесь в пользу безопасности: вызов без явного
    подтверждения отвергается. Забыть подтверждение нельзя — «забыл» не должно
    означать «привязал заявленный из формы `chat_id`».
    """
    account = fake_api.create_account(mail="user@example.com", mail_verified_at=NOW)

    with pytest.raises(LinkRefused):
        linking.link_telegram(account.id, chat_id=999_888_777)

    assert fake_api.find_by_chat_id(999_888_777) is None
    assert fake_api.find_by_id(account.id).chat_id is None


def test_telegram_link_requires_explicit_confirmation_flag(linking: Linking, fake_api: FakeApi):
    """Подтверждение бота требуется явно — и оно же и есть условие успеха.

    Пара к отказу выше: без флага (`confirmed=False` по умолчанию) — отказ, с
    ним — привязка. Иначе тест на отказ зеленел бы и от кода, который вообще
    никогда не привязывает Telegram.
    """
    account = fake_api.create_account(mail="user@example.com", mail_verified_at=NOW)

    result = linking.link_telegram(account.id, chat_id=999_888_777, confirmed=True)

    assert result.account.chat_id == 999_888_777


# --- W3.2: привязка Telegram к email-аккаунту ----------------------------


def test_telegram_links_to_email_account_on_empty_slot(linking: Linking, fake_api: FakeApi):
    """Главный успешный случай: пустое место — привязка проходит."""
    account = fake_api.create_account(mail="user@example.com", mail_verified_at=NOW)

    result = linking.link_telegram(account.id, chat_id=555_000_111, confirmed=True)

    assert result.method == "telegram"
    assert result.account.chat_id == 555_000_111
    # Почта не потерялась: привязка второго способа не заменяет первый.
    assert result.account.mail == "user@example.com"
    assert fake_api.find_by_chat_id(555_000_111).id == account.id


def test_linking_telegram_twice_is_refused_not_overwritten(
    linking: Linking, fake_api: FakeApi, account_linked
):
    """Инвариант 3: занятое место не перезаписывается.

    Иначе знание чужого `chat_id` давало бы доступ к аккаунту — привязка
    стала бы способом угона, а не удобством.
    """
    with pytest.raises(LinkRefused):
        linking.link_telegram(account_linked.id, chat_id=999_000_222, confirmed=True)

    assert fake_api.find_by_id(account_linked.id).chat_id == account_linked.chat_id
    assert fake_api.find_by_chat_id(999_000_222) is None


def test_foreign_chat_id_collision_is_refused(linking: Linking, fake_api: FakeApi):
    """§5: занятый `chat_id` — честная ошибка, никогда не перезапись."""
    victim = fake_api.create_account(chat_id=111_222_333)
    attacker = fake_api.create_account(mail="attacker@example.com", mail_verified_at=NOW)

    with pytest.raises(LinkRefused):
        linking.link_telegram(attacker.id, chat_id=111_222_333, confirmed=True)

    # Аккаунт жертвы не тронут.
    assert fake_api.find_by_chat_id(111_222_333).id == victim.id
    assert fake_api.find_by_id(attacker.id).chat_id is None


# --- W3.3: привязка почты к Telegram-аккаунту ----------------------------


def test_mail_links_after_verified_code(linking: Linking, fake_api: FakeApi, auth_mail, fake_mail):
    """Обратное направление: аккаунт из Telegram получает почту — по коду."""
    account = fake_api.create_account(chat_id=123_456_789)
    code = _code_for(auth_mail, fake_mail, "user@example.com")

    result = linking.link_mail(account.id, mail="user@example.com", code=code)

    assert result.method == "mail"
    assert result.account.mail == "user@example.com"
    assert result.account.chat_id == 123_456_789


def test_mail_cannot_be_linked_twice(
    linking: Linking, fake_api: FakeApi, account_linked, auth_mail, fake_mail
):
    """Симметрично Telegram: занятая почта не перезаписывается."""
    code = _code_for(auth_mail, fake_mail, "other@example.com")

    with pytest.raises(LinkRefused):
        linking.link_mail(account_linked.id, mail="other@example.com", code=code)

    assert fake_api.find_by_id(account_linked.id).mail == account_linked.mail


def test_foreign_mail_collision_is_refused(
    linking: Linking, fake_api: FakeApi, auth_mail, fake_mail
):
    """Коллизия по почте — тоже отказ, а не перезапись чужого аккаунта."""
    victim = fake_api.create_account(mail="victim@example.com", mail_verified_at=NOW)
    attacker = fake_api.create_account(chat_id=123_456_789)
    code = _code_for(auth_mail, fake_mail, "victim@example.com")

    with pytest.raises(LinkRefused):
        linking.link_mail(attacker.id, mail="victim@example.com", code=code)

    assert fake_api.find_by_mail("victim@example.com").id == victim.id
    assert fake_api.find_by_id(attacker.id).mail is None


# --- W3.5: отвязка и запрет отвязать последний способ ---------------------


def test_unlinking_last_login_method_is_refused(
    linking: Linking, fake_api: FakeApi, account_telegram_only
):
    """Аккаунт без способов входа недоступен навсегда — отвязку запрещаем.

    Это не ошибка пользователя, которую можно пережить: после неё человек не
    войдёт никаким способом. Поэтому отказ отдельный (`UnlinkRefused`), а не
    «не получилось».
    """
    with pytest.raises(UnlinkRefused):
        linking.unlink_telegram(account_telegram_only.id)

    assert fake_api.find_by_chat_id(123_456_789) is not None


def test_unlinking_mail_last_is_refused(linking: Linking, fake_api: FakeApi, account_email_only):
    """Симметрично: последняя оставшаяся почта не отвязывается."""
    with pytest.raises(UnlinkRefused):
        linking.unlink_mail(account_email_only.id)

    assert fake_api.find_by_mail("user@example.com") is not None


def test_unlinking_telegram_when_mail_remains_succeeds(linking: Linking, account_linked):
    """Отвязка разрешена, пока остаётся второй способ входа."""
    result = linking.unlink_telegram(account_linked.id)

    assert result.method == "telegram"
    assert result.account.chat_id is None
    # Почта осталась — аккаунт по-прежнему доступен.
    assert result.account.has_any_login_method()


def test_unlinking_mail_when_telegram_remains_succeeds(linking: Linking, account_linked):
    """Обратное направление отвязки."""
    result = linking.unlink_mail(account_linked.id)

    assert result.method == "mail"
    assert result.account.mail is None
    assert result.account.chat_id == account_linked.chat_id


def test_unlinking_unbound_method_is_refused(linking: Linking, account_email_only):
    """Отвязка того, чего нет, — не «успех впустую»: это состояние, о котором
    надо сказать, а не молча вернуть аккаунт без изменений."""
    with pytest.raises(LinkRefused):
        linking.unlink_telegram(account_email_only.id)


# --- отказ на несуществующем аккаунте -------------------------------------


def test_linking_for_unknown_account_is_refused(linking: Linking):
    """Нет аккаунта — осмысленный отказ, а не исключение хранилища.

    Связывание вызывается из кабинета, где аккаунт уже есть (идёт сессия);
    его отсутствие означает расхождение состояния, о котором вызывающий должен
    узнать по нашему типу отказа, а не по падению мока.
    """
    with pytest.raises(LinkRefused):
        linking.link_telegram(4_242, chat_id=555_000_111, confirmed=True)


# --- форма хранилища ------------------------------------------------------


def test_store_satisfies_the_linking_protocol(store: FakeApi):
    """Мок обязан удовлетворять протоколу, против которого написано связывание.

    Проверяется структурно: если метод переименуют в моке (или он разойдётся с
    тем, что даст живой клиент на W6), это всплывёт здесь, а не в бою тихой
    `AttributeError`.
    """
    for name in (
        "find_by_id",
        "find_by_chat_id",
        "find_by_mail",
        "link_chat_id",
        "link_mail",
        "unlink_chat_id",
        "unlink_mail",
    ):
        assert callable(getattr(store, name, None)), f"мок без {name}"


def test_link_result_requires_an_account_not_a_bool(linking: Linking, fake_api: FakeApi):
    """Итог привязки — `Account`, а не `True` и не `rowcount`.

    Связывание отвечает состоянием **после** записи: экран должен показывать
    сохранённое, а не запрошенное. `rowcount` остаётся деталью хранилища, и
    наружу из этого модуля не выходит.
    """
    account = fake_api.create_account(mail="user@example.com", mail_verified_at=NOW)

    result = linking.link_telegram(account.id, chat_id=777_666_555, confirmed=True)

    assert isinstance(result.account, Account)
    assert result.account.id == account.id
    assert result.account.chat_id == 777_666_555


# --- предпросмотр отвязки в клиенте к боту (issue #37) -------------------
#
# `BotClient.unlink_preview` — чтение, а не действие: он лишь спрашивает у
# бота, что будет, чтобы экран успел сказать это **до** нажатия. Проверяется
# стык с контрактом §4а: точное имя пути, параметр `method` и разбор ответа.
# Транспорт подставной — сеть не открывается.


def _preview_client(handler) -> BotClient:
    return BotClient(
        base_url="http://127.0.0.1:9",
        secret="test-secret",
        client=httpx.AsyncClient(base_url="http://127.0.0.1:9", transport=handler),
    )


async def test_unlink_preview_hits_contract_path_and_parses_the_answer():
    """Путь — `GET .../unlink-preview?method=`, ответ разобран в `UnlinkPreview`.

    Путь сверяется целиком: подмена `unlink-preview` на `unlink_preview`
    оставила бы подстроку на месте и прошла бы мимо — этот класс ошибки в
    проекте уже случался. `method` уходит **параметром**, а не сегментом пути:
    это часть контракта §4а.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"configs_affected": 3, "remaining_method": "telegram"})

    client = _preview_client(httpx.MockTransport(handler))
    async with client:
        preview = await client.unlink_preview(7, "mail")

    assert len(seen) == 1
    assert seen[0].url.path == "/internal/accounts/7/unlink-preview"
    assert seen[0].url.params["method"] == "mail"
    assert preview.configs_affected == 3
    assert preview.remaining_method == "telegram"


async def test_unlink_preview_without_configs_reports_zero_not_error():
    """Ноль конфигов — валидный ответ, а не ошибка: у аккаунта без Telegram
    конфигов нет вовсе. Экран по нулю молчит, но само чтение обязано пройти."""
    client = _preview_client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"configs_affected": 0, "remaining_method": None}
            )
        )
    )
    async with client:
        preview = await client.unlink_preview(1, "telegram")

    assert preview.configs_affected == 0
    assert preview.remaining_method is None


async def test_unlink_preview_bad_method_is_a_bot_error_not_a_network_failure():
    """Незнакомый `method` бот отвергает `400` — это `BotError`, не сбой связи.

    Разница для вызывающего та же, что у отвязки: по `code` он показывает
    отказ, по `BotUnreachable` — повторяет. Молчаливая подстановка метода была
    бы здесь хуже отказа: экран показал бы последствие не той отвязки.
    """
    client = _preview_client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                400, json={"error": {"code": "bad_request", "retryable": False}}
            )
        )
    )
    async with client:
        with pytest.raises(BotError) as exc:
            await client.unlink_preview(1, "tel")

    assert exc.value.code == "bad_request"
