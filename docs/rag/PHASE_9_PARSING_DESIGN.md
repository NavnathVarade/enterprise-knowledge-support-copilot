# Document Parsing & Normalization — Phase 9

## Scope delivered
Real parsers — not stubs — for every MIME type Phase 7's ingestion endpoint accepts:
`rag/parsing/text_parser.py` (text/plain, text/markdown), `csv_parser.py`, `json_parser.py`,
`pdf_parser.py` (via `pypdf`), `docx_parser.py` (via `python-docx`), unified behind
`rag/parsing/registry.py` and a common `ParsedDocument` output shape (`rag/parsing/base.py`).

## Design choices
- **A single normalized output shape** (`ParsedDocument`: text + optional pages + optional
  headings + metadata) regardless of source format, so Phase 10's chunker works against one
  interface rather than six format-specific ones (Section 17).
- **Structure is preserved where the format actually has it, not invented where it doesn't.**
  PDF gives per-page character ranges; DOCX gives headings from Word's own "Heading 1..6"
  paragraph styles; plain text gives neither, honestly, rather than a fabricated structure.
- **Encoding handling for text formats** tries `utf-8-sig` → `utf-8` → `latin-1` in order and
  normalizes CRLF/CR to LF — both are real failure modes for uploaded text files.
- **CSV and JSON are rendered as readable text** (a Markdown table, pretty-printed JSON)
  rather than passed through as raw bytes, since that's what actually reads sensibly to a
  chunker/LLM downstream, while the original structure is preserved in `metadata`.

## Proof, not assertion
All parsers are tested against **genuinely generated fixtures**: the PDF tests use
`reportlab` to render real multi-page PDFs and verify page-boundary tracking; the DOCX tests
use `python-docx` itself to build real documents with actual "Heading 1"/"Heading 2" styles
and verify correct extraction. This is a real round-trip, not a parser tested against its own
assumptions.

## Real defects found and fixed during this phase
1. **A test-construction bug, not a parser bug**, in the BOM-stripping test.
   `"\ufeffHello".encode("utf-8-sig")` double-includes the BOM (once as the encoding's own
   marker, once because the Python string already contained a literal `\ufeff` character) —
   it doesn't represent what a real BOM-prefixed file's bytes look like. Fixed by
   constructing the test bytes directly. The parser's BOM-stripping logic was correct all along.
2. **A real parser bug**: `python-docx` raises `zipfile.BadZipFile` — not
   `docx.opc.exceptions.PackageNotFoundError` — for content that isn't a ZIP container at all.
   The original exception handler only caught `PackageNotFoundError`, so genuinely corrupted
   content crashed with an unhandled `BadZipFile` instead of the expected `ParsingFailed`
   domain error. Caught by `test_docx_parser_rejects_corrupted_content` actually failing with
   the raw exception; fixed by catching both exception types.

## Not yet implemented (explicitly, not silently)
- **Not wired to ingestion.** `POST /documents` (Phase 7) still discards the uploaded bytes
  after hashing — there's no object storage (Section 18) to persist raw artifacts. These
  parsers are complete and tested in isolation, ready to be wired in once storage exists.
- **No content-type sniffing** — a parser is selected by the claimed MIME type, matching
  Phase 7's documented gap.
- **No OCR** for scanned/image-only PDFs — `PdfParser` extracts embedded text only.
- **No email or HTML parser** yet, despite Section 17 listing them — deferred until a
  concrete source connector actually needs them.
