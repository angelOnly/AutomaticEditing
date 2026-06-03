import pytest

from newsclip_agent.remote_ucms import _quote_url_for_http


def test_quote_url_for_http_encodes_non_ascii_path_and_query() -> None:
    url = (
        "http://example.com/"
        "\u89c6\u9891\u7d20\u6750/"
        "\u51e4\u51f0 \u65b0\u95fb.mp4?name=\u4e2d\u6587&x=1"
    )

    safe = _quote_url_for_http(url)

    assert safe == (
        "http://example.com/"
        "%E8%A7%86%E9%A2%91%E7%B4%A0%E6%9D%90/"
        "%E5%87%A4%E5%87%B0%20%E6%96%B0%E9%97%BB.mp4"
        "?name=%E4%B8%AD%E6%96%87&x=1"
    )
    safe.encode("ascii")


def test_quote_url_for_http_keeps_existing_percent_encoding() -> None:
    assert (
        _quote_url_for_http("http://example.com/a/%E4%B8%AD.mp4")
        == "http://example.com/a/%E4%B8%AD.mp4"
    )


def test_quote_url_for_http_rejects_unsupported_scheme() -> None:
    with pytest.raises(RuntimeError, match="unsupported download url scheme"):
        _quote_url_for_http("ftp://example.com/video.mp4")
