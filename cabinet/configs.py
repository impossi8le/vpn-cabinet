"""Скачивание `.ovpn`: право на конфиг решает подписка, а не запрос (W4.2, W4.4).

Кабинет не хранит конфиги и не знает их устройства: содержимое живёт в боте, а
кабинет только передаёт его браузеру. Отсюда две обязанности этого модуля.

Первая — **не отдавать конфиг по неработающей подписке**. Ответ на вопрос
«работает ли подписка сейчас» уже есть: `SubscriptionView.is_usable` в
`cabinet.subscriptions`. Здесь он не пересчитывается заново — статус и дату
сводит `subscriptions`, и второй копии этого правила быть не должно, иначе
экран и загрузка разойдутся в том, что считать активным. Модуль применяет
готовый ответ и отказывает **до** того, как коснётся бота.

Отказ бота — не единственная защита, а вторая линия. Бот тоже не отдаёт конфиг
по истёкшей подписке (§3 контракта), и его отказ приходит сюда кодом
`subscription_expired`. Оба пути приводят к одному исключению
`SubscriptionInactive`: у вызывающего кода один случай, а не два.

Вторая обязанность — **не дать превратить скачивание в перебор**. Частота
запросов к одному аккаунту ограничена (W4.4), числа и их обоснование — ниже.

«Нет конфига» и «чужой конфиг» — один и тот же отказ (§3 контракта). Он
держится сам: бот отвечает `config_not_found` и на то, и на другое, а модуль
переводит код в одно исключение `ConfigNotFound` с одним текстом. Различить их
снаружи нечем — значит, перебором `config_id` не выяснить, у кого что есть.

Содержимое наружу не выходит. `.ovpn` и ключи в этот репозиторий не попадают
(CLAUDE.md, «Публичность»): модуль работает с байтами, нигде их не логирует и не
печатает, а имя файла собирается по белому списку символов — иначе `config_id`
с переводом строки дописал бы в ответ произвольные заголовки.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from cabinet.bot_client import BotError, BotUnreachable
from cabinet.subscriptions import SubscriptionView

__all__ = [
    "DOWNLOAD_WINDOW_SECONDS",
    "MAX_DOWNLOADS_PER_WINDOW",
    "OVPN_CONTENT_TYPE",
    "ConfigDownload",
    "ConfigError",
    "ConfigNotFound",
    "ConfigRetired",
    "ConfigRevoked",
    "ConfigService",
    "ConfigSource",
    "DownloadLimiter",
    "DownloadRateLimited",
    "SubscriptionInactive",
    "config_filename",
]

# Тип содержимого из публичного контракта §4. Один, а не «что-нибудь
# octet-stream»: браузер по нему решает, открыть профиль или сохранить файл.
OVPN_CONTENT_TYPE = "application/x-openvpn-profile"

# --- Лимит скачиваний (W4.4) ---------------------------------------------
#
# Числа подобраны под одну задачу: пустить человека с несколькими устройствами,
# но не дать гонять бота в цикле. Скачивание — редкое действие: профиль берут
# один раз на устройство, а не листают как список. Десять за пять минут — это
# перенос на второе-третье устройство и повтор после неудачной попытки, но не
# площадка для скрипта, который выкачивает конфиг запрос за запросом.
MAX_DOWNLOADS_PER_WINDOW = 10
DOWNLOAD_WINDOW_SECONDS = 300

_HASH_PREFIX = "sha256:"

# Имя файла для `Content-Disposition`. Белый список, а не чёрный: всё, чего в
# нём нет, отбрасывается, поэтому перевод строки или кавычка из `config_id` в
# заголовок не попадут.
_FILENAME_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_FILENAME_FALLBACK = "config"


class ConfigError(Exception):
    """Конфиг не выдан. Сообщение — для оператора, не для UI."""


class SubscriptionInactive(ConfigError):
    """Подписка не даёт доступа: истекла, заблокирована или ещё не началась.

    Один тип на все пути отказа — и на локальную проверку `is_usable`, и на
    ответ бота `subscription_expired`. Вызывающий код показывает одно состояние
    «доступ закрыт», а не выбирает между двумя одинаковыми.
    """


class ConfigNotFound(ConfigError):
    """Нет такого конфига **или** он чужой.

    Намеренно один случай: различив их, кабинет отвечал бы разными текстами на
    перебор `config_id`, и по этим текстам выяснялось бы, у кого что есть.
    """


class ConfigRevoked(ConfigError):
    """Конфиг отозван. Отдельно от «не найден»: это состояние доступа, а не
    опечатка в идентификаторе, и экран показывает его иначе."""


class ConfigRetired(ConfigError):
    """Конфиг снят на сервере: устарел, но у аккаунта он был. Экран предлагает
    взять другой, а не «обновить список»."""


class DownloadRateLimited(ConfigError):
    """Слишком много скачиваний с одного аккаунта за окно.

    Отделено от прочих отказов: вызывающий код показывает «попробуйте позже», а
    не «ошибка». `retry_after_seconds` — сколько ждать.
    """

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ConfigSource(Protocol):
    """Откуда берётся содержимое `.ovpn`.

    Интерфейс, а не конкретный `BotClient`: модуль описывает поведение
    скачивания, а не то, что за ним стоит HTTP к боту. Совпадает с
    `BotClient.get_ovpn` — перевод в боевой источник не нужен, а тест
    подставляет свою реализацию, не открывая сеть (как `MailSender` в
    `mail_sender`).
    """

    async def get_ovpn(self, account_id: int, config_id: str) -> bytes:
        """Содержимое профиля байтами. Отказы — `BotError` с кодом контракта."""
        ...


@dataclass(frozen=True)
class ConfigDownload:
    """Готовый к передаче профиль.

    Ничего лишнего: только то, что браузеру нужно, чтобы сохранить файл, и хеш
    для проверки после записи. Сам профиль лежит в `content` и наружу из модуля
    не печатается.
    """

    content: bytes
    content_type: str
    filename: str
    hash: str

    @property
    def size(self) -> int:
        """Размер в байтах. Может понадобиться заголовку `Content-Length`."""
        return len(self.content)


def _sha256(content: bytes) -> str:
    """Хеш содержимого в форме контракта: `sha256:<hex>`.

    Считается здесь, а не берётся из заголовка бота: `get_ovpn` отдаёт только
    байты, и хеш над теми же байтами — это ровно та проверка, которую клиент
    сделает после записи (публичный контракт §4).
    """
    return _HASH_PREFIX + hashlib.sha256(content).hexdigest()


def config_filename(config_id: str) -> str:
    """Имя файла для `Content-Disposition`, безопасное для заголовка.

    Всё, кроме белого списка, отбрасывается: `id` приходит из адреса запроса,
    то есть от пользователя, и перевод строки в нём дописал бы в ответ чужие
    заголовки. Пустой результат заменяется на запасное имя — файл без имени
    хуже, чем файл с невыразительным.
    """
    cleaned = "".join(ch for ch in config_id if ch in _FILENAME_SAFE)
    return f"{cleaned or _FILENAME_FALLBACK}.ovpn"


@dataclass
class DownloadLimiter:
    """Счётчик скачиваний на аккаунт за скользящее окно.

    Часы подаются снаружи: тест на лимит обязан быть детерминированным, а не
    зависеть от того, как быстро отработал прогон; в бою — `datetime.now(UTC)`.

    Состояние — один словарь в памяти процесса, и это осознанный выбор, а не
    заготовка: как и коды в `auth_mail`, это короткоживущий счётчик на минуты,
    которому не нужно переживать перезапуск. Учёт ведётся по `account_id`, а не
    по адресу: за одним аккаунтом может стоять несколько устройств, и лимит
    должен считать их действия вместе.
    """

    max_downloads: int = MAX_DOWNLOADS_PER_WINDOW
    window_seconds: int = DOWNLOAD_WINDOW_SECONDS
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    # Журнал скачиваний по аккаунту. Чистится по окну при каждой проверке.
    _history: dict[int, list[datetime]] = field(default_factory=dict, init=False, repr=False)

    def _recent(self, account_id: int, moment: datetime) -> list[datetime]:
        """Скачивания этого аккаунта внутри окна. Заодно чистит старое."""
        window_start = moment - timedelta(seconds=self.window_seconds)
        recent = [seen for seen in self._history.get(account_id, []) if seen > window_start]
        self._history[account_id] = recent
        return recent

    def check(self, account_id: int) -> None:
        """Отказ, если лимит уже исчерпан. Ничего не записывает."""
        recent = self._recent(account_id, self.now())
        if len(recent) >= self.max_downloads:
            raise DownloadRateLimited(
                f"исчерпан лимит скачиваний: {self.max_downloads} за окно",
                retry_after_seconds=self.window_seconds,
            )

    def record(self, account_id: int) -> None:
        """Занять место в журнале. Вызывается перед обращением к боту."""
        self._history.setdefault(account_id, []).append(self.now())

    def rollback(self, account_id: int) -> None:
        """Вернуть место в журнале: попытка не состоялась.

        Нужно для случая «бот недоступен»: сетевой сбой — не действие человека,
        и съедать на него квоту значило бы наказывать за чужую аварию. Тот же
        приём, что в `auth_mail.request_code` при сбое доставки.
        """
        history = self._history.get(account_id)
        if history:
            history.pop()


# Соответствие кодов бота нашим отказам. Таблица, а не цепочка `if`: список
# кодов — часть контракта, и его видно целиком. Незнакомый код сюда не
# попадает и обрабатывается отдельно.
_BOT_ERROR_CODES: dict[str, type[ConfigError]] = {
    "subscription_expired": SubscriptionInactive,
    "config_not_found": ConfigNotFound,
    "config_revoked": ConfigRevoked,
    "config_retired": ConfigRetired,
}


@dataclass
class ConfigService:
    """Выдача `.ovpn` с проверкой права и лимитом.

    Порядок в `download` — это и есть правило «не отдавать по неактивной
    подписке»:

    1. подписка не работает → отказ, к боту не ходим;
    2. лимит исчерпан → отказ;
    3. только теперь запрос к боту.

    Проверка подписки стоит первой намеренно: она дешёвая, решает главное и не
    тратит ни квоту человека, ни запрос к боту на заведомо закрытый случай.
    """

    source: ConfigSource
    limiter: DownloadLimiter = field(default_factory=DownloadLimiter)

    async def download(
        self,
        *,
        account_id: int,
        config_id: str,
        subscription: SubscriptionView,
    ) -> ConfigDownload:
        """Отдать профиль, если подписка работает и лимит не исчерпан.

        `subscription` — уже сведённое состояние (`subscriptions.subscription_view`),
        а не сырой ответ бота: право решается по `is_usable`, и пересчитывать
        статус здесь нечего.
        """
        if not subscription.is_usable:
            raise SubscriptionInactive("подписка не даёт доступа к конфигу")

        self.limiter.check(account_id)
        self.limiter.record(account_id)
        try:
            content = await self.source.get_ovpn(account_id, config_id)
        except BotUnreachable:
            # Бот не ответил — это не отказ по логике и не попытка человека.
            self.limiter.rollback(account_id)
            raise
        except BotError as exc:
            raise _config_error_from_bot(exc) from exc

        return ConfigDownload(
            content=content,
            content_type=OVPN_CONTENT_TYPE,
            filename=config_filename(config_id),
            hash=_sha256(content),
        )


def _config_error_from_bot(exc: BotError) -> ConfigError:
    """Перевести отказ бота в наш тип по коду.

    Код — часть контракта (§3), ветвимся по нему, а не по тексту. Незнакомый код
    не выдаём за «не найдено»: он означает расхождение версий, и вызывающий код
    должен увидеть отказ, а не пустой конфиг.
    """
    kind = _BOT_ERROR_CODES.get(exc.code)
    if kind is None:
        return ConfigError(f"бот отказал в конфиге: {exc.code}")
    return kind(exc.message or exc.code)
