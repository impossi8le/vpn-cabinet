"""Точка входа кабинета: HTTP-слой поверх готовых модулей (W6.6).

Зачем отдельный слой. До этого кабинета как запускаемого сервиса не было —
были модули (вход, сессии, подписки) и разметка, но открыть экран в браузере
было нечем. Этот файл — единственное место, где модули соединяются в
приложение, и потому единственное, где видно, из чего кабинет собран.

Сборка зависимостей — одна функция `build_app`, принимающая реализации
параметрами. Это ключ к подмене мока на живой бот: когда на W6 появится
настоящий `BotClient` и живая почта, поменяется **аргумент** `build_app`, а не
роуты. Роуты не знают, настоящий под ними бот или подставной, — иначе подмена
потребовала бы правки каждой ручки, и однажды одна из них осталась бы на моке.

Сессия кабинета — в cookie `HttpOnly` + `SameSite=Lax`. Почему так:

- **`HttpOnly`** — токен не виден из JavaScript. Сессия не должна быть
  достижима разметкой: XSS, читающий `localStorage`, уносит вход целиком.
- **`SameSite=Lax`** — иначе переход по ссылке из письма (внешний сайт →
  кабинет) не отправит cookie, и человек, кликнувший ссылку, снова окажется
  на экране входа с уже действующей сессией. Lax пропускает навигацию верхнего
  уровня и при этом не отдаёт cookie сторонним POST — то есть закрывает CSRF
  на изменяющих запросах.
- **`Max-Age`** — cookie переживает закрытие браузера. Это не удобство, а
  главный случай проекта: кабинет открывают раз в недели или месяцы, и
  сессионная cookie умирала бы вместе с браузером раньше, чем серверная сессия
  успевала пригодиться. Срок берётся из `SessionService.ttl` — один на cookie и
  на запись в хранилище. Долгая cookie — долгий доступ, поэтому она не отменяет
  ничего из своего же устройства: срок **скользит** (`SessionService.resolve`
  продлевает живую сессию), истекает у забытой, а `HttpOnly` и `SameSite` держат
  токен вне JS и вне сторонних POST. Cookie выпускается заново при каждом
  заходе — одним middleware на всё приложение: продление, записанное только в
  хранилище, оставляло бы в браузере прежний срок, и активного человека
  выбрасывало бы через TTL от первого входа (issue #53).

Границ сервиса это не пересекает: кабинет по-прежнему не ходит в базу
(CLAUDE.md), а говорит с хранилищем через те же протоколы, что и модули.
"""

from __future__ import annotations

import inspect
import json
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol
from urllib.parse import quote, unquote

from fastapi import Body, FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from cabinet.accounts import Account
from cabinet.auth_mail import AuthMail, AuthMailError, RateLimited
from cabinet.bot_client import (
    AUTH_MODE_LINK,
    POLL_CONFIRMED,
    POLL_DENIED,
    POLL_PENDING,
    Balance,
    BotError,
    BotUnreachable,
    Challenge,
    Order,
    Referral,
    TelegramAuthClient,
    TelegramLogin,
    TrialEligibility,
    public_code_and_secret,
    secret_hash,
)
from cabinet.configs import (
    ConfigError,
    ConfigNotFound,
    ConfigService,
    ConfigSource,
    DownloadLimiter,
)
from cabinet.linking import CHAT_ID_TAKEN, Linking, LinkRefused, UnlinkRefused
from cabinet.mail_sender import MailError, MailSender
from cabinet.payments import (
    PAYMENT_STATUS_SUCCEEDED,
    Payments,
    PaymentsError,
    PaymentsTransportError,
    UnconfiguredPayments,
    is_trusted_notification_ip,
)
from cabinet.pending_mail import PendingMail
from cabinet.pricing import PRICES, calculate_price
from cabinet.render import (
    BUY_DONE_URL,
    DEFAULT_BUY_DAYS,
    DEFAULT_BUY_DEVICES,
    PURCHASE_DEVICES,
    RATE_LIMIT_ERROR,
    STATIC_DIR,
    TELEGRAM_CHECK_LABEL,
    render_buy,
    render_buy_confirm,
    render_buy_done,
    render_buy_pay,
    render_buy_servers,
    render_legal,
    render_linking,
    render_login,
    render_login_telegram,
    render_login_waiting,
    render_subscriptions,
    trial_refusal_note,
)
from cabinet.sessions import SessionService, SessionView, SingleSessionStore
from cabinet.signin import MailSignInError, SignInError, SignInService
from cabinet.subscriptions import SubscriptionView, subscription_views
from cabinet.theme import Tone

__all__ = ["COOKIE_NAME", "build_app"]

# Имя cookie сессии. Одно на весь кабинет: второе имя означало бы вторую
# сессию, и человек входил бы дважды.
COOKIE_NAME = "cabinet_session"

# Cookie отказа на экране входа. Текст отказа — состояние экрана, а не часть
# ссылки: он живёт здесь, до первого GET, который его прочитает и погасит
# (issue #55). `HttpOnly` — разметка до значения не дотягивается; короткий
# `Max-Age` — дольше этого состояния носить нечего.
LOGIN_NOTICE_COOKIE = "cabinet_login_notice"
# Срок жизни cookie отказа. Секунд лимита (60) с запасом на переход по
# редиректу; истечение здесь только страховка — гасит cookie первый же GET.
LOGIN_NOTICE_TTL_SECONDS = 300

# Cookie посещения для воронки (issue #64). Значение — случайные байты
# (`secrets`), без данных человека: оно нужно, чтобы два анонимных шага
# (`login_shown`, `code_requested`) считались одним человеком, а не двумя
# показами. Ставится там, где впервые понадобилось (`GET /login`), и едет в
# каждом событии. Срок — как у сессии (`SessionService.ttl`): воронка живёт
# дольше одного перехода, а cookie посещения должна его пережить.
VISITOR_COOKIE = "cabinet_visitor"

# Адрес отправителя писем по умолчанию. Это не боевой ящик, а подставной
# (`example.com` зарезервирован для примеров): репозиторий публичный, и
# настоящего домена здесь быть не может. В бою адрес приходит из конфига.
DEFAULT_SENDER = "cabinet@example.com"


class AccountStore(Protocol):
    """Хранилище аккаунтов, как его видит приложение.

    Сведено к тому, что нужно HTTP-слою: прочитать аккаунт по id (для экрана)
    и перечислить подписки.

    Форму дают **двое** — и это стоило проекту дефекта (#46): `tests.fakes.FakeApi`
    умеет её по определению, а живой `BotClient` не умеет вовсе. Его методы —
    корутины и называются иначе (`list_subscriptions`, `find_account_by_chat_id`),
    поэтому роуты за ним отвечали 500, а тесты этого не видели: все они шли
    против мока. Живой клиент приобретает эту форму **через адаптер**
    (`cabinet.bot_client.BotStore`), который и передаёт `build_from_env`.
    """

    def find_by_id(self, account_id: int) -> Account | None: ...

    def subscriptions_for(self, account_id: int) -> list[object]: ...


def _account_money(
    store: AccountStore, account_id: int
) -> tuple[Balance | None, Referral | None, bool]:
    """Деньги человека для экрана подписок: баланс, рефералка, признак `telegram_required`.

    Хранилище может не уметь этот раздел: `AccountStore` — это то, что обязан
    дать любой бот, а баланс и рефералка появились позже (W5б, #40) и на
    подставном боте их вовсе нет. Поэтому метод ищется на месте, а его
    отсутствие — не сбой, а «показать нечего»: рушить весь экран подписок из-за
    необязательного блока значило бы спрятать от человека то, что работает.

    `telegram_required` возвращается отдельно от «данных нет» намеренно. Когда
    бот говорит, что у аккаунта нет Telegram, в кабинете нельзя рисовать `0 ₽`:
    ноль читался бы как «вам ничего не начислили», хотя сумма просто неизвестна.
    Это состояние называется текстом, а не значением.

    Недоступность бота (`BotUnreachable`) здесь — тот же «показать нечего», что и
    `BotError`: деньги человека — необязательный блок, и рушить из-за них экран
    подписок значило бы прятать от человека то, что работает (issue #49).
    """
    get_balance = getattr(store, "get_balance", None)
    get_referral = getattr(store, "get_referral", None)
    if get_balance is None or get_referral is None:
        return None, None, False
    try:
        balance = get_balance(account_id)
    except BotError as exc:
        if exc.code == "telegram_required":
            return None, None, True
        return None, None, False
    except BotUnreachable:
        return None, None, False
    try:
        referral = get_referral(account_id)
    except (BotError, BotUnreachable):
        referral = None
    return balance, referral, False


def _trial_eligibility(
    store: AccountStore, account_id: int
) -> tuple[TrialEligibility | None, bool]:
    """Право на триал для шага подтверждения: ответ бота или «не проверено».

    Возвращается пара «ответ, недоступность». Разведены они не для удобства, а
    по смыслу: `TrialEligibility` — **проверенный факт** (доступен или причина
    отказа), `unreachable=True` — бот не ответил, и о человеке это не говорит
    ничего. Свести второе к «недоступен» значило бы выдать сбой сервиса за
    решение о человеке — ровно та ошибка, от которой сторожит экран.

    Хранилище может не уметь эту ручку (старый подставной бот): отсутствие
    метода — тот же «не проверено», что и недоступность, а не сбой экрана.
    Причина отказа приходит `200` с `eligible=False` и кодом в `reason`, поэтому
    `BotError` здесь — не ожидаемое «не положен», а расхождение: считаем его
    недоступностью проверки.
    """
    check = getattr(store, "trial_eligibility", None)
    if check is None:
        return None, True
    try:
        return check(account_id), False
    except (BotError, BotUnreachable):
        return None, True


def _default_store() -> AccountStore:
    """Подставной бот по умолчанию.

    Импорт подставного бота ленивый и намеренно оставлен здесь. Пока живого
    роутера `/internal/*` нет, запустить кабинет иначе нечем, и на этой волне
    подставной бот — единственная существующая реализация. Импорт на уровне
    модуля связал бы кабинет с мок-слоем намертво (в том числе в бою); ленивый
    импорт такой связи не создаёт — как только боевой `build_app` передаст
    живой клиент, эта ветка перестанет выполняться вовсе.
    """
    from tests.fakes import FakeApi

    return FakeApi()


def _default_mail_sender() -> MailSender:
    """Отправитель писем на подставном транспорте.

    Письма никуда не уходят: транспорт держит их в памяти. Так запуск кабинета
    не требует ни SMTP, ни ключей — тех же границ держатся тесты (ни сети, ни
    базы). Боевой отправитель собирается из окружения (`MailSender.from_env`)
    и передаётся в `build_app`.
    """
    from tests.fakes import FakeMailSender

    return MailSender(sender=DEFAULT_SENDER, transport=FakeMailSender())


def _default_payments() -> Payments:
    """Магазин ЮKassa по умолчанию — отказывающая заглушка.

    Умолчание для тестов и стенда, как `_default_store` и `_default_mail_sender`:
    запуск без магазина не должен падать на старте, но и изображать оплату нельзя.
    Заглушка отказывает на использовании (`UnconfiguredPayments`), и экран оплаты
    показывает это отдельным состоянием. Боевой клиент собирается из окружения
    (`cabinet.payments.build_payments`) и передаётся в `build_app` из `__main__`.
    """
    return UnconfiguredPayments()


