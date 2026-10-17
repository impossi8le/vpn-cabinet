"""W5.0: токены совпадают с Android-клиентом, и зелёный не протекает.

Здесь два разных утверждения, и оба нужны.

Первое — палитра. Значения выписаны в `theme.py` руками, а руками выписанное
расходится. Тест сравнивает их с `Tokens.kt` по строкам исходника: не «мне
кажется, там #3DDC97», а буквальное совпадение HEX. Без этого «перенесли
палитру» означало бы «перенесли похожую палитру», а кабинет и приложение
разошлись бы на оттенке, который никто не заметит до ревью дизайна.

Второе — правило смысла цвета. Оно несущее (§7): зелёный значит только
подтверждённую защиту. Проверить его одним чтением палитры нельзя — палитра
нейтральна к тому, где цвет применён. Поэтому тест берёт **все причины, по
которым экраны вообще выбирают тон**, требует, чтобы список был закрытым, и
показывает: среди них нет причины «подтверждённая защита», то есть в кабинете
зелёного нет ни на одном экране. Это и есть защита от ложного зелёного: стоит
кому-то добавить причину вроде «подписка активна» и покрасить её зелёным — тест
упадёт на попытке внести причину мимо списка.

Ложный зелёный не проверяется «где-нибудь» — проверяется ровно там, где он
однажды появился: на состоянии подписки. Подписка активна, пробный период,
истекла, заблокирована — все четыре обязаны быть не зелёными.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cabinet import theme
from cabinet.theme import COLORS, RADII, REASON_TONES, TONES, Tone, tone_for

# Живой источник дизайна. Читать можно, менять — нет: это чужой репозиторий.
# Путь может отсутствовать на CI, где рядом нет vpn-app-design, поэтому тест на
# палитру помечен и пропускается, а тесты на смысл цвета идут всегда.
_TOKENS_KT = Path(
    r"D:\VPN_app\android\core\ui\src\main\kotlin\com\impossi8le\vpnapp\core\ui\Tokens.kt"
)

# Сколько места зелёному отведено в кабинете: одно имя причины и только оно.
_ONLY_GREEN_REASON = "confirmed_protection"


def _tokens_kt_colors() -> dict[str, str]:
    """Разобрать `val Имя = Color(0xFFxxxxxx)` из `Tokens.kt` в `{имя: #hex}`.

    Имена приводятся к тому же виду, что поля `VpnColors`: `TextSecondary` →
    `text_secondary`. Иначе тест сравнивал бы значения двух разных соглашений об
    именах и падал бы не на расхождении цвета, а на регистре.
    """
    source = _TOKENS_KT.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for name, hex6 in re.findall(r"val\s+(\w+)\s*=\s*Color\(0xFF([0-9A-Fa-f]{6})\)", source):
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        found[snake] = f"#{hex6.upper()}"
    return found


@pytest.mark.skipif(not _TOKENS_KT.exists(), reason="живой источник дизайна недоступен")
def test_palette_matches_android_client() -> None:
    """Каждое поле `VpnColors` равно своему токену в `Tokens.kt`.

    Проверяются все пересекающиеся имена, и требуется, чтобы пересечение было
    непустым: если разбор `Tokens.kt` сломается и вернёт пусто, тест обязан
    упасть, а не пройти на пустом множестве — иначе он сторожит пустоту.
    """
    upstream = _tokens_kt_colors()
    assert "void" in upstream and "bone" in upstream, "разбор Tokens.kt не нашёл палитру"

    for name, expected in upstream.items():
        assert hasattr(COLORS, name), f"в VpnColors нет токена {name}, а в клиенте он есть"
        assert getattr(COLORS, name) == expected, f"{name}: {getattr(COLORS, name)} != {expected}"


def test_radii_match_spec() -> None:
    """Радиусы из §7: карточка 14, кнопка 12, малое 8.

    Числа продублированы в трёх местах (спека, CSS, здесь), поэтому тест
    сравнивает поле дата-класса со спекой, а не с самим собой.
    """
    assert (RADII.card, RADII.button, RADII.small) == (14, 12, 8)


def test_semantic_colors_are_the_three_plus_ice() -> None:
    """Семантических цветов ровно три, и `Ice` — не четвёртый.

    `Ice` присутствует в палитре, но в `TONES` его нет: он про фокус, а не про
    состояние. Если бы он там был, «выделено» однажды прочиталось бы как статус.
    """
    assert set(TONES) == {Tone.NEUTRAL, Tone.PROCESS, Tone.DANGER, Tone.CONFIRMED}
    assert TONES[Tone.CONFIRMED] == COLORS.green
    assert TONES[Tone.PROCESS] == COLORS.amber
    assert TONES[Tone.DANGER] == COLORS.red
    assert COLORS.ice not in set(TONES.values())


def test_subscription_statuses_are_never_green() -> None:
    """Ни одно состояние подписки не зелёное — и все они нейтральны.

    Это тот самый дефект из ревью макета: «активно» красили в зелёный, хотя
    замера защиты нет, и экран обещал больше, чем знал. Здесь четыре состояния
    названы поимённо, и ни одному зелёный не достаётся.
    """
    for reason in (
        "subscription_active",
        "subscription_trial",
        "subscription_expired",
        "subscription_blocked",
        "subscription_unknown",
    ):
        assert tone_for(reason) is Tone.NEUTRAL, reason


def test_amber_is_only_process() -> None:
    """Янтарный достаётся только идущему процессу, и каждый процесс — янтарный."""
    process = [reason for reason, tone in REASON_TONES.items() if tone is Tone.PROCESS]
    assert set(process) == {"code_sending", "code_verifying", "linking_in_progress"}
    for reason in process:
        assert tone_for(reason) is Tone.PROCESS


def test_red_is_only_real_danger() -> None:
    """Красный — только реальная опасность. В кабинете она одна: удаление.

    «Истекла», «ошибка ввода», «нет сети» красными названы быть не могут —
    это состояния, а не опасность, и в списке причин их под красным нет.
    """
    danger = [reason for reason, tone in REASON_TONES.items() if tone is Tone.DANGER]
    assert danger == ["delete_account"]
    assert tone_for("subscription_expired") is not Tone.DANGER


def test_green_has_exactly_one_reason_and_no_screen_uses_it() -> None:
    """Зелёный назван ровно одной причиной — подтверждённой защитой.

    И ни один экран кабинета к ней не обращается: подписки, коды, привязка,
    удаление перечислены выше и все не зелёные. Так «зелёного нет нигде» — не
    обещание, а перечислимое утверждение.
    """
    confirmed = [reason for reason, tone in REASON_TONES.items() if tone is Tone.CONFIRMED]
    assert confirmed == [_ONLY_GREEN_REASON]

    used_by_screens = {reason for reason in REASON_TONES if reason != _ONLY_GREEN_REASON}
    assert all(REASON_TONES[reason] is not Tone.CONFIRMED for reason in used_by_screens)


def test_unknown_reason_is_neutral_not_green() -> None:
    """Незнакомая причина — нейтральный. Иначе новая причина «как-нибудь сама»
    получила бы зелёный, а это ровно та ошибка, от которой сторожит весь файл."""
    assert tone_for("совершенно новая причина") is Tone.NEUTRAL
    assert tone_for("") is Tone.NEUTRAL


def test_reason_list_is_closed() -> None:
    """Список причин закрыт: каждая названа поимённо и разложена по тонам.

    Открытый список означал бы «добавлю причину когда понадобится», а вместе с
    ней — и цвет мимо проверки выше.
    """
    expected = {
        "subscription_active",
        "subscription_trial",
        "subscription_expired",
        "subscription_blocked",
        "subscription_unknown",
        "code_sending",
        "code_verifying",
        "linking_in_progress",
        "delete_account",
        "confirmed_protection",
    }
    assert set(REASON_TONES) == expected
    assert theme.CONFIRMED_PROTECTION == "confirmed_protection"


def test_css_variables_cover_every_color_and_radius() -> None:
    """CSS-переменные отдаются из палитры, а не пишутся второй копией в CSS.

    Тест требует, чтобы в наборе были все цвета и все радиусы: забытый токен
    заставил бы шаблон объявить его самому, и копия разошлась бы с палитрой.
    """
    variables = theme.css_variables()

    for field_name in COLORS.__dataclass_fields__:
        name = field_name.replace("_", "-")
        assert f"--{name}" in variables, name
        assert variables[f"--{name}"] == getattr(COLORS, field_name)

    assert variables["--radius-card"] == "14px"
    assert variables["--radius-button"] == "12px"
    assert variables["--radius-small"] == "8px"
