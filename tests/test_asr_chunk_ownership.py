from types import SimpleNamespace

from newsclip_agent.pipeline import PipelineRunner


def _runner() -> PipelineRunner:
    return PipelineRunner.__new__(PipelineRunner)


def test_asr_windows_keep_core_and_decode_ranges() -> None:
    runner = _runner()
    runner.config = SimpleNamespace(
        funasr={
            "segment_seconds": 60,
            "segment_overlap_seconds": 1.0,
            "max_segment_seconds": 90,
        }
    )
    runner._load_step_json = lambda _step: {
        "chunks": [
            {"chunk_id": "chunk_0001", "start": 0.0, "end": 10.0},
            {"chunk_id": "chunk_0002", "start": 10.0, "end": 20.0},
        ]
    }

    windows = runner._asr_windows_from_chunks()

    assert windows[0]["core_start"] == 0.0
    assert windows[0]["core_end"] == 10.0
    assert windows[0]["decode_start"] == 0.0
    assert windows[0]["decode_end"] == 11.0
    assert windows[1]["owner_chunk_id"] == "chunk_0002"
    assert windows[1]["decode_start"] == 9.0
    assert windows[1]["decode_end"] == 20.0


def test_normalize_asr_segments_preserves_owner_fields() -> None:
    runner = _runner()
    segments = runner._normalize_asr_segments(
        [
            {
                "start": 49.0,
                "end": 61.0,
                "text": "凤凰聚焦",
                "owner_chunk_id": "chunk_0006",
                "chunk_id": "chunk_0006",
                "window_id": "chunk_0006",
                "source_id": "source_002",
                "core_start": 50.0,
                "core_end": 60.0,
                "decode_start": 49.0,
                "decode_end": 61.0,
                "timing_quality": "window_coarse",
            }
        ],
        "",
    )

    segment = segments[0]
    assert segment["owner_chunk_id"] == "chunk_0006"
    assert segment["source_id"] == "source_002"
    assert segment["core_start"] == 50.0
    assert segment["decode_end"] == 61.0


def test_owner_selection_does_not_expand_ten_second_chunk_to_neighbor_windows() -> None:
    runner = _runner()
    segments = [
        {"id": 1, "start": 39.0, "end": 51.0, "text": "previous", "owner_chunk_id": "chunk_0005"},
        {"id": 2, "start": 49.0, "end": 61.0, "text": "owned", "owner_chunk_id": "chunk_0006"},
        {"id": 3, "start": 59.0, "end": 71.0, "text": "next", "owner_chunk_id": "chunk_0007"},
    ]
    chunk = {"chunk_id": "chunk_0006", "start": 50.0, "end": 60.0}

    selected = runner._asr_segments_for_chunk(segments, chunk)

    assert [segment["text"] for segment in selected] == ["owned"]


def test_legacy_midpoint_fallback_assigns_each_overlap_window_once() -> None:
    runner = _runner()
    segments = [
        {"id": 1, "start": 39.0, "end": 51.0, "text": "previous"},
        {"id": 2, "start": 49.0, "end": 61.0, "text": "owned"},
        {"id": 3, "start": 59.0, "end": 71.0, "text": "next"},
    ]
    chunks = [
        {"chunk_id": "chunk_0005", "start": 40.0, "end": 50.0},
        {"chunk_id": "chunk_0006", "start": 50.0, "end": 60.0},
        {"chunk_id": "chunk_0007", "start": 60.0, "end": 70.0},
    ]

    selected = [
        [segment["text"] for segment in runner._asr_segments_for_chunk(segments, chunk)]
        for chunk in chunks
    ]

    assert selected == [["previous"], ["owned"], ["next"]]


def test_owner_selection_filters_context_only_and_other_sources() -> None:
    runner = _runner()
    segments = [
        {
            "id": 1,
            "start": 50.0,
            "end": 55.0,
            "text": "owned",
            "owner_chunk_id": "chunk_0006",
            "source_id": "source_002",
        },
        {
            "id": 2,
            "start": 49.0,
            "end": 50.0,
            "text": "context",
            "owner_chunk_id": "chunk_0006",
            "source_id": "source_002",
            "context_only": True,
        },
        {
            "id": 3,
            "start": 50.0,
            "end": 55.0,
            "text": "other source",
            "owner_chunk_id": "chunk_0006",
            "source_id": "source_003",
        },
    ]
    chunk = {
        "chunk_id": "chunk_0006",
        "source_id": "source_002",
        "local_start_seconds": 50.0,
        "local_end_seconds": 60.0,
    }

    selected = runner._asr_segments_for_chunk(segments, chunk)

    assert [segment["text"] for segment in selected] == ["owned"]
