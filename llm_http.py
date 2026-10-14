"""Общие куски openai-совместимых LLM-провайдеров (extract/digest)."""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable


def extra_body(url: str | None) -> dict:
    """DashScope-qwen по умолчанию «думает»: на больших промптах reasoning
    съедает минуты и упирается в таймаут — для structured-извлечения выключаем."""
    return {"enable_thinking": False} if "dashscope" in (url or "").lower() else {}


def call_with_deadline(fn: Callable[..., Any], deadline: float,
                       *args: Any, **kwargs: Any) -> Any:
    """Зовёт fn не дольше deadline секунд. socket-timeout бессилен, когда
    сервер цедит байты струйкой, — поэтому жёсткий wall-clock: висящий
    daemon-поток не держит процесс и умирает вместе с ним."""
    q: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            q.put(("ok", fn(*args, **kwargs)))
        except Exception as exc:  # пробрасываем вызывающему как есть
            q.put(("err", exc))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        kind, val = q.get(timeout=deadline)
    except queue.Empty:
        raise TimeoutError(f"нет ответа провайдера за {deadline}с") from None
    if kind == "err":
        raise val
    return val
