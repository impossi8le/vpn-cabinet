"""Сессии кабинета: доступ по аккаунту, а не по способу входа (W2.5).

Главный случай проекта — человек, зарегистрировавшийся по почте и Telegram не
привязавший. У него `chat_id` пуст, и сессия, привязанная к `chat_id`, такому
человеку недоступна вовсе: он не войдёт никак. Поэтому владелец сессии здесь —
**`account_id`**; `chat_id` остаётся атрибутом аккаунта и достаётся из него при
нужде.

Требует этого не удобство, а один инвариант: у человека, который входит из
приложения (Telegram) и из кабинета (почта), состояние входа обязано быть
**одним**. Выход из аккаунта и истечение срока должны действовать на обеих
дверях сразу, иначе человек выходит в браузере и остаётся внутри в приложении.
Две независимые таблицы сессий разошлись бы — вопрос времени. Поэтому модуль
говорит с одним хранилищем сессий (`SessionStore`) и требует от него ровно то,
что может дать единая таблица сессий бота (§4 контракта: `POST
/internal/sessions/for-account`, `GET /internal/sessions/current`,
`POST /internal/sessions/revoke`).

Границы: кабинет не ходит в базу (CLAUDE.md, «Границы сервиса»). Живой роутер
`/internal/sessions/*` подставится на W6 как ещё одна реализация `SessionStore`;
до неё модуль проверен против `SingleSessionStore` из этого же файла — мок-слой
`tests/fakes.py` трогать нельзя, он общий с другими волнами.

Сравнение токена. Поиск идёт по **хешу**, а не по самому токену: в хранилище
лежит `sha256`. Сравнивать токен строкой напрямую было бы нельзя — обычное `==`
выходит на первом несовпавшем байте, и по времени ответа токен подбирается
посимвольно. Здесь такого сравнения нет вовсе, и приём тот же, что у кодов в
`auth_mail`: наружу из хранилища открытый токен не выходит никогда.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from cabinet.accounts import Account

__all__ = [
    "DEFAULT_SESSION_TTL",
    "MIN_TOKEN_BYTES",
    "AccountStore",
    "NoAccount",
    "Session",
    "SessionRefused",
    "SessionService",
    "SessionStore",
    "SessionView",
    "SingleSessionStore",
    "hash_token",
    "new_token",
]

# Сколько живёт сессия кабинета. Тридцать дней — это «вошёл и забыл про вход»,
# как в приложении, но не токен без срока: угнанный токен обязан перестать
# работать сам, без действий человека.
DEFAULT_SESSION_TTL = timedelta(days=30)

# Энтропия токена. 32 байта `secrets` — 256 бит; перебор невозможен, и значение
# вынесено в константу, чтобы уступка здесь не прошла незамеченной: короткий
# токен превращает подбор из невозможного в вопрос времени.
MIN_TOKEN_BYTES = 32


class SessionRefused(Exception):
    """Сессия не выдана. Сообщение — для оператора и лога, не для экрана."""


class NoAccount(SessionRefused):
    """Аккаунт для сессии не существует.

    Отделено от прочих отказов: это не «плохой токен», а расхождение состояния
    (аккаунт не создан или пришёл неверный `account_id`). Вызывающему коду здесь
    нечего повторять — надо заводить аккаунт.
    """


class AccountStore(Protocol):
    """Хранилище аккаунтов, как его видит выдача сессии.

    Форма — та же, что у связывания (`cabinet.linking.AccountStore`): входу
    нужен `find_by_id`, и заводить второй протокол ради одного совпадающего
    метода значило бы плодить соглашения о том же самом. Живой `BotClient` и мок
    эту форму уже дают.
    """

    def find_by_id(self, account_id: int) -> Account | None:
        """Прочитать аккаунт по `id`."""
        ...


class SessionStore(Protocol):
    """Хранилище сессий, как его видит кабинет.

    Токен ходит наружу только в момент выдачи: в хранилище лежит его **хеш**, и
    `touch`/`revoke` принимают хеш, а не токен — иначе токен оседал бы в логах и
    в памяти вне момента выдачи.

    Возвращается `Session`, а не `bool`: вызывающему нужны `account_id` и
    `expires_at` — иначе срок пришлось бы хранить второй копией, а без копии не
    показать, до когда человек вошёл.

    Время последней активности обновляет хранилище, а не вызывающий: владелец
    строки сессии — оно, и второй копии этого времени быть не должно.
    """

    def create(self, *, account_id: int, token_hash: str, expires_at: datetime) -> Session:
        """Записать сессию и вернуть строку, какой она легла в хранилище."""
        ...

    def find(self, token_hash: str) -> Session | None:
        """Найти сессию по хешу токена. `None` — не найдена."""
        ...

    def touch(self, token_hash: str, *, moment: datetime) -> None:
        """Отметить активность. Отсутствие сессии — не ошибка."""
        ...

    def revoke(self, token_hash: str) -> None:
        """Отозвать сессию. Повторный отзыв — не ошибка (§4: выход идемпотентен)."""
        ...


@dataclass(frozen=True)
class Session:
    """Строка сессии, какой её отдаёт хранилище.

    `account_id` — владелец. `chat_id` здесь нет намеренно: способ входа в
    сессии не участвует, и место ему — в аккаунте.

    `last_seen_at` — справочное время, и оно **не** сдвигает `expires_at`: сессия
    с активностью продлевалась бы бесконечно, пока человек заходит хотя бы раз в
    месяц, и срок перестал бы что-либо значить.
    """

    token_hash: str
    account_id: int
    expires_at: datetime
    last_seen_at: datetime | None = None


@dataclass(frozen=True)
class SessionView:
    """Сессия, какой её видит вызывающий код после выдачи или проверки.

    Токен здесь есть: он показывается **один раз**, в момент выдачи, и держать
    его копию в хранилище незачем — оттуда он не вернётся никогда. Вызывающий
    сам решает, куда его положить (cookie `HttpOnly`), но видеозапись сессии без
    токена неполна, поэтому он входит в результат.
    """

    account_id: int
    token: str
    expires_at: datetime


def hash_token(token: str) -> str:
    """Хеш токена для хранилища.

    Хранится вместо токена: репозиторий публичный, и токен, попавший в дамп или
    лог, — это уже угнанная сессия. По хешу токен не восстанавливается, а
    сравнение с ним ничего о токене не сообщает.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(nbytes: int = MIN_TOKEN_BYTES) -> str:
    """Случайный токен. Источник — `secrets`, не `random`: `random` предсказуем
    по начальному состоянию, а предсказуемость токена означает чужой вход.

    Минимум энтропии проверяется, а не подразумевается: уступка в один аргумент
    ослабила бы все сессии разом, и заметить это надо здесь, а не в бою.
    """
    if nbytes < MIN_TOKEN_BYTES:
        raise ValueError(f"токен короче {MIN_TOKEN_BYTES} байт: энтропии не хватает")
    return secrets.token_urlsafe(nbytes)


