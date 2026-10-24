"""Разбор разметки в тестах: формы и ссылки.

Правило проекта: тест водит человека **по разметке**, а не по адресам, собранным
руками. Оно появилось после дефекта #56 — форма шага кода не несла адрес, а тест
подставлял поля сам, и потому был зелёным при неработающем входе.

Помощники вынесены отдельно, чтобы сквозной прогон не завёл пятую копию парсера:
до этого `_FormParser` жил в `test_login_forms`, а парсер ссылок — ещё в двух
файлах. Переписывать на них существующие тесты не нужно: они зелёные и проверяют
то же самое.
"""

from __future__ import annotations

from html.parser import HTMLParser


class _Markup(HTMLParser):
    """Формы (адрес, метод, поля) и ссылки (адрес, подпись) одной разметки."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, object]] = []
        self.links: list[tuple[str, str]] = []
        self._form: dict[str, object] | None = None
        self._href: str | None = None
        self._text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._form = {
                "action": attributes.get("action") or "",
                "method": (attributes.get("method") or "get").lower(),
                "fields": {},
            }
        elif tag == "input" and self._form is not None:
            name = attributes.get("name")
            if name:
                fields = self._form["fields"]
                assert isinstance(fields, dict)
                # Значение по умолчанию — то, что положил сервер (скрытые поля,
                # возвращённый адрес): тест отправляет именно его, а не догадку.
                fields[name] = attributes.get("value") or ""
        elif tag == "a":
            self._href = attributes.get("href")
            self._text = ""

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, self._text.strip()))
            self._href = None


def _parse(html: str) -> _Markup:
    parser = _Markup()
    parser.feed(html)
    return parser


def forms(html: str) -> list[dict[str, object]]:
    return _parse(html).forms


def form(html: str, action: str) -> dict[str, object]:
    """Форма по адресу. Нет такой — тест падает здесь, а не «не нашёл поле»."""
    for candidate in _parse(html).forms:
        if str(candidate["action"]).endswith(action):
            return candidate
    raise AssertionError(f"в разметке нет формы с адресом {action}")


def links(html: str) -> list[tuple[str, str]]:
    return _parse(html).links


def hrefs(html: str, label: str) -> list[str]:
    return [href for href, text in _parse(html).links if text == label]


def link(html: str, label: str) -> str:
    """Единственная ссылка с такой подписью. Несколько — ошибка разметки."""
    found = hrefs(html, label)
    assert found, f"в разметке нет ссылки «{label}»"
    assert len(found) == 1, f"ссылок «{label}» несколько: {found}"
    return found[0]


def link_containing(html: str, needle: str) -> str:
    """Ссылка, в подписи которой встречается текст.

    Нужно там, где подпись **составная**: вариант выбора нарисован одним `<a>` с
    вложенными элементами — срок, цена и цена в месяц лежат внутри одной ссылки,
    и её полный текст не равен «180 дней». Требовать точного совпадения значило
    бы запретить такую разметку, а не проверить переход.
    """
    for href, text in _parse(html).links:
        if needle in text:
            return href
    raise AssertionError(f"в разметке нет ссылки, содержащей «{needle}»")
