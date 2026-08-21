#!/usr/bin/env python3
"""
Zotero library exporter — writes the whole library out as zotero.bib.

Reads the local Zotero database and emits a BibTeX file covering every
citable item, so anything in the library can be cited without exporting by
hand. The curated refs.bib is left untouched and stays authoritative: any
item whose generated key already exists there is skipped, so keys you
already cite can never be redefined or broken.

Cite from either file — documents load both:

    \\bibliography{../refs,../zotero}

Citation keys are surname(s) plus year: putnam1993, hubershipan2002,
przeworskietal2000. Where that collides, a letter is appended
(fearon2003, fearon2003a, ...). Keys are stable between runs as long as the
item's authors and year do not change.

The Zotero database is opened from a temporary copy and never written to,
so this is safe to run while Zotero is open.

Usage:
    python3 tools/export_zotero_bib.py            # write zotero.bib + index
    python3 tools/export_zotero_bib.py --dry-run  # report only
"""

import argparse
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = Path.home() / "Zotero" / "zotero.sqlite"
CURATED = REPO / "refs.bib"
OUT_BIB = REPO / "zotero.bib"
OUT_INDEX = REPO / "zotero-keys.txt"

# Zotero item type -> BibTeX entry type.
TYPE_MAP = {
    "journalArticle": "article",
    "magazineArticle": "article",
    "newspaperArticle": "article",
    "encyclopediaArticle": "incollection",
    "book": "book",
    "bookSection": "incollection",
    "conferencePaper": "inproceedings",
    "thesis": "phdthesis",
    "report": "techreport",
    "manuscript": "unpublished",
    "preprint": "misc",
    "webpage": "misc",
    "blogPost": "misc",
    "document": "misc",
    "dataset": "misc",
    "presentation": "misc",
    "videoRecording": "misc",
    "podcast": "misc",
    "interview": "misc",
    "letter": "misc",
    "film": "misc",
    "statute": "misc",
    "case": "misc",
    "bill": "misc",
    "hearing": "misc",
    "patent": "misc",
    "map": "misc",
    "artwork": "misc",
    "audioRecording": "misc",
    "computerProgram": "misc",
    "email": "misc",
    "forumPost": "misc",
    "instantMessage": "misc",
    "radioBroadcast": "misc",
    "tvBroadcast": "misc",
}

# Zotero field -> BibTeX field, applied per entry type where sensible.
FIELD_MAP = {
    "title": "title",
    "publicationTitle": "journal",
    "bookTitle": "booktitle",
    "proceedingsTitle": "booktitle",
    "encyclopediaTitle": "booktitle",
    "publisher": "publisher",
    "place": "address",
    "volume": "volume",
    "issue": "number",
    "pages": "pages",
    "edition": "edition",
    "series": "series",
    "DOI": "doi",
    "url": "url",
    "institution": "institution",
    "university": "school",
    "abstractNote": None,   # deliberately dropped: noise in a .bib
}

