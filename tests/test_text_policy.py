from __future__ import annotations

from newsclip_agent.text_policy import (
    find_reporter_byline_issues,
    reporter_byline_must_not_include,
    sanitize_reporter_bylines,
)


def test_detects_reporter_byline_from_subtitle_noise() -> None:
    text = "欧盟如何维持统一立场，仍是欧盟面临的重要挑战。凤凰卫视记者卢森堡报道。"

    issues = find_reporter_byline_issues(text)

    assert issues
    assert "凤凰卫视记者卢森堡报道" in issues[0].text


def test_sanitizes_reporter_byline_without_touching_news_person_names() -> None:
    text = "欧盟外长在卢森堡讨论地区安全。凤凰卫视记者卢森堡报道。冯德莱恩表示，各方仍需协调立场。"

    cleaned, issues = sanitize_reporter_bylines(text)

    assert issues
    assert "凤凰卫视记者" not in cleaned
    assert "报道" not in cleaned
    assert "冯德莱恩" in cleaned
    assert "欧盟外长" in cleaned


def test_contract_forbidden_terms_are_explicit() -> None:
    terms = reporter_byline_must_not_include()

    assert "凤凰卫视记者" in terms
    assert "发回报道" in terms
    assert "为您报道" in terms
