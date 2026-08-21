#!/usr/bin/env python3
"""
Zotero note importer — turns Zotero notes into reading-note .tex files.

Reads the local Zotero database, pulls every note in the configured
collections, and writes one reading folder per note in the same shape as a
hand-written reading:

    Reading Notes/Reading List/Lipset 1959/Lipset_1959_Notes.tex

Notes from a collection mapped to Extra Readings land there instead, which is
what makes the compendium tag them EXTERNAL. Once written, the files are
ordinary notes — tools/build_compendium.py discovers them automatically and
places them chronologically.

The Zotero database is opened from a temporary copy and never written to, so
this is safe to run while Zotero is open.

Existing files are left alone unless --force is passed, so re-running after
adding notes in Zotero only brings in what is new; your edits to previously
imported files survive.

Usage:
    python3 tools/import_zotero.py             # import new notes
    python3 tools/import_zotero.py --dry-run   # report, write nothing
    python3 tools/import_zotero.py --force     # overwrite existing files
"""

import argparse
import html as htmllib
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ZOTERO = Path.home() / "Zotero"
DB = ZOTERO / "zotero.sqlite"
STORAGE = ZOTERO / "storage"

# Zotero collection -> destination folder inside the repo. Anything under
# "Extra Readings" is marked EXTERNAL by the compendium builder.
COLLECTIONS = {
    "Comp Exam 2026": REPO / "Reading Notes" / "Reading List",
    "Additional Readings": REPO / "Reading Notes" / "Extra Readings",
}

# Parent-item titles to leave out, with the reason.
SKIP_TITLES = {
    "Deliberative democracy: essays on reason and politics":
        "note is catalog boilerplate, and the reading was deliberately removed",
}

# Metadata for items Zotero is missing fields for, keyed by parent title. Kept
# here rather than edited into the generated .tex, so a re-run still places and
# cites them correctly.
OVERRIDES = {
    "Endogenous State Capacity": {
        "year": 2024,
        "publicationTitle": "Annual Review of Political Science",
    },
}

PREAMBLE = r"""\documentclass[12pt]{article}
\usepackage[margin=1in]{geometry}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage{parskip}
\usepackage{graphicx}
\usepackage{xcolor}
\usepackage{hyperref}
\usepackage{fontenc}
"""

# Characters Zotero stores literally that pdflatex is happier without.
TEXT_SUBS = [
    ("\u201c", "``"), ("\u201d", "''"), ("\u2018", "`"), ("\u2019", "'"),
    ("\u2014", "---"), ("\u2013", "--"), ("\u2026", r"\ldots{}"),
    ("\u00a0", " "), ("\u200b", ""),
]

LATEX_SPECIALS = [
    ("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
    ("#", r"\#"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
    ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}"),
]


def esc(text):
    """Escape a plain-text run for LaTeX."""
    for a, b in LATEX_SPECIALS:
        text = text.replace(a, b)
    for a, b in TEXT_SUBS:
        text = text.replace(a, b)
    return text


class Zot:
    def __init__(self, db):
        self.c = sqlite3.connect(db)

    def field(self, item, name):
        r = self.c.execute(
            """select v.value from itemData d
               join fields f on f.fieldID = d.fieldID
               join itemDataValues v on v.valueID = d.valueID
               where d.itemID = ? and f.fieldName = ?""", (item, name)).fetchone()
        return r[0] if r else ""

    def creators(self, item):
        """[(firstName, lastName)] in author order."""
        return [(r[0] or "", r[1] or "") for r in self.c.execute(
            """select cd.firstName, cd.lastName from itemCreators ic
               join creators cd on cd.creatorID = ic.creatorID
               where ic.itemID = ? order by ic.orderIndex""", (item,))]

    def notes(self, collection):
        # Ordered so that repeated runs assign the same names to the same
        # notes; without this, two notes sharing an author and year could swap
        # folders between runs.
        return self.c.execute(
            """select n.itemID, n.parentItemID, n.note
               from itemNotes n
               join collectionItems ci
                    on ci.itemID = coalesce(n.parentItemID, n.itemID)
               join collections col on col.collectionID = ci.collectionID
               where col.collectionName = ?
               order by n.parentItemID, n.itemID""", (collection,)).fetchall()

    # Leading bytes -> the extension graphicx needs to recognise the file.
    MAGIC = [
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"GIF8", ".gif"),
        (b"%PDF", ".pdf"),
    ]

    def attachment_path(self, key):
        """Return (path, extension) for a note's embedded image.

        Zotero often stores these as a bare file named `image` with no
        extension, which graphicx cannot use, so the type is sniffed from the
        file's first bytes and a proper extension supplied.
        """
        d = STORAGE / key
        if not d.is_dir():
            return None, None
        files = [p for p in d.iterdir()
                 if p.is_file() and not p.name.startswith(".")]
        if not files:
            return None, None
        known = [p for p in files
                 if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".pdf")]
        src = known[0] if known else files[0]
        if src.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".pdf"):
            return src, src.suffix.lower()
        head = src.open("rb").read(8)
        for sig, ext in self.MAGIC:
            if head.startswith(sig):
                return src, ext
        return None, None


