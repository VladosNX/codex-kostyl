from __future__ import annotations

import html

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound


class MarkdownRenderer:
    def __init__(self) -> None:
        self._formatter = HtmlFormatter(nowrap=True)
        self._md = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": True})
        self._md.enable("table")
        self._md.options["highlight"] = self._highlight

    def _highlight(self, code: str, language: str, _attrs: str = "") -> str:
        try:
            lexer = get_lexer_by_name(language) if language else TextLexer()
        except ClassNotFound:
            lexer = TextLexer()
        return highlight(code, lexer, self._formatter)

    def render(self, text: str) -> str:
        body = self._md.render(text)
        return f"""
        <style>
          body {{ color: #e2e4df; font-family: sans-serif; font-size: 14px; line-height: 1.45; word-wrap: break-word; }}
          p {{ margin: 5px 0 8px 0; }}
          h1, h2, h3 {{ color: #f0f1ed; margin: 14px 0 7px 0; }}
          pre {{ background: #161818; border: 1px solid #303432; border-radius: 7px; padding: 11px; white-space: pre-wrap; word-wrap: break-word; }}
          code {{ color: #dfe5df; background: #282c2a; padding: 1px 4px; word-wrap: break-word; }}
          a {{ color: #a8c7b1; text-decoration: none; }}
          blockquote {{ color: #a8ada8; border-left: 3px solid #54655a; margin-left: 3px; padding-left: 11px; }}
          li {{ margin: 3px 0; }}
          table {{ border-collapse: collapse; margin: 9px 0 12px 0; }}
          th, td {{ border: 1px solid #424844; padding: 6px 9px; vertical-align: top; }}
          th {{ color: #f0f1ed; background: #292d2b; font-weight: 600; }}
          td {{ color: #d6d9d4; background: #1c1f1e; }}
        </style>{body}
        """


def plain_pre(text: str) -> str:
    return f"<pre style='white-space:pre-wrap; word-wrap:break-word; color:#bdc3bd; font-family:monospace; font-size:12px'>{html.escape(text)}</pre>"
