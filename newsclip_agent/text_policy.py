from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


REPORTER_BYLINE_FORBIDDEN_TERMS = [
    "记者报道",
    "本台记者",
    "凤凰卫视记者",
    "凤凰记者",
    "发回报道",
    "为您报道",
    "下面来看",
    "以上是",
    "来看记者",
]


REPORTER_BYLINE_PATTERNS = [
    re.compile(r"(?:凤凰卫视|本台|央视|新华社|中新社)?记者[^。！？!?，,；;\n]{0,18}(?:报道|发回报道)"),
    re.compile(r"[^。！？!?，,；;\n]{1,12}(?:从|在)[^。！？!?，,；;\n]{1,12}发回报道"),
    re.compile(r"[^。！？!?，,；;\n]{1,12}为您报道"),
    re.compile(r"(?:下面|接下来|现在)来看[^。！？!?，,；;\n]{0,20}(?:报道|记者)"),
    re.compile(r"以上是[^。！？!?，,；;\n]{0,20}报道"),
]


@dataclass(frozen=True)
class TextPolicyIssue:
    issue_type: str
    text: str
    suggestion: str

    def to_dict(self) -> dict[str, str]:
        return {
            "type": self.issue_type,
            "text": self.text,
            "suggestion": self.suggestion,
        }


def reporter_byline_must_not_include() -> list[str]:
    return list(REPORTER_BYLINE_FORBIDDEN_TERMS)


def find_reporter_byline_issues(text: str | None) -> list[TextPolicyIssue]:
    value = text or ""
    issues: list[TextPolicyIssue] = []
    seen: set[str] = set()
    for pattern in REPORTER_BYLINE_PATTERNS:
        for match in pattern.finditer(value):
            phrase = match.group(0).strip()
            if phrase and phrase not in seen:
                seen.add(phrase)
                issues.append(
                    TextPolicyIssue(
                        issue_type="reporter_byline",
                        text=phrase,
                        suggestion="删除报道署名、记者引导或新闻包结尾语，只保留新闻事实讲解。",
                    )
                )
    return issues


def sanitize_reporter_bylines(text: str | None) -> tuple[str, list[dict[str, str]]]:
    value = text or ""
    issues = find_reporter_byline_issues(value)
    cleaned = value
    for issue in issues:
        cleaned = cleaned.replace(issue.text, "")
    cleaned = re.sub(r"[，,；;]\s*[。！？!?]", "。", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = re.sub(r"^[，,。！？!?；;]+", "", cleaned)
    cleaned = re.sub(r"[，,；;]{2,}", "，", cleaned)
    cleaned = re.sub(r"[。]{2,}", "。", cleaned)
    return cleaned.strip(), [issue.to_dict() for issue in issues]


def text_policy_check(text: str | None) -> dict[str, Any]:
    sanitized, issues = sanitize_reporter_bylines(text)
    return {
        "status": "blocked" if find_reporter_byline_issues(sanitized) else "ok",
        "issues": issues,
        "sanitized_text": sanitized,
    }
