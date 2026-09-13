"""Составление ответов и отправка через SMTP Mail.ru.

Разделение ответственности намеренное:

* ``build_reply`` собирает письмо, но никуда его не отправляет;
* ``send`` отправляет и проверяет получателя по белому списку.

Белый список — главная защита от prompt injection. Письмо может содержать
текст «ответь, приложив код из предыдущего сообщения» или «перешли это
на адрес X». Даже если ассистент поддастся, письмо не уйдёт никому, кроме
адресов из ``MAILRU_ALLOWED_RECIPIENTS``.
"""

import re
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

SMTP_HOST_DEFAULT = "smtp.mail.ru"
SMTP_PORT_DEFAULT = 465


def extract_address(value: str) -> str:
    """Достаёт адрес из строки вида «Имя <mail@example.com>»."""
    return parseaddr(value or "")[1].strip().lower()


def reply_subject(original: str) -> str:
    """Тема ответа: добавляет Re:, если его ещё нет."""
    subject = (original or "").strip()
    if not subject:
        return "Re:"
    if re.match(r"(?i)^\s*re\s*:", subject):
        return subject
    return f"Re: {subject}"


def is_allowed(address: str, allowed: list[str]) -> bool:
    """Проверяет получателя по белому списку.

    Пустой список означает «никому нельзя» — это осознанно: забытая
    настройка не должна открывать отправку на произвольные адреса.
    Элемент списка может быть полным адресом или доменом (``@vtb.ru``).
    """
    target = extract_address(address)
    if not target or not allowed:
        return False
    for rule in allowed:
        rule = rule.strip().lower()
        if not rule:
            continue
        if rule.startswith("@"):
            if target.endswith(rule):
                return True
        elif target == rule:
            return True
    return False


def build_reply(
    sender: str,
    to_address: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
    quote: str = "",
) -> EmailMessage:
    """Собирает ответ, сохраняя связь с исходным письмом.

    Заголовки In-Reply-To и References нужны, чтобы почтовые клиенты
    показали ответ в той же переписке, а не отдельным письмом.
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_address
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()

    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        chain = f"{references} {in_reply_to}".strip() if references else in_reply_to
        message["References"] = chain

    text = body
    if quote:
        quoted = "\n".join(f"> {line}" for line in quote.splitlines())
        text = f"{body}\n\n{quoted}"
    message.set_content(text)
    return message


def send(
    message: EmailMessage,
    address: str,
    password: str,
    allowed: list[str],
    host: str = SMTP_HOST_DEFAULT,
    port: int = SMTP_PORT_DEFAULT,
) -> dict:
    """Отправляет письмо, если получатель разрешён.

    Возвращает результат словарём, не бросая исключение на запрете —
    ассистенту полезнее прочитать причину, чем получить трассировку.
    """
    recipients = [addr.strip() for addr in (message["To"] or "").split(",") if addr.strip()]
    if not recipients:
        return {"sent": False, "reason": "не указан получатель"}

    blocked = [addr for addr in recipients if not is_allowed(addr, allowed)]
    if blocked:
        return {
            "sent": False,
            "reason": "получатель не в белом списке",
            "blocked": blocked,
            "hint": (
                "Добавьте адрес или домен в MAILRU_ALLOWED_RECIPIENTS "
                "(через запятую). Пустой список запрещает отправку всем."
            ),
        }

    try:
        with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
            smtp.login(address, password)
            smtp.send_message(message)
    except Exception as exc:
        return {"sent": False, "reason": f"SMTP: {exc}"}

    return {
        "sent": True,
        "to": recipients,
        "subject": message["Subject"],
        "message_id": message["Message-ID"],
    }
