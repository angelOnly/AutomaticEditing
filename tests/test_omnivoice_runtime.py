from __future__ import annotations


def test_omnivoice_runtime_dependency_check_reports_higgs_requirement() -> None:
    from newsclip_agent.tts_omnivoice import check_omnivoice_runtime

    result = check_omnivoice_runtime()

    assert "transformers_version" in result
    assert "has_higgs_audio_tokenizer" in result
    if not result["ok"]:
        assert result["error"]
