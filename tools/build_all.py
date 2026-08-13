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

# The compendium references images by repo-relative path, so unlike every
# other file it must be compiled from the repo root rather than -cd.
COMPENDIUM = REPO / "Compendium" / "compendium.tex"

AUX_SUFFIXES = (
    ".aux", ".log", ".out", ".toc", ".fls", ".fdb_latexmk",
    ".synctex.gz", ".blg", ".bcf", ".run.xml", ".lof", ".lot",
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


def compile_one(tex, force=False):
    """Return (tex, ok, message). Compiles in the file's own directory."""
    rel = tex.relative_to(REPO)
    cmd = ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-quiet"]
    if force:
        cmd.append("-g")

    if tex.resolve() == COMPENDIUM.resolve():
        cmd += [f"-outdir={tex.parent}", str(tex)]
        cwd = REPO
    else:
        cmd += ["-cd", str(tex)]
        cwd = REPO

    started = time.time()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    secs = time.time() - started

    if proc.returncode != 0:
        return rel, False, first_error(tex, proc)
    return rel, True, f"{secs:.1f}s"


def first_error(tex, proc):
    """Pull the real TeX error (and its line number) out of the .log."""
    log = tex.with_suffix(".log")
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


def clean():
    n = 0
    for p in REPO.rglob("*"):
        if any(part in SKIP_PARTS for part in p.relative_to(REPO).parts):
            continue
        if p.is_file() and p.name.endswith(AUX_SUFFIXES):
            p.unlink()
            n += 1
    print(f"Removed {n} auxiliary files")


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
    ap.add_argument("--clean", action="store_true", help="delete aux files and exit")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 4))
    args = ap.parse_args()

    if not shutil.which("latexmk"):
        sys.exit("latexmk not found on PATH")

    if args.clean:
        clean()
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
