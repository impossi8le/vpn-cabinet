"""Домен подписок: одно непротиворечивое состояние на экран.

`bot_client` отдаёт подписку такой, какой её отдаёт сервис: `status` — строка
контракта, `end_date` — дата или ничего. Экрану нужно другое — состояние, из
которого сразу видно, работает ли подписка сейчас и сколько ей осталось. Здесь
это состояние и получается. Ходить в бот и разбирать ответы остаётся делом
`bot_client`; второй копии этих правил не заводится.

Два источника правды и их конфликт. Сервис называет статус, дата говорит о
времени — и они могут расходиться: подписка с истёкшим сроком ещё помечена
активной (статус не успели обновить), бывает и обратное. Разрешается одним
правилом:

    дата может только отнять доступ, но не вернуть его.

- срок прошёл → подписка истекла, каким бы ни был статус сервиса;
- срок не прошёл, а сервис говорит `expired` → подписка истекла: датой доступ
  не возвращается, иначе ошибка в дате открывала бы конфиг;
- `blocked` не отменяется ничем: это решение человека, а не показание часов.

Незнакомый статус доступа не даёт: новый статус сервиса показывается как
`unknown`, а не подтягивается к активному.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil

from cabinet.bot_client import Subscription

__all__ = [
    "EXPIRING_SOON_DAYS",
    "STATUS_ACTIVE",
    "STATUS_BLOCKED",
    "STATUS_EXPIRED",
    "STATUS_LABELS",
    "STATUS_TRIAL",
    "STATUS_UNKNOWN",
    "SubscriptionView",
    "days_left",
    "is_past",
    "normalize_status",
    "resolve_status",
    "subscription_view",
    "subscription_views",
]

# Статусы контракта `/me`. Те же строки, что в `tests/fakes.py`: если мок и
# домен разойдутся в написании, разойдётся и поведение.
STATUS_ACTIVE = "active"
STATUS_TRIAL = "trial"
STATUS_EXPIRED = "expired"
STATUS_BLOCKED = "blocked"

# Статус, которого нет в контракте. Отдельное значение, а не «оставим как
# пришло»: экран должен показать что-то осмысленное, а доступ такой статус не
# даёт ни при каких условиях.
STATUS_UNKNOWN = "unknown"

# Статусы, дающие доступ. Список закрытый: всё, чего в нём нет, доступа не даёт.
_USABLE_STATUSES = (STATUS_ACTIVE, STATUS_TRIAL)

# Тексты для экрана. Здесь, а не в шаблоне: одно состояние должно читаться
# одинаково везде, где оно показывается.
STATUS_LABELS = {
    STATUS_ACTIVE: "Активна",
    STATUS_TRIAL: "Пробный период",
    STATUS_EXPIRED: "Истекла",
    STATUS_BLOCKED: "Заблокирована",
    STATUS_UNKNOWN: "Состояние неизвестно",
}

# «Скоро закончится» — за сколько дней до срока экран начинает предупреждать.
EXPIRING_SOON_DAYS = 3

# Порядок для списка: работающее впереди нерабочего, иначе пользователь с
# несколькими подписками видел бы вверху ту, которой уже не пользуется.
_STATUS_ORDER = {
    STATUS_ACTIVE: 0,
    STATUS_TRIAL: 1,
    STATUS_EXPIRED: 2,
    STATUS_BLOCKED: 3,
    STATUS_UNKNOWN: 4,
}

# Подписка без срока в сортировке уходит в конец: `None` о сроке не говорит
# ничего, и ставить её рядом со «сгорает завтра» нельзя.
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)

_SECONDS_PER_DAY = 86_400


def _as_utc(value: datetime) -> datetime:
    """Привести время к UTC.

    Бот может отдать дату без зоны (`2026-11-16T12:00:00`), а `now` приходит с
    зоной. Сравнить такие `datetime` напрямую нельзя — Python бросит
    `TypeError`, и экран упадёт на сравнении. Наивное время договорились
    считать UTC: так его пишет сервис.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_status(reported: str) -> str:
    """Привести статус сервиса к известному.

    Регистр и пробелы не должны решать, работает подписка или нет: `"Active"`
    и `" active "` — это `active`. Всё незнакомое — `unknown`, а не «наверное,
    активна».
    """
    candidate = (reported or "").strip().lower()
    if candidate in STATUS_LABELS:
        return candidate
    return STATUS_UNKNOWN


