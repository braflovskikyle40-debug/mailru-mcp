"""Работа с почтой Mail.ru по IMAP.

Здесь собраны обходы особенностей Mail.ru, на которые легко потратить
несколько часов:

* имена папок передаются в модифицированном UTF-7 (RFC 3501);
* массовый FETCH заголовков рвёт соединение;
* длинные сессии обрываются, поэтому операции идут партиями с
  переподключением.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

# Партии подобраны опытным путём: больше — сервер рвёт соединение.
FETCH_BATCH = 200
COPY_BATCH = 200


def encode_folder(name: str) -> str:
    """Имя папки в модифицированный UTF-7 (RFC 3501).

    Mail.ru принимает кириллические имена только в этой кодировке:
    «Архив» на проводе выглядит как ``&BBAEQARFBDgEMg-``. Отличие от
    обычного utf-7 — вместо ``+`` используется ``&``, а сам ``&``
    экранируется как ``&-``.
    """
    if name.isascii():
        return name
    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        chunk = "".join(buffer).encode("utf-7").decode("ascii")
        out.append(chunk.replace("+", "&").replace("/", ","))
        buffer.clear()

    for char in name:
        if char == "&":
            flush()
            out.append("&-")
        elif char.isascii():
            flush()
            out.append(char)
        else:
            buffer.append(char)
    flush()
    return "".join(out)


def decode_folder(raw: str) -> str:
    """Обратное преобразование: имя папки из UTF-7 в читаемый вид."""
    if "&" not in raw:
        return raw
    try:
        return raw.replace("&", "+").replace(",", "/").encode("ascii").decode("utf-7")
    except Exception:
        return raw


def decode_mime(raw: str | None) -> str:
    """MIME-заголовок (=?utf-8?B?...?=) в читаемый текст."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def extract_body(message: email.message.Message, limit: int) -> str:
    """Текст письма: предпочитает text/plain, иначе чистит HTML от тегов."""
    plain, html = "", ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
        except Exception:
            continue
        if content_type == "text/plain" and not plain:
            plain = text
        elif content_type == "text/html" and not html:
            html = text

    text = plain or html
    if not plain and html:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


@dataclass
class Letter:
    """Одно письмо в удобном для инструментов виде."""

    uid: str
    sender: str
    sender_name: str
    subject: str
    date: str
    body: str = ""
    attachments: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "sender": self.sender,
            "sender_name": self.sender_name,
            "subject": self.subject,
            "date": self.date,
            "body": self.body,
            "attachments": self.attachments,
        }


