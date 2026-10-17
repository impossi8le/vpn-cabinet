"""Связывание аккаунтов: сценарии атаки, которые обязаны не проходить.

Это тест задания W3.4. Класс ошибки, который здесь воспроизводится, — авто-
связывание по заявлению одной стороны без проверки другой. Он давал реальные
уязвимости: CVE-2026-53516 (Better Auth), nOAuth (Microsoft, 2023).

Если этот файл краснеет — ломать ничего не надо, надо чинить связывание.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cabinet.accounts import Account, LinkError

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def test_pre_account_hijack_is_impossible_without_verification():
    """Атака: злоумышленник заранее занимает чужой email.

    Шаг 1 — злоумышленник регистрирует `victim@example.com`, но не подтверждает
    его. Шаг 2 — жертва входит через Telegram. Шаг 3 — если система связывает
    аккаунты по заявленному адресу, личность жертвы прилипает к записи
    злоумышленника, и он сохраняет доступ.

    Здесь воспроизведён Шаг 1: незаподтверждённая почта не даёт создать
    валидный аккаунт, поэтому прилипать не к чему.
    """
    with pytest.raises(LinkError, match="без подтверждения"):
        Account(mail="victim@example.com", mail_verified_at=None)


def test_telegram_cannot_overwrite_linked_chat_id():
    """Второй вектор: привязка своего Telegram к чужому аккаунту.

    Аккаунт уже несёт `chat_id`. Попытка привязать другой отвергается на уровне
    модели (`can_link_telegram`), а в базе — условием `WHERE chat_id IS NULL`,
    из-за которого UPDATE затронет 0 строк вместо тихой перезаписи.
    """
    victim = Account(chat_id=111_222_333, mail="victim@example.com", mail_verified_at=NOW)
    assert victim.can_link_telegram() is False


def test_mail_cannot_overwrite_linked_mail():
    """Симметрично: занятая почта не перезаписывается."""
    acc = Account(chat_id=111_222_333, mail="first@example.com", mail_verified_at=NOW)
    assert acc.can_link_mail() is False


def test_link_target_must_be_empty_not_just_different():
    """Формулировка инварианта: привязка возможна на ПУСТОЕ место.

    Разница существенная. «Привязать другой» и «привязать, если пусто» —
    разные правила, и второе закрывает перезапись как класс.
    """
    empty = Account(chat_id=111)
    assert empty.can_link_mail() is True

    filled = Account(chat_id=111, mail="a@b.com", mail_verified_at=NOW)
    assert filled.can_link_mail() is False
