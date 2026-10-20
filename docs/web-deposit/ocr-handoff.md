# Handoff: a scan's first pages, read through the platform's vision model

20 October 2026. Follows `weak-band-handoff.md` (v0.15.0 to v0.15.2).
Built the same day as v0.16.0.

## The problem

A PDF with no text layer (a scan) gave the passage index nothing:
`promoter/passages.py` reads text with pypdf, finds none, and leaves one
marker row (`position == NO_TEXT`) so the file is not fetched again. The
sample dataset had one such file of seven,
ContemporaryformsofSlavery_McDougall_03.pdf, a 45-page UN report scanned
to image. It could not be found by its own title.

## The options, costed (19 October)

| option | cost per scan | what it needs on the VM | where it fails |
|---|---|---|---|
| Tesseract in the image | 1 to 4 s a page on one vCPU, no network, free | about 60 MB more image, a system package to track, language data | varied type, columns, cover-page layout; a 1,000-page scan ties the promoter up for an hour |
| the platform's vision-capable chat alias | one chat call a page: about 2,000 image tokens in, the page's text out; metered, and a 429 pauses the run | nothing: two wheels (pypdfium2, Pillow) to render pages | long documents (every page is a call); a model may tidy what it reads |
| the embedvl alias on page images | one embed call a page | the same two wheels | gives a vector, never the text: nothing to show, nothing for the word match. Rejected. |

Shortcuts considered: the first pages only (the title, author and
opening are most of what a search needs to find a file); a cap with the
skipped count reported; reading only the pages pypdf found empty; a
low-resolution pass to decide which pages hold text (saves little, since
the render is cheap and the call is the cost).

Decision (user, 19 October): no OCR engine on the VM, fewer moving
parts; the platform reads the first pages. Reading: for a cover page the
vision model is the better fit (layout and varied type are where
Tesseract fails, and a title page is where a model's tidying does least
harm); whole-document OCR, if ever wanted, would be Tesseract.

## The probe (20 October)

`scripts/ocr_probe.py` sent the first two pages of the McDougall scan to
`arc:lite` at 150 dpi (1,275 by 1,651 pixels, 211 KB and 370 KB as
JPEG). Both came back whole and in reading order: running header,
document number, title, author, the contents list, then the opening
paragraphs; nothing invented. Each page was 2,147 prompt tokens, 364 and
673 completion tokens, 1.7 s and 2.3 s. Output in
`scripts/ocr_probe_2026-10-20.txt`. The decision rule was: readable
title, author and opening, build it; a refusal or poor text, fall back
to Tesseract on the same two pages. It passed.

## What was built

- `PROMOTER_LLM_OCR_MODEL` (blank = off, the default) names the chat
  alias; `PROMOTER_OCR_PAGES` (default 2, at least 1) says how many pages
  of each scan are read. `promoter/config.py`, `.env.promoter.example`.
- `Platform.transcribe(image, mime)` in `crsw_web/llm.py`: one chat call,
  the page as a `data:` URL, temperature 0, no thinking pass, the prompt
  asks for every word in reading order and the single word NONE for a
  blank page (which becomes ""). `PlatformError` on failure like the rest.
- `promoter/passages.py`: `page_images` renders the first pages with
  pypdfium2 and writes JPEG with Pillow (both wheels, added to the `web`
  extra; the image is still `python:3.12-slim`). `build` takes `ocr` (the
  platform's `transcribe`, or None when off) and `ocr_pages`; when pypdf
  finds no text in a PDF, the pages are transcribed, chunked and embedded
  like any other text. Every row records the page count it was read
  under in a new column `ocr` (0 for a text layer), so raising the page
  count re-reads the scans and nothing else, and switching OCR off
  leaves what was read in place. A blank transcription keeps the marker
  row under the page count tried (not tried again); a failed call keeps
  the marker at 0 with a warning, so the next run tries again. The web
  VM's DuckDB copy inserts an explicit column list and ignores the new
  column; nothing changes on the web side.
- The summary line (`passages_written`) and the `passages_finished`
  totals carry `files_ocr` (scans read) and `pages_ocr` (images sent).
  `files_no_text` now means "no text layer and not transcribed".
- Page images go to the platform only for datasets whose sensitivity is
  in `PROMOTER_LLM_SENSITIVITIES` and that are not excluded, since the
  build is only called for those. Off is silent.

## Cost in use

Per scan: `PROMOTER_OCR_PAGES` chat calls, about 2,000 prompt tokens
each plus the page's text out, paid once per file version (the build
caches by checksum). The sample dataset: one scan, two calls, under five
seconds.

## Verification

Tests: `tests/test_llm.py` (TestTranscribe), `tests/test_promoter_passages.py`
(TestOcr, TestOcrSettings); the full suite green; the secrets check clean.

Live, after v0.16.0 on the internal VM with `PROMOTER_LLM_OCR_MODEL=arc:lite`
in `.env.promoter`: `$P passages --dataset rs1/legal-sources/green/0_raw/international-instruments-sample`
should print `passages_written` with `files_ocr 1, pages_ocr 2,
files_no_text 0` and a passage count up by the two pages' pieces; after
the web VM's next index cycle, a search for the report's title finds the
cover passage. If the alias does not accept images the warning names the
page and the HTTP body, the marker row stays, and the run completes.

## Not this change

Whole-document OCR, Tesseract, the low-resolution triage, image
embeddings; the reranker; neighbouring passages shown twice; the
`DeleteObjectVersion` grant.
