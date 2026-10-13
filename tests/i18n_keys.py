"""Collect the translation keys the UI uses, straight from the sources.

Static keys come from A.t("...") calls (all string literals inside, so ternaries count),
data-i18n / data-i18n-ph / data-tip attributes, and the panel helpers in panels.js whose
arguments are keys. Dynamic families (error codes, undo labels, tool ids, ...) are listed
by hand next to the code that builds them.
"""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"

# helper name -> indexes of arguments that are translation keys (panels.js)
HELPERS = {"head": (1, 2), "field": (0, 2), "check": (1, 2), "range": (1,), "section": (0,), "btn": (1,),
           "seg": (), "kv": (0,)}

DYNAMIC = {
    # e_ + backend error codes (api.py, core/*) and the network failure in core.js
    "e_": ["NOT_FOUND", "CANCELLED", "INVALID", "FILE_NOT_FOUND", "FILE_LOCKED", "ERROR", "BAD_PAGES", "STALE",
           "NEED_PASSWORD", "WRONG_PASSWORD", "NOT_ALLOWED", "NEED_PATH", "CANNOT_DELETE_ALL", "BAD_KIND",
           "NOTHING_FOUND", "BUSY", "NOTHING_TO_OCR", "NEED_TWO", "NO_DOC", "NEED_PASSWORDS", "SAVE_AS_COPY",
           "NOT_SUPPORTED", "EMPTY_SIGNATURE", "OPEN_FILE", "SOURCE_PROTECTED", "NETWORK"],
    # op_ + undo labels (Session.edit labels in api.py; annotation kinds)
    "op_": ["rotate", "delete", "reorder", "insert", "duplicate", "crop", "watermark", "header", "metadata", "ink",
            "rect", "ellipse", "line", "arrow", "highlight", "underline", "strikeout", "squiggly", "text", "note",
            "image", "redact", "move", "resize", "edit", "scrub", "form", "flatten", "bookmarks", "attachment", "ocr"],
    # t_ + rail tools (panels.js) and annotation tools (tools.js)
    "t_": ["open", "annotate", "fillsign", "protect", "redact", "watermark", "organize", "ocr", "export", "combine",
           "info", "select", "hand", "highlight", "underline", "strikeout", "pen", "rect", "ellipse", "line", "arrow",
           "text", "note", "eraser"],
    # mode_ + modes that show a hint instead of the style row (tools.js renderStyleRow)
    "mode_": ["select", "hand", "eraser", "redact", "crop", "sigbox", "place"],
    # an_ + annotation types listed in the comments panel
    "an_": ["Ink", "Square", "Circle", "Line", "FreeText", "Text", "Highlight", "Underline", "StrikeOut", "Squiggly",
            "Stamp", "Redact", "Polygon", "PolyLine", "Caret", "FileAttachment"],
    # sig_ + signature validation statuses (core/signing.py)
    "sig_": ["valid", "untrusted", "modified", "invalid"],
    "month": [str(n) for n in range(1, 13)],
    # plain keys picked from arrays / variables: shortcut rows (app.js), export cards (panels.js),
    # savedToast message keys, the read-aloud tip
    "": ["undoRedo", "zoomInOut", "prevNextPage", "selectHand", "drawTools", "markTools", "textNoteEraser",
         "holdToPan", "deleteSelected", "cancelTool", "exDocx", "exDocxHint", "exTxt", "exTxtHint", "exImages",
         "exImagesHint", "exEmbedded", "exEmbeddedHint", "exPrint", "exPrintHint", "signedTo", "protectedTo",
         "splitDone", "exportedN", "stopReading", "dateFmt"],
    # A.plural(n, "page") -> pageOne / pageFew / pageMany
    "page": ["One", "Few", "Many"],
}

