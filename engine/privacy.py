"""Anonymisation helpers for collected reviews: standard library only.

A review is kept with what lets a prospect check it (its date, its link on Google) and without who wrote
it: no name, no photo, no profile. The author becomes a keyed hash, so "three complaints from three
different customers" can be counted without knowing anyone. Text is scrubbed of the personal data
customers sometimes type into a review (phone, e-mail, CPF, CNPJ, links, @handles).

Pseudonymised data can still be personal data under LGPD when it can be linked back to a person, and the
review link can. Treat this as risk reduction, not as a legal guarantee.
"""
import hashlib, hmac, os, re, secrets, unicodedata
from datetime import datetime, timedelta, timezone

SALT_FILE = "reviews.salt"

CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
HANDLE = re.compile(r"(?<![\w.])@\w{3,}")
# Brazilian phones with or without country code and area code: 8 or 9 digits in two groups.
PHONE = re.compile(r"(?<![\d/])(?:\+?55[\s.-]?)?(?:\(?\d{2}\)?[\s.-]?)?9?\d{4}[\s.-]?\d{4}(?![\d/])")


def scrub(text):
    """The text with phones, e-mails, CPF, CNPJ, links and @handles replaced by a tag."""
    if not isinstance(text, str):
        return ""
    for rx, tag in ((CNPJ, "[cnpj]"), (CPF, "[cpf]"), (EMAIL, "[email]"), (URL, "[link]"),
                    (HANDLE, "[usuario]"), (PHONE, "[telefone]")):
        text = rx.sub(tag, text)
    return " ".join(text.split())


def fold(text):
    """Lower case, no accents: what keyword searches and the lexicon match against."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def load_salt(data_dir):
    """The project's secret salt: created once, kept in the data folder, never exported."""
    path = os.path.join(data_dir, SALT_FILE)
    try:
        with open(path, "rb") as f:
            salt = f.read()
            if len(salt) >= 16:
                return salt
    except FileNotFoundError:
        pass
    salt = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(salt)
    return salt


def author_id(salt, key):
    """A short keyed hash of whatever identifies the author (profile id, else name), or "" for none.
    The key is never stored; the hash cannot be turned back into it without the salt."""
    if not key:
        return ""
    return hmac.new(salt, str(key).encode("utf-8"), hashlib.sha256).hexdigest()[:12]


# ── "2 months ago", "há 3 semanas", "hace un año" -> an approximate date ──
UNITS = {  # folded prefix -> (days, precision)
    "minut": (0, "dia"), "hora": (0, "dia"), "hour": (0, "dia"), "min": (0, "dia"),
    "dia": (1, "dia"), "day": (1, "dia"),
    "seman": (7, "semana"), "week": (7, "semana"),
    "mes": (30, "mes"), "month": (30, "mes"),
    "ano": (365, "ano"), "year": (365, "ano"),
}
WORDS = {"um": 1, "uma": 1, "a": 1, "an": 1, "un": 1, "one": 1, "dois": 2, "duas": 2}
RELATIVE = re.compile(r"(?<![a-z])(?:(?:ha|hace)\s+)?(\d+|um|uma|a|an|un|one|dois|duas)\s+([a-z]+)(?:\s+ago|\s+atras)?\b")


def approx_date(text, now=None):
    """(ISO date, precision) for a relative date such as "2 months ago" or "há 3 semanas"; ("", "") when
    it cannot be read. The date is the middle of the period, so a "month" is off by two weeks at most."""
    now = now or datetime.now(timezone.utc)
    s = fold(text)
    s = re.sub(r"^(editado|edited|editada|editado\s+em)\s+", "", s).strip()
    if s in ("ontem", "yesterday", "ayer"):
        return (now - timedelta(days=1)).date().isoformat(), "dia"
    if s in ("hoje", "today", "hoy", "agora", "now", "ahora"):
        return now.date().isoformat(), "dia"
    m = RELATIVE.search(s)
    if not m:
        return "", ""
    n = int(m.group(1)) if m.group(1).isdigit() else WORDS.get(m.group(1), 0)
    unit = next((v for k, v in UNITS.items() if m.group(2).startswith(k)), None)
    if not n or not unit:
        return "", ""
    days, precision = unit
    back = days * n
    if days > 1:
        back += days // 2  # the middle of the period
    return (now - timedelta(days=back)).date().isoformat(), precision


def from_micros(ts):
    """ISO date for a Google timestamp in microseconds (or milliseconds/seconds), "" when it is not one."""
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return ""
    for div in (1_000_000, 1000, 1):
        sec = v / div
        if 1_200_000_000 <= sec <= 2_300_000_000:  # 2008 .. 2042
            return datetime.fromtimestamp(sec, timezone.utc).date().isoformat()
    return ""
