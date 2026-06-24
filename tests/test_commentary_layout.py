from __future__ import annotations

from pathlib import Path

import pytest

from newsclip_agent import commentary
from newsclip_agent.utils import write_json


# --------------------------------------------------------------------------- #
# 纯函数：normalize / clamp / markdown
# --------------------------------------------------------------------------- #
def test_normalize_layout_clamps_into_range() -> None:
    assert commentary._normalize_layout({"insert_after_index": -5}, 3)[0] == 0
    assert commentary._normalize_layout({"insert_after_index": 99}, 3)[0] == 2
    assert commentary._normalize_layout({"insert_after_index": "oops"}, 3)[0] == 0
    assert commentary._normalize_layout({}, 3)[0] == 0
    assert commentary._normalize_layout({"insert_after_index": 1}, 3)[0] == 1
    # 兼容备用键名 index
    assert commentary._normalize_layout({"index": 2}, 3)[0] == 2
    # 单段时只能是 0
    assert commentary._normalize_layout({"insert_after_index": 5}, 1)[0] == 0


def test_normalize_script_clamps_paragraphs_and_title() -> None:
    cmt = {"max_paragraphs": 3, "min_paragraphs": 3, "title_max_chars": 10}
    material = {"title_hint": "", "topic": "某主题"}

    title, paragraphs, warnings = commentary._normalize_script(
        {"title": "标题", "paragraphs": ["a", "b", "c", "d"]}, cmt, material
    )
    assert title == "标题"
    assert paragraphs == ["a", "b", "c"]  # 截到 max_paragraphs
    assert warnings == []


def test_normalize_script_title_fallback_and_empty() -> None:
    cmt = {"max_paragraphs": 5, "min_paragraphs": 3, "title_max_chars": 22}
    material = {"title_hint": "", "topic": "某主题"}

    title, paragraphs, warnings = commentary._normalize_script({"paragraphs": []}, cmt, material)
    assert title == "某主题"
    assert paragraphs == []
    assert "title_fallback" in warnings
    assert "no_paragraphs" in warnings
    assert any(w.startswith("paragraphs_below_min") for w in warnings)


def test_normalize_script_truncates_overlong_title() -> None:
    cmt = {"max_paragraphs": 5, "min_paragraphs": 1, "title_max_chars": 10}
    material = {"title_hint": "", "topic": "T"}
    long_title = "一二三四五六七八九十一二三四五六七八九十"  # 20 字 > 10 + 8

    title, _paragraphs, warnings = commentary._normalize_script(
        {"title": long_title, "paragraphs": ["x"]}, cmt, material
    )
    assert len(title) == 10
    assert "title_truncated" in warnings


def test_assemble_markdown_inserts_video_after_index() -> None:
    md = commentary._assemble_markdown("标题", ["p0", "p1", "p2"], 1, "draft.mp4")
    assert '<video controls src="draft.mp4"></video>' in md
    # 视频应在 p1 之后、p2 之前
    assert md.index("p1") < md.index("<video") < md.index("p2")


def test_assemble_markdown_handles_empty_paragraphs() -> None:
    md = commentary._assemble_markdown("标题", [], 0, "draft.mp4")
    assert "# 标题" in md
    assert "<video" in md


# --------------------------------------------------------------------------- #
# 数据 join：build_commentary_material
# --------------------------------------------------------------------------- #
def _make_task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task_highlight_reassembly"
    write_json(
        task_dir / "edit/reassembly_cut_plan/v1/reassembly_cut_plan.json",
        {
            "output_videos": [
                {
                    "reassembly_id": "hr_001",
                    "title": "",
                    "clips": [
                        {"source_clip_id": "c1", "role": "核心事件", "duration_seconds": 8.4, "selection_reason": "sr1"},
                        {"source_clip_id": "c2", "duration_seconds": 6.0, "selection_reason": "sr2"},
                        {"source_clip_id": "c3", "duration_seconds": 5.0, "selection_reason": "sr3"},
                    ],
                }
            ]
        },
    )
    write_json(
        task_dir / "edit/reassembly_drafts/v1/reassembly_render_outputs.json",
        {
            "version": "v1",
            "outputs": [
                {
                    "reassembly_id": "hr_001",
                    "file": "edit/reassembly_drafts/v1/hr_001/highlight_reassembly_hr_001_draft.mp4",
                }
            ],
        },
    )
    write_json(
        task_dir / "agents/asr_event_candidate/v1/asr_event_candidate.json",
        {
            "candidate_clip_pool": [
                {"clip_id": "c1", "speech": "原声1", "summary": "摘要1"},
                {"clip_id": "c2", "asr_text": "原声2"},
            ]
        },
    )
    write_json(
        task_dir / "agents/video_understanding/v1/video_analysis.json",
        {"main_topic": "某主题", "summary": "整体摘要", "key_facts": ["事实A", "事实B"]},
    )
    return task_dir


