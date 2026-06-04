from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import time

from .utils import extract_json_object, image_to_data_url, now_iso, write_json


@dataclass
class LLMCallResult:
    model: str
    raw_text: str
    parsed: Any
    usage: dict[str, Any]
    finish_reason: str
    latency_ms: int


class OpenAICompatibleClient:
    def __init__(self, api_key: str, base_url: str, timeout: int = 180, max_retries: int = 3):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries or 1))

    def call_json(
        self,
        *,
        model: str,
        prompt: str,
        input_data: Any,
        temperature: float = 0.2,
        image_paths: list[str] | None = None,
        fallback_models: list[str] | None = None,
        debug_dir: str | Path | None = None,
        max_tokens: int | None = None,
    ) -> LLMCallResult:
        models = [model] + [m for m in (fallback_models or []) if m and m != model]
        last_error: Exception | None = None
        debug_path = Path(debug_dir) if debug_dir else None
        for model_name in models:
            for attempt in range(self.max_retries):
                started = time.time()
                debug_payload: dict[str, Any] = {
                    "model": model_name,
                    "attempt": attempt + 1,
                    "created_at": now_iso(),
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "status": "started",
                }
                try:
                    text_payload = prompt + "\n\n输入数据：\n" + _safe_json(input_data)
                    if image_paths:
                        content: str | list[dict[str, Any]] = [{"type": "text", "text": text_payload}]
                    else:
                        content = text_payload
                    for path in image_paths or []:
                        content.append({"type": "image_url", "image_url": {"url": image_to_data_url(path)}})
                    request_payload: dict[str, Any] = {
                        "model": model_name,
                        "messages": [{"role": "user", "content": content}],
                        "temperature": temperature,
                    }
                    if max_tokens is not None:
                        request_payload["max_tokens"] = max_tokens
                    response = self.client.chat.completions.create(**request_payload)
                    choice = response.choices[0]
                    raw_text = choice.message.content or ""
                    debug_payload.update({
                        "status": "model_returned",
                        "finish_reason": getattr(choice, "finish_reason", "") or "",
                        "raw_text": raw_text,
                    })
                    try:
                        parsed = extract_json_object(raw_text)
                    except Exception as parse_exc:
                        debug_payload["parse_error"] = str(parse_exc)
                        repaired_text = self._repair_json_response(
                            model=model_name,
                            raw_text=raw_text,
                            parse_error=parse_exc,
                        )
                        debug_payload["repair_raw_text"] = repaired_text
                        try:
                            parsed = extract_json_object(repaired_text)
                            raw_text = repaired_text
                            debug_payload["status"] = "repaired"
                        except Exception as repair_parse_exc:
                            debug_payload["repair_parse_error"] = str(repair_parse_exc)
                            self._write_debug_attempt(debug_path, model_name, attempt, debug_payload)
                            raise
                    usage = response.usage.model_dump() if getattr(response, "usage", None) else {}
                    debug_payload.update({
                        "status": "success",
                        "usage": usage,
                        "latency_ms": int((time.time() - started) * 1000),
                    })
                    self._write_debug_attempt(debug_path, model_name, attempt, debug_payload)
                    return LLMCallResult(
                        model=model_name,
                        raw_text=raw_text,
                        parsed=parsed,
                        usage=usage,
                        finish_reason=getattr(choice, "finish_reason", "") or "",
                        latency_ms=int((time.time() - started) * 1000),
                    )
                except Exception as exc:
                    last_error = exc
                    debug_payload.update({
                        "status": "failed",
                        "error": str(exc),
                        "latency_ms": int((time.time() - started) * 1000),
                    })
                    self._write_debug_attempt(debug_path, model_name, attempt, debug_payload)
                    if attempt + 1 >= self.max_retries:
                        break
                    time.sleep(1.5 * (attempt + 1))
        debug_hint = f"，调试响应目录: {debug_path}" if debug_path else ""
        raise RuntimeError(f"LLM 调用失败，已尝试模型 {models}{debug_hint}: {last_error}") from last_error

    def _write_debug_attempt(self, debug_dir: Path | None, model: str, attempt: int, payload: dict[str, Any]) -> None:
        if debug_dir is None:
            return
        safe_model = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in model)[:80]
        write_json(debug_dir / f"attempt_{safe_model}_{attempt + 1}.json", payload)

    def _repair_json_response(self, *, model: str, raw_text: str, parse_error: Exception) -> str:
        repair_prompt = (
            "下面是一段模型输出的 JSON，但它无法被 Python json.loads 解析。\n"
            f"解析错误：{parse_error}\n\n"
            "请只修复 JSON 语法问题，不要改写字段含义，不要新增解释，不要输出 Markdown，"
            "只返回一个可以被 json.loads 直接解析的完整 JSON。\n\n"
            "原始输出如下：\n"
            f"{raw_text}"
        )
        response = self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": repair_prompt}],
            temperature=0,
        )
        return response.choices[0].message.content or ""


def _safe_json(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
