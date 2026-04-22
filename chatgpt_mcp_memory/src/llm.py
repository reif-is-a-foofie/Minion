from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ollama_limits import acquire_ollama_inference, merged_ollama_options


@dataclass(frozen=True)
class LlmResponse:
    content: str
    raw: Dict[str, Any]


def chat(
    *,
    model: str,
    system: str,
    user: str,
    options: Optional[Dict[str, Any]] = None,
    timeout_seconds: Optional[float] = None,
) -> LlmResponse:
    """
    Global/shared LLM entrypoint for this project.

    Today it targets local Ollama, but callers should depend on this wrapper
    (not `ollama` directly) so we can swap/extend providers later.
    """
    try:
        import ollama  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "Missing dependency 'ollama'. Install deps:\n"
            "  pip install -r requirements.txt"
        ) from e

    client_kwargs: Dict[str, Any] = {}
    if timeout_seconds is not None:
        client_kwargs["timeout"] = float(timeout_seconds)

    opts = merged_ollama_options(options)
    with acquire_ollama_inference():
        resp = ollama.chat(  # type: ignore
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options=opts,
            **client_kwargs,
        )

    content = (resp.get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM returned empty response content.")

    return LlmResponse(content=content, raw=resp)

