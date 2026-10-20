"""
inventory_biometrics.py

One-off reconnaissance script for the lab/bloodwork PDFs the owner has
dropped into data/medical/_inbox/. Extracts discrete lab-result rows (a
test-name label, a value, a unit, optionally a reference range) and writes
an inventory report to data/medical/_inventory_report.md: every distinct
label variant found (alias-table candidates -- the same test often appears
under different names across providers), how many files/occurrences each
had, and a rough grouping of which files share a layout (a proxy for how
many distinct provider-format parsers this will eventually need).

This is reconnaissance only -- nothing is written to data/measurements/ or
any canonical store, and no downstream code reads this report's output yet.

--- Medical data handling (see CLAUDE.md for the full section) ---

- Structural extraction only, never narrative. Any row that doesn't get
  linked into a complete label+value(+unit)(+range) match is discarded
  immediately -- not logged, not counted individually, not held in memory
  past the check that rejected it. Physician notes, impressions, diagnosis
  codes, and any other free text are out of scope by construction.
- Terminal output stays minimal by design: file/row/label counts, label
  text (test names are safe to share -- see CLAUDE.md's "summarize by test
  name and count, not by value"), and one example value per distinct label
  (explicitly scoped to sanity-check parsing, not a per-instance dump).
  The full per-match detail, including the raw source rows for each match
  for confirm-against-source review, lives only in the gitignored report
  file.
- Source PDFs are never modified or deleted by this script. Discard-after-
  confirm (per CLAUDE.md) is a manual, owner-driven step that happens once
  the report has been reviewed against the source PDFs -- not something
  this script does automatically.

--- Extraction approach ---

pdfplumber's extract_tables() found nothing usable (no real ruling-line
grid). Word-to-word spacing within a visual line is also useless as a
column-boundary signal here (gaps are ~2pt everywhere, label or not).

What actually recovers the structure: these are patient-portal-style
flowsheet exports where each lab result spans SEVERAL separate visual
lines, not one row -- typically:
    <TEST NAME>
    Normal range: <low> - <high> <unit>
    <value>
    <trend/history numbers>
...repeated down the page, often in two side-by-side column blocks. A
second document format found here (a "results letter" style export)
mixes a similar "<value> ... Resulted on <date> ... Reference Range ...
<low> - <high>" pattern with narrative comments alongside it.

So extraction works at the ROW level (words on one page grouped by
y-position, sorted by x-position, joined into a single text line), then
scans for a row matching a numeric reference-range shape as an anchor,
and links the nearest not-yet-used label-shaped row and value-shaped row
to it. Anything that doesn't get linked this way is discarded, whatever
it is -- that's what keeps narrative content (which never matches the
range-anchor shape) out.

Usage:
    python inventory_biometrics.py
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INBOX_DIR = REPO_ROOT / "data" / "medical" / "_inbox"
REPORT_PATH = REPO_ROOT / "data" / "medical" / "_inventory_report.md"

ROW_TOP_TOLERANCE = 3  # y-distance (pt) within which words are treated as the same row
BLOCK_GAP_THRESHOLD = 60  # x-gap (pt) beyond which a page is treated as two side-by-side column blocks
NEARBY_ROWS = 3  # how many rows above/below an anchor to search for its label/value

# Lines containing any of these are administrative/header/footer noise, not
# lab results, even though some could otherwise pass a shape check.
NOISE_RE = re.compile(
    r"\b(patient|dob|date of birth|physician|ordered|collected|received|"
    r"reported|specimen|accession|account|mrn|page \d+ of|fasting|"
    r"ordering|reviewing|authorized|comments?)\b",
    re.IGNORECASE,
)

BARE_VALUE_RE = re.compile(r"^[<>]?\s*\d+\.?\d*\s*%?\s*(H|L|HH|LL|A)?$", re.IGNORECASE)
BARE_RANGE_RE = re.compile(r"^[<>]?\s*\d+\.?\d*\s*-\s*\d+\.?\d*\s*([A-Za-z0-9/*^%.]{0,15})?$")
EMBEDDED_RANGE_RE = re.compile(
    r"(?:range|resulted)\D{0,20}?(?P<low>[<>]?\d+\.?\d*)\s*-\s*(?P<high>\d+\.?\d*)\s*(?P<unit>[A-Za-z0-9/*^%.]{0,15})?",
    re.IGNORECASE,
)
BARE_UNIT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9/%^.\-]{0,14}$")
# A name followed by a credential suffix (e.g. "Guilherme Corral, DO") is a
# physician's name, not a lab-result label -- reject it as a label
# candidate even though it otherwise has the right shape (letters, short).
# Not end-anchored: a stray trailing word (e.g. a unit from an adjacent
# column bleeding into the same reconstructed row) shouldn't defeat this.
CREDENTIAL_RE = re.compile(r",\s*(MD|DO|NP|PA-?C|DDS|RN|DPM|OD)\b", re.IGNORECASE)
# A real lab-test label is a short noun phrase ("Blood Urea Nitrogen
# (BUN)"). A row containing several of these common prose words, or ending
# in a sentence-final period, is a narrative fragment (physician
# commentary, patient-facing interpretation text) that leaked through the
# row-shape check some other way -- reject it as a label candidate no
# matter how short it looks.
PROSE_STOPWORD_RE = re.compile(
    r"\b(the|is|are|be|been|being|and|or|not|used|shown|significant|your|levels?|"
    r"optimal|interchangeably|can|may|should|will|with|for|that|this|these|those|"
    r"have|has|had|please|note|see|consult)\b",
    re.IGNORECASE,
)

KNOWN_LAB_COMPANIES = ["quest diagnostics", "labcorp", "sonora quest", "arup", "byram", "sunrise"]


@dataclass
class Match:
    label: str
    value: str
    unit: str | None
    range: str | None
    source_file: str
    raw_rows: str


@dataclass
class FileReport:
    filename: str
    page_count: int = 0
    block_count: int = 0
    row_count: int = 0
    detected_company: str | None = None
    matches: list[Match] = field(default_factory=list)
    layout_signature: str = ""


def split_row_into_halves(row_words: list[dict]) -> tuple[list[dict], list[dict] | None]:
    """If this row's own words show one big horizontal gap, it's really two
    side-by-side column entries sharing a y-position -- split there. A
    page-wide gap check doesn't work here because some rows (trend/history
    data) spread words across the full page width, which fills in any
    would-be gap when looked at globally."""
    row_words = sorted(row_words, key=lambda w: w["x0"])
    if len(row_words) < 2:
        return row_words, None
    best_gap, best_idx = 0.0, None
    for k in range(len(row_words) - 1):
        gap = row_words[k + 1]["x0"] - row_words[k]["x1"]
        if gap > best_gap:
            best_gap, best_idx = gap, k
    if best_gap > BLOCK_GAP_THRESHOLD:
        return row_words[: best_idx + 1], row_words[best_idx + 1 :]
    return row_words, None


def cluster_rows(words: list[dict]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: w["top"]):
        for r in rows:
            if abs(r[0]["top"] - w["top"]) <= ROW_TOP_TOLERANCE:
                r.append(w)
                break
        else:
            rows.append([w])
    return rows


def is_label_like(text: str) -> bool:
    text = text.strip()
    if not text or len(text) > 60:
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    if BARE_VALUE_RE.match(text) or BARE_RANGE_RE.match(text):
        return False
    if NOISE_RE.search(text) or CREDENTIAL_RE.search(text):
        return False
    if len(text.split()) == 1 and (BARE_UNIT_RE.match(text) or re.search(r"\d", text)):
        return False  # a lone unit token (e.g. "mg/dL", "10*3/uL") is not a test name
    if text.rstrip().endswith("."):
        return False  # a real test name doesn't end in a sentence-final period
    if len(PROSE_STOPWORD_RE.findall(text)) >= 2:
        return False  # multiple common prose words -> a sentence fragment, not a label
    if " - " in text:
        return False  # "Brand - Section" style header/branding text, not a test name
    return True


def link_matches_in_block(row_texts: list[str], filename: str) -> list[Match]:
    consumed: set[int] = set()
    matches: list[Match] = []

    for i, text in enumerate(row_texts):
        if i in consumed:
            continue
        text_stripped = text.strip()
        embedded = EMBEDDED_RANGE_RE.search(text)
        bare = BARE_RANGE_RE.match(text_stripped)
        if embedded:
            low, high, unit = embedded.group("low"), embedded.group("high"), embedded.group("unit")
        elif bare:
            m = re.match(r"^([<>]?\d+\.?\d*)\s*-\s*(\d+\.?\d*)\s*([A-Za-z0-9/*^%.]{0,15})?$", text_stripped)
            low, high, unit = m.group(1), m.group(2), m.group(3)
        else:
            continue

        anchor_idx = i
        used_this_round = {anchor_idx}

        label_idx = None
        for j in [i - 1, i - 2, i + 1, i - 3]:
            if 0 <= j < len(row_texts) and j not in consumed and j not in used_this_round:
                if is_label_like(row_texts[j]):
                    label_idx = j
                    break
        if label_idx is None:
            continue
        used_this_round.add(label_idx)

        value_idx = None
        for j in [i + 1, i - 1, i + 2, i - 2]:
            if 0 <= j < len(row_texts) and j not in consumed and j not in used_this_round:
                if BARE_VALUE_RE.match(row_texts[j].strip()):
                    value_idx = j
                    break
        if value_idx is None:
            continue
        used_this_round.add(value_idx)

        if not unit:
            for j in [i + 1, i - 1, i + 2]:
                if 0 <= j < len(row_texts) and j not in consumed and j not in used_this_round:
                    t = row_texts[j].strip()
                    if BARE_UNIT_RE.match(t) and not re.search(r"\d", t):
                        unit = t
                        used_this_round.add(j)
                        break

        consumed |= used_this_round
        matches.append(Match(
            label=row_texts[label_idx].strip(),
            value=row_texts[value_idx].strip(),
            unit=unit,
            range=f"{low}-{high}",
            source_file=filename,
            raw_rows=f"[label] {row_texts[label_idx].strip()} | [range] {text_stripped} | [value] {row_texts[value_idx].strip()}",
        ))

    return matches


def process_file(path: Path) -> FileReport:
    report = FileReport(filename=path.name)
    with pdfplumber.open(path) as pdf:
        report.page_count = len(pdf.pages)
        full_text_lower = "\n".join((p.extract_text() or "") for p in pdf.pages).lower()
        for company in KNOWN_LAB_COMPANIES:
            if company in full_text_lower:
                report.detected_company = company
                break
        del full_text_lower  # narrative/full text goes out of scope here, not retained further

        for page in pdf.pages:
            words = page.extract_words()
            if not words:
                continue
            rows = sorted(cluster_rows(words), key=lambda r: r[0]["top"])
            report.row_count += len(rows)

            left_stream: list[str] = []
            right_stream: list[str] = []
            split_row_count = 0
            for row in rows:
                left, right = split_row_into_halves(row)
                left_stream.append(" ".join(w["text"] for w in left))
                if right:
                    split_row_count += 1
                    right_stream.append(" ".join(w["text"] for w in right))

            report.block_count = max(report.block_count, 2 if split_row_count else 1)
            report.matches.extend(link_matches_in_block(left_stream, path.name))
            report.matches.extend(link_matches_in_block(right_stream, path.name))

    company = report.detected_company or "unknown"
    report.layout_signature = f"blocks:{report.block_count}|pages:{report.page_count}|company:{company}"
    return report


def group_by_layout(reports: list[FileReport]) -> dict[str, list[FileReport]]:
    groups: dict[str, list[FileReport]] = defaultdict(list)
    for r in reports:
        groups[r.layout_signature].append(r)
    return groups


def write_report(reports: list[FileReport]) -> None:
    all_matches = [m for r in reports for m in r.matches]
    by_label: dict[str, list[Match]] = defaultdict(list)
    for m in all_matches:
        by_label[m.label].append(m)

    groups = group_by_layout(reports)

    lines: list[str] = []
    lines.append("# Biometric Inventory Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Source: `data/medical/_inbox/` ({len(reports)} files)")
    lines.append("")
    lines.append(
        "Reconnaissance only -- nothing here has been written to `data/measurements/` "
        "or any canonical store. Review each row below against its source PDF before "
        "trusting it (per CLAUDE.md's \"Confirm against source\" rule)."
    )
    lines.append("")

    lines.append("## Provider Format Groups")
    lines.append("")
    lines.append(f"{len(groups)} distinct layout signature(s) found across {len(reports)} files:")
    lines.append("")
    for i, (sig, group_reports) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), start=1):
        lines.append(f"**Group {i}** -- `{sig}` -- {len(group_reports)} file(s):")
        for r in group_reports:
            lines.append(f"  - {r.filename} ({len(r.matches)} matched rows / {r.row_count} candidate rows)")
        lines.append("")

    lines.append("## Distinct Test Labels")
    lines.append("")
    lines.append(f"{len(by_label)} distinct label variants across {len(all_matches)} matched rows.")
    lines.append("")
    lines.append("| Label (as printed) | # files | # occurrences | Example unit | Example range |")
    lines.append("|---|---|---|---|---|")
    for label, ms in sorted(by_label.items(), key=lambda kv: kv[0].lower()):
        files = sorted({m.source_file for m in ms})
        example_unit = next((m.unit for m in ms if m.unit), "")
        example_range = next((m.range for m in ms if m.range), "")
        lines.append(f"| {label} | {len(files)} | {len(ms)} | {example_unit} | {example_range} |")
    lines.append("")

    lines.append("## Per-File Detail")
    lines.append("")
    lines.append(
        "One row per matched value, with the raw source rows alongside the parsed "
        "fields, for confirm-against-source review."
    )
    lines.append("")
    for r in reports:
        lines.append(f"### {r.filename}")
        lines.append(f"_{len(r.matches)} matched rows out of {r.row_count} candidate rows, layout: `{r.layout_signature}`_")
        lines.append("")
        if r.matches:
            lines.append("| Label | Value | Unit | Range | Raw source rows |")
            lines.append("|---|---|---|---|---|")
            for m in r.matches:
                raw = m.raw_rows.replace("|", "\\|")
                lines.append(f"| {m.label} | {m.value} | {m.unit or ''} | {m.range or ''} | {raw} |")
        else:
            lines.append("_No rows matched the lab-result shape in this file._")
        lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def print_safe_summary(reports: list[FileReport]) -> None:
    """Console output: file/row/label counts, label names, and one example
    value per distinct label -- scoped for sanity-checking the parser, not
    a per-instance dump. Never narrative text."""
    all_matches = [m for r in reports for m in r.matches]
    by_label: dict[str, list[Match]] = defaultdict(list)
    for m in all_matches:
        by_label[m.label].append(m)
    groups = group_by_layout(reports)

    print(f"Processed {len(reports)} file(s) from {INBOX_DIR}")
    for r in reports:
        print(f"  {r.filename}: {len(r.matches)} matched / {r.row_count} candidate rows ({r.layout_signature})")
    print()
    print(f"{len(by_label)} distinct test-label variants across {len(all_matches)} matched rows "
          f"(label, # files, # occurrences, one example value):")
    for label in sorted(by_label, key=str.lower):
        ms = by_label[label]
        files = sorted({m.source_file for m in ms})
        example = ms[0]
        example_str = f"{example.value}{(' ' + example.unit) if example.unit else ''}"
        print(f"  - {label} ({len(files)} files, {len(ms)}x) e.g. {example_str}")
    print()
    print(f"{len(groups)} distinct layout signature(s) (rough provider-format count):")
    for sig, grp in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  - {sig}: {len(grp)} file(s)")
    print()
    print(f"Full report (per-match detail, for confirm-against-source review): {REPORT_PATH}")


def main() -> None:
    if not INBOX_DIR.exists():
        raise SystemExit(f"{INBOX_DIR} does not exist.")
    pdf_paths = sorted(INBOX_DIR.glob("*.pdf"))
    if not pdf_paths:
        raise SystemExit(f"No PDFs found in {INBOX_DIR}.")

    reports = [process_file(p) for p in pdf_paths]
    write_report(reports)
    print_safe_summary(reports)


if __name__ == "__main__":
    main()
