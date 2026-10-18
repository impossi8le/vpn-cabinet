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

Границ сервиса это не пересекает: кабинет по-прежнему не ходит в базу
(CLAUDE.md), а говорит с хранилищем через те же протоколы, что и модули.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from cabinet.accounts import Account
from cabinet.auth_mail import AuthMail, AuthMailError, RateLimited
from cabinet.bot_client import Balance, BotError, Referral
from cabinet.configs import (
    ConfigError,
    ConfigNotFound,
    ConfigService,
    ConfigSource,
    DownloadLimiter,
)
from cabinet.linking import Linking, LinkRefused, UnlinkRefused
from cabinet.mail_sender import MailSender
from cabinet.render import (
    STATIC_DIR,
    render_linking,
    render_login,
    render_login_telegram,
    render_login_waiting,
    render_subscriptions,
)
from cabinet.sessions import SessionService, SingleSessionStore
from cabinet.signin import MailSignInError, SignInError, SignInService
from cabinet.subscriptions import SubscriptionView, subscription_views
from cabinet.theme import Tone

__all__ = ["COOKIE_NAME", "build_app"]

# Имя cookie сессии. Одно на весь кабинет: второе имя означало бы вторую
# сессию, и человек входил бы дважды.
COOKIE_NAME = "cabinet_session"

# Адрес отправителя писем по умолчанию. Это не боевой ящик, а подставной
# (`example.com` зарезервирован для примеров): репозиторий публичный, и
# настоящего домена здесь быть не может. В бою адрес приходит из конфига.
DEFAULT_SENDER = "cabinet@example.com"


