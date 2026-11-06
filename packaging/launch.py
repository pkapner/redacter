"""Entry point for the bundled app (see redacter.spec)."""

import sys


def ocr_selftest(tmp) -> bool:
    """Real Vision OCR on a scanned-looking page: is the SSN found, and is
    it boxed where it is in the image (not where a text layer says)?"""
    import pymupdf

    from redacter import ocr
    from redacter.config import Config
    from redacter.detect import find_matches

    if ocr.engine() is None:
        print("selftest: Vision OCR is not available")
        return False
    src = pymupdf.open()
    page = src.new_page()
    page.insert_text((72, 120), "Policyholder: Mary Major  SSN 123-45-6789", fontsize=16)
    where = page.search_for("123-45-6789")[0]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3))
    scan = pymupdf.open()
    scan.new_page(width=page.rect.width, height=page.rect.height).insert_image(
        page.rect, pixmap=pix)
    cfg = Config()
    cfg.terms = ["Mary Major"]
    found = {m.text: m for m in find_matches(scan, cfg)}
    ssn = found.get("123-45-6789")
    ok = ssn is not None and "Mary Major" in found and ssn.rects[0].intersects(where)
    print("selftest OCR:", "OK" if ok else f"FAILED (read {sorted(found)})")
    return ok


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
        if sys.platform == "darwin":
            ok = ok and ocr_selftest(Path(tmp))
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
