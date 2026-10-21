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
.venv/bin/pip install -e '.[gui]'      # drop [gui] for the command line only
```

Start the app with `.venv/bin/redacter-gui`, or build a Mac app:

### Mac app (.dmg)

```sh
scripts/build-dmg.sh        # on a Mac; writes dist/Redacter.dmg
```

This builds a self-contained `Redacter.app`, with Python and every library
inside, on a disk image. Open the `.dmg` and drag Redacter to Applications.
You can then drop PDFs on its Dock icon, or use **Open With → Redacter** in
Finder.

The app is signed ad hoc, not with an Apple Developer ID. It opens normally
on the Mac that built it. On another Mac, macOS will block it the first
time: right-click the app, choose **Open**, then confirm.

## The app

1. **Drop your PDFs** (or a folder of them) on the window. The first one
   is scanned right away, and all of them appear in the **Files** list. Click
   a file to review it; your choices are remembered per file. To reuse an
   existing `redact.toml`, drop it on the window too; the app remembers it.
2. **Review.** Every match is outlined on the page and listed on the left.
   Red outlines will be redacted, green ones are kept.
   - **Select:** click a match (or untick it in the list) to keep it.
     Right-click it in the list to never redact that value.
   - **Draw box:** drag a rectangle to black out anything the detectors
     can't see, like a signature image. Right-click a box to remove it.
   - **Add term:** drag across text, such as a name, to redact it
     everywhere. It's saved to your settings.
   - **Settings tab:** edit your terms, exceptions, detectors and custom
     patterns.
3. **Save review PDF…** (optional) writes one unredacted copy with every
   planned redaction outlined, for checking or printing. Delete it after.
4. **Redact…** writes the redacted PDF, verifies it, and shows the result.
   Drag the file chip at the bottom straight to Finder, or use Open /
   Show in Finder.
5. **Redact all files…** (with several files) asks for an output folder and
   redacts every file in the list in parallel, one process per CPU core,
   using each file's review choices. Files you didn't open get your normal
   settings. Any file that fails, such as one needing a password you haven't
   entered, is listed with the reason.

Scanning and redacting run in the background with a progress bar, so the
window never freezes.

## Command line

```sh
# 1. Create a config and list your names, addresses, employers, dependents
redacter init                 # writes ./redact.toml (mode 600)
$EDITOR redact.toml

# 2. Review: list every match with an ID, and write an outlined review copy
redacter scan 2025-return.pdf --review review.pdf

# 3. Apply, keeping any matches you don't want removed (IDs from step 2)
redacter apply 2025-return.pdf -o 2025-return.redacted.pdf --skip 4,17

# 4. Check the redacted PDF, then delete the review copy
rm review.pdf
```

`./redact.toml` is used automatically if present; otherwise pass `-c FILE`.
Encrypted PDFs prompt for their password.

`scan --skip 4,17` shows skipped matches in green in the review copy, so
you can check your choice before applying. IDs stay the same as long as the file and
config don't change.

### When something is missed

If you can see PII on the page that `scan` doesn't list, check how the tool
reads that spot:

```sh
redacter inspect 2025-return.pdf "social security"
```

This prints every text span near the label with its font, and counts
the images and drawn shapes there. Digits show as `#`, and characters
with no readable text mapping get their own symbols, so you can share the
output without revealing the number. If the number is missing from the
list, or shows as unreadable symbols, text search can't find it; use a
`[[boxes]]` entry for it. `scan` also warns about pages containing
unreadable characters.

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

- **Redacts every copy.** A number found anywhere, such as a phone number,
  is also removed everywhere else it appears, whatever its formatting
  (`212-472-5638`, `(212) 472 5638`, `no.2124725638`).
- **Flattens fillable forms first.** Values typed into fillable form fields
  aren't page text, so they'd be missed without this.
- **Strips hidden places:** document info and XMP metadata (on the document
  and on every page, image and font), EXIF/XMP/comments inside JPEG images,
  accessibility tags, page labels, named destinations, bookmarks,
  attachments, JavaScript, links, thumbnails, invisible text, and form data.
  The output gets a fresh file ID, so it can't be matched to the original.
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
  `*.png` files.
- Review copies (`scan --review`, or **Save review PDF…** in the app) show
  the **unredacted** text. Delete them when done.
- Avoid putting PII on the command line, where it ends up in shell history.
  Use the config file instead.

## Development

```sh
.venv/bin/pip install -e '.[gui,test]'
.venv/bin/pytest          # app tests run headless; they skip without PySide6
```

PyMuPDF is pinned below 2.0, because 2.0 shifts the text that follows a
redaction left by one character. `test_text_after_redaction_does_not_move`
catches this.
