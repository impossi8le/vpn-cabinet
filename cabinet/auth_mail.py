"""Коды подтверждения на почту: выдача, проверка, TTL, лимиты (W2.2).

Вход по почте — основной способ входа, а не резервный: у человека за границей
нет ни Telegram, ни VPN, и почта остаётся единственной дверью. Поэтому модуль
держит не «удобство», а три обязательных свойства кода:

- **Код одноразовый.** Проверка удаляет запись: повтор того же кода не проходит.
- **Код живёт ограниченное время.** Истёкший не пускает ни при совпадении.
- **Код нельзя ни подобрать, ни задосить.** Ограничено число попыток на код и
  число отправок на адрес.

Хранилище — в памяти процесса, и это осознанный выбор, а не заготовка.
Кабинет по границам сервиса (§3.4 спеки, CLAUDE.md) **не трогает MySQL**: SQL
со схемой авторизации в публичный репозиторий не попадает. Коды — короткоживущее
состояние на минуты, и переживать перезапуск процесса ему не нужно; вынести его
в бота — работа волны W6, вместе с живым бэкендом. Интерфейс модуля от этого не
изменится: тесты работают против `FakeMailSender`, живую почту никто не трогает.

Почему хеш, а не сам код. В памяти лежит `sha256` кода. Репозиторий публичный,
и код в дампе памяти или логе — это уже предъявленный код. Открытого текста
здесь нет ни в хранилище, ни в сообщениях об ошибках.

Почему `hmac.compare_digest`, а не `==`. Сравнение обычной строкой выходит на
первом несовпавшем байте, и по времени ответа код подбирается посимвольно.
`compare_digest` сравнивает за постоянное время независимо от совпадения.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cabinet.mail_sender import DEFAULT_CODE_TTL_MINUTES, MailError, MailSender

# --- Значения по умолчанию ------------------------------------------------
#
# Числа подобраны под одну задачу: пустить человека, но не дать коду стать
# оружием. Каждое объяснено там, где применяется.

# Длина кода. Шесть цифр — 10^6 вариантов против лимита попыток в 5: шанс
# подобрать ~5e-6 на одну выдачу. Четыре цифры при том же лимите дали бы 5e-4,
# что для доступа к подписке много; восемь — уже неудобно набирать с телефона.
CODE_LENGTH = 6

# Сколько раз код можно ввести неправильно, прежде чем он погаснет. Пять —
# это терпимость к опечатке и раскладке, но не площадка для перебора.
MAX_ATTEMPTS = 5

# Пауза между отправками на один адрес. Минута отсекает «нажал ещё раз, потому
# что не пришло» и не мешает человеку, который просто ждёт письмо.
RESEND_INTERVAL_SECONDS = 60

# Сколько писем одному адресу за окно. Пять в час — потолок против рассылки
# чужому ящику и против исчерпания квоты почтового сервиса одним адресом.
MAX_SENDS_PER_WINDOW = 5
SEND_WINDOW_SECONDS = 3600


class AuthMailError(Exception):
    """Код не выдан или не принят. Сообщение — для оператора, не для UI."""


class RateLimited(AuthMailError):
    """Слишком частая отправка на этот адрес.

    Отделено от прочих отказов: вызывающий код показывает «попробуйте через
    минуту», а не «ошибка». `retry_after_seconds` — сколько ждать.
    """

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class NoPendingCode(AuthMailError):
    """Кода для этого адреса нет: не запрашивали, либо он уже использован.

    Отдельный тип, потому что повтор уже сработавшего кода — не «неверный
    код», а именно отсутствие операции: одноразовость здесь и проверяется.
    """


class CodeExpired(AuthMailError):
    """Срок кода истёк. Совпадение цифр значения не имеет."""


class CodeExhausted(AuthMailError):
    """Попытки исчерпаны — код погашен, даже если следующая цифра верна."""


class CodeRejected(AuthMailError):
    """Код не совпал, но попытки ещё остались.

    `attempts_left` — для подсказки «осталось N попыток», а не для UI-текста
    отказа: сам факт неверного кода наружу показывается один и тот же.
    """

    def __init__(self, message: str, *, attempts_left: int) -> None:
        super().__init__(message)
        self.attempts_left = attempts_left


def _hash(code: str) -> str:
    """Хеш кода. Хранится вместо кода, сравнивается через `compare_digest`."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _normalize(mail: str) -> str:
    """Привести адрес к сопоставимому виду.

    `User@Example.com` и `user@example.com` — один ящик. Без нормализации
    лимит обходился бы регистром, а код, запрошенный с «большой» буквой, не
    принимался бы при вводе строчными.
    """
    return mail.strip().lower()


def generate_code(length: int = CODE_LENGTH) -> str:
    """Случайный код из цифр, с ведущими нулями.

    Источник — `secrets`, не `random`: `random` предсказуем по начальному
    состоянию, а здесь предсказуемость кода означает чужой вход. Код собирается
    по цифре, а не из числа: число потеряло бы ведущий ноль и код стал бы
    короче ожидаемого.
    """
    return "".join(secrets.choice("0123456789") for _ in range(length))


@dataclass
class _Pending:
    """Выданный код: хеш, срок и счётчик попыток. Живёт только в памяти."""

    code_hash: str
    expires_at: datetime
    attempts: int = 0

    def attempts_left(self) -> int:
        return MAX_ATTEMPTS - self.attempts


