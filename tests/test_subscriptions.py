"""W4.1: список подписок со сроком и статусом.

Тесты отвечают на вопросы, которые иначе всплыли бы на экране:

- что видит пользователь по каждой подписке из `/me` (все статусы);
- что делать, когда статус сервиса и дата говорят разное;
- где проходит граница срока — секунда до, момент наступления, секунда после;
- что показывать, когда срока нет вовсе или он не разобрался.

Считаются только состояния, а не разметка: тексты экрана проверяются на W5.2.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fakes import STATUS_ACTIVE, STATUS_BLOCKED, STATUS_EXPIRED, STATUS_TRIAL, Subscription

from cabinet.bot_client import Subscription as ClientSubscription
from cabinet.subscriptions import (
    EXPIRING_SOON_DAYS,
    STATUS_UNKNOWN,
    days_left,
    is_past,
    resolve_status,
    subscription_view,
    subscription_views,
)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def make(
    *,
    id: str = "1",
    name: str = "Подписка",
    status: str = STATUS_ACTIVE,
    end_date: datetime | None = None,
    location: str | None = None,
) -> ClientSubscription:
    """Подписка в том виде, в каком её отдаёт клиент бота."""
    return ClientSubscription(id=id, name=name, status=status, end_date=end_date, location=location)


def from_fixture(raw: Subscription) -> ClientSubscription:
    """Мост от мок-фикстуры (`expires_at`) к объекту клиента (`end_date`).

    Мок отдаёт строку `/me` с именем поля `expires_at`, клиент — с `end_date`.
    Переводим здесь, чтобы тесты на фикстурах не заводили второй модели
    подписки.
    """
    return make(
        id=str(raw.id),
        status=raw.status,
        end_date=raw.expires_at,
    )


# --- статусы: все состояния из `/me` --------------------------------------


def test_active_subscription_with_future_date_is_usable(subscription_active: Subscription):
    """Главный случай: активна, срок впереди — доступ есть."""
    view = subscription_view(from_fixture(subscription_active), now=NOW)
    assert view.status == STATUS_ACTIVE
    assert view.is_usable is True
    assert view.end_date_is_known is True


def test_trial_is_usable():
    """Пробный период — тоже доступ, а не «почти активна»."""
    view = subscription_view(make(status=STATUS_TRIAL, end_date=NOW + timedelta(days=7)), now=NOW)
    assert view.status == STATUS_TRIAL
    assert view.is_usable is True


def test_expired_subscription_is_not_usable(subscription_expired: Subscription):
    """Истёкшая: срок прошёл, доступа нет."""
    view = subscription_view(from_fixture(subscription_expired), now=NOW)
    assert view.status == STATUS_EXPIRED
    assert view.is_usable is False


def test_blocked_subscription_is_not_usable(subscription_blocked: Subscription):
    """Заблокированная: срок ещё не истёк, но доступа нет — это разные причины,
    и на экране они должны читаться по-разному."""
    view = subscription_view(from_fixture(subscription_blocked), now=NOW)
    assert view.status == STATUS_BLOCKED
    assert view.is_usable is False
    assert view.days_left is not None and view.days_left > 0


def test_unknown_status_does_not_grant_access():
    """Новый статус сервиса доступа не даёт: незнакомое не подтягивается к
    активному «на всякий случай»."""
    view = subscription_view(make(status="frozen", end_date=NOW + timedelta(days=30)), now=NOW)
    assert view.status == STATUS_UNKNOWN
    assert view.is_usable is False
    assert view.status_label


def test_status_case_and_spaces_do_not_decide_access():
    """Регистр и пробелы не должны решать, работает подписка или нет."""
    view = subscription_view(make(status=" Active ", end_date=NOW + timedelta(days=1)), now=NOW)
    assert view.status == STATUS_ACTIVE
    assert view.is_usable is True


# --- конфликт «статус против даты» ----------------------------------------


def test_past_date_beats_active_status():
    """Статус `active`, но срок прошёл — верим дате. Статус мог не обновиться,
    а доступ кончился: показать «активна» значило бы обещать то, чего нет."""
    view = subscription_view(
        make(status=STATUS_ACTIVE, end_date=NOW - timedelta(seconds=1)), now=NOW
    )
    assert view.status == STATUS_EXPIRED
    assert view.is_usable is False
    assert view.reported_status == STATUS_ACTIVE


def test_future_date_does_not_overrule_expired_status():
    """Обратная сторона: `expired`, но срок впереди — верим статусу. Датой
    доступ не возвращается, иначе опечатка в дате открывала бы конфиг."""
    view = subscription_view(
        make(status=STATUS_EXPIRED, end_date=NOW + timedelta(days=30)), now=NOW
    )
    assert view.status == STATUS_EXPIRED
    assert view.is_usable is False
    assert view.reported_status == STATUS_EXPIRED


def test_blocked_wins_over_future_date():
    """Блокировка — решение человека, и она перевешивает любую дату."""
    view = subscription_view(
        make(status=STATUS_BLOCKED, end_date=NOW + timedelta(days=365)), now=NOW
    )
    assert view.status == STATUS_BLOCKED
    assert view.is_usable is False


def test_blocked_wins_over_past_date():
    """Заблокированная с прошедшим сроком остаётся заблокированной: причину
    отказа выбирает блокировка, а не порядок проверок."""
    view = subscription_view(make(status=STATUS_BLOCKED, end_date=NOW - timedelta(days=5)), now=NOW)
    assert view.status == STATUS_BLOCKED


@pytest.mark.parametrize(
    ("reported", "end_date", "expected"),
    [
        (STATUS_ACTIVE, None, STATUS_ACTIVE),
        (STATUS_ACTIVE, NOW - timedelta(days=1), STATUS_EXPIRED),
        (STATUS_EXPIRED, None, STATUS_EXPIRED),
        (STATUS_BLOCKED, None, STATUS_BLOCKED),
        ("нечто", None, STATUS_UNKNOWN),
    ],
)
def test_resolve_status_table(reported: str, end_date: datetime | None, expected: str):
    """Сведение в таблице: одно правило, проверенное по всем сочетаниям."""
    assert resolve_status(reported, end_date, now=NOW) == expected


# --- границы времени ------------------------------------------------------


def test_boundary_exact_expiry_moment_is_already_expired():
    """Граница включительна: в сам момент окончания срок уже не действует."""
    assert is_past(NOW, now=NOW) is True
    view = subscription_view(make(status=STATUS_ACTIVE, end_date=NOW), now=NOW)
    assert view.status == STATUS_EXPIRED


def test_boundary_one_second_before_expiry_is_still_usable():
    """Секунда до окончания — подписка ещё работает: сдвиг границы на секунду
    в одну сторону ломал бы доступ, в другую — оставлял бы его после срока."""
    view = subscription_view(
        make(status=STATUS_ACTIVE, end_date=NOW + timedelta(seconds=1)), now=NOW
    )
    assert view.status == STATUS_ACTIVE
    assert view.is_usable is True


def test_boundary_one_second_after_expiry_is_closed():
    view = subscription_view(
        make(status=STATUS_ACTIVE, end_date=NOW - timedelta(seconds=1)), now=NOW
    )
    assert view.status == STATUS_EXPIRED


def test_no_end_date_is_unknown_not_expired():
    """Подписка без срока — не истёкшая: `None` о времени не говорит ничего,
    поэтому дата такое состояние не отменяет."""
    view = subscription_view(make(status=STATUS_ACTIVE, end_date=None), now=NOW)
    assert view.status == STATUS_ACTIVE
    assert view.end_date_is_known is False
    assert view.days_left is None
    assert view.is_usable is True


def test_unparsid_date_does_not_crash_the_screen():
    """Дата, которую бот отдал не в ISO-8601, клиент превращает в `None`, и
    экран показывает «срок неизвестен», а не падает."""
    raw = ClientSubscription.from_api(
        {"id": 1, "name": "x", "status": STATUS_ACTIVE, "end_date": "не дата"}
    )
    view = subscription_view(raw, now=NOW)
    assert view.end_date is None
    assert view.is_usable is True


def test_non_utc_timezone_is_compared_correctly():
    """Дата в другой зоне сравнивается по моменту времени, а не по цифрам на
    часах: 15:00+03:00 — это те же 12:00 UTC, то есть ровно момент окончания."""
    end = datetime(2026, 10, 17, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    assert end == NOW
    assert subscription_view(make(status=STATUS_ACTIVE, end_date=end), now=NOW).status == (
        STATUS_EXPIRED
    )


def test_naive_datetime_is_read_as_utc():
    """Наивную дату бота считаем UTC. Без этого сравнение наивного и зонного
    времени бросило бы `TypeError` — экран упал бы на ровном месте."""
    naive = datetime(2026, 11, 16, 12, 0)  # без tzinfo
    view = subscription_view(make(status=STATUS_ACTIVE, end_date=naive), now=NOW)
    assert view.status == STATUS_ACTIVE
    assert view.days_left == 30


# --- days_left и «скоро закончится» ---------------------------------------


def test_days_left_is_none_without_end_date():
    assert days_left(None, now=NOW) is None


def test_days_left_is_zero_when_expired():
    """Истёкшая — ноль, а не отрицательное число: «минус три дня» на экране
    читалось бы как ошибка."""
    assert days_left(NOW - timedelta(days=3), now=NOW) == 0


def test_days_left_rounds_up_partial_day():
    """20 часов до срока — это «остался 1 день», а не «0»: иначе живая
    подписка выглядела бы истёкшей."""
    assert days_left(NOW + timedelta(hours=20), now=NOW) == 1


def test_days_left_exact_days():
    assert days_left(NOW + timedelta(days=3), now=NOW) == 3


def test_expiring_soon_at_boundary():
    """Порог предупреждения: ровно на границе — предупреждаем, на день дальше —
    нет."""
    at_boundary = subscription_view(
        make(status=STATUS_ACTIVE, end_date=NOW + timedelta(days=EXPIRING_SOON_DAYS)), now=NOW
    )
    beyond = subscription_view(
        make(status=STATUS_ACTIVE, end_date=NOW + timedelta(days=EXPIRING_SOON_DAYS + 1)), now=NOW
    )
    assert at_boundary.expires_soon is True
    assert beyond.expires_soon is False


def test_expired_subscription_is_not_expiring_soon():
    """Об истёкшей не предупреждают «скоро закончится»: предупреждать нечего."""
    view = subscription_view(make(status=STATUS_EXPIRED, end_date=NOW - timedelta(days=1)), now=NOW)
    assert view.expires_soon is False


def test_subscription_without_end_date_is_not_expiring_soon():
    """Без срока предупредить нельзя — и не выдумываем его."""
    view = subscription_view(make(status=STATUS_ACTIVE, end_date=None), now=NOW)
    assert view.expires_soon is False


# --- список ---------------------------------------------------------------


def test_empty_list_stays_empty():
    """W4.3: «подписок нет» — состояние экрана, а не ошибка."""
    assert subscription_views([], now=NOW) == []


def test_list_keeps_every_subscription():
    """Ни одна подписка не теряется и не добавляется: список только приводится
    к порядку."""
    items = [
        make(id="1", status=STATUS_ACTIVE, end_date=NOW + timedelta(days=10)),
        make(id="2", status=STATUS_EXPIRED, end_date=NOW - timedelta(days=1)),
        make(id="3", status=STATUS_BLOCKED, end_date=NOW + timedelta(days=5)),
    ]
    views = subscription_views(items, now=NOW)
    assert {view.id for view in views} == {"1", "2", "3"}


def test_usable_subscriptions_come_first_and_soonest_first():
    """Порядок: работающее впереди нерабочего, внутри — то, что сгорит раньше.
    Иначе вверху оказалась бы подписка, которой уже не пользуются."""
    items = [
        make(id="later", status=STATUS_ACTIVE, end_date=NOW + timedelta(days=30)),
        make(id="dead", status=STATUS_EXPIRED, end_date=NOW - timedelta(days=1)),
        make(id="soon", status=STATUS_ACTIVE, end_date=NOW + timedelta(days=2)),
    ]
    assert [view.id for view in subscription_views(items, now=NOW)] == ["soon", "later", "dead"]


def test_subscription_without_end_date_sorts_after_dated_ones():
    """Подписка без срока не встаёт рядом с «сгорает завтра»: `None` о времени
    ничего не говорит, поэтому уходит в конец своей группы."""
    items = [
        make(id="no-date", status=STATUS_ACTIVE, end_date=None),
        make(id="dated", status=STATUS_ACTIVE, end_date=NOW + timedelta(days=100)),
    ]
    assert [view.id for view in subscription_views(items, now=NOW)] == ["dated", "no-date"]


def test_order_is_stable_regardless_of_service_order():
    """Порядок не зависит от того, как ответил сервис: иначе две одинаковые
    подписки менялись бы местами между запросами."""
    first = make(id="a", status=STATUS_ACTIVE, end_date=NOW + timedelta(days=1))
    second = make(id="b", status=STATUS_ACTIVE, end_date=NOW + timedelta(days=1))
    assert [v.id for v in subscription_views([first, second], now=NOW)] == ["a", "b"]
    assert [v.id for v in subscription_views([second, first], now=NOW)] == ["a", "b"]


# --- интеграция с ответом бота --------------------------------------------


def test_view_is_built_from_raw_api_payload():
    """Полный путь: словарь ответа бота → `Subscription` → представление."""
    raw = ClientSubscription.from_api(
        {
            "id": 42,
            "name": "Basic",
            "status": STATUS_ACTIVE,
            "end_date": (NOW + timedelta(days=5)).isoformat(),
            "location": "nl",
        }
    )
    view = subscription_view(raw, now=NOW)
    assert (view.id, view.name, view.location) == ("42", "Basic", "nl")
    assert view.status == STATUS_ACTIVE
    assert view.days_left == 5
    assert view.expires_soon is False


def test_status_domain_constants_match_the_mock_contract():
    """Строки статусов домена и мока обязаны совпадать: разойдись они —
    разошлось бы и поведение, а тесты на моке подтверждали бы не то."""
    from cabinet import subscriptions

    assert subscriptions.STATUS_ACTIVE == STATUS_ACTIVE
    assert subscriptions.STATUS_TRIAL == STATUS_TRIAL
    assert subscriptions.STATUS_EXPIRED == STATUS_EXPIRED
    assert subscriptions.STATUS_BLOCKED == STATUS_BLOCKED
