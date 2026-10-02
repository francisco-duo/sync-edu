"""Geração determinística de e-mails institucionais. Funções puras, sem estado."""

import re
import unicodedata
from collections.abc import Collection

DEFAULT_EMAIL_DOMAIN = "example.edu"
MAX_LOCAL_PART_LENGTH = 64

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")


def build_local_part(first_name: str, last_name: str, fallback_id: str) -> str:
    """Parte local do e-mail: `primeironome.ultimosobrenome`, sem acentos, em minúsculas.

    Usa o primeiro token de `first_name` e o último de `last_name`. Se qualquer uma das partes
    ficar vazia depois da normalização, usa `aluno{id normalizado}`.
    """
    first = _normalize(_first_token(first_name))
    last = _normalize(_last_token(last_name))
    if not first or not last:
        return f"aluno{_normalize(fallback_id)}"
    return f"{first}.{last}"[:MAX_LOCAL_PART_LENGTH]


def allocate_email(local_part: str, domain: str, taken: Collection[str]) -> str:
    """Primeiro e-mail livre entre `local`, `local2`, `local3`, ...

    `taken` deve conter e-mails completos em minúsculas. O sufixo numérico é concatenado
    direto (`joao.silva2`) e a parte local nunca passa de 64 caracteres.
    """
    attempt = 1
    while True:
        suffix = "" if attempt == 1 else str(attempt)
        local = local_part[: MAX_LOCAL_PART_LENGTH - len(suffix)] + suffix
        email = f"{local}@{domain}"
        if email not in taken:
            return email
        attempt += 1


def _first_token(value: str) -> str:
    tokens = value.split()
    return tokens[0] if tokens else ""


def _last_token(value: str) -> str:
    tokens = value.split()
    return tokens[-1] if tokens else ""


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALPHANUMERIC.sub("", without_marks.lower())