STR = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'')
KEYLIKE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _call_args(src: str, start: int) -> list[str]:
    """Split the argument list of a call whose '(' is at src[start-1]; returns raw arg texts."""
    depth, i, args, cur = 0, start, [], []
    in_str = None
    tmpl_stack: list[int] = []  # brace depth where each ${ started
    while i < len(src):
        c = src[i]
        if in_str:
            cur.append(c)
            if c == "\\":
                cur.append(src[i + 1]); i += 2; continue
            if in_str == "`" and c == "$" and src[i + 1] == "{":
                cur.append("{"); i += 2; tmpl_stack.append(depth); depth += 1; in_str = None; continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in "\"'`":
            in_str = c; cur.append(c); i += 1; continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if c == "}" and tmpl_stack and tmpl_stack[-1] == depth - 1:
                tmpl_stack.pop(); depth -= 1; cur.append(c); in_str = "`"; i += 1; continue
            if depth == 0:
                args.append("".join(cur)); return args
            depth -= 1
        elif c == "," and depth == 0:
            args.append("".join(cur)); cur = []; i += 1; continue
        cur.append(c)
        i += 1
    return args


def _literal(arg: str) -> str | None:
    m = re.fullmatch(r'\s*"([^"]*)"\s*|\s*\'([^\']*)\'\s*', arg)
    return (m.group(1) if m.group(1) is not None else m.group(2)) if m else None


def static_keys() -> dict[str, set[str]]:
    """key -> set of files using it."""
    out: dict[str, set[str]] = {}

    def add(k, f):
        if k and KEYLIKE.match(k):
            out.setdefault(k, set()).add(f)

    files = sorted((WEB / "js").glob("*.js")) + [WEB / "index.html"]
    for path in files:
        src = path.read_text(encoding="utf-8")
        name = path.name
        if name == "i18n.js":
            continue
        for m in re.finditer(r"\bA\.t\(", src):
            args = _call_args(src, m.end())
            if not args:
                continue
            first = args[0]
            if re.search(r'["\']\s*\+', first):  # dynamic prefix like "e_" + code: listed in DYNAMIC
                continue
            for s in STR.finditer(first):
                before, after = first[:s.start()].rstrip(), first[s.end():].lstrip()
                if before.endswith(("===", "!==")) or after.startswith(("===", "!==")):
                    continue  # compared value, not a key
                add(s.group(1) if s.group(1) is not None else s.group(2), name)
        for m in re.finditer(r'data-(?:i18n|i18n-ph|tip)="([^"$]+)"', src):
            add(m.group(1), name)
        if name == "panels.js":
            for fn, idxs in HELPERS.items():
                for m in re.finditer(rf"(?<![\w.]){fn}\(", src):
                    if src[max(0, m.start() - 9):m.start()].endswith("function ") or src[m.start() - 6:m.start()] == "const ":
                        continue
                    args = _call_args(src, m.end())
                    for k in idxs:
                        if k < len(args):
                            add(_literal(args[k]), name)
                    if fn == "seg" and len(args) > 1:
                        for pair in re.finditer(r'\[\s*"[^"]*"\s*,\s*"([^"]*)"\s*\]', args[1]):
                            add(pair.group(1), name)
            for m in re.finditer(r"pagesField\(", src):
                add("pagesRange", name)
                add("pagesPh", name)
    for prefix, items in DYNAMIC.items():
        for it in items:
            out.setdefault(prefix + it, set()).add("dynamic")
    return out


# labels that are shown as-is (language names, formats) and need no translation
LITERAL_LABELS = {"Srpski", "English", "Norsk", "PNG", "JPG"}


def needed_keys() -> set[str]:
    return {k for k in static_keys() if k not in LITERAL_LABELS}


def defined_rows() -> list[list[str]]:
    """Rows of web/js/i18n.js as [key, sr, en, no] (parsed, not executed)."""
    src = (WEB / "js" / "i18n.js").read_text(encoding="utf-8")
    body = src[src.index("const ROWS = ["):src.index("];")]
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        cells = [m.group(1) for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', line)]
        rows.append(cells)
    return rows


if __name__ == "__main__":
    keys = static_keys()
    for k in sorted(keys):
        print(f"{k:28s} {', '.join(sorted(keys[k]))}")
    print(len(keys), "keys")
