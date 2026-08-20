#!/usr/bin/env python3
"""
Batch builder — compiles every .tex in the repo to PDF, in parallel.

By default only rebuilds files whose .tex is newer than its .pdf, so a
routine run after editing two notes takes seconds rather than minutes.
Each file is compiled in its own directory (latexmk -cd) so that bare
image references like {fig1.jpg} resolve the way they do in your editor.

Usage:
    python3 tools/build_all.py              # build what changed
    python3 tools/build_all.py --all        # force rebuild everything
    python3 tools/build_all.py --watch      # rebuild on save, until Ctrl-C
    python3 tools/build_all.py --clean      # remove aux files, keep PDFs
    python3 tools/build_all.py "Dahl*"      # only matching paths

No dependencies beyond the standard library and latexmk.
"""

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_PARTS = {".git", "__pycache__", "tools"}

# Hidden per-folder scratch dir for LaTeX's auxiliary files. Dot-prefixed so
# Finder and most editors hide it; latexmk keeps its .fdb_latexmk database
# here, so incremental rebuilds still work.
BUILD_DIR = ".build"

# The compendium references images by repo-relative path, so unlike every
# other file it must be compiled from the repo root rather than -cd.
COMPENDIUM = REPO / "Comprehensive Content" / "Compendium" / "compendium.tex"

AUX_SUFFIXES = (
    ".aux", ".log", ".out", ".toc", ".fls", ".fdb_latexmk",
    ".synctex.gz", ".blg", ".bbl", ".bcf", ".run.xml", ".lof", ".lot",
)


def tex_files(patterns=None):
    out = []
    for p in sorted(REPO.rglob("*.tex")):
        rel = p.relative_to(REPO)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        if patterns and not any(
            fnmatch.fnmatch(str(rel), pat) or fnmatch.fnmatch(p.name, pat)
            for pat in patterns
        ):
            continue
        out.append(p)
    return out


def is_stale(tex):
    pdf = tex.with_suffix(".pdf")
    if not pdf.exists():
        return True
    return tex.stat().st_mtime > pdf.stat().st_mtime


REL_BIB_RE = re.compile(r"\\(?:bibliography|addbibresource)\{[^}]*\.\.\/")


def needs_in_place(tex):
    """True if the file cites a .bib outside its own folder.

    BibTeX resolves such a path against its working directory, which under
    -outdir is .build/ — one level too deep — and no BIBINPUTS or -bibfudge
    setting overrides that. These files are built in place instead.
    """
    try:
        return bool(REL_BIB_RE.search(tex.read_text(errors="replace")))
    except OSError:
        return False


def compile_in_place(tex, cmd, aux, rel):
    """Build in the source folder, then tuck the aux files back into .build."""
    aux.mkdir(exist_ok=True)
    stem = tex.stem

    # Restore any previous aux state so latexmk can still build incrementally.
    for p in aux.glob(stem + ".*"):
        if p.suffix != ".pdf":
            shutil.move(str(p), tex.parent / p.name)

    started = time.time()
    proc = subprocess.run(cmd + [tex.name], cwd=tex.parent,
                          capture_output=True, text=True)
    secs = time.time() - started

    ok = proc.returncode == 0
    msg = f"{secs:.1f}s" if ok else first_error_at(tex.parent / (stem + ".log"), proc)

    # Sweep the clutter out of the visible folder either way. Matched on the
    # full name, since ".synctex.gz" is a two-part suffix that .stem misses.
    for p in tex.parent.iterdir():
        if (p.is_file() and p.name.startswith(stem + ".")
                and p.name.endswith(AUX_SUFFIXES)):
            shutil.move(str(p), aux / p.name)

    return rel, ok, msg


