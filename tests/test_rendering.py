from codex_gui.rendering import MarkdownRenderer


def test_markdown_renderer_escapes_raw_html_and_renders_code() -> None:
    rendered = MarkdownRenderer().render("<script>alert(1)</script>\n\n```python\nprint('ok')\n```")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "print" in rendered

