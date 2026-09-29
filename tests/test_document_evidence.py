"""Behavioral checks for local PDF geometry, coverage, and OCR failures."""

from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from plan_commission_workbench.document_evidence import DocumentEvidenceError, DocumentEvidenceReader, LocalOcrWorker


def make_pdf(path: Path, streams: list[str], *, image: bool = False) -> Path:
    """Purpose: generate small valid PDFs without a global PDF writer dependency."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    page_ids = []
    for stream in streams:
        page_id = len(objects) + 1
        page_ids.append(page_id)
        resource = "/Font << /F1 3 0 R >>"
        if image:
            resource += f" /XObject << /Im0 {page_id + 2} 0 R >>"
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
                        f"/Resources << {resource} >> /Contents {page_id + 1} 0 R >>").encode())
        content = stream.encode()
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
        if image:
            objects.append(b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB "
                           b"/BitsPerComponent 8 /Length 3 >>\nstream\n\xff\xff\xff\nendstream")
    objects[1] = (f"<< /Type /Pages /Count {len(page_ids)} /Kids [" +
                  " ".join(f"{number} 0 R" for number in page_ids) + "] >>").encode()
    data, offsets = b"%PDF-1.4\n", [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    data += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    path.write_bytes(data)
    return path


def test_all_pages_and_neighboring_contact_columns_keep_geometry(tmp_path):
    path = make_pdf(tmp_path / "contacts.pdf", [
        "BT /F1 12 Tf 40 700 Td (OWNER) Tj 250 0 Td (ENGINEER) Tj ET",
        "BT /F1 12 Tf 40 650 Td (Second page) Tj ET",
    ])
    result = DocumentEvidenceReader().read(path, use_ocr=False)
    assert result["page_count"] == 2
    assert result["coverage_complete"]
    blocks = result["pages"][0]["blocks"]
    assert [block["text"] for block in blocks] == ["OWNER", "ENGINEER"]
    assert blocks[0]["bbox"][0] == pytest.approx(40, abs=2)
    assert blocks[0]["bbox"][1] > 690
    assert blocks[1]["bbox"][0] > 280
    assert result["pages"][1]["blocks"][0]["text"] == "Second page"
    # Closing each native page/document allows immediate deletion on Windows.
    path.unlink()


def test_text_footer_does_not_hide_raster_body(tmp_path):
    stream = "q 500 0 0 600 50 100 cm /Im0 Do Q BT /F1 12 Tf 30 20 Td (" + "footer " * 45 + ") Tj ET"
    path = make_pdf(tmp_path / "scan.pdf", [stream], image=True)
    result = DocumentEvidenceReader().read(path, use_ocr=False)
    assert result["text_chars"] > 200
    assert not result["coverage_complete"]
    assert "ocr_disabled" in result["pages"][0]["issues"]
    assert result["pages"][0]["image_count"] == 1


def test_injected_local_ocr_has_explicit_method_and_pdf_coordinates(tmp_path):
    path = make_pdf(tmp_path / "scan.pdf", ["q 500 0 0 600 50 100 cm /Im0 Do Q"], image=True)
    calls = []
    def ocr(pdf, pages):
        calls.append((pdf, pages))
        return {"1": {"blocks": [{"text": "OWNER", "bbox": [50, 500, 110, 515]}], "issues": []}}
    result = DocumentEvidenceReader(ocr_backend=ocr).read(path)
    assert calls == [(path, [1])]
    assert result["coverage_complete"]
    assert result["pages"][0]["coverage"] == "ocr_completed"
    assert result["pages"][0]["blocks"] == [{"text": "OWNER", "bbox": [50, 500, 110, 515], "method": "ocr"}]


def test_empty_ocr_is_unresolved_not_no_contact_success(tmp_path):
    path = make_pdf(tmp_path / "scan.pdf", ["q 500 0 0 600 50 100 cm /Im0 Do Q"], image=True)
    result = DocumentEvidenceReader(ocr_backend=lambda *_: {"1": {"blocks": [], "issues": []}}).read(path)
    assert not result["coverage_complete"]
    assert "ocr_found_no_text_in_visual_content" in result["pages"][0]["issues"]


def test_missing_ocr_page_stops_reading(tmp_path):
    path = make_pdf(tmp_path / "scan.pdf", ["q 500 0 0 600 50 100 cm /Im0 Do Q"], image=True)
    with pytest.raises(DocumentEvidenceError, match="omitted requested page"):
        DocumentEvidenceReader(ocr_backend=lambda *_: {}).read(path)


def test_repeated_labels_at_different_locations_keep_separate_provenance(tmp_path):
    path = make_pdf(tmp_path / "mixed.pdf", [
        "q 500 0 0 600 50 100 cm /Im0 Do Q BT /F1 12 Tf 40 700 Td (OWNER) Tj ET"
    ], image=True)
    blocks = [{"text": "OWNER", "bbox": [40, 700, 85, 710]},
              {"text": "OWNER", "bbox": [320, 700, 365, 710]}]
    result = DocumentEvidenceReader(ocr_backend=lambda *_: {"1": {"blocks": blocks, "issues": []}}).read(path)
    assert len(result["pages"][0]["blocks"]) == 2
    assert result["pages"][0]["blocks"][1]["bbox"][0] == 320


def test_vector_art_beside_text_is_not_claimed_as_fully_read(tmp_path):
    path = make_pdf(tmp_path / "vectors.pdf", [
        "10 10 500 600 re S BT /F1 12 Tf 40 700 Td (Some text) Tj ET"
    ])
    result = DocumentEvidenceReader().read(path, use_ocr=False)
    assert not result["coverage_complete"]
    assert "vector_graphics_not_interpreted" in result["pages"][0]["issues"]


def test_corrupt_pdf_is_a_read_failure(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_text("not a PDF")
    with pytest.raises(DocumentEvidenceError, match="bad.pdf"):
        DocumentEvidenceReader().read(path)


def test_ocr_timeout_is_bounded_and_api_credentials_are_not_forwarded(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    def run(command, **kwargs):
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert kwargs["timeout"] == 2
        raise subprocess.TimeoutExpired(command, 2)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(DocumentEvidenceError, match="timed out after 2 seconds"):
        LocalOcrWorker(timeout_seconds=2).read(tmp_path / "scan.pdf", [1])


def test_ocr_nonzero_exit_is_not_silently_treated_as_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=1, stderr="models missing"))
    with pytest.raises(DocumentEvidenceError, match="models missing"):
        LocalOcrWorker().read(tmp_path / "scan.pdf", [1])


def test_local_raster_ocr_recognizes_synthetic_contact_page(tmp_path):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("rapidocr")
    from PIL import Image, ImageDraw, ImageFont

    path = tmp_path / "raster.pdf"
    with Image.new("RGB", (900, 420), "white") as image:
        draw = ImageDraw.Draw(image)
        draw.multiline_text((55, 50), "OWNER\nPINE DEVELOPMENT LLC\n500 Lake Street\n414.555.0123",
                            font=ImageFont.load_default(size=38), fill="black", spacing=20)
        image.save(path, "PDF", resolution=150)
    result = DocumentEvidenceReader(timeout_seconds=90).read(path)
    blocks = result["pages"][0]["blocks"]
    text = " ".join(block["text"] for block in blocks)
    assert "PINEDEVELOPMENT" in text.replace(" ", "")
    assert "414.555.0123" in text
    assert all(block["method"] == "ocr" for block in blocks)
    assert all(block["bbox"][1] >= 0 for block in blocks)
    owner = next(block for block in blocks if block["text"] == "OWNER")
    phone = next(block for block in blocks if "414.555.0123" in block["text"])
    assert owner["bbox"][1] > phone["bbox"][1]


def test_build_prefetch_verifies_download_and_normalizes_model_name(monkeypatch, tmp_path):
    pytest.importorskip("rapidocr")
    import hashlib
    import importlib
    from rapidocr.inference_engine.base import InferSession
    from scripts.prepare_local_ocr import LocalOcrBuildPreparer

    payload = b"test model bytes"
    metadata = {"model_dir": "https://example.invalid/official_mobile.onnx", "SHA256": hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(InferSession, "get_model_url", lambda _: metadata)
    downloader = importlib.import_module("rapidocr.utils.download_models")
    monkeypatch.setattr(downloader, "download_task", lambda root, _: (root / "official_mobile.onnx").write_bytes(payload))
    LocalOcrBuildPreparer._download_verified(tmp_path, "runtime_infer.onnx", object())
    assert (tmp_path / "runtime_infer.onnx").read_bytes() == payload
    assert not (tmp_path / "official_mobile.onnx").exists()
    monkeypatch.setattr(downloader, "download_task", lambda *_: pytest.fail("Verified model should be reused"))
    LocalOcrBuildPreparer._download_verified(tmp_path, "runtime_infer.onnx", object())


def test_build_prefetch_rejects_corrupt_new_download(monkeypatch, tmp_path):
    pytest.importorskip("rapidocr")
    import importlib
    from rapidocr.inference_engine.base import InferSession
    from scripts.prepare_local_ocr import LocalOcrBuildPreparer

    metadata = {"model_dir": "https://example.invalid/model.onnx", "SHA256": "0" * 64}
    monkeypatch.setattr(InferSession, "get_model_url", lambda _: metadata)
    downloader = importlib.import_module("rapidocr.utils.download_models")
    monkeypatch.setattr(downloader, "download_task", lambda root, _: (root / "model.onnx").write_bytes(b"bad bytes"))
    with pytest.raises(RuntimeError, match="Checksum verification failed"):
        LocalOcrBuildPreparer._download_verified(tmp_path, "runtime.onnx", object())
    assert not (tmp_path / "runtime.onnx").exists()
