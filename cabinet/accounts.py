"""Модель аккаунта: один человек — одна строка.

Аккаунт отделён от способов входа намеренно. Почта и Telegram — атрибуты
аккаунта, а не его замена: у человека, который завёл аккаунт по почте и ещё не
привязал Telegram, `chat_id` пуст, и это нормальное состояние, а не недоделка.

Инварианты (см. §4 спеки) проверяются здесь, а не только схемой: схема ловит
UNIQUE, но не отвечает на вопрос «а можно ли это было делать».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


class LinkError(Exception):
    """Связывание отвергнуто. Сообщение — для оператора, не для UI."""


@dataclass
class Account:
    """Аккаунт. `id` — стабильный идентификатор, не зависящий ни от одного
    из способов входа: способ можно сменить, id остаётся."""

    id: int | None = None
    chat_id: int | None = None
    mail: str | None = None
    mail_verified_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_login_at: datetime | None = None
    blocked: bool = False

    # Регистрация ли это, а не вход. Нужно инварианту 4: связывать `chat_id` с
    # аккаунтом можно только пока он новый. Аккаунт, заведённый давно и вошедший
    # повторно, `chat_id` менять не даёт — иначе повторная привязка стала бы
    # способом угона (см. §3.3 спеки).
    _created_in_this_request: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self._check_invariants()

    def _check_invariants(self) -> None:
        # Инвариант 1: chat_id без подтверждения в Telegram не появляется.
        # Проверить «подтверждён ли chat_id» эта модель не может — подтверждение
        # живёт в потоке входа. Поэтому здесь фиксируется только то, что модель
        # обязана знать: почта без отметки о подтверждении недопустима.
        #
        # Инвариант 2: mail != NULL требует mail_verified_at.
        if self.mail is not None and self.mail_verified_at is None:
            raise LinkError("почта не может быть привязана без подтверждения")

        if self.mail is not None and not self.mail.strip():
            raise LinkError("почта не может быть пустой строкой")

    def has_any_login_method(self) -> bool:
        """Есть ли хоть один способ входа. Аккаунт без способов недоступен
        навсегда, поэтому отвязку последнего надо запрещать (инвариант W3.5)."""
        return self.chat_id is not None or self.mail is not None

    def can_link_telegram(self) -> bool:
        """Можно ли привязать Telegram.

        Инвариант 3: привязка — только на пустое место. Занятый `chat_id`
        никогда не перезаписывается: иначе знание чужого `chat_id` давало бы
        доступ к аккаунту.
        """
        return self.chat_id is None

    def can_link_mail(self) -> bool:
        """Можно ли привязать почту. Симметрично привязке Telegram."""
        return self.mail is None
