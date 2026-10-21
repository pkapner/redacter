"""Entry point for the bundled app (see redacter.spec)."""

import sys


def selftest() -> int:
    """Redact a generated sample end to end, to check a fresh bundle works."""
    import tempfile
    from pathlib import Path

    import pymupdf

    from redacter.config import Config
    from redacter.redact import redact_file

    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "in.pdf", Path(tmp) / "out.pdf"
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "SSN 123-45-6789", fontsize=12)
        doc.save(src)
        result = redact_file(src, out, Config())
        text = pymupdf.open(out)[0].get_text()
        ok = result.verification.ok and "6789" not in text
        # the parallel batch path: worker processes must start in the bundle
        from redacter.batch import BatchJob, run_batch
        jobs = [BatchJob(i, src, Path(tmp) / f"b{i}.pdf") for i in range(2)]
        ok = ok and all(r.ok and r.applied == 1 for r in run_batch(jobs, Config(), workers=2))
    from PySide6 import QtWidgets  # the bundled Qt libraries load
    ok = ok and hasattr(QtWidgets, "QApplication")
    print("selftest", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    # Worker processes of a frozen app re-run this executable; this makes
    # them run their task instead of starting another copy of the app.
    import multiprocessing
    multiprocessing.freeze_support()
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    from redacter.gui import main
    sys.exit(main())