class MailruClient:
    """Тонкая обёртка над imaplib с учётом особенностей Mail.ru."""

    def __init__(self, host: str, port: int, address: str, password: str) -> None:
        self._host = host
        self._port = port
        self._address = address
        self._password = password
        self._imap: imaplib.IMAP4_SSL | None = None

    # --- соединение ---

    def connect(self, mailbox: str = "INBOX") -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(self._host, self._port)
        imap.login(self._address, self._password)
        imap.select(f'"{encode_folder(mailbox)}"')
        self._imap = imap
        return imap

    def close(self) -> None:
        if self._imap is None:
            return
        for step in (self._imap.close, self._imap.logout):
            try:
                step()
            except Exception:
                pass
        self._imap = None

    def __enter__(self) -> "MailruClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- папки ---

    def list_folders(self) -> list[str]:
        imap = self.connect()
        status, rows = imap.list()
        if status != "OK":
            return []
        names: list[str] = []
        for row in rows or []:
            raw = row.decode(errors="replace")
            name = raw.rsplit(' "', 1)[-1].strip('"')
            names.append(decode_folder(name))
        return names

    def create_folder(self, name: str) -> bool:
        """Создаёт папку. Возвращает True, если она есть после вызова."""
        imap = self.connect()
        imap.create(f'"{encode_folder(name)}"')
        status, rows = imap.list()
        if status != "OK":
            return False
        target = f'"{encode_folder(name)}"'
        return any(target in row.decode(errors="replace") for row in (rows or []))

    def folder_exists(self, name: str) -> bool:
        return name in self.list_folders()

    # --- чтение ---

    def search(
        self,
        mailbox: str = "INBOX",
        since_hours: int | None = None,
        before_days: int | None = None,
        from_address: str | None = None,
        unseen_only: bool = False,
    ) -> list[bytes]:
        """UID писем по критериям. Даты IMAP работают с точностью до суток."""
        imap = self.connect(mailbox)
        criteria: list[str] = []

        if since_hours is not None:
            since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            criteria.append(f'SINCE {since.strftime("%d-%b-%Y")}')
        if before_days is not None:
            before = datetime.now(timezone.utc) - timedelta(days=before_days)
            criteria.append(f'BEFORE {before.strftime("%d-%b-%Y")}')
        if from_address:
            criteria.append(f'FROM "{from_address}"')
        if unseen_only:
            criteria.append("UNSEEN")

        query = f'({" ".join(criteria)})' if criteria else "ALL"
        status, data = imap.uid("SEARCH", None, query)
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def fetch(
        self,
        uids: list[bytes],
        mailbox: str = "INBOX",
        body_chars: int = 2000,
        since_hours: int | None = None,
    ) -> list[Letter]:
        """Читает письма по UID. Точная отсечка по времени — по заголовку Date."""
        if not uids:
            return []
        imap = self._imap or self.connect(mailbox)
        cutoff = None
        if since_hours is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

        letters: list[Letter] = []
        for uid in uids:
            status, raw = imap.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not raw or not isinstance(raw[0], tuple):
                continue
            message = email.message_from_bytes(raw[0][1])

            when = ""
            try:
                sent = email.utils.parsedate_to_datetime(message.get("Date"))
                if sent.tzinfo is None:
                    sent = sent.replace(tzinfo=timezone.utc)
                if cutoff is not None and sent < cutoff:
                    continue
                when = sent.astimezone().strftime("%d.%m.%Y %H:%M")
            except Exception:
                pass

            sender = decode_mime(message.get("From"))
            attachments = [
                decode_mime(part.get_filename())
                for part in message.walk()
                if part.get_filename()
            ]
            letters.append(Letter(
                uid=uid.decode(),
                sender=sender,
                sender_name=sender.split("<")[0].strip().strip('"') or sender,
                subject=decode_mime(message.get("Subject")) or "(без темы)",
                date=when,
                body=extract_body(message, body_chars),
                attachments=attachments,
            ))
        return letters

    # --- перемещение ---

    def move(self, uids: list[str], target: str, mailbox: str = "INBOX") -> dict:
        """Переносит письма партиями. EXPUNGE после каждой партии, чтобы
        обрыв соединения не откатывал уже сделанное."""
        if not uids:
            return {"moved": 0, "failed": 0}
        if not self.folder_exists(target):
            raise ValueError(f"Папки «{target}» нет — создайте её заранее")

        imap = self.connect(mailbox)
        encoded = encode_folder(target)
        moved = failed = 0

        for start in range(0, len(uids), COPY_BATCH):
            chunk = uids[start:start + COPY_BATCH]
            joined = ",".join(chunk)
            try:
                status, _ = imap.uid("COPY", joined, f'"{encoded}"')
                if status != "OK":
                    failed += len(chunk)
                    continue
                imap.uid("STORE", joined, "+FLAGS", "(\\Deleted)")
                imap.expunge()
                moved += len(chunk)
            except (imaplib.IMAP4.abort, OSError):
                # Mail.ru рвёт длинные сессии — переподключаемся и идём дальше.
                imap = self.connect(mailbox)
                failed += len(chunk)

        return {"moved": moved, "failed": failed}

    def count(self, mailbox: str = "INBOX") -> int:
        imap = self.connect(mailbox)
        status, data = imap.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return 0
        return len(data[0].split())