def test_build_material_joins_speech_and_order(tmp_path: Path) -> None:
    task_dir = _make_task(tmp_path)
    material = commentary.build_commentary_material(task_dir, None)

    assert material is not None
    assert material["reassembly_id"] == "hr_001"
    assert material["video_file"].endswith("highlight_reassembly_hr_001_draft.mp4")
    assert material["topic"] == "某主题"
    assert material["key_facts"] == ["事实A", "事实B"]
    # 顺序保持，speech 依次回退：pool.speech / pool.asr_text / selection_reason
    assert [c["order"] for c in material["clips"]] == [1, 2, 3]
    assert [c["speech"] for c in material["clips"]] == ["原声1", "原声2", "sr3"]
    assert material["content_hash"]


def test_build_material_returns_none_without_render(tmp_path: Path) -> None:
    task_dir = tmp_path / "no_render"
    write_json(
        task_dir / "edit/reassembly_cut_plan/v1/reassembly_cut_plan.json",
        {"output_videos": [{"reassembly_id": "hr_001", "clips": []}]},
    )
    assert commentary.build_commentary_material(task_dir, None) is None


# --------------------------------------------------------------------------- #
# 生成与缓存：mock 掉 LLM 客户端
# --------------------------------------------------------------------------- #
class _FakeResult:
    def __init__(self, parsed: object) -> None:
        self.parsed = parsed


class _FakeClient:
    def __init__(self, script: dict, layout: dict) -> None:
        self._script = script
        self._layout = layout
        self.calls: list[str] = []

    def call_json(self, *, prompt: str, **_kwargs: object) -> _FakeResult:
        self.calls.append(prompt)
        if prompt is commentary.SCRIPT_PROMPT:
            return _FakeResult(self._script)
        return _FakeResult(self._layout)


class _FakeConfig:
    def __init__(self, raw: dict) -> None:
        self.raw = raw

    @property
    def llm(self) -> dict:
        return self.raw.get("llm", {})


def _patch_client(monkeypatch: pytest.MonkeyPatch, script: dict, layout: dict) -> list[_FakeClient]:
    created: list[_FakeClient] = []

    def factory(_config: object):
        client = _FakeClient(script, layout)
        created.append(client)
        return client, "fake-model", [], "deepseek"

    monkeypatch.setattr(commentary, "_make_commentary_client", factory)
    return created


def _material() -> dict:
    return {
        "reassembly_id": "hr_001",
        "video_file": "edit/reassembly_drafts/v1/hr_001/highlight_reassembly_hr_001_draft.mp4",
        "title_hint": "",
        "topic": "T",
        "summary": "S",
        "key_facts": [],
        "clips": [
            {"order": 1, "role": "核心", "speech": "s1", "summary": "m1", "duration": 5.0},
            {"order": 2, "role": "现场", "speech": "s2", "summary": "m2", "duration": 4.0},
        ],
        "content_hash": "h",
    }


def test_generate_one_builds_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _patch_client(
        monkeypatch,
        script={"title": "新闻标题", "paragraphs": ["p0", "p1", "p2"]},
        layout={"insert_after_index": 1, "reason": "匹配"},
    )
    config = _FakeConfig({"commentary": {"max_paragraphs": 5, "min_paragraphs": 3, "title_max_chars": 22}})

    artifact = commentary.generate_one(config, _material())

    assert artifact["version"] == "commentary_v1"
    assert artifact["title"] == "新闻标题"
    assert artifact["paragraphs"] == ["p0", "p1", "p2"]
    assert artifact["insert_after_index"] == 1
    assert artifact["video"]["file"].endswith(".mp4")
    assert "<video" in artifact["markdown"]
    assert artifact["source"]["provider"] == "deepseek"
    # 文案 + 排版两次调用
    assert len(clients[0].calls) == 2


def test_generate_for_task_and_load_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_dir = _make_task(tmp_path)
    _patch_client(
        monkeypatch,
        script={"title": "新闻标题", "paragraphs": ["p0", "p1", "p2"]},
        layout={"insert_after_index": 2, "reason": "r"},
    )
    config = _FakeConfig({"commentary": {"max_paragraphs": 5, "min_paragraphs": 3, "title_max_chars": 22}})

    index_path, index = commentary.generate_for_task(config, task_dir)

    assert index_path.exists()
    assert index["outputs"] and index["outputs"][0]["reassembly_id"] == "hr_001"

    loaded = commentary.load_latest_commentary(task_dir, "hr_001")
    assert loaded is not None
    assert loaded["title"] == "新闻标题"
    assert loaded["insert_after_index"] == 2

    # rid 缺省时取最新索引第一条
    default_loaded = commentary.load_latest_commentary(task_dir)
    assert default_loaded["reassembly_id"] == "hr_001"
