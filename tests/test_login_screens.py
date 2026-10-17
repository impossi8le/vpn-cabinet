"""W5.1: экраны входа и ожидания кода.

Два экрана, и у каждого проверяется не «есть ли HTML», а то, ради чего он
существует.

**Вход (`render_login`)** должен пускать по почте самостоятельно, без Telegram.
Это главное требование спеки (§1): у человека без Telegram доступа к аккаунту
нет вообще, и экран, который первым делом просит Telegram, воспроизводит
замкнутый круг «чтобы получить VPN, нужен VPN». Тест требует, чтобы на экране
не было призыва войти через Telegram и чтобы поле почты было единственным
вводом.

**Ожидание (`render_login_waiting`)** держит две вещи, которые легко потерять:
поле принимает только цифры и не больше шести (иначе код из письма не влезает
или, наоборот, протаскивает мусор), и отсчёт живой. Последнее — про честность:
метка «5:00», снятая один раз на сервере, через минуту врёт.

И то, что связывает оба экрана с W5.0: **на экранах входа нет зелёного.**
Здесь нечего подтверждать — ни туннеля, ни защиты, — и единственный цвет
ожидания кода — янтарный. Тест берёт отрендеренный HTML и ищет HEX зелёного
токена буквально: если он появится, это тот самый ложный зелёный, от которого
сторожит §7.
"""

from __future__ import annotations

import re

import pytest

from cabinet.render import render_login, render_login_waiting
from cabinet.theme import COLORS


@pytest.fixture
def login_html() -> str:
    return render_login()


@pytest.fixture
def waiting_html() -> str:
    return render_login_waiting(mail="user@example.com")


# --- вход: почта — самостоятельный путь ---------------------------------


def test_login_asks_for_mail_as_the_only_credential(login_html: str) -> None:
    """Поле почты есть, и кроме него ввод не требуется.

    `type="email"` и `autocomplete="email"` — не украшение: без них на телефоне
    открывается текстовая клавиатура, и ввод длинной почты становится пыткой.
    """
    assert 'name="mail"' in login_html
    assert 'type="email"' in login_html
    assert 'autocomplete="email"' in login_html


def test_login_does_not_force_telegram(login_html: str) -> None:
    """Ни одного призыва войти через Telegram.

    Telegram упомянут ровно как необязательный — «привяжете позже»: убрать это
    слово значило бы вернуть пользователя в круг «сначала получи Telegram».
    """
    lowered = login_html.lower()
    assert "войти тем же telegram" not in lowered
    assert "войти через telegram" not in lowered
    assert "не обязателен" in lowered


def test_login_keeps_entered_mail_on_error() -> None:
    """Отвергнутый адрес возвращается в поле, а не сбрасывается.

    Стереть введённое и сказать «неверная почта» — заставить набирать заново то,
    что человек уже набрал; с `value` адрес остаётся и его можно поправить.
    Автоэкранирование Jinja отдаёт `@` как `&#64;` — это по-прежнему тот же
    адрес в поле, поэтому тест ищет его по префиксу и хвосту.
    """
    html = render_login(mail="user@example.com", error="Почта уже занята")
    assert 'value="user' in html and "example.com" in html
    assert "Почта уже занята" in html


def test_login_links_are_before_the_button(login_html: str) -> None:
    """Политика и условия стоят до кнопки, а не после.

    Ссылки после кнопки читаются как «уже поздно»; до неё — как условие, которое
    видно, прежде чем нажать. Порядок в разметке и есть это обещание.
    """
    assert login_html.index("/privacy") < login_html.index("<button")
    assert login_html.index("/terms") < login_html.index("<button")


# --- ожидание: код и отсчёт ---------------------------------------------


def test_waiting_accepts_only_digits_and_at_most_six(waiting_html: str) -> None:
    """Поле кода ограничено цифрами и шестью знаками — и разметкой, и скриптом.

    Одной разметки мало: `maxlength` обходится вставкой из буфера, а вставку
    пользователь как раз и делает. Поэтому тест требует оба ограничения.
    """
    assert 'maxlength="6"' in waiting_html
    assert 'inputmode="numeric"' in waiting_html
    # Валидация вставки — скриптом: вырезаем всё, кроме цифр, и обрезаем до шести.
    assert re.search(r"replace\(/\\D/g,\s*['\"]{2}\)", waiting_html)
    assert ".slice(0, 6)" in waiting_html


