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
    "TELEGRAM_ENTRY_URL",
    "TEMPLATES_DIR",
    "UNLINK_MAIL_ACTION",
    "UNLINK_TELEGRAM_ACTION",
    "render_config_unavailable",
    "render_linking",
    "render_login",
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


def _subscription_context(views: list[SubscriptionView]) -> dict[str, object]:
    """Подписки, какими их видит разметка: состояние, тон, пояснение, срок.

    Тон берётся у `theme` по причине `subscription_<status>` — не задаётся
    здесь. Именно поэтому «активна» приходит нейтральной: зелёный в теме
    закреплён за подтверждённой защитой, а срок о защите не говорит ничего.
    """
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
            }
        )
    return {"rows": rows, "has_subscriptions": bool(rows)}


def render_subscriptions(
    views: list[SubscriptionView],
    *,
    error: str | None = None,
    notices: str | None = None,
    notices_tone: str = "neutral",
) -> str:
    """Экран подписок: срок и состояние каждой, либо честное «подписок нет».

    Пустой список — состояние экрана, а не ошибка (W4.3): человек без подписки
    должен увидеть, что подписок нет, а не пустую страницу или сбой.

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
        **_subscription_context(views),
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


def render_config_unavailable(error: ConfigError) -> str:
    """Экран неудачной выдачи профиля.

    Тип исключения — часть поведения, а не деталь: вызывающий код отдаёт сюда
    сам объект отказа, и экран сам решает, что сказать. `DownloadRateLimited`
    несёт срок ожидания, поэтому у него отдельная ветка."""
    title, note = _config_error_text(error)
    retry_after = getattr(error, "retry_after_seconds", None)
    return _ENV.get_template("config_unavailable.html").render(
        **_base_context(),
        title=title,
        note=note,
        retry_after=retry_after,
    )
