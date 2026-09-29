# Milwaukee local document reader

## Current state

The Milwaukee page now defaults to local document reading. This mode makes zero
LLM calls and requires no OpenAI API key. It reads PDF text with PDFium, preserves
page coordinates, and optionally runs local RapidOCR on raster pages. Explicitly
selected experimental LLM mode retains the earlier pipeline.

The local reader is implemented and tested, but Milwaukee is still an
experimental source. It does not yet provide complete, reliable developer leads
across all documents or integrate contacts into accepted production exports.

## Implemented behavior

- Discover Milwaukee CPC candidate files and eligible PDF attachments, including
  plans, drawing packets, and Legistar PDF viewer URLs.
- Read text across all PDF pages, preserving each block's page and coordinates.
- Associate contact fields within labeled company panels. Printed OWNER roles
  remain owners; they are not automatically promoted to developers.
- Keep unlabeled details as unresolved evidence. Preserve malformed phone numbers
  with review flags instead of repairing them or borrowing nearby company data.
- Run local OCR in a bounded worker using installed models. Low-confidence OCR,
  unread visual regions, unsupported files, and request limits keep coverage
  explicitly incomplete. Failures stop and log the operation.
- Store compact contact evidence and coverage in
  `data/local_document_evidence.sqlite3`. Known source URLs reuse prior results;
  OCR configuration separates cache entries. The UI's Refresh saved documents
  control explicitly rereads sources that may have changed.
- Remove downloaded PDFs and OCR sidecars after processing. Evidence remains
  attached to its actual source URL, hash, page, and coordinates.

The reader and selector are independent shared modules. Madison extraction
behavior is unchanged. The local path resolves clear panels with rules; ambiguous
blocks currently go to operator review, not an automatic LLM fallback.

## Actual output validation

The implementation was exercised on three PDFs fetched from Milwaukee Legistar:

| Source | Reading | Result |
| --- | --- | --- |
| Everett, Exhibit A Continued | Embedded text across 54 pages | Five separate contact panels from page 1 |
| Everett, zoning affidavit | Local OCR on one scanned page | No associated contact; low-confidence OCR retained as incomplete coverage |
| Midtown Commons, Exhibit A Narrative | Embedded text across three pages | Three unresolved footer blocks; no associated developer contact |

Everett output:

| Printed role | Company / person | Phone |
| --- | --- | --- |
| OWNER | Kaeding Development Group | 952.229.4433 |
| GENERAL CONTRACTOR | VJS Construction Services | 262.542.9000 |
| PROJECT ARCHITECT | Brian Griebl, AIA / VJS Construction Services | 262.542.9000 |
| STRUCTURAL ENGINEER | Pierce Engineering, Inc | 414.278.90200 — source value flagged |
| CIVIL ENGINEER | Kapur | 414.751.7200 |

Kaeding's mailing address is 7900 International Dr, Ste 910, Bloomington, MN
55425, and its website is www.kaeding-group.com. The unrelated Madison engineer
phone and address are not assigned to Kaeding. Split website fragments are
rejoined conservatively while the original evidence text is retained.

The live discovery path for June 8, 2026 found file 252190, read its selected
Exhibit A Continued from the evidence cache, and returned the same five contacts.
A repeat with download and extraction deliberately blocked also succeeded from
the cache. Both paths made zero LLM calls.

These runs do not establish full document coverage: the 54-page packet and the
Midtown narrative were tested with OCR disabled, and the affidavit OCR remained
low-confidence. The discovery run was intentionally limited to one candidate
file and one document. Other images, documents, and candidates remain unread.

Detailed local results:

- `data/diagnostics/milwaukee_local_extraction_live_latest.json`
- `data/diagnostics/milwaukee_local_discovery_live_latest.json`

## Validation and packaging

Automated tests cover separate company columns, stacked panels, malformed
phones, OCR uncertainty, source coordinates, caching, fail-and-log behavior,
unsupported attachment coverage, and local API requests without a key. A real
raster-page OCR self-test runs with local ONNX models and no hosted model calls.

Windows builds prefetch and checksum OCR models before freezing the app. Both
portable and staged MSIX self-tests now include a scanned-page OCR check. The
source self-test was run locally; an actual Windows package build has not been
performed for this change. MSIX identity, signing, and update-feed settings are
unchanged.

## Remaining limits

Contact selection is intentionally conservative and depends on recognizable role
panels. Narrative-only associations, unusual/rotated layouts, and unreadable
scans remain review cases. Coverage measures attempted reading, not guaranteed
OCR correctness. More representative documents are needed before treating this
as reliable automated developer-contact extraction.

References: [PDFium API](https://pypdfium2.readthedocs.io/en/stable/python_api.html),
[Docling structured output](https://docling-project.github.io/docling/concepts/docling_document/).
