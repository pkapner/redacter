"""Command-line interface: redacter init | scan | apply."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import BUILTIN_PATTERNS, ConfigError, load_config
from .detect import find_matches, page_warnings
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

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return cmd_init(args)
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
        _print_table(matches, skip)
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


def _print_table(matches, skip: set[int]) -> None:
    if not matches:
        print("no matches")
        return
    w_kind = max(len("type"), *(len(m.kind) for m in matches))
    w_text = min(40, max(len("text"), *(len(m.text) for m in matches)))
    print(f"{'id':>4}  {'page':>4}  {'type':<{w_kind}}  {'text':<{w_text}}  context")
    for m in matches:
        mark = "  (skip)" if m.id in skip else ""
        text = m.text.replace("\n", " ").replace("\t", " ")
        print(f"{m.id:>4}  {m.page + 1:>4}  {m.kind:<{w_kind}}  "
              f"{text[:w_text]:<{w_text}}  {m.context}{mark}")
    print(f"\n{len(matches)} match(es); {len(skip)} skipped")


def _print_warnings(doc, cfg) -> None:
    for w in page_warnings(doc):
        print(f"warning: {w}", file=sys.stderr)
    if not cfg.terms:
        print("warning: no 'terms' configured, so names and addresses will NOT be "
              "redacted (run 'redacter init' and edit redact.toml)", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
