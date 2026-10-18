"""Связывание способов входа: единственное место, где аккаунт получает второй вход.

Тема самая опасная в проекте. Класс ошибки, которого здесь избегаем, известен по
CVE-2026-53516 (Better Auth) и nOAuth (Microsoft, 2023): сервис связывает аккаунты
по заявлению одной стороны, не проверив владение второй. Здесь это закрыто
правилом из §3.3 спеки:

    привязка нового способа входа никогда не проще, чем вход существующими.

Что правило означает в коде:

- **Telegram** приходит уже подтверждённым ботом (`/auth/link` + `/auth/poll`,
  §5 контракта). Кабинет `chat_id` не выдумывает и никому не верит на слово: в
  связывание попадает только то, что бот подтвердил.
- **Почта** связывается только после `AuthMail.verify` — код, отправленный на
  этот самый адрес, и никак иначе. Заявленный в браузере адрес без кода не
  значит ничего: pre-account hijacking проходит именно на этом шаге.
- И Telegram, и почта занимают **пустое** место (`can_link_telegram`,
  `can_link_mail`). Занятое значение не перезаписывается никогда — иначе знание
  чужого `chat_id` давало бы доступ к аккаунту.

Почему отвязка здесь же. Отвязка — та же смена способов входа, только в обратную
сторону, и её ограничение (нельзя остаться без способов входа, W3.5) — часть
того же инварианта. Держать привязку и отвязку в одном модуле — значит держать в
одном месте ответ на вопрос «сколько способов входа у аккаунта сейчас».

Два уровня защиты, а не один. Проверка «пусто ли место» есть и здесь
(`can_link_telegram`), и в хранилище (условие `WHERE ... IS NULL`). Это не
дублирование ради перестраховки: модель отвечает «можно ли было», хранилище —
«что получилось». Расхождение между ними (кто-то занял место между чтением и
записью) тоже приводит к отказу, а не к тихой перезаписи.

Незавершённое звено. `bot_client.py` уже умеет отвязку — методы `unlink_telegram`
и `unlink_mail` добавлены (issue #33). Но модуль по-прежнему работает против
интерфейса (`AccountStore`), а не против конкретного клиента: форма клиента и
формы протокола расходятся (см. `AccountStore`). Пока не появится адаптер,
связывание проверено против мока, у которого методы отвязки есть; свести живой
`BotClient` к этому интерфейсу — работа W6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cabinet.accounts import Account
from cabinet.auth_mail import AuthMail

__all__ = [
    "AccountStore",
    "LinkRefused",
    "LinkResult",
    "Linking",
    "UnlinkRefused",
    "UnlinkResult",
]


class LinkRefused(Exception):
    """Привязка отвергнута. Поле сообщения — для оператора и лога, не для UI.

    Тексты отказов здесь разные, потому что разные причины, и они нужны в логе.
    Экрану их показывать нельзя: разница между «почта занята другим аккаунтом» и
    «почта уже привязана к этому» позволяет перебором адресов выяснять, кто где
    зарегистрирован. Поэтому наружу из кабинета уходит одно сообщение, а
    подробность остаётся в этом исключении.
    """


class UnlinkRefused(Exception):
    """Отвязка отвергнута — после неё у аккаунта не осталось бы способа входа.

    Отделено от `LinkRefused`: это не ошибка пользователя, которую можно
    пережить, а запрет, за которым стоит недоступный навсегда аккаунт (W3.5).
    """


class AccountStore(Protocol):
    """Хранилище аккаунтов, как его видит связывание.

    Форма повторяет мок-слой и требования внутреннего роутера бота (§1
    контракта), а не живой `BotClient`. Причина в двух расхождениях, которые
    важны именно здесь:

    - **`rowcount`, а не объект.** Привязка и отвязка отвечают «сколько строк
      изменилось»: 1 — получилось, 0 — место оказалось занято. Только `rowcount`
      различает «привязал» и «привязал кто-то другой, пока я читал» — отдать тут
      `Account` значило бы потерять именно тот случай, ради которого условие
      `IS NULL` и стоит.
    - **Синхронные вызовы.** Связывание — короткая последовательность проверок,
      и держать её в `async` ради того, чтобы за моком стояла заглушка, значит
      протащить асинхронность в тесты ни за чем.

    Живой `BotClient` этой форме пока не соответствует: в нём теперь есть
    `unlink_telegram`/`unlink_mail` (#33), как и `link_*`, но все четыре
    отвечают `Account`, а не `rowcount`, и они асинхронные. Протокол фиксирует
    форму мока и требования роутера бота («сколько строк изменилось»); свести к
    ней живой клиент — работа адаптера на W6.
    """

    def find_by_id(self, account_id: int) -> Account | None:
        """Прочитать аккаунт: что уже привязано и пусто ли место."""
        ...

    def find_by_chat_id(self, chat_id: int) -> Account | None:
        """Найти аккаунт по Telegram id. Нужно, чтобы отличить «место занято
        другим аккаунтом» от «место заняло это же самое» до записи."""
        ...

    def find_by_mail(self, mail: str) -> Account | None:
        """Найти аккаунт по почте. Симметрично поиску по `chat_id`."""
        ...

    def link_chat_id(self, account_id: int, chat_id: int) -> int:
        """Привязать Telegram. `1` — привязали, `0` — место занято."""
        ...

    def link_mail(self, account_id: int, mail: str, mail_verified_at: datetime) -> int:
        """Привязать почту. `mail_verified_at` обязателен: без подтверждения
        почты не существует (инвариант 2)."""
        ...

    def unlink_chat_id(self, account_id: int) -> int:
        """Отвязать Telegram. `0` — отвязка оставила бы аккаунт без входа."""
        ...

    def unlink_mail(self, account_id: int) -> int:
        """Отвязать почту. Симметрично отвязке Telegram."""
        ...


@dataclass(frozen=True)
class LinkResult:
    """Итог привязки.

    `account` — состояние **после** записи, перечитанное из хранилища, а не
    собранное из входных данных: экран должен показать то, что в самом деле
    сохранено, а не то, что мы просили сохранить.
    """

    account: Account
    method: str


@dataclass(frozen=True)
class UnlinkResult:
    """Итог отвязки.

    `account` — состояние **после** отвязки, перечитанное из хранилища. Именно
    оно отвечает на вопрос «сколько способов входа у аккаунта осталось», ради
    которого отвязка последнего и запрещена: `has_any_login_method()` на нём
    обязан быть истинным.
    """

    account: Account
    method: str


# Значения `method` в результатах — те же строки, что у способов входа в
# `Account`. Отдельные константы, а не литералы по коду: экран и лог аудита
# ветвятся по этому значению, и опечатка в одном месте разошлась бы с другим.
METHOD_TELEGRAM = "telegram"
METHOD_MAIL = "mail"


class Linking:
    """Связывание и отвязка способов входа.

    Порядок проверок в каждом методе — это и есть защита, поэтому он
    зафиксирован ниже по шагам. Ошибка в порядке здесь означает угон
    аккаунта, а не косметический дефект.
    """

    def __init__(self, store: AccountStore, auth_mail: AuthMail) -> None:
        self._store = store
        # `AuthMail` нужен только для привязки почты, но передаётся всегда:
        # объект, умеющий привязать почту «без подтверждения», не должен
        # существовать вовсе — иначе однажды кто-то вызовет именно его.
        self._auth_mail = auth_mail

    # ---------- привязка ----------

    def link_telegram(
        self,
        account_id: int,
        *,
        chat_id: int,
        confirmed: bool = False,
    ) -> LinkResult:
        """Привязать Telegram к аккаунту.

        `chat_id` обязан быть **подтверждённым ботом** — то есть прийти из
        `/auth/poll`, а не из формы. Флаг `confirmed` делает это требование
        проверяемым в коде, а не подразумеваемым, и **по умолчанию выключен**:
        умолчание в пользу безопасности. Вызов без подтверждения отвергается,
        а вызывающий обязан подтвердить, что `chat_id` пришёл из флоу бота.
        Обратное умолчание (`= True`) означало бы, что забывчивый вызывающий
        привязывает заявленный `chat_id` молча — то есть ровно ту ошибку,
        которую запрещает §3.3.
        """
        if not confirmed:
            raise LinkRefused("chat_id не подтверждён Telegram-флоу бота")

        account = self._require(account_id)

        # Инвариант 3: привязка — только на пустое место. Проверка здесь
        # отвечает «можно ли было»; хранилище ниже ответит «что получилось»,
        # и разойдись они — отказ всё равно будет.
        if not account.can_link_telegram():
            raise LinkRefused("Telegram уже привязан к аккаунту: перезапись запрещена")

        # Занятый `chat_id` — коллизия с другим аккаунтом (§5): честный отказ и
        # предложение войти тем способом. Никогда — перезапись.
        if self._taken_by_other(chat_id=chat_id, account_id=account_id):
            raise LinkRefused("chat_id занят другим аккаунтом")

        # `rowcount != 1` — место заняли между чтением и записью; ровно тот
        # случай, ради которого условие `IS NULL` стоит в UPDATE.
        if self._link_chat_id(account_id, chat_id) != 1:
            raise LinkRefused("chat_id занят другим аккаунтом")

        return LinkResult(account=self._require(account_id), method=METHOD_TELEGRAM)

    def link_mail(self, account_id: int, *, mail: str, code: str) -> LinkResult:
        """Привязать почту после подтверждения кодом.

        Подтверждение — не украшение: `AuthMail.verify` проверяет, что код был
        отправлен **на этот самый адрес** и не истёк. Без этой проверки привязка
        стала бы приниматься по заявлению браузера, а это и есть pre-account
        hijacking (§3.3).

        Порядок важен: код проверяется **до** записи. Иначе неудачная попытка
        оставляла бы после себя изменённое состояние.
        """
        account = self._require(account_id)

        if not account.can_link_mail():
            raise LinkRefused("почта уже привязана: перезапись запрещена")

        # Владелец адреса доказывается кодом, а не строкой в форме.
        self._auth_mail.verify(mail, code)

        if self._taken_by_other(mail=mail, account_id=account_id):
            raise LinkRefused("почта занята другим аккаунтом")

        if self._link_mail(account_id, mail) != 1:
            raise LinkRefused("почта занята другим аккаунтом")

        return LinkResult(account=self._require(account_id), method=METHOD_MAIL)

    # ---------- отвязка ----------

    def unlink_telegram(self, account_id: int) -> UnlinkResult:
        """Отвязать Telegram.

        Отвязка последнего способа входа запрещена (W3.5): аккаунт без способов
        входа недоступен навсегда, и это не ошибка пользователя, которую можно
        пережить. Запрет проверяется и здесь (по текущему состоянию), и в
        хранилище (в момент записи) — по той же причине, что и при привязке.
        """
        account = self._require(account_id)
        if account.chat_id is None:
            raise LinkRefused("Telegram к аккаунту не привязан")
        if not self._would_remain_login_method(account, dropping=METHOD_TELEGRAM):
            raise UnlinkRefused("нельзя отвязать последний способ входа")

        if self._store.unlink_chat_id(account_id) != 1:
            raise UnlinkRefused("нельзя отвязать последний способ входа")

        return UnlinkResult(account=self._require(account_id), method=METHOD_TELEGRAM)

    def unlink_mail(self, account_id: int) -> UnlinkResult:
        """Отвязать почту. Симметрично отвязке Telegram."""
        account = self._require(account_id)
        if account.mail is None:
            raise LinkRefused("почта к аккаунту не привязана")
        if not self._would_remain_login_method(account, dropping=METHOD_MAIL):
            raise UnlinkRefused("нельзя отвязать последний способ входа")

        if self._store.unlink_mail(account_id) != 1:
            raise UnlinkRefused("нельзя отвязать последний способ входа")

        return UnlinkResult(account=self._require(account_id), method=METHOD_MAIL)

    # ---------- служебное ----------

    def _link_chat_id(self, account_id: int, chat_id: int) -> int:
        """Записать Telegram, переведя отказ хранилища в свой.

        Мок и живой бот отвечают на занятое место по-разному: мок бросает
        `CollisionError`, бот вернул бы `409 chat_id_taken`. Оба случая —
        один и тот же факт для вызывающего, поэтому здесь они сводятся к
        `LinkRefused`: наружу из связывания не должно течь исключение
        хранилища, иначе вызывающий код начнёт зависеть от того, что под ним.
        """
        try:
            return self._store.link_chat_id(account_id, chat_id)
        except Exception as exc:
            if _is_collision(exc):
                raise LinkRefused("chat_id занят другим аккаунтом") from exc
            raise

    def _link_mail(self, account_id: int, mail: str) -> int:
        """Записать почту. Симметрично `_link_chat_id`."""
        try:
            return self._store.link_mail(account_id, mail, self._auth_mail.now())
        except Exception as exc:
            if _is_collision(exc):
                raise LinkRefused("почта занята другим аккаунтом") from exc
            raise

    def _taken_by_other(
        self,
        *,
        chat_id: int | None = None,
        mail: str | None = None,
        account_id: int,
    ) -> bool:
        """Занято ли значение **другим** аккаунтом.

        Отделено от «занято ли вообще»: попытка привязать своё же значение
        повторно — не коллизия, а состояние «уже привязано», и обрабатывается
        оно раньше, проверкой `can_link_*`.
        """
        if chat_id is not None:
            holder = self._store.find_by_chat_id(chat_id)
        else:
            holder = self._store.find_by_mail(mail or "")
        return holder is not None and holder.id != account_id

    def _require(self, account_id: int) -> Account:
        """Прочитать аккаунт или отказать.

        «Нет аккаунта» — не исключение хранилища: связывание вызывается из
        кабинета, где аккаунт уже есть (идёт сессия), поэтому его отсутствие —
        осмысленный отказ, а не сбой.
        """
        account = self._store.find_by_id(account_id)
        if account is None:
            raise LinkRefused("аккаунт не найден")
        return account

    @staticmethod
    def _would_remain_login_method(account: Account, *, dropping: str) -> bool:
        """Останется ли способ входа, если убрать `dropping`.

        Считается по текущему состоянию аккаунта, а не по числу «было минус
        один»: так проверка не зависит от того, что записано в хранилище, и
        одинаково работает для обоих способов.
        """
        if dropping == METHOD_TELEGRAM:
            return account.mail is not None
        return account.chat_id is not None


# Коды коллизии из контракта бота (§1). Те же строки, что в мок-слое: если
# разойдутся, разойдётся и различение «занято» от «сломалось».
_COLLISION_CODES = ("chat_id_taken", "mail_taken", "collision")


def _is_collision(exc: BaseException) -> bool:
    """Коллизия ли это, а не другая ошибка хранилища.

    Различать обязательно: «занято чужим аккаунтом» — состояние, о котором
    человеку сообщают и предлагают войти другим способом, а «хранилище
    сломалось» — сбой, который нельзя показывать как коллизию, иначе настоящая
    причина не всплывёт никогда.

    Признак берётся не по конкретному классу, а по двум контрактным меткам:
    `code` из формата ошибок бота и имя класса мока (`CollisionError`). Импорт
    класса мока сюда означал бы зависимость боевого модуля от тестового — а
    привязка к одному имени класса сломалась бы при смене мока на живой клиент.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _COLLISION_CODES:
        return True
    return type(exc).__name__.endswith("CollisionError")