class AccountStore(Protocol):
    """Хранилище аккаунтов, как его видит приложение.

    Сведено к тому, что нужно HTTP-слою: прочитать аккаунт по id (для экрана)
    и перечислить подписки. `tests/fakes.FakeApi` и живой `BotClient` дают эту
    форму уже сейчас — иначе стык «приложение ↔ хранилище» пришлось бы
    проверять дважды.
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
    try:
        referral = get_referral(account_id)
    except BotError:
        referral = None
    return balance, referral, False


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
LOGIN_CODE_ERROR = "Код не подошёл. Проверьте цифры из письма и попробуйте ещё раз."
# Повторная отправка подряд отвергнута лимитом. Число секунд подставляется:
# «попробуйте через минуту» без числа не подсказывает, сколько ждать.
LOGIN_RATE_LIMIT = "Слишком часто. Повторите через {seconds} с."
# Адрес пуст или не указан — форма вернулась как есть.
LOGIN_MAIL_REQUIRED = "Укажите адрес почты."
# Сбой подписки: список не получен, а не «подписок нет». Пустой список — это
# отдельное честное состояние (W4.3), и путать его со сбоем нельзя.
SUBSCRIPTIONS_ERROR = "Не удалось получить список подписок. Обновите страницу позже."

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
# Привязка Telegram идёт флоу бота, и это звено ещё не подключено в кабинете
# (W6). Текст честный: кабинет не подставляет `confirmed=True` вручную — это
# сломало бы главную защиту проекта (привязка только после подтверждения
# владения, §3.3). Поэтому экран отправляет открыть бота, а не изображает успех.
LINK_TELEGRAM_BOT_FLOW = (
    "Привязка Telegram идёт через бота сервиса: откройте бота и подтвердите вход. "
    "После подтверждения Telegram появится здесь привязанным."
)
# Код подтверждения почты в разметке экрана привязки пока некуда ввести: форма
# экрана отправляет только адрес. Об этом говорится прямо, чтобы «код отправлен»
# не читалось как тупик по вине человека.
LINK_MAIL_CODE_STEP_PENDING = (
    "Ввод кода на этом экране ещё не подключён — подтверждение адреса завершается в боте."
)

# Экран входа через Telegram: ссылка на бота ещё не готова. Тот же честный
# текст, что у клиента: нарисованная кнопка, ведущая в никуда, обещает переход,
# которого сервис не сделал. Флоу бота (`/auth/link` + `/auth/poll`, §5) в
# кабинете ещё не подключён (W6), поэтому челлендж не начинается здесь, и экран
# говорит об этом прямо, а не изображает ожидание кода.
LOGIN_TELEGRAM_NOT_READY = "Ссылка на вход через Telegram ещё не готова. Начните вход заново."
# Код, введённый на экране Telegram, здесь проверить нечем: подтверждение
# приходит из флоу бота, а не из кабинета. Сказать «код не подошёл» значило бы
# свалить на человека расхождение, которого он не совершал.
LOGIN_TELEGRAM_UNAVAILABLE = (
    "Проверка кода из Telegram ещё не подключена в кабинете. Войдите по почте или начните заново."
)


def build_app(
    *,
    store: AccountStore | None = None,
    mail_sender: MailSender | None = None,
    now: Callable[[], datetime] | None = None,
    config_source: ConfigSource | None = None,
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
    clock = now if now is not None else (lambda: datetime.now(UTC))

    auth_mail = AuthMail(mail_sender=mail_sender, now=clock)
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
    app.state.configs = configs

    _add_login_routes(app, signin=signin, sessions=sessions, store=store)
    _add_subscription_routes(
        app,
        store=store,
        sessions=sessions,
        clock=clock,
        configs=configs,
    )
    _add_account_routes(app, store=store, sessions=sessions, linking=linking, clock=clock)
    return app


def _add_login_routes(
    app: FastAPI,
    *,
    signin: SignInService,
    sessions: SessionService,
    store: AccountStore,
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

    @app.get("/login", response_class=HTMLResponse)
    def login_form(mail: str | None = Query(default=None), error: str | None = None) -> str:
        """Форма входа: почта или Telegram.

        Оба пути на одном экране (§1): почта — самостоятельный вход, Telegram —
        рядом. Значение `mail` возвращается в поле, когда форма вернулась с
        ошибкой, — человеку не нужно набирать адрес заново.
        """
        return render_login(mail=mail, error=error)

    @app.post("/login")
    def login_request(mail: str = Form(default="")) -> RedirectResponse:
        """Запросить код на адрес и увести на экран ввода кода.

        Код шлёт `AuthMail` внутри `SignInService`; роут только переводит отказ
        в состояние экрана. `RateLimited` (слишком частая отправка) не сводится
        к общему отказу: у него есть срок ожидания, и экран показывает число.
        """
        address = mail.strip().lower()
        if not address:
            return _redirect_login_error(LOGIN_MAIL_REQUIRED, mail=mail)
        try:
            signin.request_mail_code(address)
        except RateLimited as exc:
            return _redirect_login_error(
                LOGIN_RATE_LIMIT.format(seconds=exc.retry_after_seconds), mail=address
            )
        except (SignInError, AuthMailError):
            # Один текст на «адрес пуст», «письмо не ушло» и прочие отказы: не
            # выдаём, кто зарегистрирован (§3.3).
            return _redirect_login_error(LOGIN_MAIL_ERROR, mail=address)
        # Адрес уводим в query, а не в cookie: его знает только этот переход,
        # и хранить его между запросами незачем.
        return RedirectResponse(f"/login/waiting?mail={quote(address)}", status_code=303)

    @app.get("/login/waiting", response_class=HTMLResponse)
    def login_waiting(mail: str = Query(default=""), error: str | None = None) -> str:
        """Экран ввода кода из письма."""
        return render_login_waiting(mail=mail, error=error)

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
            return _redirect_waiting_error(LOGIN_CODE_ERROR, mail=mail)
        except SignInError:
            # Не отказ кода, а расхождение состояния — тот же экран, но текст
            # про сбой, а не про цифры.
            return _redirect_waiting_error(LOGIN_MAIL_ERROR, mail=mail)

        response = RedirectResponse("/subscriptions", status_code=303)
        _set_session_cookie(response, result.session.token)
        return response

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
            return _redirect_login_error(LOGIN_MAIL_REQUIRED, mail=mail)
        try:
            signin.request_mail_code(address)
        except RateLimited as exc:
            return _redirect_waiting_error(
                LOGIN_RATE_LIMIT.format(seconds=exc.retry_after_seconds), mail=address
            )
        except (SignInError, AuthMailError):
            return _redirect_waiting_error(LOGIN_MAIL_ERROR, mail=address)
        return RedirectResponse(f"/login/waiting?mail={quote(address)}", status_code=303)

    @app.get("/login/telegram", response_class=HTMLResponse)
    def login_telegram(mail: str | None = Query(default=None)) -> str:
        """Вход через Telegram: экран ожидания кода из бота.

        Флоу бота (`/auth/link` + `/auth/poll`, §5) ещё не подключён в кабинете
        (W6), поэтому челлендж здесь не начинается, и `telegram_url` пуст —
        экран честно говорит, что ссылки нет, а не рисует кнопку в никуда. Это
        то же состояние, что называет клиент («Ссылка на вход ещё не готова»).
        """
        return render_login_telegram(telegram_url=None)

    @app.post("/login/telegram")
    def login_telegram_restart(
        request: Request,
        mail: str = Form(default=""),
        code: str = Form(default=""),
    ) -> Response:
        """«Войти заново» на экране Telegram — возврат к экрану входа.

        Проверять код нечем: подтверждение приходит из флоу бота, а не из
        кабинета. Показать «код не подошёл» значило бы свалить на человека
        неготовность сервиса, поэтому возвращаем на форму входа с честным
        текстом.
        """
        return _redirect_login_error(LOGIN_TELEGRAM_UNAVAILABLE, mail=mail)

    @app.post("/login/telegram/verify")
    def login_telegram_verify(
        request: Request,
        mail: str = Form(default=""),
        code: str = Form(default=""),
    ) -> Response:
        """Проверка кода из бота. Пока нечем: тем же отказом, что у «заново».

        Адрес формы (`render.TELEGRAM_VERIFY_ACTION`) обязан отвечать, а не
        404: иначе нажатие «Подтвердить» на экране Telegram уходит в никуда.
        Флоу бота, который проверит код, подключится на W6; до тех пор ответ
        честный — проверка недоступна, а не «код не подошёл».
        """
        return _redirect_login_error(LOGIN_TELEGRAM_UNAVAILABLE, mail=mail)


def _redirect_login_error(message: str, *, mail: str) -> RedirectResponse:
    """Вернуть на форму входа с текстом отказа и сохранённым адресом."""
    return RedirectResponse(f"/login?error={quote(message)}&mail={quote(mail)}", status_code=303)


def _redirect_waiting_error(message: str, *, mail: str) -> RedirectResponse:
    """Вернуть на экран кода с текстом отказа."""
    return RedirectResponse(
        f"/login/waiting?error={quote(message)}&mail={quote(mail)}", status_code=303
    )


def _set_session_cookie(response: Response, token: str) -> None:
    """Положить токен сессии в cookie так, чтобы JS до него не дотянулся.

    `httponly` — токен не читается из разметки; `samesite="lax"` — переход по
    ссылке из письма сохраняет сессию, а сторонний POST её не получает;
    `secure` здесь **не** ставится: кабинет на этой волне запускается по
    `http://127.0.0.1`, и `Secure`-cookie браузер по http не отправил бы вовсе.
    Боевой кабинет стоит за nginx с TLS, и там флаг выставляется — но это
    параметр развёртывания (W6.7), а не умолчание, ломающее локальный запуск.
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _add_subscription_routes(
    app: FastAPI,
    *,
    store: AccountStore,
    sessions: SessionService,
    clock: Callable[[], datetime],
    configs: ConfigService,
) -> None:
    """Роут подписок и скачивание конфига. Экран один, но он же — вход в кабинет."""

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

        account = store.find_by_id(view.account_id)
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
) -> None:
    """Экран способов входа и действия привязки/отвязки.

    До этого блока роутов не было вовсе: шаблон `linking.html` и
    `render.render_linking` существовали, но страница не была подключена, а
    формы слали на адреса без обработчиков (`/account/link/*`,
    `/account/unlink/*`) — человек видел кнопку, которая ничего не делала, а
    запрос отвечал 404. Здесь адреса получают обработчики.

    Подтверждение Telegram **не подделывается**. `Linking.link_telegram` требует
    `confirmed=True`, а подтверждение приходит из флоу бота (`/auth/link` +
    `/auth/poll`, §5 контракта), который в кабинете ещё не подключён (W6).
    Присвой кабинет `confirmed=True` сам — сломалась бы главная защита проекта
    (§3.3: привязка только после подтверждения владения). Поэтому роут Telegram
    честно говорит «откройте бота», а не изображает успех.
    """

    def _account(request: Request) -> tuple[Account, str] | None:
        """Аккаунт по сессии и её токен. `None` — сессии нет, нужен редирект."""
        view = sessions.resolve(request.cookies.get(COOKIE_NAME))
        if view is None:
            return None
        account = store.find_by_id(view.account_id)
        if account is None:
            sessions.sign_out(request.cookies.get(COOKIE_NAME))
            return None
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

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request) -> Response:
        """Экран способов входа. Без сессии — на вход, а не 500."""
        resolved = _account(request)
        if resolved is None:
            return RedirectResponse("/login", status_code=303)
        account, _ = resolved
        return _render(account, unlink_previews=_previews(account) or None)

    @app.post("/account/link/telegram")
    def link_telegram_action(request: Request) -> Response:
        """Привязка Telegram — только через подтверждение ботом (§3.3).

        Роут не подставляет `confirmed=True`: подтверждение приходит из флоу
        бота, которого в кабинете ещё нет (W6). Показать успех без подтверждения
        значило бы привязать заявленный `chat_id` — ровно та ошибка, от которой
        сторожит §3.3. Поэтому экран честно отправляет открыть бота.
        """
        resolved = _account(request)
        if resolved is None:
            return RedirectResponse("/login", status_code=303)
        account, _ = resolved
        return _render(
            account,
            notice=LINK_TELEGRAM_BOT_FLOW,
            notice_tone=Tone.PROCESS.value,
            unlink_previews=_previews(account) or None,
        )

    @app.post("/account/link/mail")
    def link_mail_action(request: Request, mail: str = Form(default="")) -> Response:
        """Привязка почты. Код запрашивается, а связывание — после кода (§3.3).

        Почта не привязывается «по заявлению»: `Linking.link_mail` требует
        подтверждённый кодом адрес. Здесь делается только первый шаг — код уходит
        на адрес. Завершение (ввод кода) в разметке экрана привязки пока не
        подключено, и об этом сказано прямо, а не изображено кнопкой в никуда.
        """
        resolved = _account(request)
        if resolved is None:
            return RedirectResponse("/login", status_code=303)
        account, _ = resolved

        address = mail.strip()
        if not address:
            return _render(
                account,
                error=LINK_MAIL_REQUIRED,
                unlink_previews=_previews(account) or None,
            )

        try:
            signin = app.state.signin
            signin.request_mail_code(address)
        except SignInError:
            return _render(
                account,
                error=LINK_REFUSED_ERROR,
                unlink_previews=_previews(account) or None,
            )

        return _render(
            account,
            notice=LINK_MAIL_CODE_SENT.format(mail=address) + " " + LINK_MAIL_CODE_STEP_PENDING,
            notice_tone=Tone.PROCESS.value,
            unlink_previews=_previews(account) or None,
        )

    @app.post("/account/unlink/telegram")
    def unlink_telegram_action(request: Request) -> Response:
        """Отвязка Telegram. Единственный способ входа — запрет (W3.5).

        Запрет держится в `Linking.unlink_telegram` (и повторно в хранилище);
        роут лишь показывает его человеку текстом на экране. Отсутствие кнопки
        в разметке — не защита: POST можно отправить и без неё, поэтому отказ
        обязан быть видимым здесь, а не только спрятанным в вёрстке.
        """
        resolved = _account(request)
        if resolved is None:
            return RedirectResponse("/login", status_code=303)
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
        if resolved is None:
            return RedirectResponse("/login", status_code=303)
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
    """
    if account.id is None:
        raise RuntimeError("аккаунт без id: хранилище не записало его")
    try:
        result = unlink(account.id)
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

    updated = getattr(result, "account", None) or store.find_by_id(account.id) or account
    return render(updated, notice=success)
