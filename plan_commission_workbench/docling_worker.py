"""Subprocess entry point for bounded Docling extraction."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .docling_adapter import DoclingTextExtractor


def main(argv: list[str] | None = None) -> int:
    """Purpose: run one Docling conversion and write text as JSON."""

    parser = argparse.ArgumentParser(prog="pcw-docling-worker")
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--mode", choices=("default", "full_page_ocr"), required=True)
    args = parser.parse_args(argv)
    try:
        text = _convert(args.pdf, args.mode == "full_page_ocr")
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps({"text": text}), encoding="utf-8")
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


def _convert(pdf_path: Path, force_full_page_ocr: bool) -> str:
    """Purpose: use Docling directly inside the worker process."""

    extractor = DoclingTextExtractor()
    converter = extractor._converter(force_full_page_ocr=force_full_page_ocr)
    result = converter.convert(str(pdf_path))
    return extractor._result_text(result)


if __name__ == "__main__":
    raise SystemExit(main())
