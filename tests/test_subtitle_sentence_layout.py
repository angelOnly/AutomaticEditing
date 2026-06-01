from __future__ import annotations

from pathlib import Path

from newsclip_agent.pipeline import PipelineRunner, RunOptions


def make_runner(tmp_path: Path, aspect_ratio: str = "16:9") -> PipelineRunner:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"")
    return PipelineRunner(
        RunOptions(
            input=str(source),
            task_id=f"subtitle_test_{aspect_ratio.replace(':', '_')}",
            config="config.toml",
            outputs_dir=str(tmp_path / "outputs"),
            skip_render=True,
            aspect_ratio=aspect_ratio,
        )
    )


def text_lines_from_srt(srt: str) -> list[str]:
    return [
        line
        for line in srt.splitlines()
        if line and "-->" not in line and not line.isdigit()
    ]


def test_split_subtitle_text_prefers_sentence_and_clause(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)
    text = "近期美伊双方处于边谈边打的特殊状态，伊朗革命卫队公开表态，若最终达成的停火协议遭到违反，伊朗将立刻做出对等回应。"

    chunks = runner._split_subtitle_text(text, max_chars=22)

    assert len(chunks) >= 3
    assert all(len(chunk) <= 22 for chunk in chunks)
    assert chunks[0].endswith("，")


def test_tts_segments_to_srt_generates_multiple_single_line_cues(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)
    segments = [
        {
            "text": "近期美伊双方处于边谈边打的特殊状态，伊朗革命卫队公开表态，若最终达成的停火协议遭到违反，伊朗将立刻做出对等回应。",
            "target_start_seconds": 0,
            "target_end_seconds": 6,
            "actual_duration_seconds": 6,
        }
    ]

    srt = runner._tts_segments_to_srt(segments)
    text_lines = text_lines_from_srt(srt)

    assert "00:00:00" in srt
    assert "\n2\n" in srt
    assert len(text_lines) >= 3
    assert all(len(line) <= 22 for line in text_lines)


def test_subtitle_segment_mode_keeps_legacy_single_block(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)
    runner.config.raw["subtitle"]["mode"] = "segment"
    text = "近期美伊双方处于边谈边打的特殊状态，伊朗革命卫队公开表态，若最终达成的停火协议遭到违反，伊朗将立刻做出对等回应。"

    srt = runner._tts_segments_to_srt([
        {
            "text": text,
            "target_start_seconds": 0,
            "target_end_seconds": 6,
        }
    ])

    assert "\n2\n" not in srt
    assert text_lines_from_srt(srt) == [text]
