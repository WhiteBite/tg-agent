"""Маскирование строк, похожих на секреты, в тексте сообщений.

Индекс — это твоя переписка; в ней routinely лежат ключи от панелей,
sk-… от OpenAI, токены ботов. Модель и UI не должны видеть их в открытом виде.

redact() возвращает (маскированный_текст, [найденные_типы]) — типы добавляются
пометкой в конец строки, чтобы было видно, ЧТО скрыто, но не само значение.
"""

from __future__ import annotations

import re
from typing import Optional

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("tg-bot-token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")),
    ("openai-key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("groq-key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("nvapi-key", re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")),
    ("long-hex", re.compile(r"\b[a-fA-F0-9]{64}\b")),
    # общий шаблон «ключ: …» — но не съедал конкретные префиксы выше
    ("api-key-ru", re.compile(
        r"(?i:\b(?:ключ|key|token|токен|пароль|password)\s*[:=]\s*"
        r"(?!(?:sk-|ghp_|gho_|nvapi-|xox|gsk_|github_pat_|\d{8,10}:AA))\S{8,})")),
]

# один проход: вставленные маркеры не пересканиваются, а более специфичные
# шаблоны (выше по списку) выигрывают на той же позиции
_COMBINED = re.compile(
    "|".join(f"(?P<{kind.replace('-', '_')}>{rx.pattern})" for kind, rx in _PATTERNS)
)


def redact(text: Optional[str]) -> tuple[str, list[str]]:
    """Заменяет похожее на секрет на [скрыто: тип]."""
    if not text:
        return text or "", []
    found: list[str] = []

    def sub(m: re.Match) -> str:
        kind = (m.lastgroup or "secret").replace("_", "-")
        found.append(kind)
        return f"[скрыто: {kind}]"

    out = _COMBINED.sub(sub, text)
    if found:
        seen: set[str] = set()
        uniq = [k for k in found if not (k in seen or seen.add(k))]
        out += "  [⚠ в сообщении маскировано: " + ", ".join(uniq) + "]"
    return out, found
