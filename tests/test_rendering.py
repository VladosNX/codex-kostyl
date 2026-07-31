from codex_gui.rendering import MarkdownRenderer


def test_markdown_renderer_escapes_raw_html_and_renders_code() -> None:
    rendered = MarkdownRenderer().render("<script>alert(1)</script>\n\n```python\nprint('ok')\n```")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "print" in rendered


def test_markdown_renderer_renders_tables() -> None:
    rendered = MarkdownRenderer().render(
        "| Файл | Статус |\n"
        "|:-----|:------:|\n"
        "| app.py | Готово |"
    )

    assert "<table>" in rendered
    assert "<th" in rendered
    assert "Файл" in rendered
    assert "<td" in rendered
    assert "app.py" in rendered
    assert "border-collapse: collapse" in rendered


def test_markdown_renderer_linkifies_urls_and_embeds_highlight_colors() -> None:
    renderer = MarkdownRenderer()
    assert '<a href="https://example.com">' in renderer.render("https://example.com")
    highlighted = renderer.render("```python\nprint('ok')\n```")
    assert "style=\"color:" in highlighted