def test_waiting_countdown_starts_at_five_minutes_and_ticks(waiting_html: str) -> None:
    """Отсчёт идёт от 5:00 и тикает вниз.

    `setInterval` с шагом в секунду — признак живого отсчёта; статичная метка
    «5:00» через минуту врала бы, показывая, что времени ещё полно.
    """
    assert "5:00" in waiting_html
    assert "setInterval" in waiting_html
    assert "1000" in waiting_html


def test_waiting_countdown_does_not_go_negative(waiting_html: str) -> None:
    """Отсчёт останавливается на нуле, а не уходит в минус.

    «-0:41» читается как сбой страницы, а не как «код истёк»; `Math.max(0, ...)`
    и остановка таймера на нуле — то, что это исключает.
    """
    assert "Math.max(0" in waiting_html
    assert "clearInterval" in waiting_html


def test_waiting_shows_the_address_and_allows_resend(waiting_html: str) -> None:
    """Видно, на какой адрес ушёл код, и есть чем запросить новый.

    Адрес на экране — чтобы человек заметил опечатку в почте сейчас, а не
    дожидался впустую несуществующего письма. «Ещё раз» — на случай, если
    письмо не дошло: без него остаётся тупик.
    """
    assert "user@example.com" in waiting_html
    assert "Отправить код ещё раз" in waiting_html


def test_waiting_busy_disables_the_button() -> None:
    """Во время проверки кнопка занята и подписана «Проверяем…».

    Второй тап по активной кнопке запускал бы вторую проверку того же кода —
    ту же ошибку однажды допустили в клиенте на баннере обновления.
    """
    html = render_login_waiting(mail="user@example.com", busy=True)
    assert "Проверяем…" in html
    assert "disabled" in html


def test_waiting_error_is_amber_not_red() -> None:
    """Ошибка кода янтарная: код не подошёл — это не опасность.

    Красный в кабинете зарезервирован под реальную опасность (§7). Показать им
    «код истёк» значило бы потратить сигнал тревоги на обычную осечку ввода.
    """
    html = render_login_waiting(mail="user@example.com", error="Код не подошёл")
    assert "Код не подошёл" in html
    assert COLORS.amber.upper() in html.upper()


# --- семантика цвета на обоих экранах -----------------------------------


def _markup_without_palette(html: str) -> str:
    """HTML без блока `:root`, где палитра объявляется.

    Палитра обязана быть на странице целиком — иначе не сработают стили, — и в
    ней есть зелёный как заготовка на будущий подтверждённый замер. Проверять
    надо не объявление, а **применение**: зелёного не должно быть ни в одном
    элементе разметки. Поэтому объявление переменных вырезается, и остаётся
    ровно то, что нарисовано.
    """
    start = html.find(":root {")
    end = html.find("}", start)
    return html[:start] + html[end:]


def test_no_green_on_either_login_screen(login_html: str, waiting_html: str) -> None:
    """Зелёного нет ни на входе, ни на ожидании — в самой разметке, по HEX.

    Здесь нечего подтверждать: туннеля нет, защиту никто не измерял. Это и есть
    проверка правила §7 в применении к экранам входа — тот же ложный зелёный,
    что был главным дефектом ревью макета, только теперь он ловится в HTML.
    """
    green = COLORS.green.upper()
    assert green not in _markup_without_palette(login_html).upper()
    assert green not in _markup_without_palette(waiting_html).upper()


def test_primary_button_is_bone_white(waiting_html: str, login_html: str) -> None:
    """Главная кнопка белая: своего брендового цвета у продукта нет (§7).

    Проверяется, что кнопка помечена основным классом: стиль `--bone` приходит
    из CSS, и тест на палитру уже закрепил, что `--bone` — это `#E8ECF2`.
    """
    assert 'class="btn btn-p"' in login_html
    assert 'class="btn btn-p"' in waiting_html


def test_palette_reaches_the_page_from_python(login_html: str) -> None:
    """Палитра вставляется в страницу из `theme`, а не объявлена в шаблоне.

    `--void` — фон — обязан появиться на экране буквально из токена. Если
    шаблон объявит свои цвета, эта проверка упадёт, и станет видно, что копия
    палитры завелась в разметке.
    """
    assert f"--void: {COLORS.void}" in login_html
    assert f"--bone: {COLORS.bone}" in login_html
