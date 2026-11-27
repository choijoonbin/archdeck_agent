from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def canonicalize_url(value: str, approved_hosts: set[str]) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid_url") from error
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or host not in approved_hosts
    ):
        raise ValueError("unapproved_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("literal_ip_not_allowed")
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in {"api_key", "apikey", "access_token", "token", "key"}:
            raise ValueError("credential_query_not_allowed")
        if lowered.startswith("utm_") or lowered in {"fbclid", "gclid"}:
            continue
        query.append((key, item))
    return urlunsplit(("https", host, parsed.path or "/", urlencode(query), ""))


def excerpt_digest(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def contains_instruction_marker(value: str) -> bool:
    # A triage heuristic, not a prompt-injection security boundary.
    return bool(re.search(r"ignore\s+(?:all\s+)?previous\s+instructions", value, re.I))
