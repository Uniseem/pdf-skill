"""Strict Markdown validation and normalisation.

Pipeline (all pure Python):

1. Parse with markdown-it (CommonMark + GFM tables + strict ``$``/``$$`` math).
2. Check structural invariants on the token stream: one H1 that opens the
   document, no heading-level jumps, no raw HTML, no indented code, no
   unescaped ``$`` in text, equal cell counts in every table row.
3. Validate every formula with KaTeX 0.18.9 (vendored, run in dukpy).
4. Normalise with mdformat (gfm + a strict math plugin) and prove the rendered
   HTML did not change and that formatting is idempotent.
5. Lint a math-masked copy with pymarkdownlnt (markdownlint rule set).
"""

from __future__ import annotations

import functools
import logging
import re
import types
from dataclasses import dataclass, field
from pathlib import Path

import mdformat
import mdformat.plugins
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin

KATEX_JS = Path(__file__).parent / "_vendor" / "katex" / "katex.min.js"
DOLLAR_OPTS = dict(allow_labels=False, allow_space=False, allow_digits=False, double_inline=True)
MD_EXT = {"gfm", "pdfskill_math"}
MD_OPTS = {"number": True, "wrap": "keep", "end_of_line": "lf"}
HEADING_TRAILING_PUNCT = ".,;:!。，；：！"


def _update(mdit: MarkdownIt) -> None:
    mdit.use(dollarmath_plugin, **DOLLAR_OPTS)


def _escape_dollars(text: str, node, ctx) -> str:
    return text.replace("$", "\\$")


def _fence(s: str) -> str:
    n = max([len(m) for m in re.findall(r"`+", s)] + [0]) + 1
    return "`" * n


mdformat.plugins.PARSER_EXTENSIONS["pdfskill_math"] = types.SimpleNamespace(
    update_mdit=_update,
    RENDERERS={"math_inline": lambda n, c: f"${n.content}$", "math_block": lambda n, c: f"$${n.content}$$"},
    POSTPROCESSORS={"text": _escape_dollars},
)
mdformat.plugins.PARSER_EXTENSIONS["pdfskill_mathmask"] = types.SimpleNamespace(
    update_mdit=_update,
    RENDERERS={
        "math_inline": lambda n, c: (f := _fence(n.content)) + "m" * max(1, len(n.content)) + f,
        "math_block": lambda n, c: "```math\n" + n.content.strip("\n") + "\n```",
    },
    POSTPROCESSORS={"text": _escape_dollars},
)


@functools.lru_cache(maxsize=1)
def parser() -> MarkdownIt:
    return (
        MarkdownIt("commonmark", {"html": True})
        .enable(["table", "strikethrough"])
        .use(dollarmath_plugin, **DOLLAR_OPTS)
        .disable("text_join")
    )


# -- KaTeX ---------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _katex():
    import dukpy

    it = dukpy.JSInterpreter()
    it.evaljs(
        KATEX_JS.read_text(encoding="utf-8")
        + """
      ;function pdfskillCheck(s, d, st) {
        try { katex.renderToString(s, {displayMode: d, throwOnError: true, strict: st,
              trust: false, output: "mathml", maxExpand: 1000}); return ""; }
        catch (e) { return String(e.message || e); }
      }"""
    )
    return it


def katex_error(latex: str, display: bool, strict: str = "ignore") -> str | None:
    """Return KaTeX's error message, or None when the formula renders."""
    msg = _katex().evaljs("pdfskillCheck(dukpy.s, dukpy.d, dukpy.st)", s=latex, d=display, st=strict)
    return msg or None


_LATEX_FIXES = [
    (re.compile(r"\\label\{[^{}]*\}"), ""),
    (re.compile(r"\\(?:nonumber|notag)\b"), ""),
    (re.compile(r"\\hdots\b"), r"\\cdots"),
    (re.compile(r"\\bm\{"), r"\\boldsymbol{"),
    (re.compile(r"\\mathbbm\{"), r"\\mathbb{"),
    (re.compile(r"\\textsuperscript\{"), r"^{"),
    (re.compile(r"\\textsubscript\{"), r"_{"),
]


def fix_latex(tex: str) -> str:
    """Cheap, meaning-preserving rewrites for commands KaTeX lacks."""
    for rx, repl in _LATEX_FIXES:
        tex = rx.sub(repl, tex)
    return tex.strip()


