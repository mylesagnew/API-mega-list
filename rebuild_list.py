#!/usr/bin/env python3
"""
Rebuild API Mega List distribution artifacts.

This script scans markdown files under the repository's main/ directory,
extracts listed APIs from common markdown formats, normalizes/deduplicates the
records, and writes exactly two generated files:

    dist/api_mega_list.csv
    dist/api_mega_list.json

Output schema:
    Category, API Name, Description, Link, Date_Added

Design notes:
- Standard library only: no runtime dependency installation is required.
- The parser is intentionally defensive because repository markdown may vary
  between tables, bullets, headings, inline links, and key/value blocks.
- Date_Added defaults to the current execution date when no source date exists.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit


# -----------------------------------------------------------------------------
# Paths and constants
# -----------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
# Prefer the requested main/ source tree when present. The current repository
# also has category folders at the repository root, so fall back to REPO_ROOT to
# keep the automation production-safe against the live layout.
REQUESTED_SOURCE_ROOT = REPO_ROOT / "main"
SOURCE_ROOT = REQUESTED_SOURCE_ROOT if REQUESTED_SOURCE_ROOT.exists() else REPO_ROOT
DIST_ROOT = REPO_ROOT / "dist"
CSV_PATH = DIST_ROOT / "api_mega_list.csv"
JSON_PATH = DIST_ROOT / "api_mega_list.json"

OUTPUT_HEADERS = ["Category", "API Name", "Description", "Link", "Date_Added"]
MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkdn"}

URL_RE = re.compile(r"https?://[^\s<>)\]}\"']+", re.IGNORECASE)
MD_LINK_RE = re.compile(
    r"\[([^\]]+)\]\((https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(.+?)\s*$")
FIELD_RE = re.compile(r"^\s*(?:[-+*]\s*)?([A-Za-z][A-Za-z0-9 _/\-]{1,50})\s*[:=]\s*(.+?)\s*$")

FIELD_ALIASES = {
    "category": "category",
    "api category": "category",
    "folder": "category",
    "name": "name",
    "api": "name",
    "api name": "name",
    "service": "name",
    "tool": "name",
    "platform": "name",
    "product": "name",
    "description": "description",
    "desc": "description",
    "summary": "description",
    "details": "description",
    "notes": "description",
    "url": "link",
    "link": "link",
    "docs": "link",
    "doc": "link",
    "documentation": "link",
    "documentation link": "link",
    "website": "link",
    "homepage": "link",
    "endpoint": "link",
    "date": "date_added",
    "date added": "date_added",
    "date_added": "date_added",
    "added": "date_added",
    "date discovered": "date_added",
    "discovered": "date_added",
    "created": "date_added",
}


@dataclass(frozen=True)
class ApiRecord:
    """Canonical in-memory representation of one API row."""

    category: str
    name: str
    description: str
    link: str
    date_added: str

    def as_dict(self) -> Dict[str, str]:
        """Return the exact required output schema."""
        return {
            "Category": self.category,
            "API Name": self.name,
            "Description": self.description,
            "Link": self.link,
            "Date_Added": self.date_added,
        }


# -----------------------------------------------------------------------------
# Normalization helpers
# -----------------------------------------------------------------------------

def collapse_whitespace(value: str) -> str:
    """Normalize all whitespace runs to a single space."""
    return re.sub(r"\s+", " ", value or "").strip()


def strip_markdown(value: str) -> str:
    """Remove common markdown decorations while preserving readable text."""
    value = value or ""
    value = MD_LINK_RE.sub(lambda match: match.group(1), value)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s+", "", value)
    value = re.sub(r"^\s*>\s*", "", value)
    value = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", value)
    value = value.replace("**", "").replace("__", "").replace("*", "").replace("_", "")
    value = value.replace("|", " ")
    return collapse_whitespace(value)


def normalize_header(value: str) -> str:
    """Normalize a table/key label for semantic matching."""
    value = strip_markdown(value).casefold()
    value = value.replace("-", " ").replace("_", " ").replace("/", " ")
    return collapse_whitespace(value)


def semantic_field(label: str) -> Optional[str]:
    """Map a source label to one of the internal field names."""
    return FIELD_ALIASES.get(normalize_header(label))


def first_url(value: str) -> str:
    """Extract the first URL from markdown link or plain text."""
    if not value:
        return ""

    markdown_link = MD_LINK_RE.search(value)
    if markdown_link:
        return clean_url(markdown_link.group(2))

    plain_url = URL_RE.search(value)
    if plain_url:
        return clean_url(plain_url.group(0))

    return ""


def clean_url(url: str) -> str:
    """Trim markdown/punctuation artifacts from a URL."""
    return (url or "").strip().rstrip(".,;:!?")


def canonical_url(url: str) -> str:
    """Build a stable URL key for deduplication."""
    url = clean_url(url)
    if not url:
        return ""

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    # Drop fragments for dedupe; keep query parameters because docs links may use them.
    return urlunsplit((scheme, host, path, parsed.query, ""))


def canonical_text(value: str) -> str:
    """Build a stable text key for name-based deduplication."""
    return strip_markdown(value).casefold()


def extract_date(value: str, default_date: str) -> str:
    """Return a source YYYY-MM-DD date, or the execution date fallback."""
    match = DATE_RE.search(value or "")
    return match.group(1) if match else default_date


def name_from_url(url: str) -> str:
    """Derive a conservative identifier from the URL host when no name exists."""
    parsed = urlsplit(url)
    host = parsed.netloc or parsed.path.split("/")[0]
    return host.removeprefix("www.")


# -----------------------------------------------------------------------------
# Discovery and source-level defaults
# -----------------------------------------------------------------------------

def iter_markdown_files() -> Iterator[Path]:
    """Yield all source markdown files in deterministic order."""
    if not SOURCE_ROOT.exists():
        raise FileNotFoundError(f"Expected source directory does not exist: {SOURCE_ROOT}")

    excluded_parts = {".git", ".github", "dist", "settings", "node_modules", "__pycache__"}
    excluded_root_files = {"README.md", "FOLLOW_CREATOR.md"}

    for path in sorted(
        path
        for path in SOURCE_ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in MARKDOWN_SUFFIXES
    ):
        relative = path.relative_to(SOURCE_ROOT)
        if any(part in excluded_parts for part in relative.parts):
            continue
        if SOURCE_ROOT == REPO_ROOT and len(relative.parts) == 1 and relative.name in excluded_root_files:
            continue
        yield path


def category_from_path(path: Path) -> str:
    """Infer Category from the first folder below main/, falling back to filename."""
    try:
        relative = path.relative_to(SOURCE_ROOT)
    except ValueError:
        return strip_markdown(path.stem.replace("-", " ").replace("_", " "))

    source = relative.parts[0] if len(relative.parts) > 1 else path.stem
    return strip_markdown(source.replace("-", " ").replace("_", " "))


def parse_frontmatter(lines: Sequence[str]) -> Tuple[Dict[str, str], int]:
    """
    Parse YAML-ish frontmatter when present.

    Returns a tuple of (fields, content_start_index). This is deliberately simple
    to avoid a PyYAML dependency; only scalar key/value pairs relevant to the
    output schema are needed.
    """
    if not lines or lines[0].strip() != "---":
        return {}, 0

    fields: Dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in {"---", "..."}:
            return fields, index + 1
        match = FIELD_RE.match(line)
        if match:
            field = semantic_field(match.group(1))
            if field:
                fields[field] = match.group(2).strip().strip('"\'')

    return fields, 0


# -----------------------------------------------------------------------------
# Markdown table parsing
# -----------------------------------------------------------------------------

def looks_like_table_row(line: str) -> bool:
    """Return True when a line resembles a GitHub-flavored markdown table row."""
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def is_table_separator(line: str) -> bool:
    """Detect markdown table separator rows like |---|:---:|."""
    if not looks_like_table_row(line):
        return False
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def split_table_row(line: str) -> List[str]:
    """Split a standard markdown table row into cells."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_tables(lines: Sequence[str], defaults: Dict[str, str], default_date: str) -> Tuple[List[ApiRecord], set[int]]:
    """Extract records from markdown tables and return consumed line numbers."""
    records: List[ApiRecord] = []
    consumed: set[int] = set()
    index = 0

    while index < len(lines) - 1:
        if not looks_like_table_row(lines[index]) or not is_table_separator(lines[index + 1]):
            index += 1
            continue

        header_cells = split_table_row(lines[index])
        header_fields = [semantic_field(cell) for cell in header_cells]
        table_indexes = {index, index + 1}
        index += 2

        while index < len(lines) and looks_like_table_row(lines[index]):
            table_indexes.add(index)
            if not is_table_separator(lines[index]):
                row_cells = split_table_row(lines[index])
                raw: Dict[str, str] = {}
                fallback_text: List[str] = []

                for column_index, cell in enumerate(row_cells):
                    fallback_text.append(cell)
                    field = header_fields[column_index] if column_index < len(header_fields) else None
                    if field:
                        raw[field] = cell

                # Fallback for tables with unrecognized headers.
                if "link" not in raw:
                    raw["link"] = " ".join(fallback_text)
                if "name" not in raw and row_cells:
                    raw["name"] = row_cells[0]

                record = build_record(raw, defaults, default_date)
                if record:
                    records.append(record)

            index += 1

        consumed.update(table_indexes)

    return records, consumed


