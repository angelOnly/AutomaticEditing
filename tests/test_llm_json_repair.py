from __future__ import annotations

from types import SimpleNamespace

import json
import pytest

from newsclip_agent.llm import OpenAICompatibleClient
from newsclip_agent.utils import extract_json_object


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            content = '{"title": "bad "quote"}'
        else:
            content = '{"title": "bad quote"}'
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )


class _AlwaysBadCompletions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"title": "bad" "json"}'),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )


def test_call_json_repairs_invalid_model_json() -> None:
    completions = _FakeCompletions()
    client = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client.max_retries = 1

    result = client.call_json(model="fake-model", prompt="return json", input_data={})

    assert result.parsed == {"title": "bad quote"}
    assert completions.calls == 2


def test_call_json_writes_debug_response_when_repair_fails(tmp_path) -> None:
    completions = _AlwaysBadCompletions()
    client = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client.max_retries = 1

    with pytest.raises(RuntimeError):
        client.call_json(model="fake-model", prompt="return json", input_data={}, debug_dir=tmp_path)

    debug_files = list(tmp_path.glob("attempt_fake-model_1.json"))
    assert len(debug_files) == 1
    payload = debug_files[0].read_text(encoding="utf-8")
    assert '"raw_text"' in payload
    assert '"repair_raw_text"' in payload
    debug_payload = json.loads(payload)
    assert debug_payload["parse_error"]
    assert debug_payload["repair_parse_error"]


def test_extract_json_object_accepts_json5_trailing_commas() -> None:
    parsed = extract_json_object('```json\n{"items": [1, 2,],}\n```')

    assert parsed == {"items": [1, 2]}
