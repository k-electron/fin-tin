"""Index-constituent parsing — the pure core (AD-1, AD-2).

Turns a constituent CSV (the S&P 500 list, fetched by an adapter) into tickers
plus the CIKs the source supplied. Pure: it takes *text*, never a URL — the HTTP
fetch is an injected port in `fintin.adapters.constituents`, so this module is
network-free and unit-testable with a literal string.

Nothing here is persisted (AD-1). The command that uses this materializes a
`[universe]` block for the operator to keep in `fintin.toml`; the Universe is
still derived from that config on every run.

The CIK column is optional and used as a **completeness backstop**: a symbol that
edgartools' bundled reference table cannot resolve offline still has a CIK here,
so it can be carried into `[universe].ciks` instead of becoming a gap. Symbols are
kept verbatim (normalization to the lookup-key form is resolve-time work, shared
via `fintin.core.universe.normalize_ticker`).
"""

from __future__ import annotations

import csv
import io
from typing import NamedTuple

# Accepted header spellings, lower-cased. Sources vary ("Symbol" vs "Ticker").
_SYMBOL_HEADERS = ("symbol", "ticker")
_CIK_HEADERS = ("cik",)

# A CIK is a UInt32 in the store; mirror the config guard rather than importing it
# (keeps this module dependency-free within core).
_CIK_MAX = 4_294_967_295


class Constituent(NamedTuple):
    """One index member: its ticker verbatim, plus the source's CIK when given."""

    ticker: str
    cik: int | None


class ConstituentList(NamedTuple):
    """Parsed constituents plus every row the parser refused, explained.

    ``skipped`` is never silently dropped — the caller reports it (SM-2), so a
    source that changes shape is visible rather than quietly yielding a short
    Universe."""

    constituents: tuple[Constituent, ...]
    skipped: tuple[str, ...]

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(c.ticker for c in self.constituents)

    @property
    def with_cik(self) -> tuple[Constituent, ...]:
        return tuple(c for c in self.constituents if c.cik is not None)


def _column(fieldnames: list[str] | None, candidates: tuple[str, ...]) -> str | None:
    """The first header matching ``candidates``, case/space-insensitively."""
    for name in fieldnames or []:
        if name is not None and name.strip().lower() in candidates:
            return name
    return None


def parse_constituents_csv(text: str) -> ConstituentList:
    """Parse constituent CSV text into tickers (+ CIKs where supplied). Pure.

    Requires a symbol column (``Symbol`` or ``Ticker``); a ``CIK`` column is used
    when present. Duplicate symbols collapse to their first occurrence, preserving
    source order so the emitted list is deterministic. A row with a blank symbol,
    or an unparseable/out-of-range CIK, is recorded in ``skipped`` — a malformed
    CIK degrades that row to ticker-only rather than discarding the company.

    Raises :class:`ValueError` if there is no symbol column at all — that means
    the source changed shape (or an error page was fetched), and guessing would
    silently produce an empty Universe.
    """
    reader = csv.DictReader(io.StringIO(text))
    symbol_col = _column(reader.fieldnames, _SYMBOL_HEADERS)
    if symbol_col is None:
        found = ", ".join(reader.fieldnames or []) or "(no header row)"
        raise ValueError(
            "constituent CSV has no Symbol/Ticker column — the source may have "
            f"changed shape or returned an error page. Columns found: {found}"
        )
    cik_col = _column(reader.fieldnames, _CIK_HEADERS)

    constituents: list[Constituent] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for row_no, row in enumerate(reader, start=2):  # row 1 is the header
        raw_symbol = (row.get(symbol_col) or "").strip()
        if not raw_symbol:
            skipped.append(f"row {row_no}: blank symbol")
            continue
        key = raw_symbol.upper()
        if key in seen:
            continue  # a repeated listing (e.g. dual share classes) is not an error
        seen.add(key)

        cik: int | None = None
        if cik_col is not None:
            raw_cik = (row.get(cik_col) or "").strip()
            if raw_cik:
                try:
                    parsed = int(raw_cik)
                except ValueError:
                    skipped.append(f"{raw_symbol}: unparseable CIK {raw_cik!r}")
                else:
                    if 1 <= parsed <= _CIK_MAX:
                        cik = parsed
                    else:
                        skipped.append(f"{raw_symbol}: CIK {parsed} out of range")

        constituents.append(Constituent(ticker=raw_symbol, cik=cik))

    return ConstituentList(constituents=tuple(constituents), skipped=tuple(skipped))


def replace_universe_section(toml_text: str, block: str) -> str:
    """Return ``toml_text`` with its ``[universe]`` section replaced by ``block``.

    Pure string surgery — stdlib ``tomllib`` reads TOML but cannot write it, and
    pulling in a round-tripping TOML library to rewrite one array is not worth the
    dependency. The section runs from the ``[universe]`` header to the next
    top-level header (a line starting with ``[`` in column 0) or end of file;
    everything outside it, including other sections and their comments, is
    untouched.

    Two consequences the caller must surface: **comments inside the
    ``[universe]`` section are replaced along with it**, and a config whose array
    elements start at column 0 with ``[`` (a nested array — not something this
    config uses) would confuse the boundary scan. The caller writes a ``.bak``
    first for exactly that reason.

    Raises :class:`ValueError` if there is no ``[universe]`` section, so the
    caller can append instead of silently producing a config with none.
    """
    lines = toml_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "[universe]":
            start = i
            break
    if start is None:
        raise ValueError("no [universe] section found")

    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j]
        if stripped.startswith("[") and stripped.rstrip().endswith("]"):
            # A top-level header in column 0 ends the section. Array element lines
            # in this config are indented, so they don't match.
            end = j
            break

    replacement = block.splitlines()
    # Keep exactly one blank line before the following section, if there is one.
    if end < len(lines) and (not replacement or replacement[-1] != ""):
        replacement = [*replacement, ""]
    return "\n".join([*lines[:start], *replacement, *lines[end:]]) + "\n"


def render_universe_block(
    tickers: tuple[str, ...], ciks: tuple[int, ...] = (), *, per_line: int = 8
) -> str:
    """Render a paste-ready ``[universe]`` TOML block. Pure string formatting.

    Wrapped at ``per_line`` tickers so the result stays readable (and diffs
    sanely) in a hand-edited config."""
    lines = ["[universe]", "tickers = ["]
    for start in range(0, len(tickers), per_line):
        chunk = tickers[start : start + per_line]
        lines.append("    " + " ".join(f'"{t}",' for t in chunk))
    lines.append("]")
    if ciks:
        lines.append("ciks = [")
        for start in range(0, len(ciks), per_line):
            chunk = ciks[start : start + per_line]
            lines.append("    " + " ".join(f"{c}," for c in chunk))
        lines.append("]")
    return "\n".join(lines)
