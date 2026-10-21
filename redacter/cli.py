"""Command-line interface: redacter init | scan | apply."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import BUILTIN_PATTERNS, ConfigError, load_config
import pymupdf

from .detect import TEXT_FLAGS, find_matches, mask_text, page_warnings
from .redact import RedactError, open_pdf, redact_document, render_preview, save, verify

EXAMPLE_CONFIG = Path(__file__).with_name("redact.example.toml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="redacter",
        description="Find and permanently remove PII from text-based PDFs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write a starter config file")
    p_init.add_argument("path", nargs="?", type=Path, default=Path("redact.toml"))

    def common(p):
        p.add_argument("input", type=Path, help="PDF to process")
        p.add_argument("-c", "--config", type=Path,
                       help="TOML config with your terms (default: ./redact.toml if present)")
        p.add_argument("--password", help="password for an encrypted PDF (prompted if needed)")

    p_scan = sub.add_parser("scan", help="list what would be redacted, change nothing")
    common(p_scan)
    p_scan.add_argument("--preview", type=Path, metavar="DIR",
                        help="render pages to PNG with planned redactions outlined")
    p_scan.add_argument("--skip", default="", metavar="IDS",
                        help="match IDs to keep, e.g. 3,7 (shown in green in the preview)")
    p_scan.add_argument("--json", action="store_true", help="print matches as JSON")
    p_scan.add_argument("--mask", action="store_true",
                        help="hide digits in the listing (#), for sharing it")

    p_apply = sub.add_parser("apply", help="write a redacted copy and verify it")
    common(p_apply)
    p_apply.add_argument("-o", "--output", type=Path,
                         help="output PDF (default: INPUT.redacted.pdf)")
    p_apply.add_argument("--skip", default="", metavar="IDS",
                         help="match IDs from 'scan' to leave unredacted, e.g. 3,7")
    p_apply.add_argument("--preview", type=Path, metavar="DIR",
                         help="render the redacted pages to PNG for checking")
    p_apply.add_argument("-f", "--force", action="store_true",
                         help="overwrite the output file if it exists")

    p_inspect = sub.add_parser(
        "inspect", help="show how the text near a label is read, digits masked")
    p_inspect.add_argument("input", type=Path)
    p_inspect.add_argument("label", help='text to look for, e.g. "social security"')
    p_inspect.add_argument("--below", type=float, default=60,
                           help="points below the label to include (default 60)")
    p_inspect.add_argument("--password")

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "inspect":
            return cmd_inspect(args)
        cfg = load_config(_config_path(args.config))
        skip = _parse_ids(args.skip)
        if args.command == "scan":
            return cmd_scan(args, cfg, skip)
        return cmd_apply(args, cfg, skip)
    except (ConfigError, RedactError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def cmd_init(args) -> int:
    if args.path.exists():
        print(f"error: {args.path} already exists", file=sys.stderr)
        return 1
    fd = os.open(args.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(EXAMPLE_CONFIG.read_text())
    print(f"wrote {args.path} (mode 600). Add your names and addresses to 'terms'.")
    print("Keep this file out of version control: it lists your PII.")
    return 0


def cmd_inspect(args) -> int:
    """Show what the PDF holds around a label, without revealing any numbers.

    Prints every text span near the label (by position, not reading order)
    with its font, and masks characters by class, so the output can be shared
    to debug a missed match. Also counts images and drawn shapes nearby,
    which text search cannot see.
    """
    doc = open_pdf(args.input, args.password)
    found = False
    for pno, page in enumerate(doc):
        for label in page.search_for(args.label):
            found = True
            region = pymupdf.Rect(label.x0 - 40, label.y0 - 2,
                                  label.x1 + 350, label.y1 + args.below)
            print(f"page {pno + 1}: label at x={label.x0:.0f} y={label.y0:.0f}, "
                  f"showing {region.width:.0f}x{region.height:.0f}pt below/right of it")
            raw = page.get_text("rawdict", flags=TEXT_FLAGS)
            n = 0
            for block in raw["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = "".join(ch["c"] for ch in span["chars"])
                        if not text.strip() or not pymupdf.Rect(span["bbox"]).intersects(region):
                            continue
                        n += 1
                        dx, dy = line["dir"]
                        rot = "" if abs(dy) < 1e-3 and dx > 0 else f" dir=({dx:.2f},{dy:.2f})"
                        x0, y0 = span["bbox"][:2]
                        print(f"  x={x0:5.0f} y={y0:5.0f} {span['font']!r} "
                              f"{span['size']:.1f}pt{rot}: {mask_text(text)}")
            if n == 0:
                print("  no text here")
            images = [i for i in page.get_image_info() if pymupdf.Rect(i["bbox"]).intersects(region)]
            shapes = [d for d in page.get_drawings() if d["rect"].intersects(region)]
            print(f"  {len(images)} image(s) and {len(shapes)} drawn shape(s) overlap this area")
    if not found:
        print(f"{args.label!r} not found in the PDF's text. If you can see it on the "
              f"page, that part is an image or drawn shapes, not text.")
    else:
        print("\nkey: # digit  \u25a1 control char  \u25c7 private-use char  "
              "\ufffd unmapped glyph  \u00a4 other non-ASCII")
    return 0


def cmd_scan(args, cfg, skip: set[int]) -> int:
    doc = open_pdf(args.input, args.password)
    matches = find_matches(doc, cfg)
    _check_skip(skip, matches)

    if args.json:
        print(json.dumps([
            {"id": m.id, "page": m.page + 1, "kind": m.kind, "text": m.text,
             "context": m.context, "skip": m.id in skip,
             "rects": [list(r) for r in m.rects]}
            for m in matches
        ], indent=2))
    else:
        _print_table(matches, skip, args.mask)
        if cfg.boxes:
            print(f"\nplus {len(cfg.boxes)} manual box(es) from the config")
        _print_warnings(doc, cfg)

    if args.preview:
        paths = render_preview(doc, args.preview, matches, skip)
        print(f"\npreview: {len(paths)} page(s) in {args.preview}/ "
              f"(red = will be redacted, green = skipped). "
              f"These images show the UNREDACTED text, so delete them when done.",
              file=sys.stderr)
    return 0


def cmd_apply(args, cfg, skip: set[int]) -> int:
    out = args.output or args.input.with_name(args.input.stem + ".redacted.pdf")
    if out.resolve() == args.input.resolve():
        raise RedactError("output must differ from input")
    if out.exists() and not args.force:
        raise RedactError(f"{out} exists (use --force to overwrite)")

    doc = open_pdf(args.input, args.password)
    matches = find_matches(doc, cfg)
    _check_skip(skip, matches)
    _print_warnings(doc, cfg)

    applied = redact_document(doc, matches, cfg, skip)
    skipped = [m for m in matches if m.id in skip]
    save(doc, out)
    doc.close()

    result = verify(out, cfg, applied, skipped)
    if not result.ok:
        out.unlink()
        print("error: verification FAILED, output deleted:", file=sys.stderr)
        for leak in result.leaks:
            print(f"  {leak}", file=sys.stderr)
        return 2

    print(f"redacted {len(applied)} match(es)"
          + (f" and {len(cfg.boxes)} manual box(es)" if cfg.boxes else "")
          + (f", kept {len(skipped)}" if skipped else "")
          + f" -> {out}")
    print("verified: no detected PII remains in the text, and no redacted string "
          "remains in the file")

    if args.preview:
        out_doc = open_pdf(out)
        paths = render_preview(out_doc, args.preview)
        print(f"preview: {len(paths)} page(s) in {args.preview}/")
    return 0


def _config_path(given: Path | None) -> Path | None:
    if given:
        return given
    default = Path("redact.toml")
    return default if default.exists() else None


def _parse_ids(s: str) -> set[int]:
    try:
        return {int(x) for x in s.replace(" ", "").split(",") if x}
    except ValueError:
        raise ConfigError(f"--skip takes comma-separated match IDs, got {s!r}")


def _check_skip(skip: set[int], matches) -> None:
    unknown = skip - {m.id for m in matches}
    if unknown:
        raise ConfigError(
            f"--skip IDs not found: {', '.join(map(str, sorted(unknown)))}. "
            f"IDs come from 'scan' with the same file and config."
        )


def _print_table(matches, skip: set[int], mask: bool = False) -> None:
    if not matches:
        print("no matches")
        return
    w_kind = max(len("type"), *(len(m.kind) for m in matches))
    w_text = min(40, max(len("text"), *(len(m.text) for m in matches)))
    print(f"{'id':>4}  {'page':>4}  {'type':<{w_kind}}  {'text':<{w_text}}  context")
    for m in matches:
        mark = "  (skip)" if m.id in skip else ""
        text = m.text.replace("\n", " ").replace("\t", " ")
        context = m.context
        if mask:
            text, context = mask_text(text), mask_text(context)
        print(f"{m.id:>4}  {m.page + 1:>4}  {m.kind:<{w_kind}}  "
              f"{text[:w_text]:<{w_text}}  {context}{mark}")
    print(f"\n{len(matches)} match(es); {len(skip)} skipped")


def _print_warnings(doc, cfg) -> None:
    for w in page_warnings(doc):
        print(f"warning: {w}", file=sys.stderr)
    if not cfg.terms:
        print("warning: no 'terms' configured, so names and addresses will NOT be "
              "redacted (run 'redacter init' and edit redact.toml)", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