@dataclass
class AuthMail:
    """Выдача и проверка кодов входа.

    Часы подаются снаружи: тесты на TTL и истечение обязаны быть
    детерминированными, а не зависеть от того, как быстро отработал прогон.
    В бою часы — `datetime.now(UTC)`.

    Один экземпляр — один процесс кабинета. Состояние — два словаря в памяти:
    активные коды по адресу и журнал отправок для лимита. Оба сбрасываются при
    перезапуске, и это приемлемо: код живёт минуты.
    """

    mail_sender: MailSender
    ttl_minutes: int = DEFAULT_CODE_TTL_MINUTES
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    # Активный код на адрес. Один: новый запрос заменяет старый, поэтому
    # «предыдущее письмо» перестаёт действовать, как только запрошено новое.
    _pending: dict[str, _Pending] = field(default_factory=dict, init=False, repr=False)
    # Журнал отправок: время каждой, для лимитов. Чистится по окну при выдаче.
    _sends: dict[str, list[datetime]] = field(default_factory=dict, init=False, repr=False)

    # --- выдача ----------------------------------------------------------

    def request_code(self, mail: str) -> None:
        """Выдать код на адрес и отправить письмо.

        Порядок важен: место в журнале отправок занимается **до** доставки.
        Иначе неудачная отправка либо съедала бы квоту человека, либо,
        наоборот, разрешала бы молотить неработающий транспорт без предела —
        смотря куда поставить запись. Здесь квота тратится на попытку, а при
        сбое доставки и запись, и код откатываются: письмо не ушло — считаем,
        что и запроса не было.
        """
        address = _normalize(mail)
        if not address:
            raise AuthMailError("адрес не задан")

        moment = self.now()
        self._check_rate(address, moment)

        code = generate_code()
        entry = _Pending(
            code_hash=_hash(code),
            expires_at=moment + timedelta(minutes=self.ttl_minutes),
        )
        self._pending[address] = entry
        self._sends.setdefault(address, []).append(moment)

        try:
            self.mail_sender.send_code(address, code, ttl_minutes=self.ttl_minutes)
        except MailError:
            # Транспорт не доставил. Откатываем и код, и трату квоты: держать
            # неполученный код активным — значит пустить человека в форму,
            # письма по которому у него нет.
            self._pending.pop(address, None)
            self._sends[address].pop()
            raise

    def _check_rate(self, address: str, moment: datetime) -> None:
        """Отказ, если писем этому адресу было слишком много или слишком часто."""
        window_start = moment - timedelta(seconds=SEND_WINDOW_SECONDS)
        recent = [sent for sent in self._sends.get(address, []) if sent > window_start]
        self._sends[address] = recent

        if len(recent) >= MAX_SENDS_PER_WINDOW:
            raise RateLimited(
                f"исчерпан лимит отправок на адрес: {MAX_SENDS_PER_WINDOW} за час",
                retry_after_seconds=SEND_WINDOW_SECONDS,
            )

        if recent:
            since_last = (moment - recent[-1]).total_seconds()
            if since_last < RESEND_INTERVAL_SECONDS:
                raise RateLimited(
                    "слишком частая отправка на адрес",
                    # `ceil`, а не `int` (issue #58): остаток меньше секунды
                    # `int` превратил бы в ноль, и экран сказал бы «Повторите
                    # через 0 с», хотя лимит ещё действует. Округление вверх
                    # честнее — обещает не меньше, чем придётся ждать.
                    retry_after_seconds=math.ceil(RESEND_INTERVAL_SECONDS - since_last),
                )

    # --- проверка --------------------------------------------------------

    def verify(self, mail: str, code: str) -> None:
        """Проверить код. Успех возвращает `None`, отказ — исключение.

        Исключение, а не `bool`: отказы различаются поведением вызывающего
        кода (истёк — предложить новый код; исчерпан — тоже; не совпал — дать
        ввести ещё), и свести их к `False` значило бы потерять этот выбор на
        стороне, которая знает контекст.
        """
        address = _normalize(mail)
        entry = self._pending.get(address)

        if entry is None:
            # Сравниваем с заведомо несовпадающим хешем: без этого «нет кода»
            # отвечало бы заметно быстрее «неверный код», и по времени ответа
            # выяснялось бы, запрашивали ли код на этот адрес.
            hmac.compare_digest(_hash(code), _hash(""))
            raise NoPendingCode("код не запрашивался или уже использован")

        if self.now() >= entry.expires_at:
            # Истёкший код гасим сразу: держать его дальше смысла нет, а
            # оставленная запись — это ещё живые попытки.
            del self._pending[address]
            raise CodeExpired("срок кода истёк")

        if not hmac.compare_digest(entry.code_hash, _hash(code)):
            entry.attempts += 1
            if entry.attempts >= MAX_ATTEMPTS:
                # N-я неудачная попытка гасит код целиком: после этого верный
                # код уже не примут — иначе лимит попыток ничего не ограничивал.
                del self._pending[address]
                raise CodeExhausted("попытки исчерпаны, код погашен")
            raise CodeRejected("код не совпал", attempts_left=entry.attempts_left())

        # Успех: код одноразовый. Удаляем до возврата, чтобы повторный вызов
        # с тем же кодом не нашёл записи и не прошёл.
        del self._pending[address]

    def has_pending(self, mail: str) -> bool:
        """Есть ли действующий код. Нужно форме, чтобы решить «запросить» или
        «ввести»; сам код и его хеш наружу не отдаются."""
        address = _normalize(mail)
        entry = self._pending.get(address)
        return entry is not None and self.now() < entry.expires_at