def compile_one(tex, force=False):
    """Return (tex, ok, message).

    All the auxiliary noise (.aux/.log/.fls/...) is confined to a hidden
    BUILD_DIR beside the source; only the finished PDF is copied back out,
    so each reading folder shows just its .tex and .pdf.
    """
    rel = tex.relative_to(REPO)
    aux = tex.parent / BUILD_DIR
    cmd = ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-quiet"]
    if force:
        cmd.append("-g")

    if needs_in_place(tex):
        return compile_in_place(tex, cmd, aux, rel)

    if tex.resolve() == COMPENDIUM.resolve():
        # Image paths inside the compendium are repo-relative, so it has to
        # be compiled from the repo root rather than its own folder.
        cmd += [f"-outdir={aux.relative_to(REPO)}", str(rel)]
        cwd = REPO
    else:
        cmd += [f"-outdir={BUILD_DIR}", tex.name]
        cwd = tex.parent

    # Running with -outdir means BibTeX resolves relative paths (e.g.
    # \bibliography{../refs}) against .build/ rather than the source folder,
    # so put the real locations on its search path. Trailing ':' keeps the
    # system defaults.
    env = dict(os.environ)
    roots = f"{tex.parent}:{REPO}:"
    env["BIBINPUTS"] = roots + env.get("BIBINPUTS", "")
    env["TEXINPUTS"] = roots + env.get("TEXINPUTS", "")

    started = time.time()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    secs = time.time() - started

    if proc.returncode != 0:
        return rel, False, first_error(tex, proc)

    built = aux / (tex.stem + ".pdf")
    if not built.exists():
        return rel, False, "no PDF produced"
    shutil.copy2(built, tex.with_suffix(".pdf"))
    return rel, True, f"{secs:.1f}s"


def first_error(tex, proc):
    """Pull the real TeX error (and its line number) out of the .log."""
    return first_error_at(tex.parent / BUILD_DIR / (tex.stem + ".log"), proc)


def first_error_at(log, proc):
    if log.exists():
        lines = log.read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            if line.startswith("!") and "Fatal error" not in line:
                # The "l.<n> <source>" line a few rows down locates it.
                where = ""
                for nxt in lines[i + 1 : i + 8]:
                    m = re.match(r"l\.(\d+)", nxt)
                    if m:
                        where = f" (line {m.group(1)})"
                        break
                return f"{line.lstrip('! ').strip()}{where}"
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return tail[-1][:160] if tail else "unknown error"


def clean(deep=False):
    """Sweep loose aux files. With deep=True, drop the .build dirs too."""
    files = dirs = 0
    for p in list(REPO.rglob("*")):
        rel = p.relative_to(REPO)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        if p.is_file() and p.name.endswith(AUX_SUFFIXES) and BUILD_DIR not in rel.parts:
            p.unlink()
            files += 1
    if deep:
        for p in list(REPO.rglob(BUILD_DIR)):
            if p.is_dir():
                shutil.rmtree(p)
                dirs += 1
    msg = f"Removed {files} loose auxiliary file(s)"
    if deep:
        msg += f" and {dirs} {BUILD_DIR}/ dir(s)"
    print(msg)


def run(files, force, jobs):
    if not files:
        print("Nothing to build — everything is up to date.")
        return 0

    print(f"Building {len(files)} file(s) with {jobs} workers...\n")
    failures = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(compile_one, f, force) for f in files]
        for fut in futures:
            rel, ok, msg = fut.result()
            mark = "ok  " if ok else "FAIL"
            print(f"  [{mark}] {rel}  {msg if not ok else '(' + msg + ')'}")
            if not ok:
                failures.append((rel, msg))

    print()
    if failures:
        print(f"{len(failures)} failed:")
        for rel, msg in failures:
            print(f"  {rel}\n      {msg}")
        return 1
    print(f"All {len(files)} built successfully.")
    return 0


def watch(patterns, jobs):
    print("Watching for changes — Ctrl-C to stop.\n")
    seen = {p: p.stat().st_mtime for p in tex_files(patterns)}
    try:
        while True:
            time.sleep(1)
            current = tex_files(patterns)
            changed = []
            for p in current:
                m = p.stat().st_mtime
                if p not in seen or m > seen[p]:
                    changed.append(p)
                seen[p] = m
            if changed:
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] {len(changed)} change(s) detected")
                run(changed, False, jobs)
                print()
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("patterns", nargs="*", help="optional glob(s) to filter files")
    ap.add_argument("--all", action="store_true", help="rebuild even if up to date")
    ap.add_argument("--watch", action="store_true", help="rebuild on save")
    ap.add_argument("--clean", action="store_true",
                    help="delete stray aux files littering the reading folders")
    ap.add_argument("--clean-all", action="store_true",
                    help="also delete the hidden .build dirs (forces full rebuild)")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 4))
    args = ap.parse_args()

    if not shutil.which("latexmk"):
        sys.exit("latexmk not found on PATH")

    if args.clean or args.clean_all:
        clean(deep=args.clean_all)
        return 0

    patterns = args.patterns or None

    if args.watch:
        return watch(patterns, args.jobs)

    files = tex_files(patterns)
    if not args.all:
        files = [f for f in files if is_stale(f)]
    return run(files, args.all, args.jobs)


if __name__ == "__main__":
    sys.exit(main())
