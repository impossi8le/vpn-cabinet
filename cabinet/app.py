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
from cabinet.linking import Linking
from cabinet.mail_sender import MailSender
from cabinet.render import (
    STATIC_DIR,
    render_login,
    render_login_waiting,
    render_subscriptions,
)
from cabinet.sessions import SessionService, SingleSessionStore
from cabinet.signin import MailSignInError, SignInError, SignInService
from cabinet.subscriptions import SubscriptionView, subscription_views

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


def build_app(
    *,
    store: AccountStore | None = None,
    mail_sender: MailSender | None = None,
    now: Callable[[], datetime] | None = None,
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
    signin = SignInService(
        store=store,
        auth_mail=auth_mail,
        sessions=sessions,
        # Связывание подключено: экран входа не единственная дверь, и живой
        # клиент его поддерживает. Отсутствие `linking` сделало бы `link_mail`
        # отказом развёртывания.
        linking=Linking(store=store, auth_mail=auth_mail),
    )

    # `docs_url=None`: это внутренний сервис за nginx, наружу документация не
    # выставлена (CLAUDE.md, «Границы сервиса»). Открытая `/docs` — это карта
    # внутренних ручек, которой снаружи быть не должно.
    app = FastAPI(docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.store = store
    app.state.sessions = sessions
    app.state.signin = signin
    app.state.clock = clock

    _add_login_routes(app, signin=signin, sessions=sessions, store=store)
    _add_subscription_routes(app, store=store, sessions=sessions, clock=clock)
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
) -> None:
    """Роут подписок. Экран один, но он же — вход в кабинет после сессии."""

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
