"""Рендер страниц кабинета: Jinja2 + палитра из `theme`.

Зачем отдельный слой рендера. Экраны — это разметка, но у них есть два
неизменных входа: палитра (её нельзя писать в шаблоне, иначе копия разойдётся с
`VpnColors`) и адреса форм (их вписывает роутер, а не шаблон). Здесь этот вход
собирается один раз, и шаблон получает уже готовый контекст. Роутеру остаётся
назвать экран и данные, а не помнить, какие переменные ждёт каждый шаблон.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cabinet import theme
from cabinet.accounts import Account
from cabinet.configs import (
    ConfigError,
    ConfigNotFound,
    ConfigRetired,
    ConfigRevoked,
    DownloadRateLimited,
    SubscriptionInactive,
)
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
    "LINK_TELEGRAM_ACTION",
    "STATIC_DIR",
    "TELEGRAM_ATTEMPTS_EXCEEDED",
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
    error: str | None = None,
    error_state: str | None = None,
    busy: bool = False,
    form_action: str = TELEGRAM_VERIFY_ACTION,
    restart_url: str = TELEGRAM_RESTART_URL,
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
        link_ready=bool(telegram_url),
        link_not_ready=TELEGRAM_LINK_NOT_READY,
        error=error,
        busy=busy,
        form_action=form_action,
        restart_url=restart_url,
    )


# Адреса действий связывания. Собирает их роутер, а не шаблон, — здесь только
# значения по умолчанию, чтобы экран можно было отрендерить без роутера (тесты).
LINK_TELEGRAM_ACTION = "/account/link/telegram"
LINK_MAIL_ACTION = "/account/link/mail"
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
    return {"rows": rows, "has_subscriptions": bool(rows)}


def render_subscriptions(
    views: list[SubscriptionView],
    *,
    error: str | None = None,
    config_error: ConfigError | None = None,
    config_error_id: str | None = None,
    notices: str | None = None,
    notices_tone: str = "neutral",
) -> str:
    """Экран подписок: срок и состояние каждой, либо честное «подписок нет».

    Пустой список — состояние экрана, а не ошибка (W4.3): человек без подписки
    должен увидеть, что подписок нет, а не пустую страницу или сбой.

    Отказ в конфиге приходит сюда, а не на отдельный экран: `config_error` с
    `config_error_id` показывают причину на карточке нажатой подписки, и человек
    остаётся там, откуда скачивал (W5.4). `error` — про другое: сбой самого
    списка, у которого своей подписки нет.

    `notices_tone` — тон сообщения о действии, уже названный причиной из
    `theme`. Значение по умолчанию нейтральное: незачем подсвечивать то, о чём
    зрителя не просили.
    """
    template = _ENV.get_template("subscriptions.html")
    return template.render(
        **_base_context(),
        error=error,
        notices=notices,
        notices_tone=notices_tone,
        **_subscription_context(
            views,
            config_error=config_error,
            config_error_id=config_error_id,
        ),
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


def render_linking(
    account: Account,
    *,
    error: str | None = None,
    notice: str | None = None,
    notice_tone: str = "neutral",
    link_telegram_action: str = LINK_TELEGRAM_ACTION,
    link_mail_action: str = LINK_MAIL_ACTION,
    unlink_telegram_action: str = UNLINK_TELEGRAM_ACTION,
    unlink_mail_action: str = UNLINK_MAIL_ACTION,
) -> str:
    """Экран способов входа: что привязано и чем привязать второе.

    Отвязка предлагается только там, где после неё останется второй способ:
    кнопка «отвязать» у единственного входа вела бы к недоступному навсегда
    аккаунту (§8). Экран не даёт нажать то, что всё равно будет отвергнуто.
    """
    telegram_linked = account.chat_id is not None
    mail_linked = account.mail is not None
    return _ENV.get_template("linking.html").render(
        **_base_context(),
        error=error,
        notice=notice,
        notice_tone=notice_tone,
        telegram_linked=telegram_linked,
        mail_linked=mail_linked,
        telegram_id=_mask_telegram(account.chat_id) if account.chat_id is not None else None,
        mail=account.mail,
        can_link_telegram=account.can_link_telegram(),
        can_link_mail=account.can_link_mail(),
        # Отвязка возможна, только если останется другой способ входа.
        can_unlink_telegram=telegram_linked and mail_linked,
        can_unlink_mail=mail_linked and telegram_linked,
        link_telegram_action=link_telegram_action,
        link_mail_action=link_mail_action,
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
