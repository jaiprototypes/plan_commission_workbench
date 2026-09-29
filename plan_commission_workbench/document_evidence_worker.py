"""Bounded local RapidOCR worker; only explicitly installed models are used."""

from __future__ import annotations

from contextlib import closing
import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import traceback


class RasterEvidenceReader:
    """Purpose: recognize rendered pages locally with bounded CPU and image memory."""

    MODEL_FILES = {"Det": "ch_PP-OCRv4_det_infer.onnx", "Rec": "ch_PP-OCRv4_rec_infer.onnx",
                   "Cls": "ch_ppocr_mobile_v2.0_cls_infer.onnx"}

    def __init__(self) -> None:
        import rapidocr

        root = Path(os.environ.get("PCW_LOCAL_OCR_MODELS", Path(rapidocr.__file__).parent / "models"))
        models = self.MODEL_FILES
        missing = [name for name in models.values() if not (root / name).is_file()]
        if missing:
            raise RuntimeError(f"Local OCR model files unavailable in {root}: {', '.join(missing)}")
        params = {f"{kind}.model_path": str(root / name) for kind, name in models.items()}
        params.update({"Global.log_level": "error", "Global.max_side_len": 6000,
                       "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                       "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                       "Det.limit_side_len": 1600, "Det.limit_type": "max"})
        self.engine = rapidocr.RapidOCR(params=params)

    def read(self, path: Path, numbers: list[int]) -> dict:
        """Purpose: reuse models while rendering one requested page at a time."""
        import pypdfium2 as pdfium

        results = {}
        with pdfium.PdfDocument(str(path)) as document:
            for number in numbers:
                with closing(document[number - 1]) as page:
                    results[str(number)] = self._read_page(page)
        return results

    def _read_page(self, page) -> dict:
        """Purpose: convert top-left OCR pixels into bottom-left PDF coordinates."""
        width, height = page.get_size()
        scale = min(2.5, 6000 / max(width, height), math.sqrt(16_000_000 / (width * height)))
        issues = ["ocr_render_resolution_limited"] if scale < 2 else []
        bitmap = page.render(scale=scale)
        try:
            pil_image = bitmap.to_pil()
            try:
                blocks = self._read_tiles(pil_image, page, bitmap.width, bitmap.height)
            finally:
                pil_image.close()
        finally:
            bitmap.close()
        if any(block["confidence"] < .7 for block in blocks):
            issues.append("low_confidence_ocr_text")
        return {"blocks": blocks, "issues": issues}

    def _read_tiles(self, image, page, width: int, height: int) -> list[dict]:
        """Purpose: keep plan lettering legible instead of shrinking a whole sheet."""
        import numpy as np

        blocks = []
        for top in range(0, height, 1500):
            for left in range(0, width, 1500):
                tile = image.crop((left, top, min(left + 1600, width), min(top + 1600, height)))
                try:
                    result = self.engine(np.asarray(tile), text_score=0.1)
                    blocks.extend(self._blocks(result, page, width, height, left, top))
                finally:
                    tile.close()
        # Overlapping tile edges may recognize the same line twice.
        unique = []
        for block in blocks:
            if not any(self._duplicate(block, old) for old in unique):
                unique.append(block)
        return unique

    @staticmethod
    def _duplicate(block: dict, old: dict) -> bool:
        """Purpose: suppress repeated OCR lines only at nearly identical positions."""
        return (block["text"].casefold() == old["text"].casefold()
                and abs(block["bbox"][0] - old["bbox"][0]) < 8
                and abs(block["bbox"][1] - old["bbox"][1]) < 8)

    @staticmethod
    def _blocks(result, page, pixel_width: int, pixel_height: int, left: int = 0, top: int = 0) -> list[dict]:
        """Purpose: retain OCR confidence and geometry without guessed fields."""
        import pypdfium2.raw as raw
        import ctypes

        if result.txts is None or result.boxes is None:
            return []
        output = []
        for text, quad, score in zip(result.txts, result.boxes, result.scores):
            points = []
            for x, y in quad:
                px, py = ctypes.c_double(), ctypes.c_double()
                ok = raw.FPDF_DeviceToPage(page, 0, 0, pixel_width, pixel_height, 0,
                                          round(float(x) + left), round(float(y) + top), ctypes.byref(px), ctypes.byref(py))
                if not ok:
                    raise RuntimeError("Could not map OCR coordinates to PDF page")
                points.append((px.value, py.value))
            bbox = [min(p[0] for p in points), min(p[1] for p in points),
                    max(p[0] for p in points), max(p[1] for p in points)]
            output.append({"text": str(text), "bbox": bbox, "method": "ocr", "confidence": float(score)})
        return output


def run_self_test() -> dict:
    """Purpose: verify packaged PDF rendering, local models, OCR, and coordinates."""
    from PIL import Image, ImageDraw, ImageFont

    with tempfile.TemporaryDirectory(prefix="pcw-ocr-self-test-") as workdir:
        path = Path(workdir) / "contact-scan.pdf"
        with Image.new("RGB", (900, 420), "white") as image:
            ImageDraw.Draw(image).multiline_text(
                (55, 50), "OWNER\nPINE DEVELOPMENT LLC\n500 Lake Street\n414.555.0123",
                font=ImageFont.load_default(size=38), fill="black", spacing=20,
            )
            image.save(path, "PDF", resolution=150)
        blocks = RasterEvidenceReader().read(path, [1])["1"]["blocks"]
    text = " ".join(block["text"] for block in blocks)
    if "PINEDEVELOPMENT" not in text.replace(" ", "") or "414.555.0123" not in text:
        raise RuntimeError(f"Local raster OCR self-test did not recognize expected contact text: {text}")
    owner = next((block for block in blocks if block["text"] == "OWNER"), None)
    phone = next((block for block in blocks if "414.555.0123" in block["text"]), None)
    if not owner or not phone or owner["bbox"][1] <= phone["bbox"][1]:
        raise RuntimeError("Local raster OCR self-test did not preserve PDF coordinates")
    return {"ok": True, "engine": "rapidocr-onnxruntime", "pages": 1, "text": text, "llm_calls": 0}


def main(argv: list[str] | None = None) -> int:
    """Purpose: serialize successful OCR results or return a visible failure."""
    parser = argparse.ArgumentParser(prog="pcw-document-evidence-worker")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--pages")
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)
    if not args.self_test and not all((args.pdf, args.pages, args.output_json)):
        parser.error("--pdf, --pages, and --output-json are required unless --self-test is set")
    try:
        if args.self_test:
            print(json.dumps(run_self_test()))
            return 0
        result = RasterEvidenceReader().read(args.pdf, [int(value) for value in args.pages.split(",")])
        args.output_json.write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