LATEX_SPECIALS = [
    ("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
    ("#", r"\#"), ("_", r"\_"), ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
]
TEXT_SUBS = [("\u2014", "---"), ("\u2013", "--"), ("\u2018", "`"),
             ("\u2019", "'"), ("\u201c", "``"), ("\u201d", "''"),
             ("\u00a0", " "), ("\u2026", r"\ldots{}")]


def esc(text):
    """Escape a Zotero field for LaTeX.

    Backslashes and braces present in the source are parked behind sentinels
    first: otherwise the final brace pass would also escape the braces this
    function itself introduces, turning \\ldots{} into \\ldots\\{\\}.
    """
    text = str(text)
    # Zotero fields scraped from the web carry invisible bidi/format marks
    # (U+202A, U+200E, ...). pdflatex refuses them outright, so drop every
    # control and format character before anything else.
    text = "".join(ch for ch in text
                   if unicodedata.category(ch) not in ("Cc", "Cf")
                   or ch in "\n\t")
    text = text.replace("\\", "\x00").replace("{", "\x01").replace("}", "\x02")
    for a, b in LATEX_SPECIALS[1:]:      # skip the backslash rule, done above
        text = text.replace(a, b)
    for a, b in TEXT_SUBS:
        text = text.replace(a, b)
    return (text.replace("\x00", r"\textbackslash{}")
                .replace("\x01", r"\{").replace("\x02", r"\}"))


def ascii_key(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", text.lower())


class Zot:
    def __init__(self, db):
        self.c = sqlite3.connect(db)

    def items(self):
        return self.c.execute(
            """select i.itemID, it.typeName
               from items i
               join itemTypes it on it.itemTypeID = i.itemTypeID
               where it.typeName not in ('attachment','note','annotation')
                 and i.itemID not in (select itemID from deletedItems)
               order by i.itemID""").fetchall()

    def fields(self, item):
        return {r[0]: r[1] for r in self.c.execute(
            """select f.fieldName, v.value from itemData d
               join fields f on f.fieldID = d.fieldID
               join itemDataValues v on v.valueID = d.valueID
               where d.itemID = ?""", (item,))}

    def creators(self, item):
        return self.c.execute(
            """select ct.creatorType, cd.firstName, cd.lastName
               from itemCreators ic
               join creators cd on cd.creatorID = ic.creatorID
               join creatorTypes ct on ct.creatorTypeID = ic.creatorTypeID
               where ic.itemID = ? order by ic.orderIndex""", (item,)).fetchall()


def name_list(people):
    """BibTeX 'Last, First and Last, First'.

    Zotero single-field names hold institutions ("Ireland Department of
    Communications, Climate Action, and Environment") and occasionally a whole
    author list pasted into one box. Their commas make BibTeX read them as
    Last, First and abort, so single-field names are braced to keep them
    atomic.
    """
    out = []
    for _t, first, last in people:
        first, last = (first or "").strip(), (last or "").strip()
        if first and last:
            out.append(f"{last}, {first}")
        elif last or first:
            solo = last or first
            out.append(f"{{{solo}}}" if ("," in solo or " and " in solo) else solo)
    return " and ".join(n for n in out if n)


STOPWORDS = {"the", "a", "an", "of", "and", "in", "on", "for", "to", "at",
             "is", "as", "by", "with", "from"}


def title_stem(title):
    """A key stem for items with no author: first meaningful title words."""
    words = [ascii_key(w) for w in re.split(r"\W+", title or "")]
    words = [w for w in words if w and w not in STOPWORDS]
    return "".join(words[:3])[:28]


def make_key(authors, year, title=""):
    surnames = [ascii_key(l) for _t, _f, l in authors if (l or "").strip()]
    if not surnames:
        # Anonymous items are common (web pages, agency reports). Naming them
        # after the title keeps keys meaningful and, more importantly, keeps
        # dozens of them from colliding on a single "anon" stem.
        stem = title_stem(title) or "anon"
    elif len(surnames) == 1:
        stem = surnames[0]
    elif len(surnames) == 2:
        stem = surnames[0] + surnames[1]
    else:
        stem = surnames[0] + "etal"
    # A single Zotero name field sometimes holds an entire author list, which
    # would otherwise yield a 60-character key nobody can type.
    return f"{stem[:26]}{year or 'nd'}"


def curated_keys():
    if not CURATED.exists():
        return set()
    return set(re.findall(r"@\w+\{([^,]+),", CURATED.read_text(encoding="utf-8")))


def build():
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "zotero.sqlite"
        shutil.copy2(DB, snap)
        zot = Zot(snap)

        reserved = curated_keys()
        used, entries, index, skipped = set(reserved), [], [], []

        for item_id, type_name in zot.items():
            f = zot.fields(item_id)
            people = zot.creators(item_id)
            authors = [p for p in people if p[0] == "author"] or \
                      [p for p in people if p[0] in ("editor", "contributor")]
            m = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", f.get("date", "") or "")
            year = m.group(1) if m else ""

            key = make_key(authors, year, f.get("title", ""))
            if key in reserved:
                skipped.append(key)          # refs.bib already defines it
                continue
            if key in used:                  # genuine clash inside the library
                base, n = key, 0
                while key in used:           # a-z, then a2, b2, ... — never
                    n += 1                   # gives up, so keys stay unique
                    letter = "abcdefghijklmnopqrstuvwxyz"[(n - 1) % 26]
                    run = (n - 1) // 26
                    key = f"{base}{letter}" + (str(run + 1) if run else "")
            used.add(key)

            btype = TYPE_MAP.get(type_name, "misc")
            lines = [f"@{btype}{{{key},"]

            if authors:
                lines.append(f"  author    = {{{esc(name_list(authors))}}},")
            editors = [p for p in people if p[0] == "editor"]
            if editors and authors and editors != authors:
                lines.append(f"  editor    = {{{esc(name_list(editors))}}},")

            for zfield, bfield in FIELD_MAP.items():
                if not bfield or zfield not in f:
                    continue
                val = f[zfield].strip()
                if not val:
                    continue
                # A book's own title must not also become booktitle.
                if bfield == "booktitle" and btype == "book":
                    continue
                lines.append(f"  {bfield:9} = {{{esc(val)}}},")

            if year:
                lines.append(f"  year      = {{{year}}},")
            accessed = (f.get("accessDate") or "")[:10]
            if btype == "misc" and f.get("url") and accessed:
                lines.append(f"  note      = {{Accessed {esc(accessed)}}},")
            lines.append(f"  keywords  = {{zotero}}")
            lines.append("}")
            entries.append("\n".join(lines))

            who = authors[0][2] if authors else "?"
            index.append(f"{key:34} {who[:22]:24} {year or '----':6} "
                         f"{(f.get('title','') or '')[:62]}")

        return entries, index, skipped, len(reserved)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    args = ap.parse_args()

    if not DB.exists():
        print(f"No Zotero database at {DB}")
        return 1

    entries, index, skipped, n_reserved = build()

    header = (
        "% =====================================================================\n"
        "%  AUTO-GENERATED by tools/export_zotero_bib.py — DO NOT EDIT.\n"
        "%  Every citable item in the Zotero library. Regenerate after adding\n"
        "%  items in Zotero; hand-written entries belong in refs.bib instead,\n"
        "%  which takes precedence and is never overwritten.\n"
        "% =====================================================================\n\n"
    )

    if not args.dry_run:
        OUT_BIB.write_text(header + "\n\n".join(entries) + "\n", encoding="utf-8")
        OUT_INDEX.write_text(
            f"{'KEY':34} {'FIRST AUTHOR':24} {'YEAR':6} TITLE\n"
            + "-" * 100 + "\n"
            + "\n".join(sorted(index)) + "\n", encoding="utf-8")

    verb = "Would write" if args.dry_run else "Wrote"
    print(f"{verb} {OUT_BIB.name}: {len(entries)} entries")
    print(f"{verb} {OUT_INDEX.name}: searchable key list")
    print(f"\n  refs.bib keys left authoritative: {n_reserved}")
    print(f"  library items skipped as already in refs.bib: {len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
