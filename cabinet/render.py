"""Рендер страниц кабинета: Jinja2 + палитра из `theme`.

Зачем отдельный слой рендера. Экраны — это разметка, но у них есть два
неизменных входа: палитра (её нельзя писать в шаблоне, иначе копия разойдётся с
`VpnColors`) и адреса форм (их вписывает роутер, а не шаблон). Здесь этот вход
собирается один раз, и шаблон получает уже готовый контекст. Роутеру остаётся
назвать экран и данные, а не помнить, какие переменные ждёт каждый шаблон.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cabinet import pricing, theme
from cabinet.accounts import Account
from cabinet.bot_client import Balance, Referral
from cabinet.configs import (
    ConfigError,
    ConfigNotFound,
    ConfigRetired,
    ConfigRevoked,
    DownloadRateLimited,
    SubscriptionInactive,
)
from cabinet.pricing import PriceQuote
from cabinet.subscriptions import (
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_EXPIRED,
    STATUS_TRIAL,
    STATUS_UNKNOWN,
    SubscriptionView,
)

__all__ = [
    "CONFIG_DOWNLOAD_BASE",
    "CONFIG_ERRORS",
    "CONFIG_ERROR_FALLBACK",
    "CSS_FILE",
    "CSS_URL",
    "LINK_MAIL_ACTION",
    "LINK_MAIL_CONFIRM_ACTION",
    "LINK_TELEGRAM_ACTION",
    "STATIC_DIR",
    "TELEGRAM_ATTEMPTS_EXCEEDED",
    "TELEGRAM_CHECK_LABEL",
    "TELEGRAM_CODE_EXPIRED",
    "TELEGRAM_ENTRY_URL",
    "TELEGRAM_LINK_NOT_READY",
    "TELEGRAM_RESTART_URL",
    "TELEGRAM_STATES",
    "TELEGRAM_VERIFY_ACTION",
    "TEMPLATES_DIR",
    "UNLINK_MAIL_ACTION",
    "UNLINK_TELEGRAM_ACTION",
    "render_linking",
    "render_login",
    "render_login_telegram",
    "render_login_waiting",
    "render_buy",
    "render_subscriptions",
]

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Имя файла стилей, отдаваемого статикой. Одно на весь кабинет: наборы на
# каждый экран разошлись бы, и правка «прибрать цвет» стала бы правкой в
# нескольких местах.
CSS_FILE = STATIC_DIR / "cabinet.css"
CSS_URL = "/static/cabinet.css"

# Адрес политики и условий. Ведёт наружу; в шаблоне он обязан быть, потому что
# ссылки на документы стоят до кнопки входа — обещание, что вход не прячет
# условий, а не украшение.
PRIVACY_URL = "/privacy"
TERMS_URL = "/terms"

# Куда ведёт кнопка «Войти через Telegram» на экране входа.
#
# Это НЕ адрес бота. Репозиторий публичный, и настоящий `https://t.me/<bot>`
# здесь стоять не может — как и любого реального домена. Значение ведёт на
# собственный путь входа кабинета, который в бою отвечает переходом на бота
# (флоу `/auth/link` — §2.1 контракта, защита от login-CSRF). Ссылку
# подставляет роутер, назвав `telegram_url`; умолчание нужно только чтобы
# экран рендерился без роутера — в тестах.
TELEGRAM_ENTRY_URL = "/login/telegram"

# Куда уходит код из бота на проверку и куда ведёт «Заново» при исчерпанных
# попытках. Оба адреса подставляет роутер; умолчания нужны, чтобы экран
# рендерился в тестах без него.
TELEGRAM_VERIFY_ACTION = "/login/telegram/verify"
TELEGRAM_RESTART_URL = TELEGRAM_ENTRY_URL

# Подпись кнопки опроса. Кнопка, а не автоопрос: экран обязан работать без
# JavaScript, и кнопка — единственный способ опросить надёжно. Имя вынесено
# константой, потому что на него смотрит и роут (`app.LOGIN_TELEGRAM_CHECK`).
TELEGRAM_CHECK_LABEL = "Проверить вход"

# Тексты неготовых состояний. Вынесены в константы, а не вписаны по месту в
# разметке: их сверяет тест по имени, и так строка живёт в одном месте.
#
# «Ссылка ещё не готова» — не украшение, а честность: клиент показывает ровно
# то же («Ссылка на вход ещё не готова. Нажмите «Войти» заново.»), потому что
# нарисованная кнопка, ведущая в никуда, обещает то, чего сервис не сделал.
TELEGRAM_LINK_NOT_READY = "Ссылка на вход ещё не готова. Нажмите «Войти» заново."
# Истёкший код — это не ошибка ввода человека, а истёкшее окно, и подсказка у
# него своя: начать заново, а не «исправить код».
TELEGRAM_CODE_EXPIRED = "Время входа истекло. Начните вход заново."
# Исчерпанные попытки — то же действие («заново»), но причина другая: не срок,
# а перебор. Свести их в один текст значило бы спрятать, что случилось.
TELEGRAM_ATTEMPTS_EXCEEDED = "Слишком много попыток. Начните вход заново."

# Разметка состояний кода: во что разворачивается отказ бота. Список закрытый и
# назван причинами из клиента (`AuthViewModel`), а не текстом: экран выбирает
# состояние по имени, а текст состояния — уже из этого словаря. Незнакомое
# значение сюда не попадает и трактуется как истёкшее окно: без этого новый
# код ошибки бота показал бы пустой экран.
TELEGRAM_STATES: dict[str, str] = {
    "expired": TELEGRAM_CODE_EXPIRED,
    "attempt_limit_exceeded": TELEGRAM_ATTEMPTS_EXCEEDED,
}

_ENV = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(("html", "xml")),
)


def _base_context() -> dict[str, object]:
    """Общий контекст каждого экрана: палитра, радиусы и адрес стилей.

    `css_vars` отдаются из `theme`, а не переписываются в шаблоне: значения
    палитры живут в одном месте, и тест на `theme` покрывает разом и страницы.
    """
    return {
        "colors": theme.COLORS,
        "radii": theme.RADII,
        "css_vars": theme.css_variables(),
        "css_url": CSS_URL,
        "privacy_url": PRIVACY_URL,
        "terms_url": TERMS_URL,
    }


def render_login(
    *,
    mail: str | None = None,
    error: str | None = None,
    form_action: str = "/login",
    telegram_url: str = TELEGRAM_ENTRY_URL,
) -> str:
    """Экран входа: выбор из двух равноправных путей — почта или Telegram.

    `error` сюда попадает, когда адрес отвергнут (пустой или занят другим
    аккаунтом). Это не опасность — форма просто вернётся с подсказкой, и
    янтарный текст здесь уместнее красного: красный зарезервирован под
    реальную опасность (§7).

    `telegram_url` — куда ведёт кнопка входа через Telegram. Оба способа —
    выбор, а не один путь с оговоркой: у человека может не быть доступа к
    любому из двух, и тогда спасает второй (§1, §5 спеки).
    """
    template = _ENV.get_template("login.html")
    return template.render(
        **_base_context(),
        mail=mail,
        error=error,
        form_action=form_action,
        telegram_url=telegram_url,
    )


def render_login_waiting(
    *,
    mail: str,
    error: str | None = None,
    busy: bool = False,
    form_action: str = "/login/verify",
    resend_action: str = "/login/resend",
) -> str:
    """Экран ввода кода.

    `busy` показывает «Проверяем…» и блокирует кнопку: второй тап по
    «Подтвердить» не должен запускать вторую проверку того же кода.
    """
    template = _ENV.get_template("login_waiting.html")
    return template.render(
        **_base_context(),
        mail=mail,
        error=error,
        busy=busy,
        form_action=form_action,
        resend_action=resend_action,
    )


def render_login_telegram(
    *,
    telegram_url: str | None = None,
    public_code: str | None = None,
    error: str | None = None,
    error_state: str | None = None,
    busy: bool = False,
    form_action: str = TELEGRAM_VERIFY_ACTION,
    restart_url: str = TELEGRAM_RESTART_URL,
    check_label: str = TELEGRAM_CHECK_LABEL,
) -> str:
    """Экран входа через Telegram: открыть бота, ввести код оттуда, дождаться.

    Отдельный шаблон, а не `login_waiting.html` с другим текстом. Разница не
    только в подписях: здесь код приходит из бота, а не из письма, поэтому есть
    кнопка «Открыть Telegram ещё раз» (клиент, `LoginWaitingScreen.kt`, ведёт ею
    в **бот входа**, а не в почту) и честное состояние «ссылка ещё не готова».
    Ветвление одного шаблона по строкам дало бы ему два лица и разъехалось бы
    при первой правке — так же, как разошлись бы две копии палитры.

    `telegram_url` — ссылка челленджа из `/auth/link` (`deep_link`), куда ведёт
    «Открыть Telegram ещё раз». Пока бот её не вернул, она `None`, и экран
    говорит прямо, что ссылки нет, вместо кнопки в никуда. Это то же честное
    состояние, что в клиенте: «Ссылка на вход ещё не готова.»

    `error_state` — причина отказа по имени из `TELEGRAM_STATES`; текст берётся
    отсюда, а не сочиняется на экране. Незнакомая причина (новый код бота)
    трактуется как истёкшее окно — начать заново, — а пустая страница не
    показывается. Свободный `error` остаётся для прочих отказов входа.

    `busy` — «Проверяем…» и заблокированная кнопка: второй тап не должен слать
    второй запрос тем же кодом (та же ошибка, что уже ловили в клиенте).
    """
    if error is None and error_state is not None:
        error = TELEGRAM_STATES.get(error_state, TELEGRAM_CODE_EXPIRED)
    template = _ENV.get_template("login_telegram.html")
    return template.render(
        **_base_context(),
        telegram_url=telegram_url,
        # `public_code` — код начатого челленджа. Он не секрет: он и так в
        # `deep_link` у человека, и этим полем экран говорит серверу, о каком
        # входе спрашивает. Секрет челленджа сюда не попадает никогда.
        public_code=public_code,
        link_ready=bool(telegram_url),
        link_not_ready=TELEGRAM_LINK_NOT_READY,
        error=error,
        busy=busy,
        form_action=form_action,
        restart_url=restart_url,
        check_label=check_label,
    )


# Адреса действий связывания. Собирает их роутер, а не шаблон, — здесь только
# значения по умолчанию, чтобы экран можно было отрендерить без роутера (тесты).
LINK_TELEGRAM_ACTION = "/account/link/telegram"
LINK_MAIL_ACTION = "/account/link/mail"
# Приём кода, отправленного на адрес: отдельный шаг привязки почты, а не тот же
# адрес с признаком. Один адрес на «выслать» и «проверить» не различить без
# скрытого поля-признака, и тогда отправку кода нельзя было бы повторить, не
# рискуя заодно проверить его.
LINK_MAIL_CONFIRM_ACTION = "/account/link/mail/confirm"
UNLINK_TELEGRAM_ACTION = "/account/unlink/telegram"
UNLINK_MAIL_ACTION = "/account/unlink/mail"

# Скачивание профиля идёт по id подписки: конфиг у подписки один, и адрес с
# отдельным `config_id` позволил бы запрашивать чужие конфиги перебором.
CONFIG_DOWNLOAD_BASE = "/subscriptions"


# --- подписки (W5.2, W5.4) -----------------------------------------------

# Что каждое состояние значит словами. «Истекла» и «заблокирована» — разные
# причины, и человеку за ними нужно разное: срок кончился — это часы, доступ
# закрыт сервисом — это не часы. Один общий текст «доступа нет» заставлял бы
# искать причину, которой на экране нет.
_SUBSCRIPTION_NOTES = {
    STATUS_ACTIVE: "Подписка действует до указанной даты.",
    STATUS_TRIAL: "Пробный период — до указанной даты.",
    STATUS_EXPIRED: "Срок подписки закончился.",
    STATUS_BLOCKED: "Доступ закрыт сервисом — это не связано со сроком.",
    STATUS_UNKNOWN: "Состояние подписки неизвестно. Обновите список чуть позже.",
}


def _pluralize_days(count: int) -> str:
    """«1 день», «2 дня», «5 дней».

    Русское склонение по числу: без него «осталось 1 дней» читается как сбой
    страницы, а не как срок. Исключение 11–14 — форма «дней», хотя последняя
    цифра выглядит подходящей.
    """
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} дней"
    last = count % 10
    if last == 1:
        return f"{count} день"
    if 2 <= last <= 4:
        return f"{count} дня"
    return f"{count} дней"


def _end_date_text(view: SubscriptionView) -> str | None:
    """Срок словами или `None`, если он неизвестен.

    «Неизвестен» и «прошёл» — разные состояния, и свести их к прочерку значило
    бы показать истёкшую подписку как подписку без даты."""
    if not view.end_date_is_known or view.end_date is None:
        return None
    return view.end_date.strftime("%d.%m.%Y")


def _subscription_context(
    views: list[SubscriptionView],
    *,
    error: str | None = None,
    config_error: ConfigError | None = None,
    config_error_id: str | None = None,
) -> dict[str, object]:
    """Подписки, какими их видит разметка: состояние, тон, пояснение, срок.

    Тон берётся у `theme` по причине `subscription_<status>` — не задаётся
    здесь. Именно поэтому «активна» приходит нейтральной: зелёный в теме
    закреплён за подтверждённой защитой, а срок о защите не говорит ничего.

    `config_error` с `config_error_id` кладут отказ в скачивании на карточку
    ровно той подписки, по которой его нажали. Экран у нас один — карточки, а
    не отдельная страница: человек нажал «Скачать» здесь, и ответ читает здесь
    же, не теряя, к чему он относился.

    `subscriptions_known` отделяет пустоту от неизвестности (issue #49).
    «Подписок нет» — утверждение о **известном** пустом списке (W4.3), и рядом с
    отказом (`error`) оно противоречило бы ему: список при отказе не пуст, а
    неизвестен, и человек прочёл бы «у вас ничего нет» вместо «повторите позже».
    Развилка нужна одна на все экраны, поэтому она здесь, а не в разметке:
    ошибок, после которых список неизвестен, набирается уже три (список не
    получен, бот недоступен, скачивание не состоялось).
    """
    refusal = (
        _config_refusal(config_error) if config_error is not None and config_error_id else None
    )
    rows = []
    for view in views:
        days = view.days_left
        rows.append(
            {
                "id": view.id,
                "name": view.name,
                "status_label": view.status_label,
                "note": _SUBSCRIPTION_NOTES.get(view.status, _SUBSCRIPTION_NOTES[STATUS_UNKNOWN]),
                "tone": theme.tone_for(f"subscription_{view.status}").value,
                "end_date": _end_date_text(view),
                "days_left": _pluralize_days(days) if days is not None else None,
                # Предупреждение о близком сроке — отдельная строка, а не смена
                # тона: срок не опасность, и красить его нельзя (§7).
                "expires_soon": view.expires_soon,
                "usable": view.is_usable,
                "download_url": f"{CONFIG_DOWNLOAD_BASE}/{view.id}/config",
                # Отказ виден только на своей карточке; у остальных — None.
                "refusal": refusal if view.id == config_error_id else None,
            }
        )
    return {
        "rows": rows,
        "has_subscriptions": bool(rows),
        "subscriptions_known": error is None,
    }


def _money_context(
    *,
    balance: Balance | None = None,
    referral: Referral | None = None,
    telegram_required: bool = False,
) -> dict[str, object]:
    """Деньги человека — баланс и рефералка — какими их видит разметка.

    Правило §7 держится тут: ни баланс, ни приглашённые не «достижение» и не
    «опасность». Зелёный в кабинете закреплён за подтверждённой защитой, а
    красный — за необратимым удалением аккаунта; на деньги не годится ни один.
    Поэтому блокам не назначается цвет вовсе — они читаются нейтральным текстом
    (`theme.tone_for` без причины даёт `neutral`).

    `telegram_required` — не «баланс равен нулю», а «баланса здесь нет». У
    аккаунта без Telegram кредит и рефералка живут на стороне бота под `chat_id`,
    которого у нас ещё нет, и рисовать вместо них `0 ₽` значило бы соврать:
    ноль читается как «вам ничего не начислили», тогда как на самом деле сумма
    просто нам неизвестна. Ноль, честно пришедший от бота, показывается — это
    другое состояние, и оно называется числом.
    """
    if telegram_required:
        return {
            "balance": None,
            "balance_text": None,
            "telegram_required": True,
            "balance_tone": theme.tone_for("balance").value,
            "referral": None,
            "referral_code": None,
            "referral_invited": None,
            "referral_earned": None,
        }

    balance_text = _rubles(balance.credit_rub) if balance is not None else None
    return {
        "balance": balance,
        "balance_text": balance_text,
        "telegram_required": False,
        "balance_tone": theme.tone_for("balance").value,
        "referral": referral,
        "referral_code": referral.referral_code if referral is not None else None,
        # Число приглашённых склоняется как «человек»: «приглашено 1 человек»,
        # но «2 человека». Без склонения строка читается машинной.
        "referral_invited": _people(referral.invited_count) if referral is not None else None,
        "referral_earned": _rubles(referral.rub_earned) if referral is not None else None,
    }


def _rubles(amount: int) -> str:
    """Сумма в рублях с разделителем разрядов и знаком валюты.

    Неразрывный пробел между разрядами — не украшение: «1000000 ₽» глазом не
    разбить на разряды, и человек читает не ту сумму. Неразрывный он ещё и
    затем, чтобы сумма не переносилась по строке посередине.
    """
    digits = f"{abs(int(amount)):,}".replace(",", " ")
    sign = "-" if amount < 0 else ""
    return f"{sign}{digits} ₽"


def _people(count: int) -> str:
    """«1 человек», «2 человека», «5 человек» — склонение по числу."""
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} человек"
    last = count % 10
    if last == 1:
        return f"{count} человек"
    return f"{count} человека"


def render_subscriptions(
    views: list[SubscriptionView],
    *,
    error: str | None = None,
    config_error: ConfigError | None = None,
    config_error_id: str | None = None,
    notices: str | None = None,
    notices_tone: str = "neutral",
    balance: Balance | None = None,
    referral: Referral | None = None,
    telegram_required: bool = False,
) -> str:
    """Экран подписок: срок и состояние каждой, либо честное «подписок нет».

    Пустой список — состояние экрана, а не ошибка (W4.3): человек без подписки
    должен увидеть, что подписок нет, а не пустую страницу или сбой.

    Отказ в конфиге приходит сюда, а не на отдельный экран: `config_error` с
    `config_error_id` показывают причину на карточке нажатой подписки, и человек
    остаётся там, откуда скачивал (W5.4). `error` — про другое: сбой самого
    списка, у которого своей подписки нет.

    `error` гасит карточку «Подписок нет» (issue #49): с отказом на экране список
    не пуст, а **неизвестен**, и утверждать пустоту рядом с отказом нельзя.
    Пустой список без отказа остаётся честным состоянием (W4.3) — развилку
    держит `_subscription_context`.

    `notices_tone` — тон сообщения о действии, уже названный причиной из
    `theme`. Значение по умолчанию нейтральное: незачем подсвечивать то, о чём
    зрителя не просили.

    `balance` и `referral` — деньги человека (W5б, #40): накопленный кредит и
    реферальная программа. Показываются наравне с подписками, потому что в боте
    баланс списывается как скидка при выборе тарифа, и без него человек в
    кабинете увидел бы полную цену, которой в боте не платит. `None` в любом из
    них — «нечего показать», и тогда блок не рисуется; `telegram_required`
    отделяет это состояние от честного нуля текстом, а не значением.
    """
    template = _ENV.get_template("subscriptions.html")
    return template.render(
        **_base_context(),
        error=error,
        notices=notices,
        notices_tone=notices_tone,
        **_subscription_context(
            views,
            error=error,
            config_error=config_error,
            config_error_id=config_error_id,
        ),
        **_money_context(
            balance=balance,
            referral=referral,
            telegram_required=telegram_required,
        ),
    )


# --- покупка: тариф и число устройств (#36) ------------------------------
#
# Шаги 1–2 покупки на одном экране: срок и число устройств. Цену на нём не
# считают — её считает `cabinet.pricing`, перенос формулы бота, и рендер лишь
# раскладывает готовые `PriceQuote` по вариантам. Так цена на экране и цена в
# боте остаются одним числом: впиши сюда сумму руками — и человек, вошедший по
# почте, увидел бы не то, что человек в Telegram.

# Срок и число устройств по умолчанию. 30 дней — с него начинается покупка в
# боте, и он же пробный период; одно устройство — тоже вход бота: комплект это
# уже скидка, и навязывать её на первом экране нельзя.
DEFAULT_BUY_DAYS = 30
DEFAULT_BUY_DEVICES = 1

# Сколько дней в «месяце» для цены в месяц. 30, как и в таблице спеки §2:
# 90/180/360 дней дают 133/125/108 ₽ — числа, которые человек видит в боте.
DAYS_PER_MONTH = 30

# Числа устройств, которые вообще предлагаются. Комплект образуют ровно 2 и 3 —
# только на них у `pricing` есть скидка; четвёртый вариант обещал бы несуществующее.
PURCHASE_DEVICES = (1, 2, 3)

# Проценты комплекта берутся у `pricing`, а не пишутся числами: правка скидки в
# одном месте не должна оставить на экране старый процент.
_DEVICE_DISCOUNT_PERCENTS = {
    2: pricing.BUNDLE_2_DEVICES_DISCOUNT_PERCENT,
    3: pricing.BUNDLE_3_DEVICES_DISCOUNT_PERCENT,
}

# Текст про триал. Он намеренно **не** обещает бесплатный доступ безусловно:
# триал выдают один раз и только тем, кто ещё не покупал, а кабинет этого не
# знает — покупки живут у бота. Обещание, которого кабинет не может
# подтвердить, — та же ложь, что нарисованная кнопка в никуда. Поэтому сказано
# и что это пробный период, и кто решает, кому он положен.
BUY_TRIAL_NOTE = (
    "Пробный период дают один раз — новым покупателям. "
    "Право на него проверяет бот сервиса, и кабинет заранее его не обещает."
)


def _pluralize_devices(count: int) -> str:
    """«1 устройство», «2 устройства», «5 устройств» — склонение по числу.

    Без него «2 устройство» читается как сбой страницы. Исключение 11–14 —
    форма «устройств», хотя последняя цифра выглядит подходящей.
    """
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} устройств"
    last = count % 10
    if last == 1:
        return f"{count} устройство"
    if 2 <= last <= 4:
        return f"{count} устройства"
    return f"{count} устройств"


def _buy_periods(
    days: int, devices: int, calculator: Callable[..., PriceQuote]
) -> list[dict[str, object]]:
    """Варианты срока: цена из калькулятора, месячная цена и метка триала.

    Цена за период — ответ калькулятора на одно устройство: так её называет бот
    на шаге 1, до выбора числа устройств. Цена в месяц считается из той же суммы
    (целочисленно вниз), а не берётся вторым числом — иначе она разошлась бы с
    ценой за период при правке `PRICES`.
    """
    rows = []
    for period in sorted(pricing.PRICES):
        price = calculator(period, 1).final_price
        rows.append(
            {
                "days": period,
                "label": _pluralize_days(period),
                "price": _rubles(price),
                "per_month": _rubles(price * DAYS_PER_MONTH // period),
                "selected": period == days,
                "url": f"/buy?days={period}&devices={devices}",
                # Триал помечен только у 30 дней и только честно (см. BUY_TRIAL_NOTE).
                "trial_note": BUY_TRIAL_NOTE if period == DEFAULT_BUY_DAYS else None,
            }
        )
    return rows


def _buy_devices(
    days: int, devices: int, calculator: Callable[..., PriceQuote]
) -> list[dict[str, object]]:
    """Варианты числа устройств: скидка комплекта и сумма к оплате — из калькулятора.

    Процент скидки приходит из `pricing`, рубли экономии и итог — из ответа
    калькулятора на **выбранный** срок. Так «комплект 2 устройства: −10%» и
    «экономия 28 ₽» — свойства одного расчёта, а не двух чисел, которые могут
    разойтись.
    """
    rows = []
    for count in PURCHASE_DEVICES:
        quote = calculator(days, count)
        percent = _DEVICE_DISCOUNT_PERCENTS.get(count)
        rows.append(
            {
                "count": count,
                "label": _pluralize_devices(count),
                "discount": f"-{percent}%" if percent else None,
                "saved": _rubles(quote.bundle_discount) if quote.bundle_discount else None,
                "total": _rubles(quote.final_price),
                "selected": count == devices,
                "url": f"/buy?days={days}&devices={count}",
            }
        )
    return rows


def render_buy(
    *,
    days: int = DEFAULT_BUY_DAYS,
    devices: int = DEFAULT_BUY_DEVICES,
    calculator: Callable[..., PriceQuote] = pricing.calculate_price,
) -> str:
    """Экран покупки: срок и число устройств с ценой каждого варианта.

    `calculator` — шов, а не украшение: и роут, и тест зовут одну функцию, и
    подмена калькулятора на подставной доказуемо меняет разметку. Без шва тест
    на «цена считается, а не вписана» проверял бы разметку, а не расчёт.

    `days` и `devices` — выбранная пара. Экран показывает её итог, а варианты
    ведут себя как ссылки: выбор выражается адресом, пересчёт делает сервер, и
    экран работает без JavaScript — как и форма входа.
    """
    return _ENV.get_template("buy.html").render(
        **_base_context(),
        periods=_buy_periods(days, devices, calculator),
        device_options=_buy_devices(days, devices, calculator),
        total=_rubles(calculator(days, devices).final_price),
    )


# --- связывание способов входа (W5.3) ------------------------------------

_TELEGRAM_MASK = "••••"


def _mask_telegram(chat_id: int) -> str:
    """Telegram ID маской, кроме последних четырёх цифр.

    Экран видят глазами, и скриншот из него уходит в поддержку. Полный id там
    не нужен: по последним цифрам владелец узнаёт свой аккаунт, а профиль по
    снимку не утекает.
    """
    text = str(chat_id)
    tail = text[-4:] if len(text) > 4 else text
    return f"{_TELEGRAM_MASK} {tail}"


# Как способы называются в разметке. Ключи — те же `mail`/`telegram`, что
# приходят от бота в `remaining_method`: назвать их словами должен экран, а не
# бот, потому что «Telegram» с большой буквы и «почта» строчными — вопрос
# русской подписи, а не контракта.
_UNLINK_METHOD_LABELS = {"telegram": "Telegram", "mail": "почта"}


def _pluralize_configs(count: int) -> str:
    """«1 конфиг», «2 конфига», «5 конфигов».

    Склонение по числу — как у дней подписки: без него «затронуто 1 конфигов»
    читается как сбой страницы. 11–14 — форма «конфигов», хотя последняя цифра
    выглядит подходящей.
    """
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} конфигов"
    last = count % 10
    if last == 1:
        return f"{count} конфиг"
    if 2 <= last <= 4:
        return f"{count} конфига"
    return f"{count} конфигов"


def _unlink_note(method: str, preview: object) -> dict[str, object] | None:
    """Последствие отвязки `method` — или `None`, если говорить нечего.

    Ноль затронутых конфигов — это не «ноль конфигов» на экране, а **тишина**:
    предупреждение о том, чего не будет, пугает зря и мешает прочитать то, что
    важно. Поэтому блок про конфиги существует ровно тогда, когда их затронуто
    больше нуля.

    Судьба доступа у двух способов разная, и это не косметика. Конфиги живут на
    `chat_id`, а не на аккаунте: при отвязке почты ключом остаётся Telegram, и
    конфиги достижимы; при отвязке Telegram ключ уходит, а почта его не несёт —
    конфиги остаются в системе, но достать их нечем. Тексты намеренно разные, и
    «останутся» в них честное: где доступ сохраняется — так и сказано, где
    теряется — сказано, что именно потеряется, а не что «конфиги исчезнут».
    """
    configs = int(getattr(preview, "configs_affected", 0) or 0)
    if configs <= 0:
        return None

    remaining = getattr(preview, "remaining_method", None)
    remaining_label = _UNLINK_METHOD_LABELS.get(remaining) if remaining else None

    if method == "mail":
        # Почта конфигов не несёт: их держит Telegram, и его мы не трогаем.
        # Без имени остающегося ключа фразу не строим: «ключ для них — None»
        # было бы хуже молчания, а бот в норме ключ называет (конфиги есть
        # только когда есть `chat_id`, а он и есть остающийся способ).
        outcome = (
            f"Ключ для них — {remaining_label}, и вход по нему остаётся. "
            "Конфиги останутся доступны."
            if remaining_label
            else "Конфиги останутся доступны через второй способ входа."
        )
        key = f"Основным способом входа останется {remaining_label}." if remaining_label else ""
    else:
        # Telegram — держатель конфигов. Уйдёт он — у почты нет `chat_id`,
        # к которому их перепривязать. Обещать «останутся у вас» здесь нельзя:
        # они останутся в системе, но доступа к ним не будет.
        outcome = (
            "Конфиги привязаны к Telegram. После отвязки достать их будет нечем: "
            "у почты нет связи с ними. В системе они останутся, но доступ к ним потеряется."
        )
        key = (
            f"Основным способом входа останется {remaining_label}, "
            "но ключом для конфигов он не станет."
            if remaining_label
            else ""
        )

    return {
        "count": _pluralize_configs(configs),
        "outcome": outcome,
        "key": key,
        # Идущего процесса здесь нет: отвязка ещё не начата, и подтверждённой
        # защиты тоже нет — это состояние. Тон берётся у темы (неизвестная
        # причина не названа, и это её документированное поведение —
        # приглушить, а не покрасить), а не пишется цветом: отвязка с потерей
        # доступа это состояние, а не опасность (§7).
        "tone": theme.tone_for("unlink_consequence").value,
        # Явное подтверждение нужно там, где теряется доступ, и только там.
        # При отвязке почты ключ остаётся — галочка была бы трением без
        # причины; при отвязке Telegram теряется доступ к конфигам, и одного
        # текста над кнопкой мало: его прочитают не все. Для 0 конфигов блок не
        # строится вовсе.
        "needs_confirm": method == "telegram",
    }


def _unlink_notes(previews: dict[str, object] | None) -> dict[str, dict[str, object] | None]:
    """Заметки о последствиях для обоих способов. Без данных — обе пустые.

    `None` в значении значит «показывать нечего» и читается шаблоном как
    отсутствие блока. Отсутствие данных (роутер не собрал preview) сводится к
    тому же: экран не выдумывает последствие, которого не знает.
    """
    previews = previews or {}
    return {
        method: _unlink_note(method, previews[method])
        for method in ("mail", "telegram")
        if method in previews
    }


def render_linking(
    account: Account,
    *,
    error: str | None = None,
    notice: str | None = None,
    notice_tone: str = "neutral",
    mail_pending: str | None = None,
    unlink_previews: dict[str, object] | None = None,
    link_telegram_action: str = LINK_TELEGRAM_ACTION,
    link_mail_action: str = LINK_MAIL_ACTION,
    link_mail_confirm_action: str = LINK_MAIL_CONFIRM_ACTION,
    unlink_telegram_action: str = UNLINK_TELEGRAM_ACTION,
    unlink_mail_action: str = UNLINK_MAIL_ACTION,
) -> str:
    """Экран способов входа: что привязано и чем привязать второе.

    Отвязка предлагается только там, где после неё останется второй способ:
    кнопка «отвязать» у единственного входа вела бы к недоступному навсегда
    аккаунту (§8). Экран не даёт нажать то, что всё равно будет отвергнуто.

    `mail_pending` — адрес, на который код уже отправлен, но ещё не подтверждён.
    `None` значит «шага ввода ещё нет», и карточка почты показывает форму
    адреса. Заданный адрес переключает карточку на шаг ввода кода: показать
    форму адреса после отправки значило бы попросить адрес второй раз, а
    отправленный код проверить нечем. Адрес здесь нужен ещё и потому, что
    человек должен видеть, на **какой** ящик ушло письмо, — он мог ввести
    другой.

    `unlink_previews` — по записи на способ (`mail`/`telegram`) с числом
    затронутых конфигов и остающимся ключом, как их отдаёт бот (§4а). Экран
    показывает это **до** кнопки: человек должен узнать, что потеряет доступ к
    конфигам, а не обнаружить это после нажатия. Ноль конфигов и отсутствие
    данных одинаково не рисуют блок — нечего предупреждать.
    """
    telegram_linked = account.chat_id is not None
    mail_linked = account.mail is not None
    notes = _unlink_notes(unlink_previews)
    return _ENV.get_template("linking.html").render(
        **_base_context(),
        error=error,
        notice=notice,
        notice_tone=notice_tone,
        telegram_linked=telegram_linked,
        mail_linked=mail_linked,
        telegram_id=_mask_telegram(account.chat_id) if account.chat_id is not None else None,
        mail=account.mail,
        mail_pending=mail_pending,
        can_link_telegram=account.can_link_telegram(),
        can_link_mail=account.can_link_mail(),
        # Отвязка возможна, только если останется другой способ входа.
        can_unlink_telegram=telegram_linked and mail_linked,
        can_unlink_mail=mail_linked and telegram_linked,
        unlink_mail_note=notes.get("mail"),
        unlink_telegram_note=notes.get("telegram"),
        link_telegram_action=link_telegram_action,
        link_mail_action=link_mail_action,
        link_mail_confirm_action=link_mail_confirm_action,
        unlink_telegram_action=unlink_telegram_action,
        unlink_mail_action=unlink_mail_action,
    )


# --- конфиг не выдан (W4.2, W5.4) ----------------------------------------

# Тексты отказов в скачивании. У каждого своё действие, поэтому и свой текст:
# «обновите список» и «попробуйте позже» — не одно и то же, и свалить их в
# «ошибка» значило бы лишить человека единственной подсказки. Тон у всех
# нейтральный: отказ в конфиге — состояние, а не опасность (§7).
_CONFIG_ERROR_TEXTS: dict[type[ConfigError], tuple[str, str]] = {
    SubscriptionInactive: (
        "Конфиг недоступен",
        "Подписка не даёт доступа к подключению. Проверьте её срок и состояние.",
    ),
    ConfigNotFound: (
        "Конфиг не найден",
        "Такого подключения нет или оно больше не принадлежит аккаунту.",
    ),
    ConfigRevoked: (
        "Доступ к подключению отозван",
        "Это решение сервиса, а не сбой загрузки. Возьмите другое подключение.",
    ),
    ConfigRetired: (
        "Подключение снято с сервера",
        "Оно устарело. Выберите другое подключение в списке.",
    ),
    DownloadRateLimited: (
        "Слишком много скачиваний",
        "Подождите немного и попробуйте снова — так мы защищаем аккаунт от перебора.",
    ),
}

CONFIG_ERROR_FALLBACK = (
    "Не удалось получить конфиг",
    "Повторите попытку. Если не поможет — напишите в поддержку.",
)
# Закрытый список отказов, у которых есть свой текст. Незнакомый отказ сюда не
# попадает и получает запасной текст: придумывать ему причину на экране значило
# бы выдавать догадку за сообщение сервиса.
CONFIG_ERRORS: tuple[type[ConfigError], ...] = tuple(_CONFIG_ERROR_TEXTS)


def _config_error_text(error: ConfigError) -> tuple[str, str]:
    """Текст отказа по типу исключения.

    Сравнение идёт по `isinstance`, а не по точному типу: отказ может прийти
    подклассом (так `DownloadRateLimited` расширяет `ConfigError`), и точное
    совпадение отдало бы ему запасной текст мимо собственной подсказки про
    ожидание. Порядок обхода — от конкретного к общему: `ConfigError` в конце.
    """
    for kind in CONFIG_ERRORS:
        if isinstance(error, kind):
            return _CONFIG_ERROR_TEXTS[kind]
    return CONFIG_ERROR_FALLBACK


def _config_refusal(error: ConfigError) -> dict[str, object]:
    """Отказ в конфиге, каким его кладут на карточку подписки.

    Тип исключения — часть поведения, а не деталь: вызывающий код отдаёт сам
    объект отказа, и разметка получает готовые текст и заголовок. У
    `DownloadRateLimited` есть срок ожидания, поэтому он несёт ещё и
    `retry_after`: «попробуйте позже» без числа не подсказывает, сколько ждать.
    """
    title, note = _config_error_text(error)
    return {
        "title": title,
        "note": note,
        "retry_after": getattr(error, "retry_after_seconds", None),
    }
