"""Scan user-visible console strings for words the glossary retires.

The glossary lives in docs/glossary.md. Strings that must keep a retired word
(attribution, data keys) are listed in docs/glossary-allowlist.txt, one exact
string per line; blank lines and lines starting with # are ignored.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_SOURCE = ROOT / "trace-ui" / "src"
ALLOWLIST = ROOT / "docs" / "glossary-allowlist.txt"

RETIRED = (
    "scorecard",
    "vote",
    "votes",
    "voting",
    "rationale",
    "steer",
    "steering",
    "labeling",
    "annotator",
)
RETIRED_WORD = re.compile(r"\b(" + "|".join(RETIRED) + r")\b", re.IGNORECASE)

# String literals, template literals, and JSX text between tags.
COPY_CANDIDATE = re.compile(
    r"'((?:[^'\\\n]|\\.)*)'"
    r'|"((?:[^"\\\n]|\\.)*)"'
    r"|`((?:[^`\\]|\\.)*)`"
    r"|>([^<>'\"`]+)<"
)
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)
TEMPLATE_EXPRESSION = re.compile(r"\$?\{[^{}]*\}")
CSS_TOKEN = re.compile(r"^[a-z0-9:_\[\]\-./%()#]+$")


def _allowlist() -> set[str]:
    if not ALLOWLIST.exists():
        return set()
    lines = (line.strip() for line in ALLOWLIST.read_text().splitlines())
    return {line for line in lines if line and not line.startswith("#")}


def _looks_like_css_classes(text: str) -> bool:
    tokens = text.split()
    return all(CSS_TOKEN.match(t) for t in tokens) and any("-" in t for t in tokens)


def _is_copy(text: str) -> bool:
    return len(text.split()) > 1 and not _looks_like_css_classes(text)


def retired_words_in_copy(source: str) -> list[str]:
    """Return user-visible strings in a TSX source that contain retired words."""
    source = LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", source))
    found = []
    for match in COPY_CANDIDATE.finditer(source):
        text = TEMPLATE_EXPRESSION.sub("", next(g for g in match.groups() if g is not None))
        if match.group(4) is not None and re.search(r"[;=]", text):
            continue  # TypeScript generics or comparisons, not JSX text
        text = " ".join(text.split())
        if _is_copy(text) and RETIRED_WORD.search(text):
            found.append(text)
    return found


def test_scanner_flags_retired_words_in_ui_copy() -> None:
    source = (
        "const a = <p>Your vote refers to the item</p>\n"
        "const b = 'Recorded Rationale: shown'\n"
        '<Button aria-label="Start labeling now" className="labeling-card p-3" />\n'
        "// a vote in a comment\n"
    )
    assert retired_words_in_copy(source) == [
        "Your vote refers to the item",
        "Recorded Rationale: shown",
        "Start labeling now",
    ]


def test_scanner_ignores_data_keys_and_identifiers() -> None:
    source = "const k = 'replayed-human-vote:'; const c = 'labeling-card-content space-y-5'; const d = {kind: 'vote'}"
    assert retired_words_in_copy(source) == []


def test_console_copy_uses_only_glossary_words() -> None:
    allowed = _allowlist()
    offenders = []
    for path in sorted(UI_SOURCE.rglob("*.tsx")):
        if ".test." in path.name:
            continue
        for text in retired_words_in_copy(path.read_text()):
            if text not in allowed:
                offenders.append(f"{path.relative_to(ROOT)}: {text}")
    assert not offenders, "Retired words in UI copy (see docs/glossary.md):\n" + "\n".join(offenders)
