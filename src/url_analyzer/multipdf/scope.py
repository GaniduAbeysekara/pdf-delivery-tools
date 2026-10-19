"""CSS 'scope' selectors for the multi-PDF finder.

The scope says which links count (e.g. ``#cmctree>.tlist>li:nth-of-type(1) a[data-pdf*='.pdf']``). Two things are needed from it:

* the **last compound** (``a[data-pdf*='.pdf']``) - what a matching link looks like, and which attribute carries its address;
* the **ancestor part** (``#cmctree>.tlist>li:nth-of-type(1)``) - the only region of the page that has to be opened by
  clicking. Everything outside it (e.g. Title 2 .. Title 11) is left alone, which keeps the crawl small and polite.

Selectors copied from tools are often not strict CSS. A run of ``>`` such as ``#app>>>>>>table`` (the old "deep" combinator,
or "some levels down") is read as "anywhere below" - an ordinary descendant combinator.
"""
from __future__ import annotations

import re

import soupsieve

MAX_SELECTOR = 600
_QUOTES = "\"'"


class ScopeError(ValueError):
    """The selector is not valid CSS (message is safe to show to the user)."""


def _scan(selector: str):
    """Yield (index, char, depth, quoted) for every character - depth counts [] and () nesting."""
    depth, quote = 0, ""
    for i, ch in enumerate(selector):
        if quote:
            yield i, ch, depth, True
            if ch == quote:
                quote = ""
            continue
        if ch in _QUOTES:
            quote = ch
            yield i, ch, depth, True
            continue
        if ch in "[(":
            yield i, ch, depth, False
            depth += 1
            continue
        if ch in "])":
            depth = max(0, depth - 1)
        yield i, ch, depth, False


def normalise(selector: str) -> str:
    """Strict CSS from a copied selector: a run of two or more '>' (``a>>>b``, ``a > > b``) becomes a descendant combinator."""
    sel = (selector or "").strip()
    out: list[str] = []
    i, n = 0, len(sel)
    chars = {i: (c, d, q) for i, c, d, q in _scan(sel)}
    while i < n:
        c, depth, quoted = chars[i]
        if c == ">" and depth == 0 and not quoted:
            j, count = i, 0
            while j < n:
                cj, dj, qj = chars[j]
                if cj == ">" and dj == 0 and not qj:
                    count += 1
                    j += 1
                elif cj.isspace() and dj == 0 and not qj and sel[j:].lstrip().startswith(">"):
                    j += 1
                else:
                    break
            if count >= 2:
                out.append(" ")
                i = j
                continue
        out.append(c)
        i += 1
    return re.sub(r"\s+(?=\s)", "", "".join(out)) if out else ""


def validate(selector: str, what: str = "scope") -> str:
    """The cleaned selector; raises ScopeError when it is too long or not a CSS selector soupsieve understands."""
    sel = (selector or "").strip()
    if not sel:
        return ""
    if len(sel) > MAX_SELECTOR:
        raise ScopeError(f"The {what} selector is too long (maximum {MAX_SELECTOR} characters).")
    sel = normalise(sel)
    try:
        soupsieve.compile(sel)
    except Exception as e:                       # soupsieve raises SelectorSyntaxError (a SyntaxError subclass) and others
        raise ScopeError(f"The {what} selector is not valid CSS: {str(e).splitlines()[0][:160]}") from e
    return sel


def alternatives(selector: str) -> list[str]:
    """A selector list (``a, b``) as its separate selectors."""
    parts, buf = [], []
    for _, ch, depth, quoted in _scan(selector):
        if ch == "," and depth == 0 and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def split_last_compound(selector: str) -> tuple[str, str]:
    """('ancestor part', 'last compound') of one selector; the ancestor part is '' for a single compound.
    Only combinators outside brackets, parentheses and quotes count, so ``:not(.a .b)`` and ``[title='a > b']`` stay whole."""
    sel = selector.strip()
    start = end = -1                                   # span of the last top-level combinator (whitespace and/or > + ~)
    i, n = 0, len(sel)
    marks = {i: (c, d, q) for i, c, d, q in _scan(sel)}
    while i < n:
        c, depth, quoted = marks[i]
        if depth == 0 and not quoted and (c.isspace() or c in ">+~"):
            j = i
            while j < n and marks[j][1] == 0 and not marks[j][2] and (marks[j][0].isspace() or marks[j][0] in ">+~"):
                j += 1
            if j < n:                                  # a combinator that has a compound after it
                start, end = i, j
            i = j
            continue
        i += 1
    if start < 0:
        return "", sel
    return sel[:start].strip(), sel[end:].strip()


def roots(selector: str) -> list[str]:
    """Selectors of the regions that must be opened to reveal the links. Empty list = the whole page."""
    out = []
    for alt in alternatives(selector):
        ancestor, _ = split_last_compound(alt)
        if not ancestor:
            return []                                                  # one alternative can match anywhere -> the whole page
        if ancestor not in out:
            out.append(ancestor)
    return out


def attributes(selector: str) -> list[str]:
    """Attribute names that the link selectors test (``[data-pdf*='.pdf']`` -> ``data-pdf``): they carry the address."""
    names: list[str] = []
    for alt in alternatives(selector):
        _, last = split_last_compound(alt)
        for m in re.finditer(r"\[\s*([A-Za-z_][\w:.-]*)", last):
            n = m.group(1).lower()
            if n not in names:
                names.append(n)
    return names
