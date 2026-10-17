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

__all__ = [
    "CSS_FILE",
    "CSS_URL",
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "render_login",
    "render_login_waiting",
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
) -> str:
    """Экран ввода почты.

    `error` сюда попадает, когда адрес отвергнут (пустой или занят другим
    аккаунтом). Это не опасность — форма просто вернётся с подсказкой, и
    янтарный текст здесь уместнее красного: красный зарезервирован под
    реальную опасность (§7).
    """
    template = _ENV.get_template("login.html")
    return template.render(**_base_context(), mail=mail, error=error, form_action=form_action)


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