class SessionService:
    """Выдача, проверка и отзыв сессий кабинета.

    Часы подаются снаружи: тест на истечение обязан быть детерминированным, а не
    зависеть от того, как быстро отработал прогон. В бою часы —
    `datetime.now(UTC)`.

    Фабрика токена — тоже шов, и по той же причине: коллизия источника токенов
    (дважды выданный один токен) должна проверяться детерминированно, а не
    ожиданием вероятностного события.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        accounts: AccountStore,
        ttl: timedelta = DEFAULT_SESSION_TTL,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_factory: Callable[[], str] = new_token,
    ) -> None:
        self._store = store
        self._accounts = accounts
        self._ttl = ttl
        self._now = now
        self._new_token = token_factory

    # ---------- выдача ----------

    def sign_in(self, account_id: int) -> SessionView:
        """Выдать сессию аккаунту. Способ входа значения не имеет.

        Главное свойство метода: сессия выдаётся **по аккаунту**, поэтому
        аккаунт, заведённый по почте (`chat_id = NULL`), получает её ровно так
        же, как аккаунт из Telegram. Различия способов входа здесь нет вовсе —
        иначе оно вернулось бы в сессии, откуда мы его убрали.

        Аккаунт читается перед выдачей намеренно: сессия на несуществующий
        аккаунт была бы ссылкой в пустоту, и «аккаунта нет» — это отказ входа,
        а не сбой хранилища.
        """
        if self._accounts.find_by_id(account_id) is None:
            raise NoAccount(f"аккаунт {account_id} не найден")

        token = self._new_token()
        session = self._store.create(
            account_id=account_id,
            token_hash=hash_token(token),
            expires_at=_as_utc(self._now()) + self._ttl,
        )
        return SessionView(
            account_id=session.account_id,
            token=token,
            expires_at=session.expires_at,
        )

    # ---------- проверка ----------

    def resolve(self, token: str | None) -> SessionView | None:
        """Разобрать токен в сессию или вернуть `None`.

        `None` — не отказ: экран входа на это отвечает формой. Токен приходит из
        cookie браузера, и «cookie нет» — обычное состояние первого захода, а не
        сбой, ради которого стоило бы бросать исключение.

        Срок проверяется здесь, а не в хранилище: хранилище отвечает «что
        записано», а «не истекло ли» зависит от часов, которые подаёт
        вызывающий, и считаться должно в одном месте со сроком выдачи.

        Истёкшую сессию гасим сразу: держать её дальше смысла нет, а оставленная
        запись — это ещё действующий токен в таблице.

        Активность отмечается **после** проверки срока: запрос с истёкшим токеном
        активностью не является.
        """
        if not token:
            return None

        token_hash = hash_token(token)
        session = self._store.find(token_hash)
        if session is None:
            return None

        moment = _as_utc(self._now())
        if _as_utc(session.expires_at) <= moment:
            self._store.revoke(token_hash)
            return None

        self._store.touch(token_hash, moment=moment)
        return SessionView(
            account_id=session.account_id,
            token=token,
            expires_at=session.expires_at,
        )

    # ---------- выход ----------

    def sign_out(self, token: str | None) -> None:
        """Выход. Идемпотентно: повторный выход — не ошибка (§4).

        Идемпотентность здесь не вежливость, а требование: выход — та дверь,
        которой человек закрывает себе доступ, и отказ «сессии уже нет» оставил
        бы его с ощущением, что выход не сработал, — при том что токен уже
        недействителен.
        """
        if not token:
            return
        self._store.revoke(hash_token(token))


def _as_utc(value: datetime) -> datetime:
    """Привести время к UTC.

    Хранилище может отдать срок без зоны (бот пишет `DATETIME`), а часы приходят
    с зоной — сравнить такие `datetime` напрямую Python не даст, бросит
    `TypeError` в середине обработки запроса. Наивное время договорились считать
    UTC: так его пишет сервис. Тот же приём, что в `subscriptions`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass
