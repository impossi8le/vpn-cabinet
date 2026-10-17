"""Дизайн-токены кабинета: та же палитра, что в Android-клиенте (§7 спеки).

Зачем отдельный модуль, а не цвета прямо в CSS. Палитра — не украшение, а
носитель правила: за каждым цветом закреплён один смысл. Если цвет разбросан
по шаблонам, правило перестаёт быть проверяемым — а его надо проверять, потому
что именно на нём однажды ошиблись: зелёный «защищено» стоял на экране, где
никакого замера не было. Здесь палитра и её смысл лежат в одном месте, и тест
может пройти по всем состояниям экранов и убедиться, что зелёного среди них
нет, кроме одного — подтверждённой защиты.

Источник значений — `Tokens.kt` Android-клиента и `mockup.html`; расхождение
с ними считается ошибкой, а не «своим вкусом», поэтому значения выписаны
дословно и закреплены тестом.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "CONFIRMED_PROTECTION",
    "RADII",
    "REASON_TONES",
    "TONES",
    "Tone",
    "VpnColors",
    "VpnRadii",
    "css_variables",
    "tone_for",
]


@dataclass(frozen=True)
class VpnColors:
    """Палитра, дословно из §7 спеки.

    Светлой темы нет намеренно — это решение UX-ревью, а не «ещё не сделали».
    Добавлять её «как улучшение» нельзя без новой причины, поэтому второй
    палитры здесь не заводится.
    """

    # Поверхности: от самого глубокого к самому светлому.
    void: str = "#0A0C10"
    carbon: str = "#12151B"
    obsidian: str = "#1A1E26"

    # Границы.
    hairline: str = "#262B34"
    smoke: str = "#333A45"
    edge: str = "#5A6472"

    # Текст: от самого тихого к основному.
    ash: str = "#7F8A99"
    text_secondary: str = "#8B95A3"
    mist: str = "#AAB4C2"
    bone: str = "#E8ECF2"

    # Акцент выделения и фокуса. НЕ брендовый цвет и НЕ индикатор состояния:
    # на кнопке он читается как «сюда смотреть», а не как «работает».
    ice: str = "#6FB3FF"

    # Семантические цвета. Их всего три, и каждый значит ровно одно.
    green: str = "#3DDC97"
    amber: str = "#FFB454"
    red: str = "#FF5F6D"


@dataclass(frozen=True)
class VpnRadii:
    """Радиусы скруглений из макета. Число здесь, а не в CSS: шаблон и тест
    должны читать одно значение, иначе «14» в разметке и «14» в проверке
    разъедутся при первой правке."""

    card: int = 14
    button: int = 12
    small: int = 8


COLORS = VpnColors()
RADII = VpnRadii()


class Tone(StrEnum):
    """Смысловой тон. Экран выбирает тон, а не цвет: цвет — следствие тона.

    Так «здесь зелёный» нельзя написать в разметке мимоходом — пришлось бы
    назвать причину, а причина проверяется тестом.
    """

    NEUTRAL = "neutral"
    PROCESS = "process"
    DANGER = "danger"
    CONFIRMED = "confirmed"


# Единственная причина, по которой в кабинете уместен зелёный. Имя длинное и
# точное намеренно: «подключено» или «сохранено» под него не подставляется,
# потому что это не замер защиты.
CONFIRMED_PROTECTION = "confirmed_protection"

TONES: dict[Tone, str] = {
    Tone.NEUTRAL: COLORS.mist,
    Tone.PROCESS: COLORS.amber,
    Tone.DANGER: COLORS.red,
    Tone.CONFIRMED: COLORS.green,
}

# Что на экранах кабинета значит какой тон. Список закрытый — открытый список
# тянул бы за собой «ну это же очевидно нейтрально», а очевидность здесь и
# была источником ошибки.
REASON_TONES: dict[str, Tone] = {
    # Подписки. «Активна» — не зелёная: в кабинете нет замера защиты, и
    # активная подписка говорит лишь о сроке, а не о том, что трафик защищён.
    "subscription_active": Tone.NEUTRAL,
    "subscription_trial": Tone.NEUTRAL,
    "subscription_expired": Tone.NEUTRAL,
    "subscription_blocked": Tone.NEUTRAL,
    "subscription_unknown": Tone.NEUTRAL,
    # Идущий процесс: письмо, проверка кода, привязка. Ровно янтарный.
    "code_sending": Tone.PROCESS,
    "code_verifying": Tone.PROCESS,
    "linking_in_progress": Tone.PROCESS,
    # Реальная опасность — а не «ошибка ввода» и не «нет сети». В кабинете
    # такое одно: необратимое удаление аккаунта.
    "delete_account": Tone.DANGER,
    # Подтверждённая защита. Пока в кабинете её показать неоткуда — экраны
    # кабинета замера не делают, — но причина оставлена названной, чтобы
    # зелёный не появился «сам» под другим именем.
    CONFIRMED_PROTECTION: Tone.CONFIRMED,
}


def tone_for(reason: str) -> Tone:
    """Тон по причине. Незнакомая причина — нейтральный, а не «наверное, это
    защита»: ошибка должна приглушать экран, а не красить его в зелёный."""
    return REASON_TONES.get(reason, Tone.NEUTRAL)


def css_variables() -> dict[str, str]:
    """Пары «CSS-переменная — значение» для шаблона.

    Отдавать их из Python, а не держать второй копией в файле стилей: копия
    разошлась бы с палитрой молча, и «прибрать цвет» на экране стало бы правкой
    в двух местах с гарантией, что одно забудут.
    """
    return {
        "--void": COLORS.void,
        "--carbon": COLORS.carbon,
        "--obsidian": COLORS.obsidian,
        "--hairline": COLORS.hairline,
        "--smoke": COLORS.smoke,
        "--edge": COLORS.edge,
        "--ash": COLORS.ash,
        "--text-secondary": COLORS.text_secondary,
        "--mist": COLORS.mist,
        "--bone": COLORS.bone,
        "--ice": COLORS.ice,
        "--green": COLORS.green,
        "--amber": COLORS.amber,
        "--red": COLORS.red,
        "--radius-card": f"{RADII.card}px",
        "--radius-button": f"{RADII.button}px",
        "--radius-small": f"{RADII.small}px",
    }
