#!/usr/bin/env python3
"""
Citation Dashboard — live citation-balance tracker for the 2026 comps exam.

Scans one or more .tex files for \\cite / \\citet / \\citep / \\parencite /
\\textcite / \\autocite keys, cross-references them against refs.bib, and
shows a live-updating breakdown by decade, book vs. paper/chapter, and
official vs. extra reading-list status.

Usage:
    python3 citation_dashboard.py --bib refs.bib "Exam Writing"
    python3 citation_dashboard.py --bib refs.bib --watch "Exam Writing"
    python3 citation_dashboard.py --bib refs.bib "Exam Writing/main.tex" --watch

No dependencies beyond `rich` (pip install rich), which is already on this
machine. Deliberately avoids bibtexparser or a filesystem-event watcher
(watchdog) so it has nothing extra to break on exam day — file changes are
detected by polling mtimes once a second.
"""

import argparse
import re
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

CITE_RE = re.compile(
    r"\\(?:cite|citep|citet|citeauthor|citeyear|parencite|textcite|autocite|footcite)"
    r"\*?(?:\[[^\]]*\])*\{([^}]*)\}"
)

BIB_ENTRY_RE = re.compile(
    r"@(\w+)\s*\{\s*([^,\s]+)\s*,(.*?)\n\}", re.DOTALL
)
BIB_FIELD_RE = re.compile(
    r"(\w+)\s*=\s*\{(.*?)\}\s*,?\s*(?=\n\s*\w+\s*=|\Z)", re.DOTALL
)


def parse_bib(bib_path: Path) -> dict:
    """Minimal, dependency-free .bib parser tailored to this project's own
    bib formatting (field = {value} lines). Returns key -> metadata dict."""
    text = bib_path.read_text()
    entries = {}
    for m in BIB_ENTRY_RE.finditer(text):
        entrytype, key, body = m.group(1).lower(), m.group(2).strip(), m.group(3)
        fields = {}
        for fm in BIB_FIELD_RE.finditer(body):
            fields[fm.group(1).lower()] = " ".join(fm.group(2).split())
        year_match = re.search(r"\d{4}", fields.get("year", ""))
        entries[key] = {
            "type": entrytype,
            "year": int(year_match.group()) if year_match else None,
            "status": fields.get("keywords", "unknown").strip().lower(),
            "title": fields.get("title", key),
        }
    return entries


def find_tex_files(paths: list) -> list:
    files = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.tex")))
        elif path.is_file():
            files.append(path)
    return files


def extract_cite_keys(tex_files: list) -> list:
    keys = []
    for f in tex_files:
        try:
            text = f.read_text()
        except OSError:
            continue
        for m in CITE_RE.finditer(text):
            for key in m.group(1).split(","):
                key = key.strip()
                if key:
                    keys.append(key)
    return keys


def bucket_decade(year: int) -> str:
    if year is None:
        return "Unknown"
    decade = (year // 10) * 10
    return f"{decade}s"


TYPE_LABELS = {
    "book": "Book",
    "incollection": "Chapter",
    "inbook": "Chapter",
    "article": "Article",
    "inproceedings": "Article",
}


def build_bar(count: int, max_count: int, width: int = 24) -> str:
    if max_count == 0:
        return ""
    filled = round((count / max_count) * width)
    return "█" * filled + "░" * (width - filled)


def render_breakdown(title: str, counts: dict, order: list = None) -> Table:
    table = Table(title=title, show_header=False, box=None, padding=(0, 1, 0, 0))
    table.add_column("Label", style="bold")
    table.add_column("Bar")
    table.add_column("Count", justify="right")
    max_count = max(counts.values(), default=0)
    keys = order if order else sorted(counts.keys())
    for k in keys:
        v = counts.get(k, 0)
        if v == 0 and order is None:
            continue
        table.add_row(str(k), build_bar(v, max_count), str(v))
    return table


def build_dashboard(bib: dict, tex_files: list) -> Panel:
    all_keys = extract_cite_keys(tex_files)
    total_instances = len(all_keys)
    unique_keys = sorted(set(all_keys))

    unknown = [k for k in unique_keys if k not in bib]
    known = [k for k in unique_keys if k in bib]

    by_decade, by_type, by_status = {}, {"Book": 0, "Chapter": 0, "Article": 0}, {"official": 0, "extra": 0, "unknown": 0}

    # Weight by number of times each key was actually cited, not just unique use.
    for key in all_keys:
        meta = bib.get(key)
        if not meta:
            continue
        decade = bucket_decade(meta["year"])
        by_decade[decade] = by_decade.get(decade, 0) + 1
        type_label = TYPE_LABELS.get(meta["type"], "Other")
        by_type[type_label] = by_type.get(type_label, 0) + 1
        status = meta["status"] if meta["status"] in ("official", "extra") else "unknown"
        by_status[status] = by_status.get(status, 0) + 1

    header = Text()
    header.append(f"{total_instances} citation instance(s)  ·  ", style="bold")
    header.append(f"{len(known)} unique work(s) cited", style="bold cyan")
    if unknown:
        header.append(f"  ·  {len(unknown)} UNRECOGNIZED KEY(S)", style="bold red")

    body = Table.grid(padding=(1, 3))
    body.add_row(
        render_breakdown("By Decade", by_decade),
        render_breakdown("Type", by_type, order=["Book", "Chapter", "Article"]),
        render_breakdown("Reading List", by_status, order=["official", "extra", "unknown"]),
    )

    sections = [header, "", body]

    if unknown:
        warn = Text("\nUnrecognized \\cite keys (typo, or missing from refs.bib):\n", style="bold red")
        warn.append("  " + ", ".join(unknown), style="red")
        sections.append(warn)

    files_line = Text(
        "\nWatching: " + ", ".join(str(f) for f in tex_files) if tex_files else "\nNo .tex files found.",
        style="dim",
    )
    sections.append(files_line)

    group = Table.grid()
    for s in sections:
        group.add_row(s)

    return Panel(group, title="[bold]Citation Dashboard[/bold]", subtitle="2026 Comp Exam", border_style="cyan")


def main():
    parser = argparse.ArgumentParser(description="Live citation-balance dashboard for the comps exam.")
    parser.add_argument("paths", nargs="+", help="Directory or .tex file(s) to scan for \\cite keys")
    parser.add_argument("--bib", required=True, help="Path to refs.bib")
    parser.add_argument("--watch", action="store_true", help="Auto-refresh when watched .tex files change")
    parser.add_argument("--interval", type=float, default=1.0, help="Poll interval in seconds for --watch (default 1.0)")
    args = parser.parse_args()

    bib_path = Path(args.bib)
    if not bib_path.exists():
        print(f"error: bib file not found: {bib_path}", file=sys.stderr)
        sys.exit(1)

    console = Console()

    if not args.watch:
        bib = parse_bib(bib_path)
        tex_files = find_tex_files(args.paths)
        console.print(build_dashboard(bib, tex_files))
        return

    def snapshot_mtimes(files):
        return {f: f.stat().st_mtime for f in files if f.exists()}

    bib = parse_bib(bib_path)
    tex_files = find_tex_files(args.paths)
    mtimes = snapshot_mtimes(tex_files + [bib_path])

    with Live(build_dashboard(bib, tex_files), console=console, refresh_per_second=2, screen=False) as live:
        while True:
            time.sleep(args.interval)
            tex_files = find_tex_files(args.paths)
            current = snapshot_mtimes(tex_files + [bib_path])
            if current != mtimes:
                mtimes = current
                bib = parse_bib(bib_path)
                live.update(build_dashboard(bib, tex_files))


if __name__ == "__main__":
    main()
