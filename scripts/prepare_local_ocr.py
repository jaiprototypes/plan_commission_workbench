"""Prefetch verified local OCR models at build time; runtime never downloads them."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys


# Building from a fresh checkout must not depend on an editable installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class LocalOcrBuildPreparer:
    """Purpose: collect the exact official model files the packaged reader expects."""

    def prepare(self) -> dict:
        """Purpose: download during the build and validate the installed ONNX engine."""
        import rapidocr
        from rapidocr.inference_engine.base import FileInfo
        from rapidocr.utils.typings import TaskType
        from plan_commission_workbench.document_evidence_worker import RasterEvidenceReader

        root = Path(rapidocr.__file__).resolve().parent / "models"
        root.mkdir(parents=True, exist_ok=True)
        languages = {"Det": rapidocr.LangDet.CH, "Cls": rapidocr.LangCls.CH, "Rec": rapidocr.LangRec.CH}
        for role, filename in RasterEvidenceReader.MODEL_FILES.items():
            spec = FileInfo(rapidocr.EngineType.ONNXRUNTIME, rapidocr.OCRVersion.PPOCRV4,
                            TaskType(role.lower()), languages[role], rapidocr.ModelType.MOBILE)
            self._download_verified(root, filename, spec)
        previous_root = os.environ.get("PCW_LOCAL_OCR_MODELS")
        os.environ["PCW_LOCAL_OCR_MODELS"] = str(root)
        try:
            RasterEvidenceReader()
        finally:
            if previous_root is None:
                os.environ.pop("PCW_LOCAL_OCR_MODELS", None)
            else:
                os.environ["PCW_LOCAL_OCR_MODELS"] = previous_root
        return {"ok": True, "model_directory": str(root), "models": list(RasterEvidenceReader.MODEL_FILES.values())}

    @staticmethod
    def _download_verified(root: Path, filename: str, spec) -> None:
        """Purpose: verify official hashes even after a new download completes."""
        from rapidocr.inference_engine.base import InferSession
        from rapidocr.utils.download_models import download_task

        metadata = InferSession.get_model_url(spec)
        expected = metadata.get("SHA256")
        if not expected:
            raise RuntimeError(f"Official RapidOCR metadata has no checksum for {filename}")
        target = root / filename
        if target.is_file() and LocalOcrBuildPreparer._checksum(target) == str(expected).lower():
            return
        download_task(root, spec)
        downloaded = root / Path(metadata["model_dir"]).name
        actual = LocalOcrBuildPreparer._checksum(downloaded)
        if actual.lower() != str(expected).lower():
            raise RuntimeError(f"Checksum verification failed for local OCR model {filename}")
        # RapidOCR renamed identical v4 model files from *_infer to *_mobile.
        # Keep runtime names stable only after checking the official content hash.
        if downloaded != target:
            downloaded.replace(target)

    @staticmethod
    def _checksum(path: Path) -> str:
        """Purpose: hash model files incrementally without duplicating them in memory."""
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    """Purpose: make missing runtime/model dependencies fail the build immediately."""
    print(json.dumps(LocalOcrBuildPreparer().prepare()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