async def _maybe_await(value: object) -> object:
    """Дождаться значения, если оно ожидаемое, иначе вернуть как есть.

    Нужно на стыке двух реализаций хранилища: живой `BotClient` отвечает
    корутинами (`unlink_preview`), подставной бот — обычными значениями. Сводить
    их к одной форме пришлось бы правкой мока (общий для волн), поэтому шов
    держится здесь, в вызывающем коде.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class _StoreConfigSource:
    """Адаптер «хранилище → источник `.ovpn`» для `ConfigService`.

    Кабинет не хранит конфиги: их отдаёт бот. Живой `BotClient` умеет
    `get_ovpn` (async), подставной бот — `config_for_subscription` (sync,
    возвращает `VpnConfig` или `None`). Обе формы сводятся к одному
    `get_ovpn(account_id, config_id) -> bytes`, которого ждёт `ConfigSource`.

    «Нет конфига» и «чужой конфиг» дают один и тот же `ConfigNotFound`: так же,
    как у живого бота `config_not_found` (§3 контракта), — иначе перебором
    `config_id` выяснялось бы, у кого что есть.
    """

    def __init__(self, store: object) -> None:
        self._store = store

    async def get_ovpn(self, account_id: int, config_id: str) -> bytes:
        live = getattr(self._store, "get_ovpn", None)
        if live is not None:
            return await _maybe_await(live(account_id, config_id))  # type: ignore[no-any-return]

        local = getattr(self._store, "config_for_subscription", None)
        if local is None:
            raise ConfigNotFound("источник конфигов не подключён")
        config = await _maybe_await(local(account_id, int(config_id)))
        if config is None:
            raise ConfigNotFound("конфиг недоступен")
        return str(getattr(config, "body", "")).encode("utf-8")


# --- тексты отказов ------------------------------------------------------

# Текст отказа входа по почте. Он **один** и не говорит, занят адрес или пуст:
# разный ответ на «почты нет» и «почта занята» выдал бы, кто где
# зарегистрирован, — перебором адресов это стало бы списком клиентов (§3.3).
LOGIN_MAIL_ERROR = "Не удалось отправить код на этот адрес. Проверьте адрес и попробуйте ещё раз."
# Код не принят: не запрашивался, истёк, не совпал или попытки исчерпаны.
# Для человека это одно действие — ввести код заново (см. `signin.MailSignInError`).
# Имя без `LOGIN_`: текст обслуживает оба экрана с кодом — вход и привязку
# почты, — и `LOGIN_` на втором вводило бы в заблуждение.
CODE_ERROR = "Код не подошёл. Проверьте цифры из письма и попробуйте ещё раз."
# Повторная отправка подряд отвергнута лимитом. Текст живёт в `render` (там же,
# где экран с отсчётом): число секунд показывается живой меткой, а не строкой,
# и формулировка обязана быть одна на вход и привязку — лимит тоже один.
# Адрес пуст или не указан — форма вернулась как есть.
LOGIN_MAIL_REQUIRED = "Укажите адрес почты."
# Сбой подписки: список не получен, а не «подписок нет». Пустой список — это
# отдельное честное состояние (W4.3), и путать его со сбоем нельзя.
SUBSCRIPTIONS_ERROR = "Не удалось получить список подписок. Обновите страницу позже."

# Бот недоступен — и это **не** то же, что «аккаунта нет» (issue #49). Сеть,
# таймаут, обрыв: `BotUnreachable` приходит на любом запросе к боту, а бот
# авторитетен — без него кабинет на время ослеп. Текст у этих экранов один
# (способы входа, скачивание): причина у них одна, и разные формулировки одной
# недоступности наводили бы на мысль о разных сбоях. Сессию отсюда не гасят
# никогда: человек никуда не выходил, и уводить его на вход значило бы выдать
# сбой сети за расхождение состояния.
BOT_UNREACHABLE_ERROR = "Сервис сейчас не отвечает, данные не получены. Обновите страницу позже."

# Список серверов не получен. Отдельно от «серверов нет»: пустой список — это
# факт от бота, а здесь факта не было вовсе. Текст говорит, что делать, а не
# выдаёт сбой за состояние.
BUY_SERVERS_ERROR = "Список серверов не получен. Обновите страницу позже."

# Описание платежа на странице ЮKassa. Короткое и без внутренних деталей: его
# видит покупатель, а репозиторий публичный. Названия серверов и адресов в
# описание не попадают.
BUY_PAY_DESCRIPTION = "Подписка VPN"


def _purchase_query(days: int, devices: int, picks: list[str], *, free: bool = False) -> str:
    """Общая часть адресов покупки: `days`, `devices` и повторяющиеся `pick`.

    Один сборщик на экран оплаты, возврат к серверам и повтор: набор у них один,
    и вторая копия формата разошлась бы при первой правке.

    `free` едет вместе с набором: без него возврат на экран оплаты потерял бы
    признак бесплатного пути, и человек, отказавшийся от бесплатного доступа,
    вернулся бы к оплате того же самого.
    """
    parts = [f"days={days}", f"devices={devices}"]
    parts.extend(f"pick={quote(code)}" for code in picks)
    if free:
        parts.append("free=1")
    return "&".join(parts)


# Бот недоступен на **входе**, и текст здесь свой — не по недосмотру. Код к этому
# моменту уже проверен и погашен (он одноразовый, `AuthMail.verify`), поэтому
# «обновите страницу» посоветовало бы ввести использованный код и получить «код
# не подошёл» за расхождение, которого человек не совершал. Экран возвращает к
# форме адреса: код надо запросить заново, и адрес на ней уже подставлен.
LOGIN_BOT_UNREACHABLE = "Сервис сейчас не отвечает. Запросите код заново чуть позже."

# Скачивание по адресу, за которым нет подписки аккаунта. Тот же приём, что у
# «нет конфига / чужой конфиг» в `configs`: различить их снаружи нечем, значит,
# и тексты у них одни — иначе перебором `subscription_id` выяснялось бы, у кого
# что есть.
SUBSCRIPTIONS_UNKNOWN_CONFIG = "Такого подключения нет или оно не принадлежит аккаунту."

# Отвязка единственного способа входа запрещена (W3.5): аккаунт без способов
# входа недоступен навсегда. Текст показывается на экране, а не только прячется
# отсутствием кнопки: запрет обязан быть виден и тогда, когда запрос пришёл
# помимо разметки (перезагрузка формы, отправка POST напрямую).
UNLINK_LAST_METHOD_ERROR = (
    "Это единственный способ входа — отвязать его нельзя. "
    "Сначала привяжите второй, иначе аккаунт станет недоступен навсегда."
)
# Привязка почты: адрес не назван — форма вернулась как есть.
LINK_MAIL_REQUIRED = "Укажите адрес почты."
# Код отправлен — это идущий процесс, а не завершённое действие: тон процессный
# (янтарный), как у ожидания входа (§7).
LINK_MAIL_CODE_SENT = "Код отправлен на {mail}. Введите его, чтобы подтвердить адрес."
# Общий отказ привязки. Один текст на все причины — как у входа в почту:
# разница между «занято другим аккаунтом» и «уже привязано» позволяла бы
# перебором адресов выяснять, кто где зарегистрирован (§5, §3.3).
LINK_REFUSED_ERROR = (
    "Не удалось привязать этот способ входа. Проверьте данные и попробуйте ещё раз."
)
# Отвязано — завершённое действие: тон нейтральный, зелёного здесь нет (§7).
UNLINK_DONE_TELEGRAM = "Telegram отвязан."
UNLINK_DONE_MAIL = "Почта отвязана."
# Привязано — тоже завершённое действие, и тон тот же нейтральный: зелёный в
# проекте оставлен подтверждённой защите, а не новому способу входа (§7). Текст
# говорит, что человек **получил** (вход этим адресом), а не «подключено».
LINK_DONE_MAIL = "Почта привязана — этим адресом можно входить в кабинет."
# Telegram привязан — тем же нейтральным тоном, что и почта (§7: зелёный оставлен
# подтверждённой защите, а не новому способу входа).
LINK_DONE_TELEGRAM = "Telegram привязан."

# --- Привязка Telegram: челлендж бота в режиме `link` (issue #65) ----------
#
# Тот же флоу, что у входа, но `mode="link"`: бот подтверждает `chat_id`, не
# заводя аккаунт. Состояния разведены так же, как на экране входа, и по той же
# причине — за разными причинами стоят разные действия человека.
# «Подтвердите в Telegram» — пойти в бота; «отклонено» — начать заново; «код не
# подошёл» — перечитать код; «сервис не отвечает» — подождать.
#
# Отказ «этот `chat_id` занят» показан **своим** текстом, а не общим «не удалось»:
# `chat_id` подтверждён ботом, человек владеет этим Telegram, и молчание не
# защищает ничьей тайны — только оставляет человека без подсказки (issue #65).

# Привязка начата, но человек ещё не подтвердил её в Telegram.
LINK_TELEGRAM_PENDING = (
    "Привязка ещё не подтверждена. Подтвердите её в Telegram и нажмите «Проверить привязку»."
)
# Человек отклонил привязку в Telegram — начать заново (кнопка рядом).
LINK_TELEGRAM_DENIED = "Привязка отклонена в Telegram. Если это были не вы — начните заново."
# Код из Telegram не подошёл: человек уже подтвердил в боте, действие — перечитать код.
LINK_TELEGRAM_CODE_ERROR = "Код из Telegram не подошёл. Проверьте цифры и попробуйте ещё раз."
# Челлендж истёк или уже использован (`operation_not_found`, `already_consumed`).
LINK_TELEGRAM_EXPIRED = "Время привязки истекло или она уже использована. Начните заново."
# Челленджа нет: не начинали, он протух или кабинет перезапускался (состояние в
# памяти). Обычное состояние старой вкладки, а не сбой сервера.
LINK_TELEGRAM_NOT_STARTED = "Привязка не начата или устарела. Начните заново."
# Прочий отказ бота (расхождение контракта, неизвестный код).
LINK_TELEGRAM_FAILED = "Не удалось проверить привязку. Начните заново."
# Бот недоступен на этом пути. Отдельно от «отклонено»: причина не в человеке.
LINK_TELEGRAM_BOT_UNREACHABLE = (
    "Сервис сейчас не отвечает — проверить привязку не удалось. Начните заново чуть позже."
)
# `chat_id` уже занят другим аккаунтом (`chat_id_taken`). Свой текст, потому что
# человеку нужно знать, что делать, а не «не удалось» (issue #65).
LINK_TELEGRAM_TAKEN = "Этот Telegram уже привязан к другому аккаунту."

# Отказы бота на опросе, у которых есть свой текст. Список закрытый и назван
# кодами контракта (§1, §5), а не текстом: текст меняется, код — нет. Незнакомый
# код получает `LINK_TELEGRAM_FAILED`.
LINK_TELEGRAM_POLL_ERRORS = {
    "nonce_mismatch": LINK_TELEGRAM_CODE_ERROR,
    "operation_not_found": LINK_TELEGRAM_EXPIRED,
    "already_consumed": LINK_TELEGRAM_EXPIRED,
    "invalid_secret": LINK_TELEGRAM_NOT_STARTED,
}

# --- Вход через Telegram: челлендж бота (§5) ------------------------------
#
# Экран начинается с челленджа (`/auth/link`) и завершается опросом (`/auth/poll`).
# Тексты состояний разные, и это не украшение: за разными причинами стоят разные
# действия человека. «Подтвердите в Telegram» — это пойти в бота; «отклонён» —
# начать заново; «код не подошёл» — перечитать код; «сервис не отвечает» —
# подождать. Один текст на все четыре лишил бы человека единственной подсказки.

# Подпись кнопки опроса. Имя берётся у `render`: строка живёт в одном месте, а
# роут называет её своим именем, потому что экранное состояние — его дело.
LOGIN_TELEGRAM_CHECK = TELEGRAM_CHECK_LABEL
# Вход начат, но человек ещё не подтвердил его в Telegram. Это ожидание, а не
# отказ: подтвердить нужно там, и кнопка та же.
LOGIN_TELEGRAM_PENDING = (
    "Вход ещё не подтверждён. Подтвердите его в Telegram и нажмите «Проверить вход»."
)
# Человек отклонил вход в Telegram. Отдельный текст, а не «не удалось»: отказ
# пришёл от него самого, и повторять тот же челлендж бессмысленно — надо начать
# заново (кнопка «Войти заново» стоит рядом).
LOGIN_TELEGRAM_DENIED = "Вход отклонён в Telegram. Если это были не вы — начните вход заново."
# Код из Telegram не подошёл. Свой текст, а не «вход отклонён»: человек уже
# подтвердил вход в боте, и отправлять его в Telegram снова значило бы гонять по
# кругу. Действие здесь — перечитать код в чате с ботом.
LOGIN_TELEGRAM_CODE_ERROR = "Код из Telegram не подошёл. Проверьте цифры и попробуйте ещё раз."
# Челлендж истёк или уже использован (`operation_not_found`, `already_consumed`).
# Это состояние входа, а не ошибка ввода: повторять код нечем — надо начать заново.
LOGIN_TELEGRAM_EXPIRED = "Время входа истекло или вход уже использован. Начните заново."
# Челленджа нет: не начинали, он протух или кабинет перезапускался (состояние
# челленджа живёт в памяти). Отказ обязан быть видимым, а не 500 — это обычное
# состояние старой вкладки, а не сбой сервера.
LOGIN_TELEGRAM_NOT_STARTED = "Вход не начат или устарел. Начните вход заново."
# Прочий отказ бота на опросе (расхождение контракта, неизвестный код). Общий
# текст, но с действием: начать заново, а не «обновите страницу».
LOGIN_TELEGRAM_FAILED = "Не удалось проверить вход. Начните вход заново."
# Бот недоступен на этом пути. Отдельно от «отклонено»: причина не в человеке, и
# действие другое — повторить позже. Тот же выбор, что у `LOGIN_BOT_UNREACHABLE`
# на входе по почте (#49).
LOGIN_TELEGRAM_BOT_UNREACHABLE = (
    "Сервис сейчас не отвечает — проверить вход не удалось. Начните заново чуть позже."
)

# Отказы бота на опросе, у которых есть свой текст. Список закрытый и назван
# кодами контракта (§1, §5), а не текстом сообщения: текст меняется, код — нет.
# Незнакомый код сюда не попадает и получает `LOGIN_TELEGRAM_FAILED`: выдумывать
# ему причину на экране значило бы выдавать догадку за сообщение сервиса.
LOGIN_TELEGRAM_POLL_ERRORS = {
    "nonce_mismatch": LOGIN_TELEGRAM_CODE_ERROR,
    "operation_not_found": LOGIN_TELEGRAM_EXPIRED,
    "already_consumed": LOGIN_TELEGRAM_EXPIRED,
    # Свой секрет не совпал — снаружи это неотличимо от «челленджа нет», и
    # подробность выдала бы, что кабинет разошёлся с ботом по своему состоянию.
    "invalid_secret": LOGIN_TELEGRAM_NOT_STARTED,
}


def _unreachable_screen(message: str) -> HTMLResponse:
    """Честный экран «до бота сейчас не дотянуться» (issue #49).

    Один экран на все роуты, которые читают аккаунт у бота, и намеренно без
    подробностей о том, какой именно запрос не прошёл: сеть бота, таймаут и обрыв
    соединения для человека — одно состояние, и различать их на экране значило бы
    показывать ему внутреннее устройство сервиса.

    Пустой список подписок здесь **не** «подписок нет»: отказ показан текстом
    (`error`), а честная пустота — это отдельное состояние экрана (W4.3).
    Спутать их — и недоступность читалась бы как «у вас ничего нет», а человек не
    пошёл бы обновлять страницу.

    Код ответа — `200`, как у соседних отказов: это состояние экрана, а не ошибка
    HTTP, и браузер не должен подменять разметку своей страницей отказа.
    """
    return HTMLResponse(render_subscriptions([], error=message))


# Журнал вебхука. Он появился ради оплаты: подтверждение приходит извне, никем не
# подписано, и то, что мы отклонили, обязано быть видно — иначе подделка или
# настоящий платёж с устаревшим адресом исчезнут бесследно. Ни суммы, ни тела
# уведомления, ни ключей в записи не попадает: журнал читают шире, чем задумано,
# а репозиторий и развёртывание — публичные.
logger = logging.getLogger(__name__)


def _fulfil_paid_order(
    store: AccountStore, mail_sender: MailSender, order: Order, order_id: str
) -> None:
    """Выдать конфиги по оплаченному заказу и отправить их письмом.

    Вызывается **только** на первом подтверждении (`changed`): повтор уведомления
    не должен выдать второй комплект и не должен отправить второе письмо.

    Сбой здесь не отменяет оплату и не превращается в ошибку вебхука: деньги
    приняты и записаны, конфиги выдает бот — а провайдеру, который повторит
    уведомление, повторять уже нечего (заказ отмечен оплаченным). Поэтому всё
    исключение остаётся в журнале, а человек всё равно получает доступ: конфиги
    лежат в подписках кабинета, и экран возврата туда и ведёт.
    """
    try:
        issued = store.issue_order(order_id)
    except (BotError, BotUnreachable) as exc:
        logger.warning("заказ %s оплачен, выдача не прошла: %r", order_id, exc)
        return

    try:
        account = store.find_by_id(order.account_id)
    except (BotError, BotUnreachable) as exc:
        logger.warning("заказ %s: аккаунт не прочитан: %r", order_id, exc)
        return
    recipient = (account.mail if account else None) or ""
    if not recipient:
        # Аккаунт, вошедший только через Telegram, письма не ждёт: адреса нет.
        # Это не сбой — ему доступен кабинет, и подписки там же. Пишем, чтобы
        # отсутствие письма не выглядело потерянным.
        logger.info("заказ %s: адреса для письма нет, конфиги только в кабинете", order_id)
        return

    attachments: list[tuple[str, bytes, str]] = []
    for config in issued.configs:
        try:
            content = store.get_ovpn(order.account_id, config.id)
        except (BotError, BotUnreachable) as exc:
            logger.warning("заказ %s: конфиг %s не скачан: %r", order_id, config.id, exc)
            continue
        attachments.append((f"{config.id}.ovpn", content, "application/x-openvpn-profile"))
    if not attachments:
        logger.warning("заказ %s: ни одного файла для письма, письмо не отправлено", order_id)
        return

    try:
        # Нулевая сумма — пробный период: в письме не должно быть слова «оплата»,
        # потому что денег не было. Одно слово, а человек по нему ищет списание.
        mail_sender.send_configs(recipient, attachments=attachments, paid=order.amount_rub > 0)
    except MailError as exc:
        logger.warning("заказ %s: письмо не ушло: %r", order_id, exc)


def build_app(
    *,
    store: AccountStore | None = None,
    mail_sender: MailSender | None = None,
    now: Callable[[], datetime] | None = None,
    config_source: ConfigSource | None = None,
    telegram_auth: TelegramAuthClient | None = None,
    payments: Payments | None = None,
) -> FastAPI:
    """Собрать приложение, приняв зависимости параметрами.

    Единственная точка сборки. Роуты получают готовые сервисы и не знают, что
    под ними: подставной бот или живой. На W6 сюда передадут `BotClient` и
    отправителя из окружения — правок в роутах не потребуется, а значит не
    потребуется и следить, не остался ли какой-то из них на моке.

    Часы — тоже зависимость: экраны (сроки подписок, TTL сессии) зависят от
    «сейчас», и подать их снаружи — то же требование детерминированности, что в
    `sessions` и `subscriptions`.
    """
    store = store if store is not None else _default_store()
    mail_sender = mail_sender if mail_sender is not None else _default_mail_sender()
    payments = payments if payments is not None else _default_payments()
    clock = now if now is not None else (lambda: datetime.now(UTC))
    # Клиент входа по Telegram — отдельная зависимость, а не часть хранилища: это
    # **другой** сервис бота (публичный API), другой адрес и без общего секрета.
    # Умолчание указывает на loopback, где клиентский API и слушает; боевой адрес
    # передаёт `build_from_env`.
    telegram_auth = telegram_auth if telegram_auth is not None else TelegramAuthClient()
    # Состояние начатых челленджей. В памяти процесса, как коды `AuthMail`: срок
    # челленджа задаёт бот, и переживать перезапуск этому состоянию незачем.
    telegram_login = TelegramLogin(now=clock)

    auth_mail = AuthMail(mail_sender=mail_sender, now=clock)
    # Адрес незавершённой привязки почты по аккаунту. Рядом с `auth_mail`, но
    # отдельно: тот хранит коды по адресу, а экрану нужно знать, какому
    # аккаунту какой адрес показывать (см. `pending_mail`).
    pending = PendingMail()
    sessions = SessionService(store=SingleSessionStore(), accounts=store, now=clock)
    # Связывание держим отдельной ссылкой: экрану способов входа нужны и
    # привязка, и отвязка, а `SignInService` берёт только привязку почты.
    # Один объект, а не два: «сколько способов входа у аккаунта сейчас» должно
    # считаться в одном месте, иначе привязка и отвязка разойдутся в ответе.
    linking = Linking(store=store, auth_mail=auth_mail)
    signin = SignInService(
        store=store,
        auth_mail=auth_mail,
        sessions=sessions,
        # Связывание подключено: экран входа не единственная дверь, и живой
        # клиент его поддерживает. Отсутствие `linking` сделало бы `link_mail`
        # отказом развёртывания.
        linking=linking,
    )
    # Скачивание конфига — своя зависимость, а не метод хранилища: право
    # решает подписка, а частота ограничена (`configs.ConfigService`). Источник
    # по умолчанию — адаптер над хранилищем: `.ovpn` отдаёт бот, а кабинет
    # только передаёт байты (§4 контракта).
    source = config_source if config_source is not None else _StoreConfigSource(store)
    configs = ConfigService(source=source, limiter=DownloadLimiter(now=clock))

    # `docs_url=None`: это внутренний сервис за nginx, наружу документация не
    # выставлена (CLAUDE.md, «Границы сервиса»). Открытая `/docs` — это карта
    # внутренних ручек, которой снаружи быть не должно.
    app = FastAPI(docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.store = store
    app.state.sessions = sessions
    app.state.signin = signin
    app.state.clock = clock
    app.state.linking = linking
    app.state.pending_mail = pending
    app.state.configs = configs
    app.state.telegram_auth = telegram_auth
    app.state.telegram_login = telegram_login
    app.state.payments = payments

    # Cookie сессии живёт ровно столько, сколько её серверная запись, а запись
    # продлевается при каждом заходе (`SessionService.resolve`). Cookie же,
    # выпущенная один раз при входе, несла бы прежний `Max-Age` — и активного
    # человека выбрасывало бы через TTL от **первого** входа, то есть ровно то,
    # что продление и отменяет (issue #53).
    #
    # Обновление стоит здесь, а не в роутах: в каждом из них это была бы копия,
    # которую однажды забудут приписать новому экрану. Ответ, который сам решает
    # судьбу cookie (вход выдаёт новую, выход гасит), не трогаем — иначе
    # продление затирало бы только что выданный токен.
    @app.middleware("http")
    async def _renew_session_cookie(request: Request, call_next):
        response = await call_next(request)
        token = request.cookies.get(COOKIE_NAME)
        if not token or _answers_with_session_cookie(response):
            return response

        view = sessions.resolve(token)
        if view is not None:
            _set_session_cookie(response, view.token, max_age=sessions.ttl)
        return response

    _add_login_routes(
        app,
        signin=signin,
        sessions=sessions,
        store=store,
        telegram_auth=telegram_auth,
        telegram_login=telegram_login,
    )
    _add_subscription_routes(
        app,
        store=store,
        sessions=sessions,
        clock=clock,
        configs=configs,
        payments=payments,
        mail_sender=mail_sender,
    )
    _add_account_routes(
        app,
        store=store,
        sessions=sessions,
        linking=linking,
        clock=clock,
        auth_mail=auth_mail,
        pending=pending,
        telegram_auth=telegram_auth,
        telegram_login=telegram_login,
    )
    return app


def _add_login_routes(
    app: FastAPI,
    *,
    signin: SignInService,
    sessions: SessionService,
    store: AccountStore,
    telegram_auth: TelegramAuthClient,
    telegram_login: TelegramLogin,
) -> None:
    """Роуты входа: форма, запрос кода, ввод кода.

    Замыкания, а не глобальные функции: сервисы передаются из `build_app`, и
    роут остаётся привязан к **той** зависимости, что собрана при запуске.
    Глобальная зависимость позволила бы тесту подменить сервис, который в
    другом тесте остался бы собран из мока, — и «мок» вернулся бы через заднюю
    дверь.
    """

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> RedirectResponse:
        """Корень ведёт к подпискам — на них сессия проверяется сама.

        Отдельного «дома» нет: главный экран кабинета — подписки, и лишний
        редирект через `/` только запутал бы адресную строку.
        """
        return RedirectResponse("/subscriptions", status_code=303)

    @app.get("/terms", response_class=HTMLResponse)
    def terms_page() -> Response:
        """Условия сервиса — тот документ, с которым соглашается человек.

        Экран входа говорит: «нажимая кнопку, вы соглашаетесь с условиями». Это
        обещание выполнимо, только если условия **открываются**. До этой правки
        ссылка вела в 404: живая проверка нашла это на боевом кабинете, и
        нашлась она потому, что рядом появилась гарантия возврата — обещать
        возврат и не показывать его условия значит обещать чужое.
        """
        return HTMLResponse(render_legal("terms"))

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy_page() -> Response:
        """Политика конфиденциальности — вторая ссылка с экрана входа."""
        return HTMLResponse(render_legal("privacy"))

    @app.get("/login", response_class=HTMLResponse)
    def login_form(
        request: Request,
        mail: str | None = Query(default=None),
    ) -> Response:
        """Форма входа: почта или Telegram.

        Оба пути на одном экране (§1): почта — самостоятельный вход, Telegram —
        рядом. Значение `mail` возвращается в поле, когда форма вернулась с
        ошибкой, — человеку не нужно набирать адрес заново.

        Отказ прошлого перехода приходит одноразовой cookie и **здесь же
        гасится** (`_take_login_notice`): адрес остаётся `/login`, а
        перезагрузка отказ не повторяет (issue #55). Cookie читается до рендера
        и удаляется в ответе — тем же движением.

        Параметра `error` здесь нет намеренно (issue #58). Читатель без писателя
        — не безобидный мусор: `GET /login?error=<текст>` показывал этот текст
        на нашей странице, и ссылку с чужим текстом можно было выдать за нашу.
        Текст отказа существует ровно одним путём — одноразовой cookie, которую
        ставит сам кабинет.
        """
        notice = _take_login_notice(request)
        mail = mail or _notice_str(notice.get("mail")) or None
        # Воронка начинается с показа экрана входа (issue #64). Здесь же заводится
        # идентификатор посещения — первый шаг анонимный, `account_id` у него нет.
        visitor, is_new = _visitor_of(request)
        _record_event(store, visitor=visitor, step="login_shown")
        response = HTMLResponse(
            render_login(
                mail=mail,
                error=_notice_str(notice.get("error")) or None,
                retry_after=_notice_int(notice.get("retry_after")),
            )
        )
        response.delete_cookie(LOGIN_NOTICE_COOKIE, path="/")
        return _set_visitor_cookie(response, visitor, is_new=is_new, max_age=sessions.ttl)

    @app.post("/login")
    def login_request(request: Request, mail: str = Form(default="")) -> Response:
        """Запросить код на адрес и увести на экран ввода кода.

        Код шлёт `AuthMail` внутри `SignInService`; роут только переводит отказ
        в состояние экрана. `RateLimited` (слишком частая отправка) не сводится
        к общему отказу: у него есть срок ожидания, и экран показывает число —
        живым отсчётом (`_login_refusal` кладёт и текст, и секунды в cookie).
        """
        address = mail.strip().lower()
        if not address:
            return _login_refusal(LOGIN_MAIL_REQUIRED, mail=mail)
        try:
            signin.request_mail_code(address)
        except RateLimited as exc:
            return _login_refusal(
                RATE_LIMIT_ERROR.format(seconds=exc.retry_after_seconds),
                mail=address,
                retry_after=exc.retry_after_seconds,
            )
        except (SignInError, AuthMailError):
            # Один текст на «адрес пуст», «письмо не ушло» и прочие отказы: не
            # выдаём, кто зарегистрирован (§3.3).
            return _login_refusal(LOGIN_MAIL_ERROR, mail=address)
        # Шаг воронки — только на успехе: письмо ушло (issue #64). Отказ сюда не
        # пишется, иначе «запросил код» считался бы и у тех, кому код не ушёл.
        visitor, is_new = _visitor_of(request)
        _record_event(store, visitor=visitor, step="code_requested")
        # Успех: адрес уходит в query — его знает только этот переход, и
        # хранить его между запросами незачем. Отказ так не делается: там
        # ссылка обязана остаться голой (issue #55).
        response = RedirectResponse(f"/login/waiting?mail={quote(address)}", status_code=303)
        return _set_visitor_cookie(response, visitor, is_new=is_new, max_age=sessions.ttl)

    @app.get("/login/waiting", response_class=HTMLResponse)
    def login_waiting(
        request: Request,
        mail: str = Query(default=""),
    ) -> Response:
        """Экран ввода кода из письма.

        Отказ кода приходит тем же одноразовым способом, что на входе: cookie
        читается и гасится здесь, поэтому перезагрузка не повторяет «код не
        подошёл» (issue #55).

        Параметра `error` нет по той же причине, что у формы входа (issue #58):
        текст отказа — состояние перехода, а не адрес.
        """
        notice = _take_login_notice(request)
        mail = mail or _notice_str(notice.get("mail"))
        response = HTMLResponse(
            render_login_waiting(
                mail=mail,
                error=_notice_str(notice.get("error")) or None,
                retry_after=_notice_int(notice.get("retry_after")),
            )
        )
        response.delete_cookie(LOGIN_NOTICE_COOKIE, path="/")
        return response

    @app.post("/login/waiting")
    def login_verify(
        request: Request,
        mail: str = Form(default=""),
        code: str = Form(default=""),
    ) -> Response:
        """Проверить код и выдать сессию в cookie.

        Успех — редирект на подписки, а не рендер: после входа в адресной
        строке должен стоять раздел, куда человек попал, иначе «обновить»
        повторит POST кода.
        """
        try:
            result = signin.sign_in_with_mail(mail, code)
        except MailSignInError:
            return _waiting_refusal(CODE_ERROR, mail=mail)
        except SignInError:
            # Не отказ кода, а расхождение состояния — тот же экран, но текст
            # про сбой, а не про цифры.
            return _waiting_refusal(LOGIN_MAIL_ERROR, mail=mail)
        except BotUnreachable:
            # Код верен, а аккаунт прочитать не у кого: `sign_in_with_mail` ищет
            # его у бота (`find_by_mail`) и там же заводит. Недоступность бота —
            # не «код не подошёл» и не сбой сервера (#49). На форму входа, а не
            # на экран кода: код уже погашен проверкой, и держать поле ввода
            # значило бы предложить ввести то, что не примут (`LOGIN_BOT_UNREACHABLE`).
            return _login_refusal(LOGIN_BOT_UNREACHABLE, mail=mail)

        # Шаг воронки: успешный вход. Здесь уже есть `account_id` — по нему бот
        # и сшивает анонимные шаги с человеком (issue #64).
        visitor, is_new = _visitor_of(request)
        _record_event(store, visitor=visitor, step="signed_in", account_id=result.account.id)
        response = RedirectResponse("/subscriptions", status_code=303)
        _set_session_cookie(response, result.session.token, max_age=sessions.ttl)
        return _set_visitor_cookie(response, visitor, is_new=is_new, max_age=sessions.ttl)

    @app.post("/login/verify")
    def login_verify_alias(
        request: Request,
        mail: str = Form(default=""),
        code: str = Form(default=""),
    ) -> Response:
        """Адрес формы ввода кода (`render_login_waiting.form_action`).

        Обработчик тот же, что у `/login/waiting`: адрес формы собирает роутер
        (`render.py`, умолчание `/login/verify`), и держать под ним 404 значило
        бы, что экран шлёт код в никуда. Отдельный роут, а не второй роут в
        шаблоне: шаблон не должен решать, куда идёт форма.
        """
        return login_verify(request, mail=mail, code=code)

    @app.post("/login/resend")
    def login_resend(mail: str = Form(default="")) -> RedirectResponse:
        """Повторно отправить код на адрес с экрана ожидания.

        Тот же путь, что у первого запроса, и та же защита: отказ сводится к
        одному тексту, чтобы по ответу нельзя было выяснить, кто зарегистрирован
        (§3.3). Отдельно от `/login` только потому, что форма ожидания помнит
        адрес и слать его из формы входа значило бы вернуть человека к началу.
        """
        address = mail.strip().lower()
        if not address:
            return _waiting_refusal(LOGIN_MAIL_REQUIRED, mail=mail)
        try:
            signin.request_mail_code(address)
        except RateLimited as exc:
            return _waiting_refusal(
                RATE_LIMIT_ERROR.format(seconds=exc.retry_after_seconds),
                mail=address,
                retry_after=exc.retry_after_seconds,
            )
        except (SignInError, AuthMailError):
            return _waiting_refusal(LOGIN_MAIL_ERROR, mail=address)
        return RedirectResponse(f"/login/waiting?mail={quote(address)}", status_code=303)

    @app.get("/login/telegram", response_class=HTMLResponse)
    def login_telegram() -> Response:
        """Начать вход через Telegram и показать ссылку бота.

        Челлендж начинается здесь: кабинет заводит пару «код + секрет», отдаёт
        боту **хеш секрета** и получает ссылку `deep_link`. Секрет остаётся в
        памяти процесса (`TelegramLogin`) — в разметку уходит только код, он и
        так у человека в ссылке.

        Личность здесь не заявляется ничем: `chat_id` в этом запросе нет вовсе.
        Он появится только в ответе бота на опрос, после того как человек
        подтвердит вход в Telegram (§3.3). Подставить его было бы той самой
        ошибкой авто-связывания, ради которой написана §3.3.
        """
        public_code, secret = public_code_and_secret()
        try:
            link = telegram_auth.start_login(public_code, secret_hash(secret))
        except BotUnreachable:
            # Бот не ответил — начать не удалось, и это не отказ человека.
            # Свой текст и **без** кнопки «Открыть Telegram»: ссылки нет.
            return HTMLResponse(
                render_login_telegram(telegram_url=None, error=LOGIN_TELEGRAM_BOT_UNREACHABLE)
            )
        except BotError:
            # Осмысленный отказ бота на старте (расхождение контракта). Причину
            # на экран не выносим: человеку действие одно — повторить.
            return HTMLResponse(
                render_login_telegram(telegram_url=None, error=LOGIN_TELEGRAM_FAILED)
            )

        if not link.deep_link:
            # Бот ответил успехом, но ссылки в ответе нет. Показывать нечего, и
            # пустая кнопка обещала бы переход, которого сервис не сделал.
            return HTMLResponse(
                render_login_telegram(telegram_url=None, error=LOGIN_TELEGRAM_FAILED)
            )

        telegram_login.remember(
            public_code, secret=secret, deep_link=link.deep_link, expires_at=link.expires_at
        )
        return HTMLResponse(
            render_login_telegram(telegram_url=link.deep_link, public_code=public_code)
        )

    @app.post("/login/telegram")
    def login_telegram_restart() -> RedirectResponse:
        """«Войти заново» — начать новый челлендж.

        Раньше здесь был честный отказ («флоу не подключён»); теперь заново —
        это буквально заново: `GET /login/telegram` заводит новый код и секрет, а
        старый челлендж гасит бот своим сроком. Отказывать тут значило бы
        оставить тупик ровно там, где экран советует начать заново.
        """
        return RedirectResponse("/login/telegram", status_code=303)

    @app.post("/login/telegram/verify")
    def login_telegram_verify(
        request: Request,
        public_code: str = Form(default=""),
        code: str = Form(default=""),
    ) -> Response:
        """Проверить вход у бота и, если подтверждён, выдать сессию кабинета.

        `account_id` приходит **только** отсюда — из ответа бота на опрос (§5).
        Ни форма, ни `chat_id` в ней на это не влияют: `chat_id` передаётся в
        `sign_in_with_telegram` потому, что этого требует сигнатура, и при
        заданном `account_id` не используется вовсе (см. `signin.py`). Если
        `account_id` в ответе нет — отказ, а не догадка по `chat_id`.

        Текст отказа выбирается по причине: ожидание, отказ человека, неверный
        код, истёкший челлендж и недоступность сервиса требуют разных действий.
        Сессия не выдаётся ни на одном отказе, и ни один отказ не выходит 500.
        """
        challenge = telegram_login.get(public_code)
        if challenge is None:
            # Челленджа нет: не начинали, он протух или кабинет перезапускался.
            # Это обычное состояние старой вкладки, а не сбой сервера.
            return HTMLResponse(render_login_telegram(error=LOGIN_TELEGRAM_NOT_STARTED))

        try:
            poll = telegram_auth.poll_login(public_code, challenge.secret, code.strip())
        except BotUnreachable:
            return _telegram_retry(challenge, public_code, LOGIN_TELEGRAM_BOT_UNREACHABLE)
        except BotError as exc:
            return _telegram_retry(
                challenge,
                public_code,
                LOGIN_TELEGRAM_POLL_ERRORS.get(exc.code, LOGIN_TELEGRAM_FAILED),
            )

        if poll.status == POLL_PENDING:
            return _telegram_retry(challenge, public_code, LOGIN_TELEGRAM_PENDING)
        if poll.status == POLL_DENIED:
            return _telegram_retry(challenge, public_code, LOGIN_TELEGRAM_DENIED)
        if poll.status != POLL_CONFIRMED or poll.account_id is None or poll.chat_id is None:
            # `confirmed` без `account_id` — расхождение контракта (§5). Вывести
            # аккаунт из `chat_id` здесь значило бы открыть вход в обход
            # подтверждения ботом: `chat_id` в ответе есть, но кабинет ему не
            # верит — он пользуется только `account_id`.
            return _telegram_retry(challenge, public_code, LOGIN_TELEGRAM_FAILED)

        # Челлендж израсходован ботом (операция односторонняя) — свой гасим тем
        # же движением, а не оставляем протухать.
        telegram_login.forget(public_code)
        try:
            result = signin.sign_in_with_telegram(poll.chat_id, account_id=poll.account_id)
        except BotUnreachable:
            # Бот подтвердил вход, но аккаунт прочитать не удалось: сессии нет, и
            # обещать её нельзя. Текст — про сервис, а не про код: код человек
            # ввёл верно.
            return _telegram_retry(challenge, public_code, LOGIN_TELEGRAM_BOT_UNREACHABLE)
        except SignInError:
            # Расхождение состояния: бот назвал аккаунт, которого нет. Гасить
            # нечего — сессии не было.
            return _telegram_retry(challenge, public_code, LOGIN_TELEGRAM_FAILED)

        # Шаг воронки: успешный вход через Telegram — тем же шагом, что вход по
        # почте (issue #64): человек один, и способ входа воронку не раздваивает.
        visitor, is_new = _visitor_of(request)
        _record_event(store, visitor=visitor, step="signed_in", account_id=result.account.id)
        response = RedirectResponse("/subscriptions", status_code=303)
        _set_session_cookie(response, result.session.token, max_age=sessions.ttl)
        return _set_visitor_cookie(response, visitor, is_new=is_new, max_age=sessions.ttl)


def _login_refusal(
    message: str, *, mail: str = "", retry_after: int | None = None
) -> RedirectResponse:
    """Увести на форму входа с отказом — адрес остаётся голым `/login`.

    Текст отказа едет не в ссылке, а в одноразовой cookie (issue #55): ссылка с
    `error=` показывала бы старый отказ при каждой перезагрузке и дублировала
    почту в адресной строке. Адрес в cookie — чтобы человек не набирал его
    заново; он гасится тем же GET, что показывает отказ.
    """
    response = RedirectResponse("/login", status_code=303)
    _set_login_notice(response, error=message, mail=mail, retry_after=retry_after)
    return response


def _waiting_refusal(
    message: str, *, mail: str = "", retry_after: int | None = None
) -> RedirectResponse:
    """Увести на экран кода с отказом — тем же одноразовым способом.

    Адрес экрана — без `error=`: и «код не подошёл», и отказ лимита на повторную
    отправку — состояние экрана, а не ссылка. Адрес почты остаётся в cookie,
    чтобы поле и шаг не терялись между переходами.
    """
    response = RedirectResponse("/login/waiting", status_code=303)
    _set_login_notice(response, error=message, mail=mail, retry_after=retry_after)
    return response


def _set_login_notice(
    response: Response,
    *,
    error: str,
    mail: str = "",
    retry_after: int | None = None,
) -> None:
    """Положить отказ экрана входа в одноразовую cookie.

    Значение — JSON, закодированный `quote`: в cookie недопустимы `;`, `,`,
    пробел и кавычки, а текст отказа и адрес их содержат. `HttpOnly` — значение
    не секрет, но и разметке в нём делать нечего; `Max-Age` короткий, потому что
    это состояние одного перехода, а не данные.
    """
    payload = {"error": error, "mail": mail, "retry_after": retry_after}
    response.set_cookie(
        LOGIN_NOTICE_COOKIE,
        quote(json.dumps(payload, ensure_ascii=False)),
        max_age=LOGIN_NOTICE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _take_login_notice(request: Request) -> dict[str, object]:
    """Прочитать отказ с прошлого перехода. Разбор терпим к мусору.

    Cookie приходит из браузера, и её содержимое может оказаться чем угодно
    (старый формат, обрезанное значение). Отказ — состояние экрана, а не данные:
    непонятное значение читаем как «отказа нет», а не роняем GET. Так же
    поступает `SessionService.resolve` с пустым токеном.
    """
    raw = request.cookies.get(LOGIN_NOTICE_COOKIE)
    if not raw:
        return {}
    try:
        data = json.loads(unquote(raw))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _notice_str(value: object) -> str:
    """Значение cookie строкой. Не строка — пустая: экран не выдумывает текст."""
    return value if isinstance(value, str) else ""


def _notice_int(value: object) -> int | None:
    """Секунды ожидания числом или `None`. Ноль и мусор — не отсчёт."""
    return value if isinstance(value, int) and value > 0 else None


def _telegram_retry(challenge: Challenge, public_code: str, message: str) -> HTMLResponse:
    """Экран Telegram с текстом отказа: челлендж жив, сессии нет.

    Отдаётся **тот же** экран, а не отдельная страница отказа: человек должен
    остаться там, где кнопка «Проверить вход» и «Войти заново», — иначе после
    «ещё не подтверждён» ему нечем повторить опрос. Ссылку на бота сохраняем
    (`challenge.deep_link`), чтобы «Открыть Telegram ещё раз» продолжала работать.

    Код ответа — `200`, как у соседних состояний экрана: отказ входа — не ошибка
    HTTP, и браузер не должен подменять разметку своей страницей отказа. Сессии
    здесь не выдаётся и не гасится: живая сессия другого входа не при чём, а
    входить пока не во что.
    """
    return HTMLResponse(
        render_login_telegram(
            telegram_url=challenge.deep_link,
            public_code=public_code,
            error=message,
        )
    )


def _set_session_cookie(response: Response, token: str, *, max_age: timedelta) -> None:
    """Положить токен сессии в cookie так, чтобы JS до него не дотянулся.

    `httponly` — токен не читается из разметки; `samesite="lax"` — переход по
    ссылке из письма сохраняет сессию, а сторонний POST её не получает.

    `max_age` — срок, который переживает закрытие браузера. Без него cookie была
    бы сессионной и умирала бы вместе с браузером, а серверная сессия оставалась
    бы живой: кабинет открывают раз в недели, и человек каждый раз возвращался бы
    к форме входа. Срок приходит из `SessionService.ttl` — cookie и серверная
    запись считают его одним местом, а не двумя.

    `secure` здесь **не** ставится: кабинет на этой волне запускается по
    `http://127.0.0.1`, и `Secure`-cookie браузер по http не отправил бы вовсе.
    Боевой кабинет стоит за nginx с TLS, и там флаг выставляется — но это
    параметр развёртывания (W6.7), а не умолчание, ломающее локальный запуск.
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(max_age.total_seconds()),
        httponly=True,
        samesite="lax",
        path="/",
    )


def _answers_with_session_cookie(response: Response) -> bool:
    """Ответ сам распорядился cookie сессии: выдал новую при входе или погасил
    при выходе.

    Переписывать такую cookie поверх нельзя: при входе это подменило бы только
    что выданный токен прежним, а при выходе — вернуло бы удалённую сессию.
    """
    return any(
        header.startswith(f"{COOKIE_NAME}=") for header in response.headers.getlist("set-cookie")
    )


def _visitor_of(request: Request) -> tuple[str, bool]:
    """Идентификатор посещения для воронки (issue #64): из cookie или новый.

    Новое значение — случайные байты (`secrets`), без данных человека. Возвращается
    пара «значение, новое ли»: cookie ставит вызывающий на **своём** ответе — там,
    где оно понадобилось впервые (`GET /login`), — а дальше посещение
    переиспользуется тем же cookie.
    """
    visitor = request.cookies.get(VISITOR_COOKIE)
    if visitor:
        return visitor, False
    return secrets.token_urlsafe(16), True


def _record_event(
    store: AccountStore,
    *,
    visitor: str,
    step: str,
    account_id: int | None = None,
    order_id: str | None = None,
) -> None:
    """Записать шаг воронки у бота (issue #64).

    Правило нерушимое: **аналитика не ломает экраны**. Бот может не ответить,
    отказать или вернуть не то — для человека это не меняет ничего, а ошибка
    уходит в журнал (`logger`). Хранилище может вовсе не уметь запись
    (подставной бот) — это тот же «события нет», а не сбой экрана.
    """
    record = getattr(store, "record_event", None)
    if record is None:
        return
    try:
        record(account_id=account_id, visitor=visitor, step=step, order_id=order_id)
    except Exception as exc:  # noqa: BLE001 - аналитика не ломает экран
        logger.warning("событие воронки %s не записано: %r", step, exc)


def _set_visitor_cookie(
    response: Response, visitor: str, *, is_new: bool, max_age: timedelta
) -> Response:
    """Поставить cookie посещения, если оно новое, и вернуть ответ.

    `HttpOnly` и `SameSite=Lax` — те же, что у сессии, и по тем же причинам:
    значение не секрет, но разметке в нём делать нечего, а переход по внешней
    ссылке обязан донести его до кабинета. `Secure` не ставится ради локального
    `http://127.0.0.1` (как у `_set_session_cookie`). Уже известное посещение
    cookie не переписывает: иначе каждое событие заводило бы нового человека.
    """
    if is_new:
        response.set_cookie(
            VISITOR_COOKIE,
            visitor,
            max_age=int(max_age.total_seconds()),
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


def _add_subscription_routes(
    app: FastAPI,
    *,
    store: AccountStore,
    sessions: SessionService,
    clock: Callable[[], datetime],
    configs: ConfigService,
    payments: Payments,
    mail_sender: MailSender,
) -> None:
    """Роуты подписок, покупки и оплаты. Экран подписок — он же вход в кабинет.

    `mail_sender` нужен именно здесь, а не только на входе: после подтверждения
    оплаты конфигурации уходят **письмом** (вебхук), и это единственная доставка
    для человека, который не может открыть Telegram.
    """

    @app.get("/subscriptions", response_class=HTMLResponse)
    def subscriptions_page(request: Request) -> Response:
        """Список подписок вошедшего аккаунта; без сессии — на вход.

        Без сессии здесь **редирект**, а не 500 и не пустой экран: это обычное
        состояние первого захода, а не сбой. Токен приходит из cookie, и
        «cookie нет» — не отказ, ради которого стоило бы бросать исключение
        (тот же контракт у `SessionService.resolve`).

        Сессия берётся из cookie, а не из localStorage: JS до токена дотянуться
        не должен (см. модульный docstring).
        """
        view = sessions.resolve(request.cookies.get(COOKIE_NAME))
        if view is None:
            return RedirectResponse("/login", status_code=303)

        try:
            account = store.find_by_id(view.account_id)
        except BotUnreachable:
            # Бот не ответил — это не «аккаунта нет»: сессия цела, кабинет на
            # время ослеп. Тот же экран, что у соседнего `subscriptions_for`
            # (#49): код ответа 200 и текст, а не страница сбоя сервера, и
            # **без** гашения сессии — выйти человек не пытался.
            return _unreachable_screen(SUBSCRIPTIONS_ERROR)
        if account is None:
            # Аккаунт исчез при живой сессии — расхождение состояния. Гасим
            # сессию и уводим на вход: подписок показывать не от кого.
            sessions.sign_out(request.cookies.get(COOKIE_NAME))
            return RedirectResponse("/login", status_code=303)

        try:
            raw = store.subscriptions_for(view.account_id)
        except Exception:  # noqa: BLE001 - сбой хранилища переводим в экран
            return HTMLResponse(render_subscriptions([], error=SUBSCRIPTIONS_ERROR))
        views: list[SubscriptionView] = subscription_views(raw, now=clock())
        balance, referral, telegram_required = _account_money(store, view.account_id)
        return HTMLResponse(
            render_subscriptions(
                views,
                balance=balance,
                referral=referral,
                telegram_required=telegram_required,
            )
        )

    @app.get("/buy", response_class=HTMLResponse)
    def buy_page(
        request: Request,
        days: int | None = Query(default=None),
        devices: int | None = Query(default=None),
    ) -> Response:
        """Экран покупки: выбор срока и числа устройств, с ценой каждого.

        Без сессии — редирект на вход, как у соседнего `/subscriptions`: это
        обычное состояние первого захода, а не сбой сервера и не экран чужого
        аккаунта. Сессии на экране больше ни к чему: цену человек выбирает
        до всякой покупки, и заказывать пока нечего.

        Неизвестный выбор в адресе (`?days=45&devices=9`) подменяется умолчанием,
        а не отдаётся 500: `calculate_price` на неподдерживаемый срок бросает
        `ValueError`, и чужая опечатка в ссылке не должна валить страницу. Ноль
        на неизвестный срок тоже не показывается — это продало бы подписку даром.
        """
        view = sessions.resolve(request.cookies.get(COOKIE_NAME))
        if view is None:
            return RedirectResponse("/login", status_code=303)
        visitor, is_new = _visitor_of(request)
        # Аккаунт передаём там, где сессия уже есть: иначе шаг «открыл тарифы»
        # остался бы без владельца, и выборка «все события этого аккаунта»
        # потеряла бы его — при том что человек в этот момент уже вошёл. Нашла
        # живая проверка: у соседнего `confirm_shown` аккаунт был, здесь нет.
        _record_event(store, visitor=visitor, step="buy_shown", account_id=view.account_id)
        response = HTMLResponse(
            render_buy(
                days=days if days in PRICES else DEFAULT_BUY_DAYS,
                devices=devices if devices in PURCHASE_DEVICES else DEFAULT_BUY_DEVICES,
            )
        )
        return _set_visitor_cookie(response, visitor, is_new=is_new, max_age=sessions.ttl)

    @app.get("/buy/confirm", response_class=HTMLResponse)
    def buy_confirm_page(
        request: Request,
        days: int | None = Query(default=None),
        devices: int | None = Query(default=None),
        free: bool = False,
    ) -> Response:
        """Шаг 3 покупки: сводка заказа и честное право на триал.

        Цену на экран кладёт рендер — из калькулятора, не из адреса: адрес
        называет выбор, а не сумму, и принятая из адреса цена позволила бы
        купить за рубль.

        Право на триал спрашивается у бота (`trial-eligibility`) **здесь**, а не
        на шаге 1–2: на этом шаге проверка делается, и её ответ — факт, а не
        оговорка. Недоступность бота (`_trial_eligibility`) даёт отдельное
        состояние экрана: сбой сервиса не должен читаться как «триал вам не
        положен».

        Без сессии — редирект на вход, как у соседних экранов: это обычное
        состояние первого захода, а не сбой. Аккаунт нужен именно для проверки
        права: триал привязан к человеку, а не к паре «срок + устройства».
        """
        view = sessions.resolve(request.cookies.get(COOKIE_NAME))
        if view is None:
            return RedirectResponse("/login", status_code=303)
        visitor, is_new = _visitor_of(request)
        _record_event(store, visitor=visitor, step="confirm_shown", account_id=view.account_id)
        trial, trial_unreachable = _trial_eligibility(store, view.account_id)
        response = HTMLResponse(
            render_buy_confirm(
                days=days if days in PRICES else DEFAULT_BUY_DAYS,
                devices=devices if devices in PURCHASE_DEVICES else DEFAULT_BUY_DEVICES,
                trial=trial,
                trial_unreachable=trial_unreachable,
                # Признак бесплатного пути экран не проверяет, а **несёт дальше**:
                # проверка права стоит на шаге оплаты, там же, где выдаётся
                # конфиг. Здесь он нужен только чтобы кнопка и адрес не потеряли
                # бесплатную пару по дороге.
                free=free,
            )
        )
        return _set_visitor_cookie(response, visitor, is_new=is_new, max_age=sessions.ttl)

    @app.get("/buy/servers", response_class=HTMLResponse)
    def buy_servers_page(
        request: Request,
        days: int | None = Query(default=None),
        devices: int | None = Query(default=None),
        # `Annotated`, а не `= Query(...)`: список в значении по умолчанию
        # изменяем, и ruff справедливо не считает его неизменным (`B008`).
        pick: Annotated[list[str] | None, Query()] = None,
        free: bool = False,
    ) -> Response:
        """Шаг 4 покупки: выбрать ровно столько серверов, сколько устройств.

        Выбор приходит в адресе повторяющимся `pick` (`?pick=fi&pick=fi` —
        комплект из одинаковых), поэтому экран работает без JavaScript и без
        черновика на сервере: набор восстанавливается из ссылки. Набор
        обрезается по числу устройств — сверх нужного выбирать нечего, и
        чужая ссылка с лишним `pick` не должна показывать невозможное состояние.

        Список серверов берётся у бота. Недоступность бота — не «серверов нет»,
        а отказ: пустой список был бы фактом, которого кабинет не получал.
        """
        if sessions.resolve(request.cookies.get(COOKIE_NAME)) is None:
            return RedirectResponse("/login", status_code=303)
        chosen = devices if devices in PURCHASE_DEVICES else DEFAULT_BUY_DEVICES
        try:
            servers = store.list_servers()
            error: str | None = None
        except (BotError, BotUnreachable):
            servers, error = [], BUY_SERVERS_ERROR
        # Выбор сверяется со списком, который кабинет получил: код, которого в
        # списке нет, пришёл из чужой или устаревшей ссылки, и считать его
        # выбранным значило бы показать состояние, которого нет, — «Готово:
        # выбрано ровно столько» при пустом экране. Обрезка по числу устройств
        # остаётся: сверх нужного выбирать нечего.
        known = {server.code for server in servers}
        picks = [code for code in (pick or []) if code in known][:chosen]
        return HTMLResponse(
            render_buy_servers(
                days=days if days in PRICES else DEFAULT_BUY_DAYS,
                devices=chosen,
                servers=servers,
                picked=picks,
                error=error,
                free=free,
            )
        )

    def _pay_screen(
        state: str,
        days: int,
        devices: int,
        picks: list[str] | None,
        *,
        free: bool = False,
        note: str | None = None,
    ) -> HTMLResponse:
        """Экран отказа на шаге оплаты: текст и действие по состоянию.

        Оба адреса (повтор и возврат к серверам) собираются из того же набора,
        что пришёл: набор у них один, и вторую копию формата держать незачем.
        Признак `free` едет в них вместе с набором — иначе повтор запустил бы
        платный путь вместо бесплатного.

        `note` — причина отказа, когда её знает не экран, а бот (отказ в
        бесплатном доступе).
        """
        query = _purchase_query(days, devices, list(picks or []), free=free)
        return HTMLResponse(
            render_buy_pay(
                state=state,
                days=days,
                devices=devices,
                back_url=f"/buy/servers?{query}",
                retry_url=f"/buy/pay?{query}",
                note=note,
            )
        )

    def _free_access(
        view: SessionView, account_store: AccountStore, days: int, devices: int, picks: list[str]
    ) -> Response:
        """Бесплатный путь: проверка права → заказ с нулевой суммой → выдача.

        Право проверяется **здесь**, а не только на экране подтверждения:
        `free=1` приходит из адреса, а адрес можно набрать руками. Признак
        бесплатного пути не должен сам по себе ничего давать — иначе бесплатный
        доступ получал бы любой, кто дописал параметр.

        Пара берётся из констант, а не из адреса (`days`/`devices` сюда приходят
        уже подменёнными на бесплатные): `free=1&days=360&devices=3` иначе выдал
        бы год на три устройства даром.

        Сумма заказа — **ноль**, а не цена тридцати дней: бесплатность здесь не
        скидка, а право на пробный период, и платёж по такому заказу не
        создаётся вовсе.
        """
        trial, unreachable = _trial_eligibility(account_store, view.account_id)
        if unreachable:
            return _pay_screen("bot_unreachable", days, devices, picks, free=True)
        if trial is None or not trial.eligible:
            if trial is not None and trial.reason == "telegram_required":
                return _pay_screen("telegram_required", days, devices, picks, free=True)
            return _pay_screen(
                "trial_refused",
                days,
                devices,
                picks,
                free=True,
                note=trial_refusal_note(trial.reason if trial is not None else None),
            )
        try:
            order = account_store.create_order(
                account_id=view.account_id,
                days=days,
                devices=devices,
                servers=picks,
                amount_rub=0,
            )
        except BotError as exc:
            state = "telegram_required" if exc.code == "telegram_required" else "rejected"
            return _pay_screen(state, days, devices, picks, free=True)
        except BotUnreachable:
            return _pay_screen("bot_unreachable", days, devices, picks, free=True)

        # Заказ подтверждается тем же способом, что оплаченный: `changed` здесь
        # тоже первый переход, и выдача с письмом идут одним путём. Платежа нет —
        # и это не пропущенный шаг: подтверждением служит право на триал,
        # проверенное выше.
        try:
            account_store.mark_order_paid(order.order_id, amount_rub=0)
        except (BotError, BotUnreachable):
            return _pay_screen("bot_unreachable", days, devices, picks, free=True)
        _fulfil_paid_order(account_store, mail_sender, order, order.order_id)
        return RedirectResponse(f"{BUY_DONE_URL}?order_id={quote(order.order_id)}", status_code=303)

    @app.get("/buy/pay", response_class=HTMLResponse)
    def buy_pay_page(
        request: Request,
        days: int | None = Query(default=None),
        devices: int | None = Query(default=None),
        pick: Annotated[list[str] | None, Query()] = None,
        free: bool = False,
    ) -> Response:
        """Шаг 5: собрать заказ и завершить покупку — оплатой или бесплатно.

        **Цена не читается из адреса.** Её считает `calculate_price` по паре
        `days`/`devices`, она же записывается в заказ бота, и платёж берёт сумму
        **из заказа** (`order.amount_rub`). Взятая от браузера сумма позволила бы
        купить за рубль — это первый пункт Review Focus плана.

        `free=True` — бесплатный путь, и тогда пара **не из адреса**, а
        бесплатная (30 дней, одно устройство): право на триал проверяется здесь
        же, и без подмены пары `free=1` в ссылке давал бы что угодно даром.

        Состояния разведены и не смешиваются: нет сессии — на вход; у аккаунта
        нет Telegram — бот отказывает `telegram_required`, и экран объясняет, что
        конфиг выдаётся Telegram-аккаунту (граница сервиса, а не сбой); бот не
        ответил — своё состояние; платёж не создан — своё; право на бесплатный
        доступ не подтверждено — своё, с причиной от бота. Успех — **редирект**
        (на страницу ЮKassa или на экран возврата), а не экран.
        """
        view = sessions.resolve(request.cookies.get(COOKIE_NAME))
        if view is None:
            return RedirectResponse("/login", status_code=303)
        if free:
            chosen_days, chosen_devices = DEFAULT_BUY_DAYS, DEFAULT_BUY_DEVICES
        else:
            chosen_days = days if days in PRICES else DEFAULT_BUY_DAYS
            chosen_devices = devices if devices in PURCHASE_DEVICES else DEFAULT_BUY_DEVICES

        try:
            servers = store.list_servers()
        except (BotError, BotUnreachable):
            return _pay_screen("bot_unreachable", chosen_days, chosen_devices, pick, free=free)
        # Набор сверяется со списком, который кабинет получил: код, которого в
        # списке нет, пришёл из чужой или устаревшей ссылки, и оформить на него
        # заказ значило бы записать несуществующий сервер. Неполный набор — не
        # ошибка оплаты, а незаконченный шаг 4: возвращаем к выбору.
        known = {server.code for server in servers}
        picks = [code for code in (pick or []) if code in known][:chosen_devices]
        if len(picks) != chosen_devices:
            return _pay_screen("incomplete", chosen_days, chosen_devices, picks, free=free)

        if free:
            return _free_access(view, store, chosen_days, chosen_devices, picks)

        amount = calculate_price(chosen_days, chosen_devices).final_price
        try:
            order = store.create_order(
                account_id=view.account_id,
                days=chosen_days,
                devices=chosen_devices,
                servers=picks,
                amount_rub=amount,
            )
        except BotError as exc:
            # `telegram_required` — своё состояние (граница сервиса), прочие
            # отказы бота — общий текст с действием в заказе, не в оплате.
            state = "telegram_required" if exc.code == "telegram_required" else "rejected"
            return _pay_screen(state, chosen_days, chosen_devices, picks)
        except BotUnreachable:
            return _pay_screen("bot_unreachable", chosen_days, chosen_devices, picks)

        # Шаг воронки: заказ создан — покупка начата (issue #64). Пишется именно
        # здесь, а не при выдаче страницы оплаты: заказ к этому моменту уже есть,
        # и `order_id` в событии настоящий. Отказ на создании заказа сюда не
        # доходит — там шага нет.
        visitor, is_new = _visitor_of(request)
        _record_event(
            store,
            visitor=visitor,
            step="pay_started",
            account_id=view.account_id,
            order_id=order.order_id,
        )

        try:
            payment = payments.create_payment(
                order_id=order.order_id,
                amount_rub=order.amount_rub,
                description=BUY_PAY_DESCRIPTION,
                return_url=payments.return_url(order.order_id),
            )
        except PaymentsError:
            # Магазин не настроен, провайдер недоступен или отказал — всё это
            # «оплата не начата», и это **не** то же, что недоступность бота:
            # заказ уже создан, а страница оплаты не выдана.
            return _pay_screen("payment_failed", chosen_days, chosen_devices, picks)
        if not payment.confirmation_url:
            return _pay_screen("payment_failed", chosen_days, chosen_devices, picks)
        response = RedirectResponse(payment.confirmation_url, status_code=303)
        return _set_visitor_cookie(response, visitor, is_new=is_new, max_age=sessions.ttl)

    @app.get("/buy/done", response_class=HTMLResponse)
    def buy_done_page(
        request: Request,
        order_id: str | None = Query(default=None),
    ) -> Response:
        """Возврат с оплаты: состояние **заказа**, как его отдаёт бот.

        «Оплачено» экран говорит только потому, что так сказал заказ, а не
        потому что человек вернулся на страницу: возврат подделывается,
        подтверждение приходит вебхуком (спека §3). Заказ ещё `pending` — экран
        честно говорит, что подтверждение не пришло (вебхук может дойти через
        секунды), а не объявляет оплату по переходу.

        Без `order_id` показывать нечего; неизвестный заказ (`404 order_not_found`)
        — состояние экрана, а не сбой; недоступность бота — тоже своё состояние.
        Свести их значило бы прочитать недоступность сервиса как «заказа нет».
        """
        view = sessions.resolve(request.cookies.get(COOKIE_NAME))
        if view is None:
            return RedirectResponse("/login", status_code=303)
        if not order_id:
            return HTMLResponse(render_buy_done(not_found=True))
        refresh = f"/buy/done?order_id={quote(order_id)}"
        try:
            order = store.get_order(order_id)
        except BotError as exc:
            if exc.code == "order_not_found" or exc.status == 404:
                return HTMLResponse(render_buy_done(not_found=True))
            return HTMLResponse(render_buy_done(unreachable=True, refresh_url=refresh))
        except BotUnreachable:
            return HTMLResponse(render_buy_done(unreachable=True, refresh_url=refresh))
        # Чужой заказ показывается как несуществующий. Номер заказа — случайный,
        # но это единственное, что его охраняет: подтверждать, что заказ с таким
        # номером есть, значит отдавать чужую покупку за знание номера.
        if order.account_id != view.account_id:
            return HTMLResponse(render_buy_done(not_found=True))
        return HTMLResponse(render_buy_done(order=order, refresh_url=refresh))

    @app.post("/payments/webhook/yookassa")
    def yookassa_webhook(
        request: Request,
        # `Annotated`, а не `= Body(...)`: ruff справедливо не считает вызов
        # функции в значении по умолчанию неизменным (`B008`), и у соседних
        # роутов та же форма записи.
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> JSONResponse:
        """Уведомление ЮKassa — единственное основание выдать оплаченное.

        Тело уведомления **не авторитетно**: подписи у уведомлений современного
        API нет (issue #61), поэтому «оплачено» берётся не отсюда, а из ответа
        ЮKassa на перезапрос платежа. Порядок:

        1. событие — только `payment.succeeded`, остальные игнорируются;
        2. адрес источника сверяется с сетями ЮKassa, но по нему **не отказываем**:
           список стареет, и отказ по устаревшему списку стоил бы настоящих
           покупок. Несовпадение — строка в журнале, проверка идёт дальше;
        3. платёж перезапрашивается у провайдера, и заказ берётся из **его**
           `metadata`: так подделанное уведомление не может подменить заказ,
           потому что `metadata.order_id` ставит только кабинет;
        4. статус (`succeeded`) и сумма сверяются с заказом;
        5. заказ отмечается оплаченным — и **только на первом** переходе
           выдаются конфиги и уходит письмо. Повтор уведомления не создаёт ни
           второго комплекта, ни второго письма.

        Коды ответа — часть договора с провайдером: ЮKassa повторяет уведомление,
        если ответ не 2xx. Поэтому **200** там, где повтор не поможет (чужое
        событие, уже отмечено, сумма не сошлась, заказа нет), и **503** там, где
        повтор нужен: не дозвонились до ЮKassa или до бота. Ответ 200 в этом
        случае «съел» бы оплату — уведомление больше не придёт, а заказ остался
        бы неоплаченным навсегда.

        IP берётся из `X-Real-IP`. Это заголовок, а не адрес соединения, и верить
        ему можно потому, что кабинет слушает loopback за нашим nginx, который
        перезаписывает его своим `$remote_addr`; `X-Forwarded-For` для этого не
        годится — nginx его **дополняет** присланным клиентом.
        """
        data = payload or {}
        if str(data.get("event") or "") != "payment.succeeded":
            return JSONResponse({"ok": True})
        payment_id = str((data.get("object") or {}).get("id") or "")
        if not payment_id:
            return JSONResponse({"ok": True})

        source = request.headers.get("x-real-ip")
        if not is_trusted_notification_ip(source):
            logger.warning("вебхук: адрес источника вне сетей ЮKassa (%r), проверяю платёж", source)

        try:
            payment = payments.get_payment(payment_id)
        except PaymentsTransportError:
            # Провайдер недоступен: уведомление стоит повторить.
            return JSONResponse({"ok": False}, status_code=503)
        except PaymentsError:
            # Провайдер ответил отказом — повтор ничего не изменит.
            logger.warning("вебхук: платёж %s не прочитан у провайдера", payment_id)
            return JSONResponse({"ok": True})

        if payment.status != PAYMENT_STATUS_SUCCEEDED:
            return JSONResponse({"ok": True})
        order_id = payment.order_id or ""
        if not order_id:
            # Платёж без нашего заказа в метаданных: чужой или созданный не нами.
            logger.warning("вебхук: у платежа %s нет order_id в метаданных", payment_id)
            return JSONResponse({"ok": True})

        try:
            order = store.get_order(order_id)
        except BotUnreachable:
            return JSONResponse({"ok": False}, status_code=503)
        except BotError:
            logger.warning("вебхук: заказ %s не найден", order_id)
            return JSONResponse({"ok": True})

        if payment.amount_rub != order.amount_rub:
            logger.warning(
                "вебхук: сумма платежа %s не совпала с суммой заказа %s", payment_id, order_id
            )
            return JSONResponse({"ok": True})

        try:
            paid = store.mark_order_paid(order_id, amount_rub=payment.amount_rub)
        except BotUnreachable:
            return JSONResponse({"ok": False}, status_code=503)
        except BotError as exc:
            logger.warning("вебхук: заказ %s не отмечен оплаченным: %r", order_id, exc)
            return JSONResponse({"ok": True})

        if paid.changed:
            # Первое подтверждение — единственный момент, когда покупка состоялась
            # (issue #64). `visitor` пуст: вебхук приходит от провайдера, cookie
            # посещения у него нет, а человек здесь опознан `account_id`. Повтор
            # уведомления сюда не доходит — `changed` уже был `false`.
            _record_event(
                store, visitor="", step="paid", account_id=order.account_id, order_id=order_id
            )
            _fulfil_paid_order(store, mail_sender, order, order_id)
            # `issued` — там же, после выдачи конфигов и письма: это то же
            # подтверждение, что и `paid`, и воронка закрывается уже выданным
            # доступом, а не только принятыми деньгами.
            _record_event(
                store, visitor="", step="issued", account_id=order.account_id, order_id=order_id
            )
        return JSONResponse({"ok": True})

    @app.get("/subscriptions/{subscription_id}/config")
    async def subscription_config(request: Request, subscription_id: str) -> Response:
        """Скачать `.ovpn` по подписке — право решает подписка, а не запрос.

        Адрес называет **подписку**, а не конфиг: конфиг у подписки один, и
        отдельный `config_id` в адресе позволил бы перебирать чужие конфиги
        (W4.2, `render.CONFIG_DOWNLOAD_BASE`).

        Отказы возвращаются **тем же экраном подписок** с текстом на карточке
        нажатой подписки (`config_error` + `config_error_id`), а не отдельной
        страницей: человек нажал «Скачать» здесь — здесь и читает ответ, не
        теряя из виду, к чему он относился (W5.4). Код ответа при этом 200:
        это состояние экрана, а не ошибка HTTP, и браузер не должен подменять
        разметку своей страницей отказа.
        """
        view = sessions.resolve(request.cookies.get(COOKIE_NAME))
        if view is None:
            return RedirectResponse("/login", status_code=303)

        try:
            raw = store.subscriptions_for(view.account_id)
            views = subscription_views(raw, now=clock())
        except Exception:  # noqa: BLE001 - сбой хранилища переводим в экран
            return HTMLResponse(render_subscriptions([], error=SUBSCRIPTIONS_ERROR))

        chosen = _find_subscription(views, subscription_id)
        if chosen is None:
            # Подписки с таким id у аккаунта нет — тот же отказ, что «нет
            # конфига / чужой конфиг»: различить их снаружи нечем.
            return _config_refusal_screen(
                store,
                view.account_id,
                views,
                ConfigNotFound(SUBSCRIPTIONS_UNKNOWN_CONFIG),
                subscription_id,
            )

        try:
            download = await configs.download(
                account_id=view.account_id,
                config_id=subscription_id,
                subscription=chosen,
            )
        except BotUnreachable:
            # Бот не ответил на запрос профиля — отказ не по логике, и конфига
            # из него взяться не может. Это не «конфиг не найден» (`ConfigError`
            # ниже, отказ на карточке) и не 500: скачивание просто не состоялось,
            # и повторить его надо позже (#49). Список уже получен — экран
            # показывается тот же, с текстом вместо файла.
            return HTMLResponse(render_subscriptions(views, error=BOT_UNREACHABLE_ERROR))
        except ConfigError as exc:
            return _config_refusal_screen(store, view.account_id, views, exc, subscription_id)

        # Содержимое не печатается и не логируется: репозиторий публичный,
        # `.ovpn` наружу из ответа не уходит (CLAUDE.md, «Публичность»).
        return Response(
            content=download.content,
            media_type=download.content_type,
            headers={"Content-Disposition": f'attachment; filename="{download.filename}"'},
        )


def _find_subscription(
    views: list[SubscriptionView], subscription_id: str
) -> SubscriptionView | None:
    """Подписка аккаунта по id из адреса или `None`.

    Список уже отфильтрован по аккаунту (`subscriptions_for`), поэтому чужой
    id сюда просто не попадает — и это тот же случай, что «нет такого».
    """
    for view in views:
        if str(view.id) == str(subscription_id):
            return view
    return None


def _config_refusal_screen(
    store: AccountStore,
    account_id: int,
    views: list[SubscriptionView],
    error: ConfigError,
    subscription_id: str,
) -> Response:
    """Экран подписок с отказом скачивания.

    Отказ в норме ложится **на карточку** нажатой подписки: причина показана
    там же, где действие (W5.4), и остаётся различимой — текст берётся по типу
    отказа в `render`.

    Карточки может не быть вовсе: адрес назвал подписку, которой у аккаунта нет
    (чужой id или опечатка). Тогда `config_error_id` ни на что не ложится, и
    `render` молчит — а человек увидел бы обычный список без объяснения, почему
    скачивание не началось. Поэтому здесь отказ уходит в общий текст экрана
    (`error`), а не теряется: молчание на отказ читается как сбой страницы.
    """
    balance, referral, telegram_required = _account_money(store, account_id)
    on_card = _find_subscription(views, subscription_id) is not None
    return HTMLResponse(
        render_subscriptions(
            views,
            error=None if on_card else SUBSCRIPTIONS_UNKNOWN_CONFIG,
            config_error=error if on_card else None,
            config_error_id=str(subscription_id) if on_card else None,
            balance=balance,
            referral=referral,
            telegram_required=telegram_required,
        )
    )


def _add_account_routes(
    app: FastAPI,
    *,
    store: AccountStore,
    sessions: SessionService,
    linking: Linking,
    clock: Callable[[], datetime],
    auth_mail: AuthMail,
    pending: PendingMail,
    telegram_auth: TelegramAuthClient,
    telegram_login: TelegramLogin,
) -> None:
    """Экран способов входа и действия привязки/отвязки.

    До этого блока роутов не было вовсе: шаблон `linking.html` и
    `render.render_linking` существовали, но страница не была подключена, а
    формы слали на адреса без обработчиков (`/account/link/*`,
    `/account/unlink/*`) — человек видел кнопку, которая ничего не делала, а
    запрос отвечал 404. Здесь адреса получают обработчики.

    Подтверждение Telegram **не подделывается**. `Linking.link_telegram` требует
    `confirmed=True`, и это подтверждение приходит из флоу бота (`/auth/link` +
    `/auth/poll`, §5 контракта) в режиме `link`: бот подтверждает `chat_id`, не
    заводя аккаунт, а кабинет отдаёт его существующему аккаунту (issue #65).
    Присвой кабинет `confirmed=True` сам — сломалась бы главная защита проекта
    (§3.3: привязка только после подтверждения владения). `chat_id` из формы или
    адреса не берётся нигде: он приходит **только** из ответа бота, подтвердившего
    человека.
    """

    def _account(request: Request) -> tuple[Account, str] | Response:
        """Аккаунт по сессии и её токен — либо **готовый ответ**, если аккаунта нет.

        Исходов три, и свести их к двум нельзя (#49):

        - сессии нет — на вход (`RedirectResponse`);
        - аккаунт исчез (`404 account_not_found`) — тем же редиректом, но **с
          гашением сессии**: подписок и способов входа показывать не от кого;
        - бот недоступен (`BotUnreachable`) — экран с отказом и **живой сессией**:
          кабинет на время ослеп, а человек никуда не выходил.

        Ответ отдаётся отсюда, а не исключением: у всех роутов этого блока первое
        действие — «взять аккаунт», и решение «что показать вместо него» обязано
        быть в одном месте, иначе одна ручка однажды осталась бы без него.
        """
        view = sessions.resolve(request.cookies.get(COOKIE_NAME))
        if view is None:
            return RedirectResponse("/login", status_code=303)
        try:
            account = store.find_by_id(view.account_id)
        except BotUnreachable:
            return _unreachable_screen(BOT_UNREACHABLE_ERROR)
        if account is None:
            sessions.sign_out(request.cookies.get(COOKIE_NAME))
            return RedirectResponse("/login", status_code=303)
        return account, request.cookies.get(COOKIE_NAME) or ""

    def _previews(account: Account) -> dict[str, object]:
        """Последствия отвязки от бота, если он их умеет отдавать.

        Бот может не ответить — тогда данных нет, и экран молчит о конфигах,
        а не выдумывает последствие (`render._unlink_notes`). Показывать
        догадку о числе конфигов страшнее их отсутствия.

        Ожидаемый ответ (живой `BotClient.unlink_preview` — корутина) сюда не
        попадает: эти экраны синхронные, а недожденный preview в разметке всё
        равно прочёлся бы как «данных нет». Свести асинхронный флоу бота —
        работа W6; пока о последствиях говорит подставной бот, отвечающий
        значением.
        """
        preview = getattr(store, "unlink_preview", None)
        if preview is None or account.id is None:
            return {}
        out: dict[str, object] = {}
        for method in ("mail", "telegram"):
            try:
                value = preview(account.id, method)
            except Exception:  # noqa: BLE001 - сбой preview не ломает экран
                continue
            if not inspect.isawaitable(value):
                out[method] = value
        return out

    def _render(account: Account, **kwargs: object) -> HTMLResponse:
        """Экран способов входа с последствиями отвязки, но без их пересчёта."""
        return HTMLResponse(render_linking(account, **kwargs))  # type: ignore[arg-type]

    def _link_telegram_retry(
        challenge: Challenge | None,
        public_code: str | None,
        message: str,
        *,
        account: Account,
    ) -> HTMLResponse:
        """Экран привязки Telegram с текстом отказа: челлендж жив, привязки нет.

        Отдаётся **тот же** экран, а не отдельная страница отказа: человек должен
        остаться там, где кнопка «Проверить привязку» и «Начать заново», — иначе
        после «ещё не подтверждена» ему нечем повторить опрос. Ссылку на бота
        сохраняем (`challenge.deep_link`), чтобы «Открыть Telegram» продолжала
        работать.

        Когда челленджа нет (`challenge is None` — не начинали или протух),
        показывать нечего, кроме отказа: ссылки и поля кода нет, и экран
        возвращается к стартовой кнопке с текстом.

        Код ответа — `200`, как у соседних состояний экрана: отказ — не ошибка
        HTTP. Сессии здесь не выдаётся и не гасится: живая сессия другого входа
        не при чём.
        """
        if challenge is None or public_code is None:
            return _render(account, error=message, unlink_previews=_previews(account) or None)
        return _render(
            account,
            error=message,
            telegram_link_url=challenge.deep_link,
            telegram_link_code=public_code,
            unlink_previews=_previews(account) or None,
        )

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request) -> Response:
        """Экран способов входа. Без сессии — на вход, а не 500.

        Шаг ввода кода восстанавливается здесь, а не только в ответе на
        отправку: из почтового ящика человек возвращается на `/account` обычным
        GET, и без этого поле кода пропадало при живом коде в письме (#44).
        `live_address` отдаёт адрес, только пока код действует: погасший код
        возвращает экран к форме адреса.
        """
        resolved = _account(request)
        if isinstance(resolved, Response):
            return resolved
        account, _ = resolved
        return _render(
            account,
            mail_pending=pending.live_address(account.id, auth_mail),
            unlink_previews=_previews(account) or None,
        )

    @app.post("/account/link/telegram")
    def link_telegram_action(request: Request) -> Response:
        """Начать привязку Telegram: челлендж бота в режиме `link` (§3.3, #65).

        Кабинет заводит пару «код + секрет» и просит у бота ссылку в режиме
        **привязки**: бот подтвердит `chat_id`, не заводя аккаунт. Обычный вход
        здесь не годится — свежий `chat_id` в режиме `login` занял бы новый
        аккаунт, и привязка конфликтовала бы сама с собой (issue #65).

        Личность здесь не заявляется ничем: `chat_id` в этом запросе нет и быть
        не может. Он появится только в ответе бота на опрос, после подтверждения
        в Telegram, и роут проверки (§3.3) отдаст его связыванию. Подставить
        `chat_id` из формы значило бы открыть связывание по заявлению браузера —
        ровно класс CVE-2026-53516 / nOAuth.

        Секрет остаётся в памяти процесса (`TelegramLogin`) — в разметку уходит
        только код: он и так у человека в ссылке.
        """
        resolved = _account(request)
        if isinstance(resolved, Response):
            return resolved
        account, _ = resolved

        public_code, secret = public_code_and_secret()
        try:
            link = telegram_auth.start_login(public_code, secret_hash(secret), mode=AUTH_MODE_LINK)
        except BotUnreachable:
            # Бот не ответил — начать не удалось, и это не отказ человека. Свой
            # текст и **без** кнопки «Открыть Telegram»: ссылки нет.
            return _render(
                account,
                error=LINK_TELEGRAM_BOT_UNREACHABLE,
                unlink_previews=_previews(account) or None,
            )
        except BotError:
            # Осмысленный отказ бота на старте (расхождение контракта). Причину
            # на экран не выносим: человеку действие одно — повторить.
            return _render(
                account,
                error=LINK_TELEGRAM_FAILED,
                unlink_previews=_previews(account) or None,
            )

        if not link.deep_link:
            # Бот ответил успехом, но ссылки в ответе нет: показывать нечего, и
            # пустая кнопка обещала бы переход, которого сервис не сделал.
            return _render(
                account,
                error=LINK_TELEGRAM_FAILED,
                unlink_previews=_previews(account) or None,
            )

        telegram_login.remember(
            public_code, secret=secret, deep_link=link.deep_link, expires_at=link.expires_at
        )
        return _render(
            account,
            telegram_link_url=link.deep_link,
            telegram_link_code=public_code,
            unlink_previews=_previews(account) or None,
        )

    @app.post("/account/link/telegram/verify")
    def link_telegram_verify(
        request: Request,
        public_code: str = Form(default=""),
        code: str = Form(default=""),
    ) -> Response:
        """Проверить привязку у бота и отдать `chat_id` существующему аккаунту.

        `chat_id` приходит **только** отсюда — из ответа бота на опрос (§5), в
        форме режима привязки: `{"status": "confirmed", "chat_id": <id>}` без
        `account_id` и без `session_token`. Форма и адрес на это не влияют: в
        разметке поля `chat_id` нет, а подставленное в тело значение не читается
        вовсе — `Linking.link_telegram` получает `chat_id` из ответа бота.

        Сессия здесь **не** выдаётся: привязка — не вход. Человек уже вошёл;
        привязка лишь добавляет способ. Режим `link` для того и заведён, чтобы
        подтверждение привязки не выдавало сессию (§5, issue #65).

        Текст отказа выбирается по причине: ожидание, отказ человека, неверный
        код, истёкший челлендж, занятый `chat_id` и недоступность сервиса
        требуют разных действий, и ни один отказ не выходит 500.
        """
        resolved = _account(request)
        if isinstance(resolved, Response):
            return resolved
        account, _ = resolved

        challenge = telegram_login.get(public_code)
        if challenge is None:
            # Челленджа нет: не начинали, он протух или кабинет перезапускался.
            # Обычное состояние старой вкладки, а не сбой сервера.
            return _link_telegram_retry(None, None, LINK_TELEGRAM_NOT_STARTED, account=account)

        try:
            poll = telegram_auth.poll_login(public_code, challenge.secret, code.strip())
        except BotUnreachable:
            return _link_telegram_retry(
                challenge, public_code, LINK_TELEGRAM_BOT_UNREACHABLE, account=account
            )
        except BotError as exc:
            return _link_telegram_retry(
                challenge,
                public_code,
                LINK_TELEGRAM_POLL_ERRORS.get(exc.code, LINK_TELEGRAM_FAILED),
                account=account,
            )

        if poll.status == POLL_PENDING:
            return _link_telegram_retry(
                challenge, public_code, LINK_TELEGRAM_PENDING, account=account
            )
        if poll.status == POLL_DENIED:
            return _link_telegram_retry(
                challenge, public_code, LINK_TELEGRAM_DENIED, account=account
            )
        if poll.status != POLL_CONFIRMED or poll.chat_id is None:
            # `confirmed` без `chat_id` — расхождение контракта (§5): привязывать
            # нечего, а выдумывать `chat_id` нельзя.
            return _link_telegram_retry(
                challenge, public_code, LINK_TELEGRAM_FAILED, account=account
            )

        # Челлендж израсходован ботом (операция односторонняя) — свой гасим тем
        # же движением, а не оставляем протухать.
        telegram_login.forget(public_code)
        try:
            result = linking.link_telegram(account.id, chat_id=poll.chat_id, confirmed=True)  # type: ignore[arg-type]
        except BotUnreachable:
            # Бот подтвердил привязку, но запись не прошла: привязки нет, и
            # обещать её нельзя. Текст — про сервис, а не про код.
            return _link_telegram_retry(
                challenge, public_code, LINK_TELEGRAM_BOT_UNREACHABLE, account=account
            )
        except LinkRefused as exc:
            # `chat_id_taken` — свой честный текст: человек доказал владение этим
            # Telegram, и «занят другим аккаунтом» подсказывает, что делать.
            # Прочие отказы (уже привязан, аккаунт пропал) — общим текстом §5.
            message = LINK_TELEGRAM_TAKEN if exc.code == CHAT_ID_TAKEN else LINK_REFUSED_ERROR
            return _link_telegram_retry(challenge, public_code, message, account=account)

        # Успех: аккаунт показывается из результата (`LinkResult.account`
        # перечитан из хранилища), а не из прежнего чтения.
        return _render(result.account, notice=LINK_DONE_TELEGRAM)

    @app.post("/account/link/mail")
    def link_mail_action(request: Request, mail: str = Form(default="")) -> Response:
        """Привязка почты. Код запрашивается, а связывание — после кода (§3.3).

        Почта не привязывается «по заявлению»: `Linking.link_mail` требует
        подтверждённый кодом адрес. Здесь делается только первый шаг — код уходит
        на адрес, — а экран после него держит шаг ввода: показать форму адреса
        снова значило бы не принять код, который уже отправлен (issue #44).

        Адрес нормализуется, как на входе (`signin._normalize_mail`): запись на
        `User@Example.com` разошлась бы с входом, который ищет
        `user@example.com`, и человек завёл бы второй аккаунт на тот же ящик.
        """
        resolved = _account(request)
        if isinstance(resolved, Response):
            return resolved
        account, _ = resolved

        address = mail.strip().lower()
        if not address:
            return _render(
                account,
                error=LINK_MAIL_REQUIRED,
                unlink_previews=_previews(account) or None,
            )

        try:
            signin = app.state.signin
            signin.request_mail_code(address)
        except RateLimited as exc:
            # `RateLimited` — подкласс `AuthMailError`, и ветка обязана стоять
            # выше неё: иначе «слишком часто» свелось бы к общему отказу и
            # потеряло срок ожидания. Адрес остаётся в шаге — код из первого
            # письма всё ещё действует, и терять шаг нельзя, поэтому
            # `mail_pending`, а не возврат к форме адреса.
            pending.remember(account.id, address)
            return _render(
                account,
                error=RATE_LIMIT_ERROR.format(seconds=exc.retry_after_seconds),
                mail_pending=address,
                unlink_previews=_previews(account) or None,
            )
        except (SignInError, AuthMailError):
            return _render(
                account,
                error=LINK_REFUSED_ERROR,
                unlink_previews=_previews(account) or None,
            )

        # Код ушёл — адрес остаётся в памяти до подтверждения: человек уйдёт в
        # почту и вернётся на `/account` GET-ом, и шаг обязан его дождаться.
        pending.remember(account.id, address)
        return _render(
            account,
            notice=LINK_MAIL_CODE_SENT.format(mail=address),
            notice_tone=Tone.PROCESS.value,
            mail_pending=address,
            unlink_previews=_previews(account) or None,
        )

    @app.post("/account/link/mail/confirm")
    def link_mail_confirm(
        request: Request,
        mail: str = Form(default=""),
        code: str = Form(default=""),
    ) -> Response:
        """Подтвердить код и довести привязку почты до конца (issue #44).

        Это второй шаг того же сценария, что и `link_mail_action`: там код уходит
        на адрес, здесь он предъявляется. Кода не было, он истёк или не совпал —
        отказ на экране, а не 500: `SignInService.link_mail` переводит отказы
        `AuthMail` в `MailSignInError` (`signin._code_refusal`).

        Аккаунт берётся **из сессии**, а не из формы. Адрес приезжает скрытым
        полем и подменяем, поэтому он ничему не верит на слово: код хранится
        **по адресу** (`AuthMail`), и код, выданный на другой ящик, у
        подставленного не найдётся. Само связывание — не здесь, а в
        `Linking.link_mail` (§3.3: только на пустое место, после проверки кода).

        `SignInError` целиком здесь **не** ловится: в этом вызове он значит
        «связывание не подключено», то есть ошибку развёртывания. Показать её
        как «не удалось привязать» значило бы спрятать настоящий сбой.

        На экране успеха аккаунт — из результата (`LinkResult.account` перечитан
        из хранилища), а не из прежнего чтения: показать надо то, что записано.
        """
        resolved = _account(request)
        if isinstance(resolved, Response):
            return resolved
        account, _ = resolved

        address = mail.strip().lower()
        try:
            result = app.state.signin.link_mail(account.id, address, code)
        except MailSignInError:
            # Код не принят. Поле кода остаётся (`mail_pending`): иначе отказ
            # выглядел бы тупиком, а человек с письмом на руках — без шага.
            return _render(
                account,
                error=CODE_ERROR,
                mail_pending=address,
                unlink_previews=_previews(account) or None,
            )
        except LinkRefused:
            # Один текст на все причины отказа привязки (§5): разница между
            # «занято другим аккаунтом» и «уже привязано» выдала бы перебором
            # адресов, кто где зарегистрирован. Шаг не гасим — причина может
            # быть поправимой (опечатка в адресе).
            return _render(
                account,
                error=LINK_REFUSED_ERROR,
                mail_pending=address,
                unlink_previews=_previews(account) or None,
            )
        except BotUnreachable:
            # Связывание читает аккаунт и занятое место у бота — сбой на этих
            # чтениях (или на самой записи) значит «не дозвонились», а не «почта
            # занята»: показать общий отказ привязки значило бы свалить на
            # человека сбой сети (#49).
            #
            # Про код обещать наугад нельзя, и врать тоже. `Linking.link_mail`
            # читает аккаунт, а `AuthMail.verify` вызывает уже **до** записи
            # (`cabinet/linking.py`), и проверка код гасит — он одноразовый.
            # Значит, упасть на чтении и упасть на записи — разные состояния: в
            # первом письмо ещё действует, во втором код уже израсходован.
            # Общего слова здесь не хватит, поэтому шага кода мы не рисуем вовсе:
            # адрес остаётся в `PendingMail`, но экран отдаёт его только пока код
            # жив (`live_address` спрашивает `AuthMail.has_pending`), так что
            # погашенный код в поле ввода не вернётся — человек увидит форму
            # адреса и попросит письмо заново. Тот же тупик, что обойдён на входе
            # (`LOGIN_BOT_UNREACHABLE`), здесь закрыт уже написанным правилом, и
            # второй копии ему не нужно.
            return _unreachable_screen(BOT_UNREACHABLE_ERROR)

        # Адрес привязан — шаг кода закончен. На отказах выше адрес **не**
        # забывается: код ещё живой, и человеку нужно поле, чтобы повторить
        # ввод, а не просить письмо заново.
        pending.forget(account.id)
        return _render(result.account, notice=LINK_DONE_MAIL)

    @app.post("/account/unlink/telegram")
    def unlink_telegram_action(request: Request) -> Response:
        """Отвязка Telegram. Единственный способ входа — запрет (W3.5).

        Запрет держится в `Linking.unlink_telegram` (и повторно в хранилище);
        роут лишь показывает его человеку текстом на экране. Отсутствие кнопки
        в разметке — не защита: POST можно отправить и без неё, поэтому отказ
        обязан быть видимым здесь, а не только спрятанным в вёрстке.
        """
        resolved = _account(request)
        if isinstance(resolved, Response):
            return resolved
        account, _ = resolved
        return _do_unlink(
            account,
            linking.unlink_telegram,
            store=store,
            success=UNLINK_DONE_TELEGRAM,
            render=_render,
            previews=_previews(account),
        )

    @app.post("/account/unlink/mail")
    def unlink_mail_action(request: Request) -> Response:
        """Отвязка почты. Симметрично отвязке Telegram."""
        resolved = _account(request)
        if isinstance(resolved, Response):
            return resolved
        account, _ = resolved
        return _do_unlink(
            account,
            linking.unlink_mail,
            store=store,
            success=UNLINK_DONE_MAIL,
            render=_render,
            previews=_previews(account),
        )


def _do_unlink(
    account: Account,
    unlink: Callable[[int], object],
    *,
    store: AccountStore,
    success: str,
    render: Callable[..., Response],
    previews: dict[str, object],
) -> Response:
    """Отвязать способ входа и показать исход — успех или запрет.

    Запрет последнего способа (`UnlinkRefused`) — не ошибка человека, которую
    можно пережить: за ней стоит недоступный навсегда аккаунт (W3.5). Поэтому
    он показывается отдельным текстом, а не общим «не удалось привязать»;
    прочие отказы (`LinkRefused`) сводятся к общему — по той же причине, что и
    у привязки: разница между ними выдаёт чужие регистрации (§5).

    `previews` пересчитывается вызывающим **после** отвязки: остаток способов
    изменился, и прежние последствия уже не про это состояние.

    Недоступность бота (`BotUnreachable`) — не отказ человека и не запрет: отвязка
    читает аккаунт перед записью, и между этими двумя запросами бот может
    перестать отвечать (#49). Экран говорит «повторите позже», а не «не удалось
    отвязать» (причина не в том, что человек что-то сделал не так) и тем более не
    показывает сбой сервера. Подтвердить исход отсюда нельзя: запись могла и
    пройти, а узнать об этом не у кого, — поэтому и успеха здесь не рисуется.
    """
    if account.id is None:
        raise RuntimeError("аккаунт без id: хранилище не записало его")
    try:
        result = unlink(account.id)
        updated = getattr(result, "account", None) or store.find_by_id(account.id) or account
    except UnlinkRefused:
        return render(
            account,
            error=UNLINK_LAST_METHOD_ERROR,
            unlink_previews=previews or None,
        )
    except LinkRefused:
        return render(
            account,
            error=LINK_REFUSED_ERROR,
            unlink_previews=previews or None,
        )
    except BotUnreachable:
        return _unreachable_screen(BOT_UNREACHABLE_ERROR)

    return render(updated, notice=success)