def author_list(authors):
    """Full names in the style of the hand-written citations.

    'Cook, Karen Schweers, and Margaret Levi' — first author inverted, the
    rest in normal order.
    """
    def full(a):
        first, last = a
        return f"{last}, {first}".strip(", ") if a is authors[0] else \
               f"{first} {last}".strip()

    names = [full(a) for a in authors]
    if not names:
        return "Unknown"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]}, and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def reading_name(authors, year):
    """Match the naming already used across the repo: 'Cook and Levi 1990'."""
    last = [a[1] for a in authors]
    if not last:
        stem = "Unknown"
    elif len(last) == 1:
        stem = last[0]
    elif len(last) == 2:
        stem = f"{last[0]} and {last[1]}"
    else:
        stem = f"{last[0]} et al"
    # Folder names double as chapter titles and as path fragments baked into
    # \includegraphics, so fold accents to ASCII the way the hand-made folders
    # already do ("Muller and Strom 1999"). The citation keeps the real
    # spelling.
    stem = unicodedata.normalize("NFKD", stem)
    stem = "".join(ch for ch in stem if not unicodedata.combining(ch))
    stem = re.sub(r"[^\w &'-]", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return f"{stem} {year}" if year else stem


def year_of(zot, pid, title):
    override = OVERRIDES.get(title, {}).get("year")
    if override:
        return override
    m = re.search(r"\b(1[6-9]\d\d|20\d\d)\b", zot.field(pid, "date") or "")
    return int(m.group(1)) if m else None


def citation(zot, pid, authors, year):
    """A citation line in the same style as the hand-written notes."""
    who = author_list(authors)
    title = zot.field(pid, "title")
    over = OVERRIDES.get(title, {})
    journal = over.get("publicationTitle") or zot.field(pid, "publicationTitle")
    book = zot.field(pid, "bookTitle")
    vol, issue = zot.field(pid, "volume"), zot.field(pid, "issue")
    pages, pub = zot.field(pid, "pages"), zot.field(pid, "publisher")

    out = [f"{esc(who)}. ``{esc(title)}''"]
    if year:
        out.append(f" [{year}]")
    out.append(".\\\\\n")
    host = journal or book
    if host:
        out.append(f"\\textit{{{esc(host)}}}")
        if vol:
            out.append(f" {esc(vol)}")
            if issue:
                out.append(f"({esc(issue)})")
        if pages:
            out.append(f": {esc(pages)}")
        out.append(".")
    elif pub:
        out.append(f"{esc(pub)}.")
    else:
        out.append("Working paper.")
    return "".join(out)


def html_to_tex(note, folder, zot, dry_run=False):
    """Convert one Zotero note's HTML into a LaTeX body."""
    s = note or ""
    images, dropped = [], []

    def take_image(m):
        key = re.search(r'data-attachment-key="([^"]+)"', m.group(0))
        if not key:
            return ""
        src, ext = zot.attachment_path(key.group(1))
        if not src:
            dropped.append(key.group(1))
            return ""
        dest_name = f"figure-{len(images) + 1}{ext}"
        images.append((src, dest_name))
        return hold(
            f"\n\n\\begin{{center}}\n"
            f"\\includegraphics[width=0.85\\textwidth]{{{dest_name}}}\n"
            f"\\end{{center}}\n\n")

    # Every fragment of real LaTeX this function emits is parked in `literals`
    # behind a control-character token. Escaping then runs over the prose only
    # and cannot see the markup, which is what previously turned figures into
    # visible "\begin{center}" text on the page.
    literals = []

    def hold(tex):
        literals.append(tex)
        return f"\x00{len(literals) - 1}\x01"

    s = re.sub(r"<img\b[^>]*>", take_image, s)

    # Block structure first, so the inline pass sees plain runs.
    s = re.sub(r"</p\s*>|<br\s*/?>", "\n\n", s, flags=re.I)
    s = re.sub(r"<li\b[^>]*>", lambda m: hold("\n\\item "), s, flags=re.I)
    s = re.sub(r"<(ul|ol)\b[^>]*>",
               lambda m: hold("\n\\begin{itemize}\n"), s, flags=re.I)
    s = re.sub(r"</(ul|ol)\s*>",
               lambda m: hold("\n\\end{itemize}\n"), s, flags=re.I)
    s = re.sub(r"<blockquote\b[^>]*>",
               lambda m: hold("\n\\begin{quote}\n"), s, flags=re.I)
    s = re.sub(r"</blockquote\s*>",
               lambda m: hold("\n\\end{quote}\n"), s, flags=re.I)

    # Inline emphasis: the command and its braces are held, the wrapped text
    # stays exposed so it still gets escaped.
    for tag, cmd in (("em", "textit"), ("i", "textit"), ("strong", "textbf"),
                     ("b", "textbf"), ("u", "underline")):
        s = re.sub(rf"<{tag}\b[^>]*>(.*?)</{tag}\s*>",
                   lambda m, c=cmd: hold("\\" + c + "{") + m.group(1) + hold("}"),
                   s, flags=re.I | re.S)

    s = re.sub(r"<[^>]+>", "", s)          # drop remaining markup
    s = htmllib.unescape(s)
    s = esc(s)
    s = re.sub(r"\x00(\d+)\x01", lambda m: literals[int(m.group(1))], s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    if not dry_run:
        for src, dest_name in images:
            shutil.copy2(src, folder / dest_name)
    return s, len(images), dropped


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--force", action="store_true",
                    help="overwrite .tex files that already exist")
    args = ap.parse_args()

    if not DB.exists():
        print(f"No Zotero database at {DB}")
        return 1

    # Copy first: Zotero holds a lock on the live file while it is running.
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "zotero.sqlite"
        shutil.copy2(DB, snapshot)
        zot = Zot(snapshot)

        written, skipped, existing, lost = [], [], [], []
        claimed, renamed = {}, []
        for collection, dest_root in COLLECTIONS.items():
            for _nid, pid, note in zot.notes(collection):
                if not pid:
                    skipped.append(("(standalone note)", "no parent item"))
                    continue
                title = zot.field(pid, "title")
                if title in SKIP_TITLES:
                    skipped.append((title[:44], SKIP_TITLES[title]))
                    continue
                year = year_of(zot, pid, title)
                if not year:
                    skipped.append((title[:44], "no year in Zotero"))
                    continue

                authors = zot.creators(pid)
                name = reading_name(authors, year)

                # Two notes can land on the same author-and-year label — a
                # second note on one article, or two papers from the same year.
                # Suffix the later ones rather than letting the first win
                # silently.
                if name in claimed and claimed[name] != _nid:
                    base = name
                    for suffix in "bcdefghijklmnopqrstuvwxyz":
                        if f"{base}{suffix}" not in claimed:
                            name = f"{base}{suffix}"
                            break
                    renamed.append((base, name))
                claimed[name] = _nid

                folder = dest_root / name
                target = folder / f"{name.replace(' ', '_')}_Notes.tex"

                if target.exists() and not args.force:
                    existing.append(name)
                    continue

                if not args.dry_run:
                    folder.mkdir(parents=True, exist_ok=True)
                body, n_img, dropped_imgs = html_to_tex(
                    note, folder, zot, args.dry_run)
                for key in dropped_imgs:
                    lost.append(f"{name}: image attachment {key} unreadable")
                doc = (
                    PREAMBLE
                    + "\n\\title{\\textbf{" + esc(name) + " RG Notes}\\\\\n"
                    + "\\large " + citation(zot, pid, authors, year) + "}\n"
                    + "\\author{}\n\\date{}\n\n"
                    + "\\begin{document}\n\n\\maketitle\n\n"
                    + body + "\n\n\\end{document}\n"
                )
                if not args.dry_run:
                    target.write_text(doc, encoding="utf-8")
                written.append((name, len(body.split()), n_img,
                                "EXTERNAL" if "Extra Readings" in str(dest_root) else ""))

        verb = "Would import" if args.dry_run else "Imported"
        print(f"{verb} {len(written)} notes\n")
        for name, words, n_img, tag in sorted(written):
            extra = f"  [{n_img} image(s)]" if n_img else ""
            print(f"   {name:34} {words:>5} words {tag}{extra}")
        if existing:
            print(f"\nAlready present, left untouched ({len(existing)}):")
            for n in sorted(existing):
                print(f"   {n}")
        if skipped:
            print(f"\nSkipped ({len(skipped)}):")
            for n, why in sorted(skipped):
                print(f"   {n} — {why}")
        if renamed:
            print(f"\nName already taken, filed under a suffix ({len(renamed)}):")
            for base, new in renamed:
                print(f"   {base} -> {new}")
        if lost:
            print(f"\nWARNING — images that could not be recovered ({len(lost)}):")
            for n in lost:
                print(f"   {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
