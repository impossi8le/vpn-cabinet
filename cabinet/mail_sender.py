"""Отправка письма с кодом подтверждения: одна функция, транспорт из конфига.

Вызывающий код не знает, каким транспортом ушло письмо. Он передаёт адрес, код и
срок жизни кода — остальное решает конфиг. Транспорта два: `smtp` (SMTP с
**обязательным** STARTTLS — соединение шифруется до передачи пароля, поэтому
`AUTH` открытым текстом невозможен) и `api` (HTTP-API почтового сервиса).

Живую почту тесты не трогают: транспорт — поле `MailSender`, в тесте он
подменяется. Наружу при прогоне тестов не уходит ничего.

Секреты и коды в сообщениях об ошибках не повторяются: исключения описывают
класс сбоя, а не его содержимое — репозиторий публичный.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

import httpx

TRANSPORT_SMTP = "smtp"
TRANSPORT_API = "api"

DEFAULT_SMTP_PORT = 587
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CODE_TTL_MINUTES = 10

SUBJECT = "Код для входа в кабинет"

# Тема письма с оплаченным доступом. Отдельная константа, а не строка на месте:
# тема — та часть письма, которую видно в списке писем, и по ней человек решает,
# открывать ли его. Менять её надо осознанно, а не попутно.
PAID_SUBJECT = "Доступ готов: ваши конфигурации"
_HEADER_BREAKS = ("\r", "\n")

# Вложение: (имя файла, содержимое, mime-тип). Имя — `str` (бывает кириллица),
# содержимое — `bytes` (`.ovpn` может быть бинарным). Собирать письмо в одну
# строку нельзя: содержимое не текст, а имя кодируется отдельно от байтов.
Attachment = tuple[str, bytes, str]


class MailError(Exception):
    """Письмо не отправлено. Сообщение — для оператора: без кода и без адреса."""


class MailConfigError(MailError):
    """Конфигурация неполна или противоречива.

    Это отказ на старте, а не на конкретном письме: неверный конфиг должен
    ронять сервис сразу, а не выясняться при первой попытке входа человека.
    """


class MailAddressError(MailError):
    """Адрес получателя или отправителя не годится. Ошибка вызывающего."""


class MailTransportError(MailError):
    """Транспорт не доставил письмо: сеть, авторизация, отказ сервиса."""


class MailAttachmentError(MailError):
    """Транспорт не умеет вложение, а отправить письмо без файла нельзя.

    Молчаливое письмо без конфига человек прочитает как «покупка потерялась» —
    и это хуже честной ошибки. Поэтому отказ виден сразу, а не прячется за
    письмом, в котором нет главного.
    """


def _check_header(value: str, what: str) -> str:
    """Заголовок письма не должен нести перевод строки.

    Иначе адрес с `\\r\\n` дописал бы в письмо произвольные заголовки — вплоть до
    подмены получателя. Адрес приходит из формы, то есть от пользователя.
    """
    if any(ch in value for ch in _HEADER_BREAKS):
        raise MailAddressError(f"{what}: перевод строки в заголовке письма")
    return value


def _require_address(value: str | None, what: str) -> str:
    if value is None or not value.strip():
        raise MailAddressError(f"{what}: адрес не задан")
    address = _check_header(value.strip(), what)
    # Минимальная проверка формы. Полная валидация адреса — RFC 5322 — не
    # задача транспорта: он должен поймать явный мусор и пропустить остальное.
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        raise MailAddressError(f"{what}: адрес не похож на почтовый")
    return address


def _positive_number(raw: str, what: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise MailConfigError(f"{what}: не число") from exc
    if value <= 0:
        raise MailConfigError(f"{what}: должно быть больше нуля")
    return value


def _required(env: Mapping[str, str], key: str, transport: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise MailConfigError(f"транспорт {transport}: не задана {key}")
    return value


@dataclass
class MailSettings:
    """Конфиг отправки, собранный из окружения.

    Один набор переменных, общий для обоих транспортов: `MAIL_TRANSPORT`
    выбирает, какие из них обязательны. Транспорт, у которого не хватает
    обязательной переменной, — ошибка конфига, а не тихий откат на другой.
    """

    transport: str
    sender: str

    smtp_host: str = ""
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_user: str = ""
    smtp_password: str = ""

    api_url: str = ""
    api_key: str = ""

    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MailSettings:
        source = os.environ if env is None else env

        transport = source.get("MAIL_TRANSPORT", TRANSPORT_SMTP).strip().lower()
        if transport not in {TRANSPORT_SMTP, TRANSPORT_API}:
            raise MailConfigError(
                f"MAIL_TRANSPORT: неизвестный транспорт {transport!r}; "
                f"ожидается {TRANSPORT_SMTP!r} или {TRANSPORT_API!r}"
            )

        sender = _require_address(source.get("MAIL_FROM"), "MAIL_FROM")

        timeout_raw = source.get("MAIL_TIMEOUT", "").strip()
        timeout = (
            _positive_number(timeout_raw, "MAIL_TIMEOUT")
            if timeout_raw
            else DEFAULT_TIMEOUT_SECONDS
        )

        if transport == TRANSPORT_SMTP:
            host = _required(source, "MAIL_SMTP_HOST", transport)
            port_raw = source.get("MAIL_SMTP_PORT", "").strip()
            port = (
                int(_positive_number(port_raw, "MAIL_SMTP_PORT")) if port_raw else DEFAULT_SMTP_PORT
            )
            user = _required(source, "MAIL_SMTP_USER", transport)
            password = _required(source, "MAIL_SMTP_PASSWORD", transport)
            return cls(
                transport=transport,
                sender=sender,
                smtp_host=host,
                smtp_port=port,
                smtp_user=user,
                smtp_password=password,
                timeout=timeout,
            )

        url = _required(source, "MAIL_API_URL", transport)
        api_key = _required(source, "MAIL_API_KEY", transport)
        return cls(
            transport=transport,
            sender=sender,
            api_url=url,
            api_key=api_key,
            timeout=timeout,
        )


class Transport(Protocol):
    """Шов, ради которого модуль и разделён: функция отправки одна, а доставляет
    письмо то, что подставлено в `MailSender.transport`.

    Транспорт принимает уже разобранное письмо — адресата, тему, текст и, если
    есть, вложения — и не знает ни про коды, ни про TTL. Подставной транспорт в
    тестах (`fake_mail`) повторяет ровно этот вызов, поэтому подменяется без
    правок вызывающего. Транспорт, не умеющий вложение, обязан отказать
    (`MailAttachmentError`), а не отправить письмо без файла.
    """

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment] = (),
    ) -> None: ...


@dataclass
class SmtpTransport:
    """SMTP со STARTTLS. Без шифрования не отправляем.

    `smtplib.SMTP.starttls()` обязателен, а не опционален: без него пароль и
    код уходят открытым текстом. Проверка сертификата сервера тоже не
    отключается — иначе STARTTLS защищал бы только от пассивного слушателя.
    """

    host: str
    port: int
    user: str
    password: str
    sender: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    context: ssl.SSLContext = field(default_factory=ssl.create_default_context)

    transport_name: str = field(default=TRANSPORT_SMTP, init=False)

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment] = (),
    ) -> None:
        message = EmailMessage()
        message["From"] = formataddr((self.sender, self.sender))
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        # `add_attachment` (а не `set_content`) — имя файла и байты кодирует он:
        # кириллическое имя уходит в RFC 2231, содержимое — в base64. Собрать
        # это руками значило бы сломать разбор на стороне почтового клиента.
        for filename, content, mime_type in attachments:
            maintype, _, subtype = mime_type.partition("/")
            message.add_attachment(
                content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=filename,
            )

        address = f"{self.host}:{self.port}"
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
                client.ehlo()
                client.starttls(context=self.context)
                client.ehlo()
                client.login(self.user, self.password)
                client.send_message(message)
        except smtplib.SMTPException as exc:
            raise MailTransportError(f"smtp {address}: отправка не удалась") from exc
        except OSError as exc:
            raise MailTransportError(f"smtp {address}: соединение не установлено") from exc


@dataclass
class ApiTransport:
    """HTTP-API почтового сервиса — Brevo.

    Тело и заголовок подогнаны под Brevo, а не под «обобщённый» API: у него
    свой формат (`sender.email`, `to` списком объектов, `textContent`) и своё
    имя заголовка (`api-key`, без `Bearer`). Обобщённый формат выглядел
    красивее, но с ним письма просто не уходили — а узнать это можно было
    только живой отправкой, что и делает такие расхождения дорогими.

    Транспорт не знает ни схемы сервиса, ни его лимитов: он доставляет и
    честно сообщает, если не доставил.
    """

    url: str
    api_key: str
    sender: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    client: httpx.Client | None = None

    transport_name: str = field(default=TRANSPORT_API, init=False)

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment] = (),
    ) -> None:
        # Brevo принимает вложение отдельным полем со ссылкой на файл у себя,
        # а не байтами в запросе. Пока этого нет — отказываем явно: письмо без
        # конфига человек прочитает как потерянную покупку и не поймёт, что
        # делать. Отказ видно сразу, тихое письмо — нет.
        if attachments:
            raise MailAttachmentError(
                "api: транспорт не умеет вложение; письмо с файлом не отправлено"
            )

        payload = {
            "sender": {"email": self.sender},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": body,
        }
        # `api-key`, а не `Authorization: Bearer` — Brevo принимает ключ
        # только в своём заголовке, и с чужим отвечает 401.
        headers = {"api-key": self.api_key, "accept": "application/json"}

        try:
            client = self.client or httpx.Client(timeout=self.timeout)
            try:
                response = client.post(self.url, json=payload, headers=headers)
                response.raise_for_status()
            finally:
                if self.client is None:
                    client.close()
        except httpx.HTTPStatusError as exc:
            raise MailTransportError(f"api: сервис ответил {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise MailTransportError("api: сервис недоступен") from exc


@dataclass
class MailSender:
    """Отправитель писем. `sender` — адрес в поле «От», им подписывается письмо.

    Класс знает только про один вид письма — код подтверждения. Так и задумано:
    второй вид письма (например, «почта изменена») появится отдельным методом,
    а не ещё одной функцией отправки.
    """

    sender: str
    transport: Transport

    @classmethod
    def build(cls, settings: MailSettings) -> MailSender:
        """Собрать отправителя по конфигу. Транспорт выбирается здесь и больше
        нигде — вызывающий код о нём не знает."""
        if settings.transport == TRANSPORT_SMTP:
            transport: Transport = SmtpTransport(
                host=settings.smtp_host,
                port=settings.smtp_port,
                user=settings.smtp_user,
                password=settings.smtp_password,
                sender=settings.sender,
                timeout=settings.timeout,
            )
        elif settings.transport == TRANSPORT_API:
            transport = ApiTransport(
                url=settings.api_url,
                api_key=settings.api_key,
                sender=settings.sender,
                timeout=settings.timeout,
            )
        else:  # pragma: no cover - недостижимо: транспорт проверен в MailSettings
            raise MailConfigError(f"неизвестный транспорт {settings.transport!r}")

        return cls(sender=settings.sender, transport=transport)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MailSender:
        return cls.build(MailSettings.from_env(env))

    def send_code(
        self,
        to: str,
        code: str,
        ttl_minutes: int = DEFAULT_CODE_TTL_MINUTES,
    ) -> None:
        """Отправить код подтверждения на `to`.

        Одна функция на все случаи: и регистрация, и вход, и привязка почты —
        вызывающий код различает их по смыслу, а не по транспорту. Ошибку не
        глушим: код на почту — единственный путь входа, и «письмо не ушло, но
        сделали вид, что ушло» оставило бы человека у неработающей формы.
        """
        recipient = _require_address(to, "получатель")
        if not code.strip():
            raise MailAddressError("код: пустой код не отправляем")

        body = (
            f"Ваш код для входа: {code}\n\n"
            f"Код действует {ttl_minutes} мин. "
            f"Если вы не запрашивали код, просто проигнорируйте письмо.\n"
        )
        self.transport.send(to=recipient, subject=SUBJECT, body=body)

    def send_configs(
        self, to: str, *, attachments: Sequence[Attachment], paid: bool = True
    ) -> None:
        """Отправить конфигурации **файлами** на `to`.

        Ради этого письма кабинет и делается: у человека, который не может
        открыть Telegram (VPN ещё не работает — это и есть его случай), оплаченное
        лежит там, куда он не попадает. Поэтому конфигурации едут вложением, а не
        ссылкой: ссылка ведёт на страницу кабинета, где нужна сессия, а из
        почтового клиента человек приходит без неё и упирается во вход.

        `paid=False` — выдача за пробный период: доступа выдан, но денег не было,
        и «оплата прошла» здесь была бы неправдой. Слово «оплата» человек читает
        как факт списания и идёт его искать.

        Пустой список вложений — отказ, а не письмо: «файлы во вложении» без
        файлов читается как потерянная покупка. Ошибку транспорта не глушим и не
        подменяем успехом — вызывающий код решает, что делать, а оплата и выдача к
        этому моменту уже совершены и от письма не зависят.
        """
        recipient = _require_address(to, "получатель")
        if not attachments:
            raise MailAddressError("конфигурации: письмо без вложений не отправляем")

        opening = "Оплата прошла — доступ готов." if paid else "Доступ готов."
        body = (
            f"{opening}\n\n"
            "Во вложении файлы конфигурации (.ovpn) — по одному на каждое устройство.\n"
            "Откройте файл в приложении VPN или импортируйте его и подключитесь.\n\n"
            "Срок доступа виден в кабинете. Не пересылайте файлы: каждый из них —\n"
            "доступ, и выдан он вам.\n"
            "Если что-то не подошло — ответьте на это письмо.\n"
        )
        self.transport.send(
            to=recipient, subject=PAID_SUBJECT, body=body, attachments=tuple(attachments)
        )


def from_env(env: Mapping[str, str] | None = None) -> MailSender:
    """Отправитель из окружения. Точка входа для сервиса."""
    return MailSender.from_env(env)
