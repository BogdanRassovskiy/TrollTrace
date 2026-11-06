"""Нормализация URL и хэши текста для дедупликации (README §4.1)."""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Параметры отслеживания, которые не меняют содержимое страницы.
TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid"}
TRACKING_PREFIXES = ("utm_",)

_WHITESPACE = re.compile(r"\s+")


def normalize_url(url: str, *, keep_fragment: bool = False) -> str:
    """Схема и хост в нижнем регистре, без www, фрагмента, трекинговых параметров и хвостового /.

    Оставшиеся параметры сортируются: у разных ссылок на одну статью порядок бывает разным.
    keep_fragment — когда фрагмент адресует отдельный документ (выступление в стенограмме).
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port and not (
        (parts.scheme == "http" and parts.port == 80)
        or (parts.scheme == "https" and parts.port == 443)
    ):
        host = f"{host}:{parts.port}"
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith(TRACKING_PREFIXES)
    )
    path = parts.path.rstrip("/") or ""
    fragment = parts.fragment if keep_fragment else ""
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), fragment))


def strip_tracking(url: str) -> str:
    """Убирает только трекинговые параметры (utm_…), остальное не трогает — по этому адресу качаем.

    RSS RT добавляет ?utm_source=rss, а robots.txt RT запрещает адреса с «?».
    """
    parts = urlsplit(url.strip())
    if not parts.query:
        return url.strip()
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith(TRACKING_PREFIXES)
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


def text_hash(text: str) -> str:
    """SHA-256 текста с нормализованными пробелами: правки в вёрстке не создают новую версию."""
    normalized = _WHITESPACE.sub(" ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
