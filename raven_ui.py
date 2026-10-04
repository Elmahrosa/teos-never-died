"""Shared presentation layer for RAVEN's Telegram surfaces.

Every panel the bot renders is built here so the bot reads as one product rather
than a pile of unrelated strings. Three rules shaped this module:

* **Honest provenance.** Footers state where the data actually came from. No
  panel claims verification that did not happen - a security tool that asserts
  "verified" over unverified figures is worse than one that says nothing.

* **Fixed geometry.** Telegram renders proportional-width Unicode poorly and
  box-drawing characters drift between clients, so panels use a fixed 34-column
  inner width, which is what the rest of the codebase already assumed.

* **No Markdown dependency.** These render as plain text inside code fences.
  Legacy Telegram Markdown has no bold and no links, so panels that need
  emphasis use block characters instead of `**`. That also means raw AI output
  dropped inside a panel cannot break the surrounding layout.

Plain text, not Markdown, is deliberate: `send_long` falls back to an unparsed
retry whenever a model emits a stray `_` or `*`, and a half-rendered panel with
visible asterisks looks broken. Code fences survive that fallback intact.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

# Inner width for every panel. Matches the pre-existing convention in the bot.
WIDTH = 34

# ── Glyphs ────────────────────────────────────────────────────────────────
RULE = "─" * WIDTH
TICK = "✓"
CROSS = "✗"
DOT = "•"
ARROW = "›"
LOCK = "🔒"

BAR_FULL = "█"
BAR_EMPTY = "░"

TIER_ICON = {
    "free": "🟢",
    "pro": "🔵",
    "enterprise": "🟣",
}

# Feature keys, in display order, with the glyph each gets in a matrix.
FEATURES: Sequence[Tuple[str, str]] = (
    ("text", "Text chat"),
    ("scan", "Security scans"),
    ("image", "Image analysis"),
    ("document", "Document analysis"),
    ("video", "Video analysis"),
    ("translate", "Translation"),
    ("voice", "Voice replies"),
)


def esc(value: object) -> str:
    """Render a value for display inside a panel.

    Panels are shown inside code fences, where a stray backtick would close the
    fence early and dump raw formatting into the chat. Model output and
    user-supplied targets both pass through here.
    """
    text = "" if value is None else str(value)
    # Tabs are flattened too: a tab inside a code fence renders as a variable
    # number of columns and breaks the alignment every panel depends on.
    return (text.replace("`", "'")
                .replace("\r", " ")
                .replace("\t", " "))


def header(icon: str, title: str, subtitle: str = "") -> List[str]:
    """Standard panel header: icon, title, optional right-aligned subtitle."""
    lines = [f"{icon} {esc(title).upper()}"]
    if subtitle:
        lines.append(esc(subtitle))
    lines.append(RULE)
    return lines


def kv(label: str, value: object, width: int = 11) -> str:
    """One aligned key/value row."""
    return f" {esc(label)[:width]:<{width}} {esc(value)}"


def kv_row(label: str, value: object) -> str:
    """Compact key/value row using the panel's bullet style."""
    return f" {DOT} {esc(label):<9} {esc(value)}"


def section(title: str) -> List[str]:
    """An in-panel subheading."""
    return ["", f" {esc(title).upper()}"]


def progress_bar(used: int, limit: int, width: int = 18) -> str:
    """A metered-usage bar.

    `limit` of UNLIMITED (-1) renders as a full bar plus the word "unlimited",
    which is honest: the caller has not set a ceiling, so no partial fill would
    imply one exists.
    """
    if limit is None or limit == -1:
        return f"{BAR_FULL * width}  unlimited"
    limit = max(0, int(limit))
    used = max(0, int(used))
    if limit == 0:
        return f"{BAR_EMPTY * width}  none included"
    filled = int(round(width * min(used, limit) / limit))
    return f"{BAR_FULL * filled}{BAR_EMPTY * (width - filled)}  {used}/{limit}"


def feature_matrix(included: Iterable[str]) -> List[str]:
    """Two-column tick/cross grid of available features."""
    have = set(included)
    rows: List[str] = []
    items = [(label, key in have) for key, label in FEATURES]
    half = (len(items) + 1) // 2
    left, right = items[:half], items[half:]
    for i in range(half):
        lk, lv = left[i]
        cell_l = f"{TICK if lv else CROSS} {lk}"
        if i < len(right):
            rk, rv = right[i]
            cell_r = f"{TICK if rv else CROSS} {rk}"
            rows.append(f"  {cell_l:<22} {cell_r}")
        else:
            rows.append(f"  {cell_l}")
    return rows


def notice(text: str, icon: str = "⚠") -> str:
    """A standalone warning line."""
    return f"{icon} {esc(text)}"


def footer(source: str, disclaimer: str = "") -> List[str]:
    """Provenance line, optionally followed by a disclaimer.

    `source` must describe where the data genuinely came from. Do not pass a
    reassurance string here; the whole point of this helper is that the footer
    is a claim the code can actually back up.
    """
    lines = [RULE, f"{DOT} {esc(source)}"]
    if disclaimer:
        lines.append(esc(disclaimer))
    return lines


def panel(lines: Sequence[str]) -> str:
    """Wrap rendered lines in a code fence for stable monospace display.

    Every line is escaped on the way in. Dynamic content - model output, user
    targets, exchange symbols - reaches this function from untrusted sources,
    and a single stray backtick in any of them would close the fence early and
    dump the rest of the panel into the chat as broken formatting.
    """
    body = "\n".join(esc(line) for line in lines)
    return f"```\n{body}\n```"


def table(headers: Sequence[str], rows: Sequence[Sequence[object]],
          aligns: Optional[Sequence[str]] = None) -> List[str]:
    """Render a compact aligned table.

    Column widths come from the content, not a guess, so a long symbol name
    cannot push a price out of alignment the way fixed padding would.
    """
    cols = len(headers)
    aligns = list(aligns or ["l"] * cols)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(esc(row[i])))

    def fmt(cells: Sequence[object], bold: bool = False) -> str:
        out = []
        for i in range(cols):
            text = esc(cells[i])
            pad = widths[i] - len(text)
            if aligns[i] == "r":
                out.append(" " * pad + text)
            else:
                out.append(text + " " * pad)
        return " " + "  ".join(out).rstrip()

    lines = [fmt(headers, bold=True), " " + "  ".join("─" * w for w in widths)]
    for row in rows:
        lines.append(fmt(row))
    return lines