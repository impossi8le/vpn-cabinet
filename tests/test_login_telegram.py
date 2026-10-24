"""W5а.2: экран входа через Telegram — код из бота.

Кнопка «Войти через Telegram» на экране входа обещала флоу, которого за ней не
было: ссылка вела на `/login/telegram`, а экрана там не стояло. Этот тест
закрывает звено целиком и проверяет его не по наличию HTML, а по тому, ради
чего экран существует.

**Код приходит из бота, а не из письма.** Поле кода, ограничение «только цифры,
максимум 6» и живой отсчёт — те же, что на экране ожидания письма, потому что
требование клиента то же (`it.filter(Char::isDigit).take(6)`). Разница в
источнике кода и в текстах, и тест берёт их **дословно из клиента**
(`implemented-ui-spec.md`, раздел LoginWaiting): «Откройте Telegram — бот пришлёт
код подтверждения». Подпись кнопки входа — «Открыть Telegram»: тем же текстом её
называет план входа (`docs/plans/2026-10-24-telegram-login.md`), и это дорога **в**
бота на первом заходе, а не только повтор. Сочинённый текст разошёлся бы с
приложением, и человек, перешедший из app на сайт, увидел бы другой продукт.

**Честность состояний** — правило проекта, а не украшение. Ссылка на бота ещё не
готова — экран говорит прямо, а не рисует пустую кнопку. Код истёк или попытки
исчерпаны — сказано, что делать. Проверяется и то, что зелёного на экране нет:
здесь нечего подтверждать (§7).
"""

from __future__ import annotations

import re

import pytest

from cabinet import render
from cabinet.render import render_login_telegram
from cabinet.theme import COLORS

# Ссылка челленджа из `/auth/link`. В тесте — условная: реальный адрес бота в
# публичном репозитории стоять не может.
BOT_LINK = "https://example.invalid/bot?start=login_demo"


@pytest.fixture
def telegram_html() -> str:
    return render_login_telegram(telegram_url=BOT_LINK)


def _markup_without_palette(html: str) -> str:
    """HTML без блока `:root`, где объявляется палитра.

    Зелёный в палитре есть как заготовка на будущий подтверждённый замер;
    проверять надо применение — зелёного не должно быть ни в одном элементе
    разметки. Тот же приём, что в `test_login_screens.py`.
    """
    start = html.find(":root {")
    end = html.find("}", start)
    return html[:start] + html[end:]


# --- поле кода: цифры и шесть знаков -------------------------------------


def test_code_field_accepts_only_digits_and_at_most_six(telegram_html: str) -> None:
    """Поле кода — только цифры, не больше шести, и разметкой, и скриптом.

    Одной разметки мало: `maxlength` обходится вставкой из буфера, а вставку
    пользователь как раз и делает — код из бота удобнее скопировать, чем
    набирать. Поэтому тест требует оба ограничения, как и у поля кода из письма.
    """
    assert 'maxlength="6"' in telegram_html
    assert 'inputmode="numeric"' in telegram_html
    assert re.search(r"replace\(/\\D/g,\s*['\"]{2}\)", telegram_html)
    assert ".slice(0, 6)" in telegram_html


def test_code_field_is_labeled_as_coming_from_the_bot(telegram_html: str) -> None:
    """Метка поля — «Код из бота» (текст клиента), а не «Код из письма».

    Источник кода назван прямо: с подписью про письмо человек ждал бы письма,
    которого бот не шлёт.
    """
    assert "Код из бота" in telegram_html


# --- тексты из клиента ----------------------------------------------------


def test_body_text_names_the_bot_as_the_code_source(telegram_html: str) -> None:
    """Тело экрана — дословно из клиента: код придёт от бота.

    Формулировка выверена на живом приложении; сочинять свою значило бы
    показать на сайте другой экран, чем в app.
    """
    assert "Откройте Telegram" in telegram_html
    assert "бот пришлёт" in telegram_html


def test_reopen_button_opens_the_bot_again(telegram_html: str) -> None:
    """Есть кнопка «Открыть Telegram», ведущая в бот входа.

    Это требование клиента (`LOGIN_WAITING_REOPEN_TAG`): ссылка открывает бота
    **входа** по `deep_link` из `/auth/link`, а не бота поддержки — в
    поддержке нет кнопки «Подтвердить вход», и вход там завершить нельзя.
    Подпись — «Открыть Telegram», а не «…ещё раз»: это дорога **в** бота на
    первом заходе, а не только повтор уже пройденного.
    """
    # Подпись дословно и целиком: «Открыть Telegram» — префикс старой «Открыть
    # Telegram ещё раз», и проверка вхождения пропустила бы возврат к ней.
    assert ">Открыть Telegram</a>" in telegram_html
    assert BOT_LINK in telegram_html


def test_reopen_link_is_a_link_not_a_form(telegram_html: str) -> None:
    """«Открыть Telegram» — ссылка: адрес открывается переходом, не POST.

    Передавать в переход нечего, а форма отправила бы запрос туда, где ждут
    страницу.
    """
    assert f'<a class="btn btn-g" href="{BOT_LINK}" target="_blank"' in telegram_html


