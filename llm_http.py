"""Общие куски openai-совместимых LLM-провайдеров (extract/digest)."""

from __future__ import annotations

import json
import queue
import threading
import urllib.error
import urllib.request
from typing import Any, Callable


def post(req: urllib.request.Request, timeout: float) -> Any:
    """POST JSON; HTTP-ошибки поднимаются с отрывком тела ответа — иначе
    причина 400 (контент-фильтр, лимит параметра) не диагностируется."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def key_list(value: str | None) -> list[str | None]:
    """*_KEY env: один ключ или список через запятую; пусто → [None] (без авторизации)."""
    keys = [k.strip() for k in (value or "").split(",") if k.strip()]
    return keys or [None]


def post_json_keys(url: str, keys: list[str | None], payload: dict,
                   timeout: float) -> Any:
    """POST JSON с перебором ключей по порядку: 401/403 ротирует следующий,
    прочие ошибки поднимаются сразу."""
    data = json.dumps(payload).encode()
    last: Exception | None = None
    for key in keys or [None]:
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            return post(req, timeout)
        except RuntimeError as exc:
            if "HTTP 401" not in str(exc) and "HTTP 403" not in str(exc):
                raise
            last = exc
    if last is not None:
        raise last
    raise RuntimeError("нет ключей")


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
