"""W5.2–W5.4: экраны подписок и связывания, отказ в конфиге на карточке.

Каждый экран проверяется по тому, ради чего он существует, а не по наличию
HTML.

**Подписки (`render_subscriptions`)** обязаны различать состояния, а не сообщать
одно «доступ есть / нет». «Истекла» и «заблокирована» — разные причины, и
человеку за ними нужно разное: срок кончился — это часы, доступ закрыт сервисом —
это не часы. Свести их в один текст значило бы спрятать единственную подсказку о
том, что произошло.

**Связывание (`render_linking`)** не должно предлагать отвязать последний способ
входа: кнопка, ведущая к заведомому отказу, — та же ложь, что кнопка, которая
ничего не делает. И Telegram ID показан маской: экран уходит в поддержку
скриншотом.

**Отказ в конфиге** показывается на карточке подписки, по которой нажали
скачать, а не на отдельном экране: уехав на другую страницу, человек теряет из
виду, к чему относилась ошибка. При этом отказы остаются различимыми —
«истекла», «отозван», «лимит» требуют разного действия и разных текстов.

Общее для всех экранов — правило §7: **зелёного нет нигде.** Здесь нечего
подтверждать: ни туннеля, ни защиты. Проверка буквальная, по HEX, и смотрит на
применение цвета, а не на объявление палитры, — как это уже сделано для экранов
входа (`test_login_screens.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cabinet import render
from cabinet.accounts import Account
from cabinet.bot_client import Subscription
from cabinet.configs import (
    ConfigError,
    ConfigNotFound,
    ConfigRetired,
    ConfigRevoked,
    DownloadRateLimited,
    SubscriptionInactive,
)
from cabinet.subscriptions import STATUS_ACTIVE, SubscriptionView, subscription_views
from cabinet.theme import COLORS, Tone

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _views(*subscriptions: Subscription) -> list:
    return subscription_views(subscriptions, now=NOW)


def _subscription(**kwargs: object) -> Subscription:
    base = {"id": "1", "name": "Подключение", "status": STATUS_ACTIVE}
    base.update(kwargs)
    return Subscription(**base)  # type: ignore[arg-type]


def _markup_without_palette(html: str) -> str:
    """HTML без блока `:root`, где палитра объявляется.

    Палитра обязана быть на странице целиком, и зелёный в ней есть как заготовка
    на будущий подтверждённый замер. Проверять надо применение: зелёного не
    должно быть ни в одном элементе разметки.
    """
    start = html.find(":root {")
    end = html.find("}", start)
    return html[:start] + html[end:]


def _assert_no_green(html: str) -> None:
    assert COLORS.green.upper() not in _markup_without_palette(html).upper()


# --- подписки: состояния различимы ---------------------------------------


def test_active_subscription_is_not_green() -> None:
    """«Активна» нейтральна: срок о защите не говорит (§7).

    Это тот самый дефект из ревью макета — активное состояние красили зелёным,
    хотя никакого замера не было. Здесь он проверяется на самом экране, а не
    только в палитре: экран рисует активную подписку, и зелёного в разметке нет.
    """
    html = render.render_subscriptions(
        _views(_subscription(status=STATUS_ACTIVE, end_date=NOW + timedelta(days=30)))
    )
    assert "Активна" in html
    _assert_no_green(html)


@pytest.mark.parametrize(
    "status",
    ["active", "trial", "expired", "blocked"],
)
def test_no_subscription_status_renders_green(status: str) -> None:
    """Ни одно состояние подписки не зелёное — на четырёх статусах поимённо."""
    html = render.render_subscriptions(
        _views(_subscription(status=status, end_date=NOW + timedelta(days=10)))
    )
    _assert_no_green(html)
    assert COLORS.amber.upper() not in _markup_without_palette(html).upper()


def test_expired_and_blocked_read_differently() -> None:
    """«Истекла» и «заблокирована» — разные тексты, а не одно «доступа нет».

    Разные причины, разные подсказки: истёкшую продлевают, заблокированную —
    выясняют. Одинаковый текст заставлял бы гадать, что случилось.
    """
    html = render.render_subscriptions(
        _views(
            _subscription(
                id="1", name="Истекшая", status="expired", end_date=NOW - timedelta(days=1)
            ),
            _subscription(
                id="2", name="Заблокированная", status="blocked", end_date=NOW + timedelta(days=5)
            ),
        )
    )
    assert "Истекла" in html
    assert "Заблокирована" in html
    # И пояснения под ними — разные, а не общая фраза.
    assert "Срок подписки закончился." in html
    assert "Доступ закрыт сервисом" in html


def test_expired_and_blocked_are_not_red() -> None:
    """Истёкшая и заблокированная — состояния, а не опасность (§7).

    Красный в кабинете зарезервирован под реальную опасность. Потратить его на
    «срок прошёл» значило бы приравнять кончившуюся подписку к угрозе.
    """
    html = render.render_subscriptions(
        _views(
            _subscription(id="1", status="expired", end_date=NOW - timedelta(days=1)),
            _subscription(id="2", status="blocked", end_date=NOW + timedelta(days=5)),
        )
    )
    assert COLORS.red.upper() not in _markup_without_palette(html).upper()


def test_subscription_shows_end_date_and_remaining_days() -> None:
    """Виден срок: дата и сколько осталось, с русским склонением числа дней."""
    html = render.render_subscriptions(
        _views(_subscription(status=STATUS_ACTIVE, end_date=NOW + timedelta(days=1)))
    )
    assert "18.10.2026" in html
    # Округление вверх: 24 часа — это «1 день», а не «0 дней».
    # Проверяем подпись и значение вместе: слово «осталось» стоит подписью
    # поля, а не в самом значении — иначе строка читалась бы «Осталось /
    # осталось 1 день».
    assert "Осталось</dt>" in html
    assert "1 день" in html
    assert "осталось 1 день" not in html


def test_subscription_without_end_date_says_it_is_unknown() -> None:
    """Срок неизвестен — так и написано, а не прочерк и не «истекла».

    «Неизвестен» и «прошёл» — разные состояния: подписка без даты работает.
    """
    html = render.render_subscriptions(_views(_subscription(end_date=None)))
    assert "неизвестен" in html


def test_download_button_only_on_usable_subscription() -> None:
    """Кнопка скачивания есть только у работающей подписки.

    У истёкшей и заблокированной конфиг всё равно не отдаётся (`is_usable`),
    и предлагать кнопку значило бы обещать то, что сервис отвергнет.
    """
    usable = render.render_subscriptions(
        _views(_subscription(status=STATUS_ACTIVE, end_date=NOW + timedelta(days=30)))
    )
    assert "/config" in usable

    dead = render.render_subscriptions(
        _views(_subscription(status="expired", end_date=NOW - timedelta(days=1)))
    )
    assert "/config" not in dead
    assert "Скачивание недоступно" in dead


# --- подписки: пусто и предупреждение о сроке ----------------------------


def test_empty_list_is_not_an_error() -> None:
    """Пустой список — «подписок нет», а не пустая страница и не сбой (W4.3)."""
    html = render.render_subscriptions([])
    assert "Подписок нет" in html
    assert "empty" in html


def test_expiring_soon_warns_without_changing_the_tone() -> None:
    """Скоро истекает — предупреждение есть, но зелёного и красного нет.

    Предупреждение о сроке — это срок, а не опасность и не защита: отдельная
    строка, а не смена цвета значка.
    """
    html = render.render_subscriptions(
        _views(_subscription(status=STATUS_ACTIVE, end_date=NOW + timedelta(days=2)))
    )
    assert "скоро закончится" in html
    _assert_no_green(html)
    assert COLORS.red.upper() not in _markup_without_palette(html).upper()


def test_far_subscription_has_no_soon_warning() -> None:
    """Далёкий срок не пугает: предупреждение показывается только близко к концу."""
    html = render.render_subscriptions(
        _views(_subscription(status=STATUS_ACTIVE, end_date=NOW + timedelta(days=30)))
    )
    assert "скоро закончится" not in html


# --- связывание способов входа -------------------------------------------


def test_linking_shows_what_is_attached() -> None:
    """Видно, что привязано: почта и Telegram по отдельности."""
    html = render.render_linking(Account(id=1, mail="user@example.com", mail_verified_at=NOW))
    assert "user@example.com" in html
    assert "Привязана" in html
    assert "Не привязан" in html


def test_linking_masks_telegram_id() -> None:
    """Telegram ID показан маской, а не целиком.

    Экран уходит в поддержку скриншотом; последние цифры владельцу хватает,
    чтобы узнать свой аккаунт, а профиль по снимку не утекает.
    """
    html = render.render_linking(Account(id=1, chat_id=123456789))
    assert "123456789" not in html
    assert "6789" in html
    assert "••••" in html


def test_linking_offers_unlink_only_when_another_method_remains() -> None:
    """Отвязать предлагается только там, где останется второй вход.

    Аккаунт без способов входа недоступен навсегда (§8), поэтому у
    единственного способа кнопки отвязки быть не должно — она вела бы к
    заведомому отказу.
    """
    mail_only = render.render_linking(Account(id=1, mail="user@example.com", mail_verified_at=NOW))
    assert "Отвязать почту" not in mail_only
    assert "привяжите Telegram" in mail_only

    telegram_only = render.render_linking(Account(id=1, chat_id=123456789))
    assert "Отвязать Telegram" not in telegram_only
    assert "привяжите почту" in telegram_only

    both = render.render_linking(
        Account(id=1, chat_id=123456789, mail="user@example.com", mail_verified_at=NOW)
    )
    assert "Отвязать почту" in both
    assert "Отвязать Telegram" in both


def test_linking_offers_to_attach_the_missing_second_method() -> None:
    """У недостающего способа есть форма привязки — иначе экран был бы тупиком."""
    mail_only = render.render_linking(Account(id=1, mail="user@example.com", mail_verified_at=NOW))
    assert "Привязать Telegram" in mail_only

    telegram_only = render.render_linking(Account(id=1, chat_id=123456789))
    assert "Получить код" in telegram_only
    assert 'name="mail"' in telegram_only


def test_linking_mail_attachment_takes_an_email_field() -> None:
    """Привязка почты — через поле email: адрес должен уходить как адрес."""
    html = render.render_linking(Account(id=1, chat_id=123456789))
    assert 'type="email"' in html
    assert 'autocomplete="email"' in html


def test_linking_has_no_green() -> None:
    """На экране связывания зелёного нет: тут нечего подтверждать (§7)."""
    html = render.render_linking(
        Account(id=1, chat_id=123456789, mail="user@example.com", mail_verified_at=NOW)
    )
    _assert_no_green(html)


def test_linking_notice_tone_comes_from_caller() -> None:
    """Тон сообщения задаёт вызывающий код, а не разметка.

    Привязка — идущий процесс, и её тон янтарный; значение приходит значением,
    поэтому «зелёный где попало» здесь не появляется.
    """
    html = render.render_linking(
        Account(id=1, mail="user@example.com", mail_verified_at=NOW),
        notice="Привязываем Telegram…",
        notice_tone=Tone.PROCESS.value,
    )
    assert "Привязываем Telegram…" in html
    assert "tone-process" in html


# --- отказ в выдаче конфига: на карточке подписки ------------------------


def _usable_view(id_: str = "sub-1", name: str = "Домашний") -> SubscriptionView:
    return SubscriptionView(
        id=id_,
        name=name,
        status=STATUS_ACTIVE,
        reported_status=STATUS_ACTIVE,
        end_date=NOW + timedelta(days=30),
        days_left=30,
    )


def test_refusal_appears_on_the_card_of_the_pressed_subscription() -> None:
    """Отказ виден на карточке той подписки, по которой нажали скачать.

    Это и есть замена отдельному экрану: человек нажал здесь — здесь и читает
    ответ, не теряя из виду, к какой подписке он относился.
    """
    views = [_usable_view("sub-1", "Домашний"), _usable_view("sub-2", "Телефон")]
    html = render.render_subscriptions(
        views,
        config_error=ConfigRevoked("x"),
        config_error_id="sub-2",
    )

    title = "Доступ к подключению отозван"
    assert title in html
    # Отказ стоит на карточке sub-2, а не sub-1: разбираем HTML по границе второй
    # карточки — её открывает имя подписки «Телефон».
    first, second = html.split("Телефон", 1)
    assert title in second
    assert title not in first


def test_refusal_without_id_lands_on_no_card() -> None:
    """Без указания подписки отказ не приписывается ни одной карточке.

    Приписать его первой попавшейся — значит сказать человеку, что сломан доступ
    к подписке, которая в порядке.
    """
    html = render.render_subscriptions([_usable_view("sub-1")], config_error=ConfigRevoked("x"))
    assert "Конфиг отозван" not in html


def test_config_refusals_read_differently() -> None:
    """У каждого отказа свой текст и своё действие.

    «Обновите список» и «попробуйте позже» — не одно и то же; свалить их в
    «ошибка» значило бы лишить человека единственной подсказки. Различение
    держится на `isinstance` в `_config_error_text` (W5.4).
    """

    def card(error: ConfigError) -> str:
        return render.render_subscriptions(
            [_usable_view()], config_error=error, config_error_id="sub-1"
        )

    inactive = card(SubscriptionInactive("x"))
    not_found = card(ConfigNotFound("x"))
    revoked = card(ConfigRevoked("x"))
    retired = card(ConfigRetired("x"))

    notes = {
        inactive.split("refusal-s", 1)[1],
        not_found.split("refusal-s", 1)[1],
        revoked.split("refusal-s", 1)[1],
        retired.split("refusal-s", 1)[1],
    }
    assert len(notes) == 4


def test_rate_limit_shows_how_long_to_wait() -> None:
    """У «слишком много скачиваний» показан срок ожидания.

    Без него «попробуйте позже» не подсказывает, сколько именно ждать, и человек
    либо бросает, либо долбит кнопку (`DownloadRateLimited.retry_after_seconds`).
    """
    html = render.render_subscriptions(
        [_usable_view()],
        config_error=DownloadRateLimited("x", retry_after_seconds=120),
        config_error_id="sub-1",
    )
    assert "Повторить можно" in html
    assert "120" in html


def test_unknown_config_error_gets_a_neutral_fallback() -> None:
    """Незнакомый отказ не выдаётся за «не найдено» — у него запасной текст.

    Иначе расхождение версий с ботом выглядело бы как «конфига нет», и причина
    не всплыла бы никогда.
    """
    html = render.render_subscriptions(
        [_usable_view()], config_error=ConfigError("что-то новое"), config_error_id="sub-1"
    )
    title, note = render.CONFIG_ERROR_FALLBACK
    assert title in html
    assert note in html


def test_config_refusals_have_no_green_and_no_red() -> None:
    """Отказ в конфиге — состояние: ни зелёного, ни красного (§7)."""
    for error in (
        SubscriptionInactive("x"),
        ConfigNotFound("x"),
        ConfigRevoked("x"),
        ConfigRetired("x"),
        DownloadRateLimited("x", retry_after_seconds=60),
        ConfigError("x"),
    ):
        html = render.render_subscriptions(
            [_usable_view()], config_error=error, config_error_id="sub-1"
        )
        body = _markup_without_palette(html)
        _assert_no_green(html)
        # Красное объявлено в палитре для необратимого удаления аккаунта, но на
        # отказе не применяется: отказ в конфиге — состояние, а не опасность (§7).
        assert COLORS.red.upper() not in body.upper()


def test_the_standalone_refusal_screen_is_gone() -> None:
    """Отдельного экрана отказа больше нет — ни функции, ни шаблона.

    Экран 6 снят: ошибка живёт на карточке, и воскрешать страницу нечем. Проверка
    держит это удаление, чтобы функция не вернулась «на всякий случай».
    """
    assert not hasattr(render, "render_config_unavailable")
    assert "render_config_unavailable" not in render.__all__

    templates = Path(render.__file__).parent / "templates"
    assert not (templates / "config_unavailable.html").exists()
