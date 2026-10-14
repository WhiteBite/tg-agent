"""Общие куски openai-совместимых LLM-провайдеров (extract/digest)."""

from __future__ import annotations


def extra_body(url: str | None) -> dict:
    """DashScope-qwen по умолчанию «думает»: на больших промптах reasoning
    съедает минуты и упирается в таймаут — для structured-извлечения выключаем."""
    return {"enable_thinking": False} if "dashscope" in (url or "").lower() else {}