# -----------------------------------------------------------------------------
# Key/value, heading, bullet, and loose-link parsing
# -----------------------------------------------------------------------------

def parse_key_value_blocks(lines: Sequence[str], defaults: Dict[str, str], default_date: str) -> List[ApiRecord]:
    """Extract repeated API entries expressed as key/value blocks."""
    records: List[ApiRecord] = []
    current: Dict[str, str] = {}

    def flush() -> None:
        nonlocal current
        record = build_record(current, defaults, default_date)
        if record:
            records.append(record)
        current = {}

    for line in list(lines) + [""]:
        if not line.strip():
            if current:
                flush()
            continue

        match = FIELD_RE.match(line)
        if not match:
            continue

        field = semantic_field(match.group(1))
        if not field:
            continue

        # A repeated name/link often means a new entry started without a blank line.
        if field in {"name", "link"} and field in current:
            flush()

        current[field] = match.group(2)

    return records


def parse_bullets(lines: Sequence[str], defaults: Dict[str, str], default_date: str) -> List[ApiRecord]:
    """Extract entries from bullet/list items that contain a URL."""
    records: List[ApiRecord] = []

    for line in lines:
        match = BULLET_RE.match(line)
        if not match:
            continue

        text = match.group(1)
        if not first_url(text):
            continue

        raw = parse_inline_entry(text)
        record = build_record(raw, defaults, default_date)
        if record:
            records.append(record)

    return records


