"""Подставные внешние сервисы: бот (`/internal/*`) и отправка почты.

До волны W6 кабинет разговаривает только с ними. Ни один тест не должен
открыть сокет или соединение с базой — иначе зелёный прогон на машине
разработчика перестаёт что-либо значить на чистом раннере.

Здесь живут in-memory реализации двух внешних зависимостей кабинета:

- `FakeApi` — то, что в бою отдаёт внутренний HTTP-роутер бота (аккаунты,
  подписки, конфиги). Умышленно ведёт себя как SQL, а не как удобный
  Python-объект: `link_chat_id` повторяет
  `UPDATE ... WHERE id = ? AND chat_id IS NULL` и возвращает `rowcount`,
  поэтому занятое значение не перезаписывается, а тест видит `0`.
- `FakeMailSender` — транспорт почты. Письма не уходят наружу, а падают в
  `outbox`, откуда тест их читает.

Инварианты §4 спеки продублированы в `FakeApi` намеренно: мок, который
разрешает больше живого сервиса, делает тесты бесполезными.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from cabinet.accounts import Account
from cabinet.bot_client import (
    ORDER_CANCELLED,
    ORDER_COMPLETED,
    ORDER_PENDING,
    BotError,
    Order,
    OrderConfig,
    OrderIssue,
    OrderPaid,
    Server,
    TrialEligibility,
)
from cabinet.bot_client import Subscription as ClientSubscription

# Статус подписки. Контракт `/me` ещё не зафиксирован — это набор мока, который
# закрепит W4.1. Пока он один на весь проект, чтобы тесты и фикстуры не
# расходились.
STATUS_ACTIVE = "active"
STATUS_TRIAL = "trial"
STATUS_EXPIRED = "expired"
STATUS_BLOCKED = "blocked"

SUBSCRIPTION_STATUSES = (STATUS_ACTIVE, STATUS_TRIAL, STATUS_EXPIRED, STATUS_BLOCKED)


class FakeApiError(Exception):
    """Ошибка подставного бота."""


class CollisionError(FakeApiError):
    """`chat_id` или `mail` уже занят другим аккаунтом.

    §5 спеки: коллизия — честная ошибка и предложение войти тем способом.
    Никогда — тихая перезапись.
    """


@dataclass
class FakeSubscription:
    """Строка хранилища подставного бота: одна подписка одного аккаунта.

    Это **не** форма ответа бота. Бот отдаёт подписку полями `id`, `name`,
    `status`, `end_date`, `location` (см. `cabinet.bot_client.Subscription`) —
    именно её, а не эту строку, возвращает `subscriptions_for`. Здесь же
    лежат служебные для мока поля: `account_id` для поиска и `config_id`
    как ссылка на конфиг. Подписка живёт дольше конфига, который могли уже
    отозвать, поэтому это отдельное поле, а не один объект.
    """

    account_id: int
    id: int
    status: str = STATUS_ACTIVE
    expires_at: datetime | None = None
    config_id: int | None = None
    # Поля формы ответа бота, которых у хранилища мока нет по умолчанию:
    # имя подписки и локация сервера — их показывает экран (W4.1).
    name: str = ""
    location: str | None = None

    def is_active(self) -> bool:
        """Даёт ли подписка доступ прямо сейчас. Истёкшая и заблокированная —
        нет; это разные состояния, но оба закрывают конфиг (W4.2)."""
        return self.status in (STATUS_ACTIVE, STATUS_TRIAL)

    def to_api(self) -> dict:
        """Строка хранилища в форму ответа бота — та же, что разбирает
        `cabinet.bot_client.Subscription.from_api`.

        Перевод живёт здесь, у мока: это его дело — отдавать то, что умеет
        разобрать клиент. Держать рядом вторую копию формы для тестов значило
        бы оставить стык «мок → клиент» непроверенным.
        """
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "end_date": self.expires_at.isoformat() if self.expires_at else None,
            "location": self.location,
        }


@dataclass
class VpnConfig:
    """Конфиг `.ovpn`, каким его отдаёт бот.

    `body` — подставной текст. Настоящих адресов, ключей и PEM здесь быть не
    может: репозиторий публичный (см. CLAUDE.md, «Публичность»).
    """

    id: int
    account_id: int
    body: str

    def is_downloadable_by(self, subscription: FakeSubscription | None) -> bool:
        """Можно ли отдать этот конфиг по этой подписке."""
        return (
            subscription is not None
            and subscription.config_id == self.id
            and (subscription.is_active())
        )


@dataclass
class FakeOrder:
    """Строка хранилища заказов подставного бота (шаг 5 покупки, #59).

    Это **не** форма ответа бота: здесь `account_id` — служебное поле мока для
    поиска, а наружу заказ отдаётся полями контракта (`to_api`). Сумма — целые
    рубли, как её считает `pricing` и как её хранит бот: суммы в копейках в
    проекте нет, и сравнивать «сколько просили» с «сколько пришло» надо в одной
    единице. Заказ живёт на `account_id`, а не на `chat_id`: конфиг выдаётся
    Telegram-аккаунту, но покупка заводится на аккаунт (граница сервиса).
    """

    order_id: str
    account_id: int
    days: int
    devices: int
    servers: list[str]
    amount_rub: int
    status: str = ORDER_PENDING
    configs: list[OrderConfig] = field(default_factory=list)

    def to_api(self) -> dict:
        """Заказ в форме ответа бота — та же, что разбирает `Order.from_api`."""
        return {
            "order_id": self.order_id,
            "status": self.status,
            "amount_rub": self.amount_rub,
            "days": self.days,
            "devices": self.devices,
            "servers": list(self.servers),
            "configs": [{"id": c.id, "name": c.name} for c in self.configs],
        }


@dataclass
class FakeApi:
    """Подставной бот. Одно поле — один «эндпоинт» живого `/internal/*`.

    Ничего не ходит в сеть. Состояние живёт в памяти и сбрасывается вместе с
    фикстурой, поэтому тесты не протекают друг в друга.
    """

    accounts: list[Account] = field(default_factory=list)
    subscriptions: list[FakeSubscription] = field(default_factory=list)
    configs: list[VpnConfig] = field(default_factory=list)
    # Серверы для покупки (шаг 4). Список общий, не на аккаунт: бот отдаёт один
    # набор на всех. Пусто по умолчанию — экран обязан честно показать «нет».
    servers: list[Server] = field(default_factory=list)
    # Ответы про право на триал, которые тест задаёт сам. Пусто — считается по
    # общей логике (триал живёт на `chat_id`, как у бота).
    trial_overrides: dict[int, TrialEligibility] = field(default_factory=dict)
    # Заказы кабинета (шаг 5). Пусто по умолчанию: заказ заводит тот же тест,
    # что проверяет экран оплаты.
    orders: list[FakeOrder] = field(default_factory=list)
    # Журнал вызовов: тест может утверждать, что кабинет ходил ровно туда,
    # куда должен, и не ходил лишний раз.
    calls: list[tuple[str, dict]] = field(default_factory=list)
    _next_account_id: int = 1
    _next_subscription_id: int = 1
    _next_config_id: int = 1
    _next_order_id: int = 1

    # --- служебное -------------------------------------------------------

    def _log(self, name: str, **kwargs: object) -> None:
        self.calls.append((name, kwargs))

    def _copy(self, account: Account) -> Account:
        """Отдаём копию: подставной бот не должен позволять тесту менять своё
        состояние в обход собственных методов, минуя проверку инвариантов."""
        return replace(account)

    def _by_id(self, account_id: int) -> Account:
        for account in self.accounts:
            if account.id == account_id:
                return account
        raise FakeApiError(f"аккаунт {account_id} не найден")

    # --- аккаунты: поиск и создание (W1.3) -------------------------------

    def create_account(
        self,
        *,
        chat_id: int | None = None,
        mail: str | None = None,
        mail_verified_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> Account:
        """Создание аккаунта. Уникальность `chat_id`/`mail` — как UNIQUE в схеме."""
        self._log("create_account", chat_id=chat_id, mail=mail)
        if chat_id is not None and self.find_by_chat_id(chat_id) is not None:
            raise CollisionError("chat_id уже занят другим аккаунтом")
        if mail is not None and self.find_by_mail(mail) is not None:
            raise CollisionError("mail уже занят другим аккаунтом")

        account = Account(
            id=self._next_account_id,
            chat_id=chat_id,
            mail=mail,
            mail_verified_at=mail_verified_at,
            # Аккаунт, заведённый этим запросом, — регистрация: только ему
            # разрешено присвоение `chat_id` (инвариант 4, §3.3).
            _created_in_this_request=True,
        )
        self._next_account_id += 1
        self.accounts.append(account)
        return self._copy(account)

    def find_by_chat_id(self, chat_id: int) -> Account | None:
        self._log("find_by_chat_id", chat_id=chat_id)
        for account in self.accounts:
            if account.chat_id == chat_id:
                return self._copy(account)
        return None

    def find_by_mail(self, mail: str) -> Account | None:
        self._log("find_by_mail", mail=mail)
        for account in self.accounts:
            if account.mail == mail:
                return self._copy(account)
        return None

    def find_by_id(self, account_id: int) -> Account | None:
        """Прочитать один аккаунт по `id` — как `GET /internal/accounts/{id}`.

        Отдельный метод, а не `_by_id`: связыванию нужно **прочитать** аккаунт
        (что уже привязано, пусто ли место), и на «не найден» оно отвечает
        отказом, а не исключением мока. `_by_id` для этого не годится — он
        бросает `FakeApiError`, то есть падение теста вместо проверяемого
        состояния.
        """
        self._log("find_by_id", account_id=account_id)
        for account in self.accounts:
            if account.id == account_id:
                return self._copy(account)
        return None

    # --- связывание (W3.2, W3.3, W3.5) -----------------------------------

    def link_chat_id(self, account_id: int, chat_id: int) -> int:
        """Привязка Telegram. Возвращает `rowcount`: 1 — привязали, 0 — нет.

        Повторяет `UPDATE accounts SET chat_id = ? WHERE id = ? AND chat_id IS NULL`.
        Пустое место, а не «другое»: занятый `chat_id` — не повод для
        перезаписи (§3.3, инвариант 3).
        """
        self._log("link_chat_id", account_id=account_id, chat_id=chat_id)
        target = self.find_by_chat_id(chat_id)
        if target is not None and target.id != account_id:
            raise CollisionError("chat_id уже занят другим аккаунтом")

        account = self._by_id(account_id)
        if not account.can_link_telegram():
            return 0
        account.chat_id = chat_id
        return 1

    def link_mail(self, account_id: int, mail: str, mail_verified_at: datetime) -> int:
        """Привязка почты. Тот же контракт `rowcount`, что у `link_chat_id`.

        `mail_verified_at` обязателен и без значения по умолчанию: почта без
        подтверждения не привязывается ни при каких обстоятельствах
        (инвариант 2). Если подстановки захочется — это и есть ошибка.
        """
        self._log("link_mail", account_id=account_id, mail=mail)
        target = self.find_by_mail(mail)
        if target is not None and target.id != account_id:
            raise CollisionError("mail уже занят другим аккаунтом")

        account = self._by_id(account_id)
        if not account.can_link_mail():
            return 0
        account.mail = mail
        account.mail_verified_at = mail_verified_at
        return 1

    def unlink_chat_id(self, account_id: int) -> int:
        """Отвязка Telegram. Последний способ входа отвязать нельзя (W3.5):
        аккаунт без способов входа недоступен навсегда."""
        self._log("unlink_chat_id", account_id=account_id)
        account = self._by_id(account_id)
        if account.chat_id is None:
            return 0
        saved = account.chat_id
        account.chat_id = None
        if not account.has_any_login_method():
            account.chat_id = saved
            return 0
        return 1

    def unlink_mail(self, account_id: int) -> int:
        """Отвязка почты. Симметрично `unlink_chat_id`."""
        self._log("unlink_mail", account_id=account_id)
        account = self._by_id(account_id)
        if account.mail is None:
            return 0
        saved = account.mail, account.mail_verified_at
        account.mail = None
        account.mail_verified_at = None
        if not account.has_any_login_method():
            account.mail, account.mail_verified_at = saved
            return 0
        return 1

    # --- подписки и конфиги (W4) -----------------------------------------

    def subscriptions_for(self, account_id: int) -> list[ClientSubscription]:
        """Список подписок аккаунта в форме ответа бота. Пустой список — не
        ошибка (W4.3).

        Возвращается то же, что отдаёт живой `/internal/.../subscriptions`:
        подписку собирает `cabinet.bot_client.Subscription.from_api`. Иначе
        потребитель (`cabinet.subscriptions`) получал бы от мока форму, которую
        настоящий клиент не строит, и связка «мок → представление» падала бы,
        а тесты этого не видели. Строка хранилища (`FakeSubscription`) наружу
        не выходит — её служебные поля (`account_id`, `config_id`) делу не
        нужны.
        """
        self._log("subscriptions_for", account_id=account_id)
        return [
            ClientSubscription.from_api(s.to_api())
            for s in self.subscriptions
            if s.account_id == account_id
        ]

    def add_subscription(self, account_id: int, **kwargs: object) -> FakeSubscription:
        """Добавить подписку в хранилище мока. Возвращается строка хранилища, а
        не форма ответа: тесту и фикстурам нужны `config_id` и `account_id`,
        которых в ответе бота нет."""
        self._log("add_subscription", account_id=account_id)
        self._by_id(account_id)
        subscription = FakeSubscription(
            account_id=account_id, id=self._next_subscription_id, **kwargs
        )
        self._next_subscription_id += 1
        self.subscriptions.append(subscription)
        return subscription

    def add_config(self, account_id: int, body: str) -> VpnConfig:
        self._log("add_config", account_id=account_id)
        self._by_id(account_id)
        config = VpnConfig(id=self._next_config_id, account_id=account_id, body=body)
        self._next_config_id += 1
        self.configs.append(config)
        return config

    def config_for_subscription(self, account_id: int, subscription_id: int) -> VpnConfig | None:
        """Конфиг подписки или `None`, если отдавать нельзя (W4.2).

        Отказы не различаются: «нет подписки», «чужая подписка» и «истекла»
        снаружи выглядят одинаково — иначе перебором `subscription_id`
        выяснялось бы, у кого что есть.
        """
        self._log("config_for_subscription", account_id=account_id, subscription_id=subscription_id)
        subscription = next(
            (
                s
                for s in self.subscriptions
                if s.id == subscription_id and s.account_id == account_id
            ),
            None,
        )
        if subscription is None:
            return None
        config = next((c for c in self.configs if c.id == subscription.config_id), None)
        if config is None or not config.is_downloadable_by(subscription):
            return None
        return config

    # --- покупка: серверы и право на триал (шаги 3–4) --------------------

    def add_server(self, code: str, name: str) -> Server:
        """Добавить сервер в набор — как `servers` в боте."""
        self._log("add_server", code=code, server_name=name)
        server = Server(code=code, name=name)
        self.servers.append(server)
        return server

    def list_servers(self) -> list[Server]:
        """Серверы для шага 4, в форме ответа бота (`GET /internal/servers`).

        Возвращается тот же тип (`bot_client.Server`), что строит живой клиент:
        иначе потребитель получал бы от мока форму, которой настоящий клиент не
        делает, и связка «мок → экран» разошлась бы незаметно.
        """
        self._log("list_servers")
        return list(self.servers)

    def set_trial(self, account_id: int, *, eligible: bool, reason: str | None = None) -> None:
        """Задать ответ про триал для аккаунта — как решил бы бот."""
        self.trial_overrides[account_id] = TrialEligibility(eligible=eligible, reason=reason)

    def trial_eligibility(self, account_id: int) -> TrialEligibility:
        """Право на триал — ответ `GET /internal/accounts/{id}/trial-eligibility`.

        Без заданного ответа повторяется логика бота: триал живёт на `chat_id`,
        поэтому аккаунту без Telegram бот отвечает `telegram_required`, а не
        «положен». Это не удобство мока, а условие тестов: разреши мок то, чего
        живой бот не делает, и зелёный прогон перестал бы что-либо значить.
        """
        self._log("trial_eligibility", account_id=account_id)
        override = self.trial_overrides.get(account_id)
        if override is not None:
            return override
        account = self._by_id(account_id)
        if account.chat_id is None:
            return TrialEligibility(eligible=False, reason="telegram_required")
        return TrialEligibility(eligible=True, reason=None)

    # --- заказы кабинета (шаг 5 покупки, #59) ----------------------------

    def _find_order(self, order_id: str) -> FakeOrder:
        for order in self.orders:
            if order.order_id == order_id:
                return order
        # Тот же код, что у живого бота: «заказа нет» — осмысленный отказ, по
        # которому экран возврата показывает «заказ не найден», а не сбой.
        raise BotError("order_not_found", "заказ не найден", status=404)

    def create_order(
        self,
        *,
        account_id: int,
        days: int,
        devices: int,
        servers: list[str],
        amount_rub: int,
    ) -> Order:
        """Заказ на аккаунт. Аккаунту без Telegram — отказ `telegram_required`.

        Это повторяет код бота (`_subscription_rows` возвращает пустой список для
        аккаунта без `chat_id`): продавать нечего, конфиг выдать некуда. Отказ
        приходит `BotError` — так же, как от живого роутера, — и экран оплаты
        ветвится по коду.
        """
        self._log("create_order", account_id=account_id, days=days, devices=devices)
        account = self._by_id(account_id)
        if account.chat_id is None:
            raise BotError("telegram_required", "конфиг выдаётся Telegram-аккаунту")
        order = FakeOrder(
            order_id=f"ord-{self._next_order_id}",
            account_id=account_id,
            days=days,
            devices=devices,
            servers=list(servers),
            amount_rub=amount_rub,
        )
        self._next_order_id += 1
        self.orders.append(order)
        return Order.from_api(order.to_api())

    def get_order(self, order_id: str) -> Order:
        """Заказ по номеру или отказ `order_not_found`."""
        self._log("get_order", order_id=order_id)
        return Order.from_api(self._find_order(order_id).to_api())

    def mark_order_paid(self, order_id: str) -> OrderPaid:
        """Отметить оплаченным. `changed=false` на повторе — как у бота.

        `changed` и есть идемпотентность вебхука: `true` только на первом
        подтверждении, по нему кабинет решает, слать ли письмо.
        """
        self._log("mark_order_paid", order_id=order_id)
        order = self._find_order(order_id)
        changed = order.status != ORDER_COMPLETED
        order.status = ORDER_COMPLETED
        return OrderPaid(order_id=order.order_id, status=order.status, changed=changed)

    def cancel_order(self, order_id: str) -> Order:
        """Отменить заказ."""
        self._log("cancel_order", order_id=order_id)
        order = self._find_order(order_id)
        order.status = ORDER_CANCELLED
        return Order.from_api(order.to_api())

    def issue_order(self, order_id: str) -> OrderIssue:
        """Выдать конфиги по заказу. Повтор — `already=true`, без второго комплекта.

        Один конфиг на выбранный сервер: так повторная выдача не создаёт второй
        комплект, а `already` это подтверждает.
        """
        self._log("issue_order", order_id=order_id)
        order = self._find_order(order_id)
        already = bool(order.configs)
        if not already:
            order.configs = [
                OrderConfig(id=f"cfg-{i + 1}", name=code) for i, code in enumerate(order.servers)
            ]
        return OrderIssue(
            order_id=order.order_id,
            configs=tuple(order.configs),
            already=already,
        )


@dataclass
class FakeMailSender:
    """Подставной транспорт почты. Письма не уходят наружу.

    Сознательно глуп: не генерирует коды и не считает попытки — это дело
    `auth_mail` (W2.2). Здесь только «письмо ушло» в виде записи в `outbox`.
    """

    outbox: list[dict[str, str]] = field(default_factory=list)

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.outbox.append({"to": to, "subject": subject, "body": body})

    def sent_to(self, mail: str) -> list[dict[str, str]]:
        return [message for message in self.outbox if message["to"] == mail]


def utc(year: int = 2026, month: int = 10, day: int = 17, hour: int = 12) -> datetime:
    """Время с явной зоной. Наивные `datetime` в тестах скрывают расхождения
    между локальным временем машины и UTC."""
    return datetime(year, month, day, hour, tzinfo=UTC)