class SingleSessionStore:
    """Сессии в памяти процесса: тот же `SessionStore`, без базы и без сети.

    Нужен, пока роутер `/internal/sessions/*` не написан: кабинет держит сессии
    сам. Форма — общий протокол, поэтому подмена на живой бот не потребует правок
    вызывающего кода, а до неё поведение сессий проверяется здесь, а не в тесте,
    который пришлось бы писать против второго хранилища.

    Состояние сбрасывается при перезапуске процесса, и это осознанный выбор для
    текущей волны, а не заготовка: как и коды в `auth_mail`, это состояние,
    которому переживать перезапуск не нужно. Переживать его сессиям предстоит
    тогда, когда их домом станет таблица бота (W6) — и тогда эту роль сыграет
    живой клиент, а не этот класс.
    """

    sessions: dict[str, Session] = field(default_factory=dict)

    def create(self, *, account_id: int, token_hash: str, expires_at: datetime) -> Session:
        """Записать сессию.

        Повтор по тому же хешу — не «новая сессия», а коллизия источника токенов:
        токен, выданный дважды, означал бы, что второй человек получает доступ
        первого. Отказ, а не тихая перезапись — иначе сессию первого сменила бы
        сессия второго, и заметить это было бы негде.
        """
        if token_hash in self.sessions:
            raise SessionRefused("хеш токена уже занят: источник токенов выдал повтор")
        session = Session(token_hash=token_hash, account_id=account_id, expires_at=expires_at)
        self.sessions[token_hash] = session
        return session

    def find(self, token_hash: str) -> Session | None:
        """Сессия или `None`. Поиск идёт по хешу, поэтому содержимое токена на
        время ответа не влияет."""
        return self.sessions.get(token_hash)

    def touch(self, token_hash: str, *, moment: datetime) -> None:
        """Отметить активность. Время пишется полем, а не сдвигом срока: иначе
        сессия жила бы вечно, пока человек заходит хотя бы изредка."""
        session = self.sessions.get(token_hash)
        if session is not None:
            self.sessions[token_hash] = replace(session, last_seen_at=moment)

    def revoke(self, token_hash: str) -> None:
        """Отозвать. Отсутствие сессии — не ошибка: выход идемпотентен."""
        self.sessions.pop(token_hash, None)