def parse_heading_sections(lines: Sequence[str], defaults: Dict[str, str], default_date: str) -> List[ApiRecord]:
    """Extract entries where an API name is a heading and details follow below."""
    records: List[ApiRecord] = []

    for index, line in enumerate(lines):
        heading = HEADING_RE.match(line)
        if not heading:
            continue

        heading_text = heading.group(1)
        context_lines: List[str] = []
        for following in lines[index + 1 : index + 10]:
            if HEADING_RE.match(following):
                break
            context_lines.append(following)

        context = " ".join(context_lines)
        if not first_url(heading_text + " " + context):
            continue

        raw = parse_inline_entry(heading_text + " " + context)
        raw.setdefault("name", heading_text)
        record = build_record(raw, defaults, default_date)
        if record:
            records.append(record)

    return records


def parse_loose_links(lines: Sequence[str], defaults: Dict[str, str], default_date: str) -> List[ApiRecord]:
    """
    Last-resort extraction for standalone markdown/plain links.

    This catches simple lists that are not valid bullets or tables, e.g.
    [Example API](https://example.com/docs) - description.
    """
    records: List[ApiRecord] = []

    for line in lines:
        if not first_url(line):
            continue
        # Avoid converting obvious prose-only paragraphs into many duplicates unless
        # they look like one compact entry.
        if len(line) > 500:
            continue
        raw = parse_inline_entry(line)
        record = build_record(raw, defaults, default_date)
        if record:
            records.append(record)

    return records


def parse_inline_entry(text: str) -> Dict[str, str]:
    """Parse one compact line into raw fields."""
    raw: Dict[str, str] = {"link": first_url(text), "date_added": text}

    markdown_link = MD_LINK_RE.search(text)
    if markdown_link:
        raw["name"] = markdown_link.group(1)

    # Remove URLs but keep markdown link labels for name/description extraction.
    readable = MD_LINK_RE.sub(lambda match: match.group(1), text)
    readable = URL_RE.sub("", readable)
    readable = strip_markdown(readable)

    # Extract inline key/value fields if the line is built that way.
    for part in re.split(r"\s+[;•]\s+", text):
        field_match = FIELD_RE.match(part)
        if field_match:
            field = semantic_field(field_match.group(1))
            if field:
                raw[field] = field_match.group(2)

    if "name" not in raw:
        pieces = re.split(r"\s+(?:-|–|—|:)\s+", readable, maxsplit=1)
        raw["name"] = pieces[0] if pieces else readable
        if len(pieces) > 1:
            raw["description"] = pieces[1]
    elif "description" not in raw:
        description = readable.replace(strip_markdown(raw["name"]), "", 1)
        raw["description"] = re.sub(r"^\s*(?:-|–|—|:)\s*", "", description).strip()

    return raw


# -----------------------------------------------------------------------------
# Record construction and deduplication
# -----------------------------------------------------------------------------

