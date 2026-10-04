"""Tests for the shared presentation layer in raven_ui.

These guard the properties the bot's panels depend on, most importantly that
dynamic content cannot break the code fence around a panel. Model output and
user-supplied targets both flow into these renderers, and a single stray
backtick in either would dump the rest of a panel into the chat as broken
formatting.

Run with:  pytest tests/test_ui.py -v
"""

import pytest

import raven_ui as ui

FENCE = "`" * 3


def body_of(rendered: str) -> str:
    """The panel's contents, without the fence delimiters."""
    return rendered.split("\n", 1)[1].rsplit("\n", 1)[0]


# ---------------------------------------------------------------------------
# Escaping: the property that matters most
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "price `tick` here",
    "```",
    "`",
    "``````",
    "model said ``` and then kept going",
])
def test_backticks_never_reach_the_panel_body(payload):
    assert "`" not in body_of(ui.panel(ui.footer(payload)))


@pytest.mark.parametrize("payload", ["a\rb", "line\nbreak", "\ttab", "trailing "])
def test_control_characters_are_flattened(payload):
    out = ui.esc(payload)
    assert "\r" not in out
    assert "\t" not in out


def test_esc_handles_none_and_numbers():
    assert ui.esc(None) == ""
    assert ui.esc(0) == "0"
    assert ui.esc(1234) == "1234"


def test_panel_is_fenced_exactly_once():
    rendered = ui.panel(["hello"])
    assert rendered.startswith(FENCE)
    assert rendered.endswith(FENCE)
    assert rendered.count(FENCE) == 2


def test_panel_with_empty_lines():
    assert body_of(ui.panel(["a", "", "b"])) == "a\n\nb"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def test_header_shape():
    lines = ui.header("X", "title", "subtitle")
    assert lines[0] == "X TITLE"
    assert lines[1] == "subtitle"
    assert lines[2] == ui.RULE


def test_header_without_subtitle_has_no_blank_line():
    lines = ui.header("X", "title")
    assert len(lines) == 2
    assert lines[-1] == ui.RULE


def test_section_is_blank_line_then_uppercase():
    assert ui.section("today") == ["", " TODAY"]


def test_footer_appends_provenance_and_disclaimer():
    lines = ui.footer("HIBP v3", "Not advice.")
    assert lines[-2] == f"{ui.DOT} HIBP v3"
    assert lines[-1] == "Not advice."


def test_footer_without_disclaimer():
    assert len(ui.footer("src")) == 2


# ---------------------------------------------------------------------------
# Metered usage
# ---------------------------------------------------------------------------

def test_progress_bar_empty_allocation():
    assert ui.progress_bar(0, 0).endswith("none included")


@pytest.mark.parametrize("limit", [-1, None])
def test_progress_bar_unlimited_is_labelled(limit):
    assert "unlimited" in ui.progress_bar(5, limit)


def test_progress_bar_partial_fill():
    bar, _, _ = ui.progress_bar(5, 10).partition("  ")
    assert bar.count(ui.BAR_FULL) == 9
    assert bar.count(ui.BAR_EMPTY) == 9


def test_progress_bar_full():
    bar, _, _ = ui.progress_bar(10, 10).partition("  ")
    assert ui.BAR_EMPTY not in bar


def test_progress_bar_over_limit_does_not_overflow():
    """Over-quota usage must not draw a bar wider than the track."""
    bar = ui.progress_bar(999, 10).partition("  ")[0]
    assert len(bar) == 18


def test_progress_bar_negative_usage_is_clamped():
    assert ui.progress_bar(-5, 10).endswith("0/10")


@pytest.mark.parametrize("width", [8, 18, 30])
def test_progress_bar_width_is_honoured(width):
    bar = ui.progress_bar(3, 10, width=width).partition("  ")[0]
    assert len(bar) == width


# ---------------------------------------------------------------------------
# Feature matrix
# ---------------------------------------------------------------------------

def test_matrix_lists_every_feature_exactly_once():
    rows = ui.feature_matrix(set())
    assert sum(r.count(ui.CROSS) for r in rows) == len(ui.FEATURES)
    assert sum(r.count(ui.TICK) for r in rows) == 0


def test_matrix_all_included_has_no_crosses():
    rows = ui.feature_matrix({key for key, _ in ui.FEATURES})
    assert sum(r.count(ui.TICK) for r in rows) == len(ui.FEATURES)
    assert ui.CROSS not in "".join(rows)


def test_matrix_mixed_counts_both_glyphs():
    rows = ui.feature_matrix({"text", "video"})
    assert sum(r.count(ui.TICK) for r in rows) == 2
    assert sum(r.count(ui.CROSS) for r in rows) == len(ui.FEATURES) - 2


def test_matrix_row_count_is_ceiling_of_half():
    rows = ui.feature_matrix(set())
    assert len(rows) == -(-len(ui.FEATURES) // 2)


def test_matrix_ignores_unknown_capability_keys():
    """An unrecognised key must not render a phantom feature."""
    rows = ui.feature_matrix({"text", "api_access", "telepathy"})
    assert sum(r.count(ui.TICK) for r in rows) == 1


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def test_table_separator_matches_column_widths():
    rows = ui.table(["Symbol", "Last"], [["BTC/USDT", "1"]])
    assert rows[1].count(ui.RULE[0]) == 8 + 4  # width of each column


def test_table_right_align():
    rows = ui.table(["A", "B"], [["x", "1"]], aligns=["l", "r"])
    assert rows[2].rstrip().endswith("1")


def test_table_escapes_cells():
    rows = ui.table(["A"], [["`"]])
    assert "`" not in rows[2]


def test_table_empty_rows_still_renders_header():
    rows = ui.table(["A", "B"], [])
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Composite panel: the shape the bot actually ships
# ---------------------------------------------------------------------------

def test_full_panel_renders_without_stray_backticks():
    lines = ui.header("\U0001F7E2", "Free plan")
    lines += ui.section("Today")
    lines += [f"  Scans  {ui.progress_bar(5, 5)}",
              f"  Media  {ui.progress_bar(0, 0)}"]
    lines += ui.section("Features")
    lines += ui.feature_matrix({"text", "translate"})
    lines += ui.footer("Local Ollama")

    rendered = ui.panel(lines)
    assert "`" not in body_of(rendered)
    assert rendered.startswith(FENCE)
    assert "unlimited" not in rendered  # Free tier has real ceilings


def test_every_tier_panel_is_fence_safe():
    """Render all three real tier configs the way the bot does."""
    import database
    import saas_manager as saas

    for tier in (database.TIER_FREE, database.TIER_PRO, database.TIER_ENTERPRISE):
        cfg = saas.tier_config(tier)
        lines = ui.header(ui.TIER_ICON[tier], cfg["label"], cfg["blurb"])
        lines += ui.section("Today")
        lines += [f"  Scans  {ui.progress_bar(0, cfg['daily_scans'])}",
                  f"  Media  {ui.progress_bar(0, cfg['daily_media'])}"]
        lines += ui.section("Features")
        lines += ui.feature_matrix(cfg["capabilities"])
        lines += ui.footer("Local Ollama + HIBP v3", "Not financial advice.")

        rendered = ui.panel(lines)
        assert "`" not in body_of(rendered), tier
        # Every configured cap is finite, so "unlimited" must never appear.
        assert "unlimited" not in rendered, tier