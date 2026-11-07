"""Экран входа: одно действие, одна короткая мысль, без объяснений очевидного.

Правило взято не из вкуса: пользователь, который тратит на простую задачу в
десять раз больше времени, не вдумчивый — он застрял; а каждый лишний абзац до
кнопки это место, где можно уйти. Поэтому проверяется не «красиво ли», а
сколько на экране главных кнопок, сколько слов в подводке и не вернулся ли
список шагов, объясняющий то, что и так видно.
"""

from __future__ import annotations

import re
from html import unescape

from cabinet.render import render_login


def _strip_tags(fragment: str) -> str:
    text = unescape(re.sub(r"<[^>]+>", " ", fragment))
    return " ".join(text.split())


def _lead(html: str) -> str:
    match = re.search(r'<p class="lead">(.*?)</p>', html, re.S)
    assert match is not None, "на экране входа пропала подводка"
    return _strip_tags(match.group(1))


def test_login_has_exactly_one_primary_action() -> None:
    """Главная кнопка одна. Вторая равноправная — уже не выбор, а качели.

    У почты белая кнопка, у Telegram — призрачная: оба пути доступны, но
    взгляд не разрывается между двумя одинаково громкими действиями.
    """
    html = render_login()

    assert html.count('class="btn btn-p"') == 1
    assert html.count('class="btn btn-g"') == 1


def test_login_lead_is_one_short_line() -> None:
    """Подводка — одно короткое предложение, а не абзац объяснений.

    Оба способа всё равно должны быть названы (это требование спеки), поэтому
    длина ограничена, а не содержание.
    """
    text = _lead(render_login())

    assert len(text.split()) <= 18, f"подводка разрослась: {text!r}"
    assert "по почте или через telegram" in text.lower()


def test_login_does_not_walk_through_three_obvious_steps() -> None:
    """Список «1. выберите 2. подтвердите 3. смотрите» убран.

    Это не инструкция, а пересказ того, что человек увидит сам: выбор способа
    нарисован кнопками, код придёт сам, подписки — следующий экран. Три строки
    до действия читались как документ.
    """
    html = render_login()

    assert 'class="steps"' not in html
    assert "Выберите способ" not in html


def test_login_says_the_account_creates_itself_once() -> None:
    """«Аккаунт заведётся сам» сказано один раз, а не в двух местах.

    Раньше эта мысль шла дважды: подводкой к шагам и отдельной плашкой про
    «оба пути ведут в один аккаунт». Повтор не убеждает, а занимает место.
    """
    html = render_login()

    assert html.count("заведётся сам") == 1
