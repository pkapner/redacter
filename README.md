# redacter

Permanently removes PII from text-based PDFs, such as tax returns exported
from tax software or filled-in IRS forms. Runs entirely locally; nothing
leaves your machine.

It doesn't just draw black boxes on top of the text: the text under each box
is deleted from the file, and the output is checked afterwards to prove it's
gone.

## Install

Requires Python 3.11+.

```sh
git clone <this repo> && cd redacter
python3 -m venv .venv
.venv/bin/pip install -e .
# optional: put it on your PATH
ln -s "$PWD/.venv/bin/redacter" ~/.local/bin/redacter
```

## Workflow

```sh
# 1. Create a config and list your names, addresses, employers, dependents
redacter init                 # writes ./redact.toml (mode 600)
$EDITOR redact.toml

# 2. Review: list every match with an ID, and render outlined previews
redacter scan 2025-return.pdf --preview preview/

# 3. Apply, keeping any matches you don't want removed (IDs from step 2)
redacter apply 2025-return.pdf -o 2025-return.redacted.pdf --skip 4,17 --preview preview-out/

# 4. Look through preview-out/ (or open the PDF), then delete the previews
rm -r preview/ preview-out/
```

`./redact.toml` is used automatically if present; otherwise pass `-c FILE`.
Encrypted PDFs prompt for their password.

`scan --skip 4,17` shows skipped matches in green in the preview, so you can
check your choice before applying. IDs stay the same as long as the file and
config don't change.

### When something is missed

If you can see PII on the page that `scan` doesn't list, check how the tool
reads that spot:

```sh
redacter inspect 2025-return.pdf "social security"
```

This prints the row with that label plus the rows after it. Digits are
masked as `#`, so you can share the output without revealing the number.
A `·` marks a small gap the tool read as a space, and a `⇥` marks a wide
gap. If the label isn't found at all, that part of the page is an image
or drawn shapes, so use a `[[boxes]]` entry for it.

## What gets redacted

| Detector         | Example                     | Default |
|------------------|-----------------------------|---------|
| `term`           | anything listed in `terms`  | on      |
| `ssn`            | `123-45-6789`, `123 45 6789` (also ITINs) | on |
| `ein`            | `12-3456789`                | on      |
| `nine_digit`     | `123456789` (undashed SSN/EIN, routing number) | on |
| `account_number` | 10–17 digit runs            | on      |
| `phone`          | `(555) 867-5309`            | on      |
| `email`          | `jane@example.com`          | on      |
| `spaced_digits`  | `1 2 3 \| 4 5 \| 6 7 8 9` (SSN/EIN in per-digit form boxes) | on |
| `date`           | `01/02/1980`                | off     |

You can also add your own regexes under `[custom]`, exceptions under `allow`,
and manual rectangles under `[[boxes]]` (for signature images and the like).
See `redacter/redact.example.toml`.

Terms ignore case and whitespace, and periods or commas after a word are
optional, so `"Jane Q. Doe"` also matches `JANE Q DOE`. A term only matches
the words it contains, in that order: list `"Doe, Jane"` and `"Doe"` on
their own if those forms appear.

## What else it does

- **Flattens fillable forms first.** Values typed into fillable form fields
  aren't page text, so they'd be missed without this.
- **Strips hidden places:** document metadata and XMP, bookmarks,
  attachments, JavaScript, links, thumbnails, invisible text, and form data.
- **Rewrites the whole file**, with no incremental saves, so old revisions
  of the text don't survive inside it.
- **Verifies the output.** It re-runs detection on the result, searches the
  extracted text and the raw decompressed file bytes for every redacted
  string, and deletes the output if anything is still there.
- **Leaves neighboring text intact.** On tightly spaced forms, the removal
  area is trimmed so it doesn't delete letters on adjacent lines.

## Limitations

- **Text-based PDFs only.** Scanned pages and images aren't OCR'd. The tool
  warns about pages with images, which you must check by eye; use `[[boxes]]`
  to black them out.
- **Names and addresses come only from your `terms` list.** There's no
  ML-based name detection, so check the scan output for anything missing.
- **Over-redaction is possible:** any 9-digit or long number is treated as
  sensitive. Use `--skip` or `allow` for false positives.
- **Always look at the result** before sharing it.

## Privacy notes

- `redact.toml` contains your PII. It's in `.gitignore`, as are `*.pdf` and
  preview images.
- `scan --preview` images show the **unredacted** text. Delete them when done.
- Avoid putting PII on the command line, where it ends up in shell history.
  Use the config file instead.

## Development

```sh
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

PyMuPDF is pinned below 2.0, because 2.0 shifts the text that follows a
redaction left by one character. `test_text_after_redaction_does_not_move`
catches this.
