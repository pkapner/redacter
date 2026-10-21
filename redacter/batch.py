"""Redact many PDFs in parallel, one process per file (up to one per core).

Kept free of Qt so worker processes start quickly. Each worker rescans its
file itself instead of receiving a match list, and keep choices travel as
match keys (see detect.match_key), which survive the renumbering.
"""

from __future__ import annotations

import multiprocessing
import os
import queue as queue_mod
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Box, Config


@dataclass
class BatchJob:
    index: int                       # caller's identifier for the file
    src: Path
    out: Path
    password: str | None = None
    kept_keys: set[tuple] = field(default_factory=set)
    boxes: list[Box] = field(default_factory=list)


@dataclass
class BatchResult:
    index: int
    out: Path
    matches: int = 0
    applied: int = 0
    kept: int = 0
    boxes: int = 0
    leaks: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.leaks


def redact_one(job: BatchJob, cfg: Config, progress_queue=None) -> BatchResult:
    """Scan, redact and verify one file. Runs in a worker process."""
    from .detect import find_matches, match_key
    from .redact import open_pdf, redact_file

    def progress(what, done, total):
        if progress_queue is not None:
            progress_queue.put((job.index, what, done, total))

    try:
        doc = open_pdf(job.src, job.password, prompt=False)
        matches = find_matches(doc, cfg, progress)
        doc.close()
        kept = {m.id for m in matches if match_key(m) in job.kept_keys}
        res = redact_file(job.src, job.out, cfg, matches, kept, job.boxes,
                          job.password, progress)
    except Exception as e:  # reported per file; the batch carries on
        return BatchResult(job.index, job.out, error=str(e) or type(e).__name__)
    return BatchResult(job.index, job.out, len(matches), len(res.applied),
                       len(res.skipped), res.boxes, list(res.verification.leaks))


def run_batch(
    jobs: list[BatchJob],
    cfg: Config,
    progress: Callable[[str, int, int], None] | None = None,
    workers: int | None = None,
) -> list[BatchResult]:
    """Run jobs across processes; results come back in job order."""
    if not jobs:
        return []
    workers = max(1, min(len(jobs), workers or os.cpu_count() or 1))
    names = {j.index: j.src.name for j in jobs}
    # spawn, not fork: forking a process that runs Qt threads is unsafe,
    # and spawn is what macOS uses anyway.
    ctx = multiprocessing.get_context("spawn")
    results: dict[int, BatchResult] = {}
    with ctx.Manager() as manager:
        q = manager.Queue()
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            futures = {pool.submit(redact_one, j, cfg, q): j for j in jobs}
            latest: dict[int, tuple] = {}
            while len(results) < len(jobs):
                try:
                    idx, what, done, total = q.get(timeout=0.1)
                    latest[idx] = (what, done, total)
                except queue_mod.Empty:
                    pass
                for fut, job in futures.items():
                    if job.index not in results and fut.done():
                        exc = fut.exception()
                        results[job.index] = fut.result() if exc is None else BatchResult(
                            job.index, job.out, error=str(exc) or type(exc).__name__)
                        latest.pop(job.index, None)
                if progress:
                    finished = len(results)
                    if latest:
                        idx, (what, done, total) = next(iter(latest.items()))
                        detail = f"{names[idx]}: {what.lower()}"
                        if total > 1:
                            detail += f" {done + 1}/{total}"
                    else:
                        detail = "finishing"
                    busy = len(latest)
                    progress(f"{finished} of {len(jobs)} files done, {busy} in progress "
                             f"({workers} at a time). {detail}", finished, len(jobs))
    return [results[j.index] for j in jobs]


def output_paths(sources: list[Path], folder: Path) -> list[Path]:
    """<stem>.redacted.pdf in folder for each source, made unique."""
    used: set[str] = set()
    outs = []
    for src in sources:
        name = f"{src.stem}.redacted.pdf"
        n = 2
        while name.lower() in used or (folder / name).resolve() == src.resolve():
            name = f"{src.stem}.redacted-{n}.pdf"
            n += 1
        used.add(name.lower())
        outs.append(folder / name)
    return outs