def iter_math(md: str):
    """Yield ``(content, display)`` for every math token in ``md``."""

    def walk(tokens):
        for t in tokens:
            if t.type in {"math_inline", "math_block", "math_inline_double"}:
                yield t.content, t.type != "math_inline"
            if t.children:
                yield from walk(t.children)

    yield from walk(parser().parse(md))


# -- validation ------------------------------------------------------------------------------------


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def _table_rows(md: str):
    in_fence = False
    for i, line in enumerate(md.splitlines(), 1):
        if re.match(r"^ {0,3}(```|~~~)", line):
            in_fence = not in_fence
        if in_fence:
            continue
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        s = re.sub(r"`[^`]*`", "", s)
        s = s.replace("\\|", "")
        yield i, s.count("|") - 1


def format_markdown(md: str) -> str:
    return mdformat.text(md, extensions=MD_EXT, options=MD_OPTS)


def _html(md: str) -> str:
    return parser().render(md)


def validate(md: str, *, lint: bool = True, katex_strict: str = "ignore") -> tuple[str, Report]:
    """Validate and normalise; return ``(formatted_markdown, report)``."""
    rep = Report()
    tokens = parser().parse(md)
    first = next((t for t in tokens if t.level == 0 and t.nesting >= 0), None)
    if first is None or first.type != "heading_open" or first.tag != "h1":
        rep.errors.append("document must start with an H1")
    last_level, h1_count = 0, 0
    for t in tokens:
        if t.type == "heading_open":
            lvl = int(t.tag[1])
            h1_count += lvl == 1
            if last_level and lvl > last_level + 1:
                rep.errors.append(f"L{t.map[0] + 1}: heading jumps h{last_level}->h{lvl}")
            last_level = lvl
        if t.type == "html_block":
            rep.errors.append(f"L{t.map[0] + 1}: raw HTML block")
        if t.type == "code_block":
            rep.errors.append(f"L{t.map[0] + 1}: indented code block")
    if h1_count > 1:
        rep.errors.append(f"{h1_count} H1 headings (expected exactly one)")

    def walk(toks):
        for t in toks:
            if t.type in {"math_inline", "math_block", "math_inline_double"}:
                err = katex_error(t.content, t.type != "math_inline", katex_strict)
                if err:
                    rep.errors.append(f"math {t.content[:60]!r}: {err[:160]}")
            elif t.type == "text" and "$" in t.content:
                rep.errors.append(f"unescaped '$' in text: {t.content[:60]!r}")
            elif t.type == "html_inline":
                rep.warnings.append(f"inline HTML {t.content[:40]!r}")
            if t.children:
                walk(t.children)

    walk(tokens)

    header_cells, prev = None, -1
    for ln, n in _table_rows(md):
        if ln != prev + 1:
            header_cells = n
        elif n != header_cells:
            rep.errors.append(f"L{ln}: table row has {n} cells, header has {header_cells}")
        prev = ln

    formatted = format_markdown(md)
    if _html(md) != _html(formatted):
        rep.errors.append("normalisation changed the rendered HTML")
    if format_markdown(formatted) != formatted:
        rep.errors.append("normalisation is not idempotent")

    if lint:
        for msg in lint_markdown(formatted):
            rep.errors.append(msg)
    return formatted, rep


def lint_markdown(md: str) -> list[str]:
    from pymarkdown.api import PyMarkdownApi

    masked = mdformat.text(md, extensions={"gfm", "pdfskill_mathmask"}, options=MD_OPTS)
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:  # PyMarkdownApi attaches a stdout handler to the root logger; keep stdout clean
        api = (
            PyMarkdownApi()
            .enable_extension_by_identifier("markdown-tables")
            .enable_extension_by_identifier("markdown-strikethrough")
            .disable_rule_by_identifier("md013")
            .set_boolean_property("plugins.md024.siblings_only", True)
            .set_string_property("plugins.md033.allowed_elements", "")
        )
        failures = api.scan_string(masked).scan_failures
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    out = []
    for f in failures:
        extra = f" {f.extra_error_information}" if f.extra_error_information else ""
        out.append(f"lint L{f.line_number}: {f.rule_id} {f.rule_name}{extra}")
    return out


def clean_heading_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    while text and text[-1] in HEADING_TRAILING_PUNCT:
        text = text[:-1].rstrip()
    return text or "Untitled"