def test_reopen_link_opens_in_a_new_tab_and_isolates_the_bot_page(telegram_html: str) -> None:
    """Ссылка в бота — новой вкладкой и без доступа назад в кабинет.

    Челлендж начат в кабинете и завершается в нём же: переход в этой вкладке
    унёс бы экран с полем кода, а возврат историей начал бы **другой** челлендж,
    и код на руках перестал бы подходить. `rel` обязан нести `noopener`: иначе
    страница бота через `window.opener` подменит адрес вкладки кабинета.
    """
    assert 'target="_blank"' in telegram_html
    assert 'rel="noopener noreferrer"' in telegram_html


# --- честность неготовой ссылки ------------------------------------------


def test_missing_link_is_stated_honestly_not_shown_as_empty_button() -> None:
    """Ссылки нет — сказано прямо, и кнопки «Открыть Telegram» нет.

    Пустая кнопка обещала бы переход, которого сервис не сделал. Клиент в этом
    состоянии честен («Ссылка на вход ещё не готова»), и сайт обязан быть
    честен так же — иначе разница текстов скрыла бы неготовность за кнопкой.
    """
    html = render_login_telegram(telegram_url=None)
    assert render.TELEGRAM_LINK_NOT_READY in html
    assert ">Открыть Telegram</a>" not in html


def test_ready_link_drops_the_not_ready_note(telegram_html: str) -> None:
    """Ссылка готова — сообщения о неготовности нет: два состояния различимы."""
    assert render.TELEGRAM_LINK_NOT_READY not in telegram_html


# --- истёкший код и исчерпанные попытки ----------------------------------


def test_expired_code_says_to_start_over() -> None:
    """Истёкшее окно — сказано, что делать: начать вход заново.

    «Время входа истекло» само по себе не подсказывает действия; текст ведёт к
    нему прямо. Формулировка — из списка ошибок клиента (`AuthViewModel`).
    """
    html = render_login_telegram(telegram_url=BOT_LINK, error_state="expired")
    assert render.TELEGRAM_CODE_EXPIRED in html


def test_attempt_limit_says_to_start_over_and_differs_from_expiry() -> None:
    """Перебор попыток — свой текст, а не тот же, что у истёкшего кода.

    Причины разные (срок против перебора), и один текст спрятал бы, что
    произошло. Оба ведут начать заново, но называют разное.
    """
    html = render_login_telegram(telegram_url=BOT_LINK, error_state="attempt_limit_exceeded")
    assert render.TELEGRAM_ATTEMPTS_EXCEEDED in html
    assert render.TELEGRAM_CODE_EXPIRED not in html


def test_unknown_error_state_falls_back_to_start_over_not_a_blank_screen() -> None:
    """Незнакомый код ошибки бота ведёт к «начать заново», а не к пустому экрану.

    Бот может добавить новое значение `status`; показать при этом ничего
    значило бы отдать пустую страницу вместо действия.
    """
    html = render_login_telegram(telegram_url=BOT_LINK, error_state="something_new")
    assert render.TELEGRAM_CODE_EXPIRED in html


def test_free_error_text_is_shown_verbatim() -> None:
    """Свободный `error` печатается как есть — для отказов, которых нет в списке."""
    html = render_login_telegram(telegram_url=BOT_LINK, error="Код не подошёл")
    assert "Код не подошёл" in html


# --- состояния проверки и кнопка -----------------------------------------


def test_busy_disables_the_button_and_changes_the_label() -> None:
    """Во время проверки кнопка занята и подписана «Проверяем…».

    Второй тап по активной кнопке запускал бы вторую проверку того же кода —
    та же ошибка, что однажды ловили в клиенте на баннере обновления.
    """
    html = render_login_telegram(telegram_url=BOT_LINK, busy=True)
    assert "Проверяем…" in html
    assert "disabled" in html


def test_countdown_ticks_from_five_minutes(telegram_html: str) -> None:
    """Отсчёт идёт от 5:00 и тикает вниз, останавливаясь на нуле.

    Статичная метка «5:00» через минуту врала бы, показывая, что времени полно.
    Остановка на нуле — чтобы «-0:41» не читалось как сбой страницы.
    """
    assert "5:00" in telegram_html
    assert "setInterval" in telegram_html
    assert "Math.max(0" in telegram_html
    assert "clearInterval" in telegram_html


def test_restart_leads_back_to_the_entry(telegram_html: str) -> None:
    """Есть чем начать заново: «Войти заново» ведёт на повторный вход.

    Без этого истёкший код и исчерпанные попытки оставляли бы тупик — экран
    говорил бы «начните заново», не давая начать.
    """
    assert "Войти заново" in telegram_html
    assert render.TELEGRAM_RESTART_URL in telegram_html


# --- семантика цвета ------------------------------------------------------


def test_no_green_on_the_telegram_screen(telegram_html: str) -> None:
    """Зелёного нет: туннеля нет, защиту никто не измерял (§7)."""
    assert COLORS.green.upper() not in _markup_without_palette(telegram_html).upper()


def test_error_is_amber_not_red() -> None:
    """Ошибка кода янтарная, а не красная: осечка ввода — не опасность (§7)."""
    html = render_login_telegram(telegram_url=BOT_LINK, error_state="expired")
    assert COLORS.amber.upper() in html.upper()
    assert COLORS.red.upper() not in _markup_without_palette(html).upper()


def test_primary_button_is_bone_white(telegram_html: str) -> None:
    """Главная кнопка белая: своего брендового цвета у продукта нет (§7)."""
    assert 'class="btn btn-p"' in telegram_html