def is_past(end_date: datetime | None, *, now: datetime) -> bool:
    """Истёк ли срок на момент `now`.

    Граница включительная: в сам момент окончания срок уже не действует.
    Секунда в запасе — действует; это и есть разница, которую проверяют
    тесты на границах.
    """
    if end_date is None:
        return False
    return _as_utc(end_date) <= _as_utc(now)


def resolve_status(reported: str, end_date: datetime | None, *, now: datetime) -> str:
    """Свести статус сервиса и дату в одно состояние для экрана.

    Порядок проверок — это и есть правило «дата отнимает, но не возвращает»:

    1. `blocked` — решение человека, оно перевешивает и дату, и любой статус;
    2. прошедший срок — доступ закрыт независимо от статуса;
    3. иначе — статус сервиса, включая `expired` с датой в будущем: дата его
       не поднимает.
    """
    status = normalize_status(reported)
    if status == STATUS_BLOCKED:
        return STATUS_BLOCKED
    if is_past(end_date, now=now):
        return STATUS_EXPIRED
    return status


def days_left(end_date: datetime | None, *, now: datetime) -> int | None:
    """Сколько дней осталось. `None` — срок неизвестен, `0` — уже истёк.

    Округляется вверх: неполные сутки показываются как «остался 1 день», а не
    как «0». Иначе подписка, которой жить ещё 20 часов, читалась бы на экране
    как истёкшая.
    """
    if end_date is None:
        return None
    remaining = (_as_utc(end_date) - _as_utc(now)).total_seconds()
    if remaining <= 0:
        return 0
    return ceil(remaining / _SECONDS_PER_DAY)


@dataclass(frozen=True)
class SubscriptionView:
    """Подписка, какой её видит экран.

    `status` — итоговое состояние (после сведения даты и статуса), а
    `reported_status` — то, что сказал сервис: расхождение видно и проверяемо,
    а не спрятано в момент сведения.
    """

    id: str
    name: str
    status: str
    reported_status: str
    end_date: datetime | None = None
    location: str | None = None
    days_left: int | None = None

    @property
    def is_usable(self) -> bool:
        """Работает ли подписка прямо сейчас. Отсюда же решается, отдавать ли
        конфиг (W4.2): отдельного «можно скачать» рядом с этим быть не должно —
        два ответа на один вопрос разъедутся."""
        return self.status in _USABLE_STATUSES

    @property
    def expires_soon(self) -> bool:
        """Скоро закончится — стоит предупредить. Истёкшая и заблокированная
        сюда не попадают: предупреждать о сроке нечего, когда доступа уже нет."""
        return (
            self.is_usable and self.days_left is not None and self.days_left <= EXPIRING_SOON_DAYS
        )

    @property
    def end_date_is_known(self) -> bool:
        """Известен ли срок. Экран различает «срок неизвестен» и «срок прошёл»."""
        return self.end_date is not None

    @property
    def status_label(self) -> str:
        """Текст состояния для экрана."""
        return STATUS_LABELS.get(self.status, STATUS_LABELS[STATUS_UNKNOWN])


def subscription_view(subscription: Subscription, *, now: datetime) -> SubscriptionView:
    """Собрать представление одной подписки.

    `now` передаётся снаружи, а не берётся из часов: иначе тесты на границы
    срока зависели бы от того, когда их запустили.
    """
    end_date = _as_utc(subscription.end_date) if subscription.end_date else None
    reported = normalize_status(subscription.status)
    return SubscriptionView(
        id=subscription.id,
        name=subscription.name,
        status=resolve_status(subscription.status, end_date, now=now),
        reported_status=reported,
        end_date=end_date,
        location=subscription.location,
        days_left=days_left(end_date, now=now),
    )


def subscription_views(
    subscriptions: Iterable[Subscription], *, now: datetime
) -> list[SubscriptionView]:
    """Список подписок для экрана, в порядке показа.

    Пустой список возвращается пустым: «подписок нет» — состояние экрана, а не
    ошибка (W4.3). Ни одна подписка не добавляется и не теряется — список
    только приводится к порядку.
    """
    views = [subscription_view(item, now=now) for item in subscriptions]
    return sorted(views, key=_sort_key)


def _sort_key(view: SubscriptionView) -> tuple[int, datetime, str, str]:
    """Работающее впереди, внутри — то, что сгорит раньше; подписки без срока
    в конце. Имя добивает равенство, id — последнее: две подписки с одним
    именем и сроком не должны меняться местами между запросами, иначе список
    на экране «дрожит» без причины."""
    return (
        _STATUS_ORDER.get(view.status, _STATUS_ORDER[STATUS_UNKNOWN]),
        view.end_date or _FAR_FUTURE,
        view.name,
        view.id,
    )