def build_record(raw: Dict[str, str], defaults: Dict[str, str], default_date: str) -> Optional[ApiRecord]:
    """Normalize raw extracted fields into a valid ApiRecord."""
    if not raw:
        return None

    category = strip_markdown(raw.get("category") or defaults.get("category") or "")
    name = strip_markdown(raw.get("name") or "")
    description = strip_markdown(raw.get("description") or "")
    link = first_url(raw.get("link") or "")

    # Recover a link from any field if the parser did not map one explicitly.
    if not link:
        link = first_url(" ".join(raw.values()))

    if not link:
        # Required output includes a Link field; skip entries that provide no URL.
        return None

    # Recover name from markdown link label, then from URL host if needed.
    if not name:
        for value in raw.values():
            markdown_link = MD_LINK_RE.search(value or "")
            if markdown_link:
                name = strip_markdown(markdown_link.group(1))
                break
    if not name:
        name = name_from_url(link)

    # Prefer explicit date fields, then any date found in the raw record, then file/default date.
    date_source = raw.get("date_added") or raw.get("date") or " ".join(raw.values())
    date_added = extract_date(date_source, defaults.get("date_added") or default_date)

    return ApiRecord(
        category=category,
        name=name,
        description=description,
        link=clean_url(link),
        date_added=date_added,
    )


def merge_records(existing: ApiRecord, candidate: ApiRecord) -> ApiRecord:
    """Merge duplicates, preserving stable data while filling blanks."""
    return ApiRecord(
        category=existing.category or candidate.category,
        name=existing.name or candidate.name,
        description=max([existing.description, candidate.description], key=len),
        link=existing.link or candidate.link,
        date_added=existing.date_added or candidate.date_added,
    )


def deduplicate(records: Iterable[ApiRecord]) -> List[ApiRecord]:
    """Deduplicate by URL first, then by API name."""
    by_url: Dict[str, ApiRecord] = {}

    for record in records:
        url_key = canonical_url(record.link)
        if not url_key:
            continue
        by_url[url_key] = merge_records(by_url[url_key], record) if url_key in by_url else record

    by_name: Dict[str, ApiRecord] = {}
    for record in by_url.values():
        # Name-only dedupe is scoped by category to avoid merging different APIs
        # that happen to share generic names like "Search".
        name_key = f"{canonical_text(record.category)}::{canonical_text(record.name)}"
        if not name_key.endswith("::"):
            by_name[name_key] = merge_records(by_name[name_key], record) if name_key in by_name else record

    return sorted(by_name.values(), key=lambda r: (r.category.casefold(), r.name.casefold(), r.link.casefold()))


# -----------------------------------------------------------------------------
# File parsing and output writing
# -----------------------------------------------------------------------------

def parse_file(path: Path, execution_date: str) -> List[ApiRecord]:
    """Run all parser strategies against one markdown file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    frontmatter, content_start = parse_frontmatter(lines)
    content_lines = lines[content_start:]

    defaults = {
        "category": frontmatter.get("category") or category_from_path(path),
        "date_added": extract_date(frontmatter.get("date_added", ""), execution_date),
    }

    records: List[ApiRecord] = []
    table_records, consumed_table_lines = parse_tables(content_lines, defaults, execution_date)
    records.extend(table_records)

    # Remove table lines before running looser parsers to reduce duplicates/noise.
    non_table_lines = [line for idx, line in enumerate(content_lines) if idx not in consumed_table_lines]

    records.extend(parse_key_value_blocks(non_table_lines, defaults, execution_date))
    records.extend(parse_bullets(non_table_lines, defaults, execution_date))
    records.extend(parse_heading_sections(non_table_lines, defaults, execution_date))
    records.extend(parse_loose_links(non_table_lines, defaults, execution_date))

    return records


def write_csv(records: Sequence[ApiRecord]) -> None:
    """Write properly escaped CSV output with required headers."""
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record.as_dict())


def write_json(records: Sequence[ApiRecord]) -> None:
    """Write pretty, deterministic JSON array output."""
    with JSON_PATH.open("w", encoding="utf-8") as handle:
        json.dump([record.as_dict() for record in records], handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main() -> None:
    """Entrypoint used locally and by GitHub Actions."""
    execution_date = date.today().isoformat()
    DIST_ROOT.mkdir(parents=True, exist_ok=True)

    records: List[ApiRecord] = []
    for markdown_file in iter_markdown_files():
        records.extend(parse_file(markdown_file, execution_date))

    final_records = deduplicate(records)
    write_csv(final_records)
    write_json(final_records)

    print(f"Parsed {len(records)} raw records")
    print(f"Wrote {len(final_records)} unique records to {CSV_PATH.relative_to(REPO_ROOT)}")
    print(f"Wrote {len(final_records)} unique records to {JSON_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
