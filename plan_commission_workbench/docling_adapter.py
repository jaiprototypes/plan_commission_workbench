"""Docling PDF text extraction adapter."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Callable

from .exceptions import DoclingExtractionError


@dataclass(frozen=True)
class DoclingTextResult:
    """Purpose: return extracted text with the Docling mode that produced it."""

    text: str
    mode: str
    output_path: Path


ProgressCallback = Callable[[str], bool | None]


class DoclingTextExtractor:
    """Purpose: run Docling and fail hard when extraction is unavailable."""

    def __init__(self, converter_factory: Callable[..., Any] | None = None) -> None:
        self.converter_factory = converter_factory
        self.full_page_ocr_backend = os.getenv("PCW_DOCLING_FULL_PAGE_OCR_BACKEND", "rapidocr").strip().lower()

    def extract_pdf_text(self, pdf_path: Path, output_dir: Path) -> str:
        """Purpose: extract text from a PDF into a temporary Docling folder."""

        return self.extract_pdf_text_result(pdf_path, output_dir).text

    def extract_pdf_text_result(
        self,
        pdf_path: Path,
        output_dir: Path,
        *,
        force_full_page_ocr: bool = False,
        use_vlm: bool = False,
        progress_callback: ProgressCallback | None = None,
        timeout_seconds: float | None = None,
    ) -> DoclingTextResult:
        """Purpose: extract text and report whether default or OCR mode was used."""

        output_dir.mkdir(parents=True, exist_ok=True)
        mode = self._mode_name(force_full_page_ocr, use_vlm)
        try:
            text = self._extract_text(
                pdf_path,
                output_dir,
                force_full_page_ocr=force_full_page_ocr,
                use_vlm=use_vlm,
                progress_callback=progress_callback,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            raise DoclingExtractionError(f"Docling {mode} failed for {pdf_path.name}: {exc}; {self._file_context(pdf_path)}") from exc
        if not text.strip():
            raise DoclingExtractionError(f"Docling {mode} returned empty text for {pdf_path.name}")
        sidecar = output_dir / f"{pdf_path.name}.{mode}.docling.txt"
        sidecar.write_text(text, encoding="utf-8")
        return DoclingTextResult(text=text, mode=mode, output_path=sidecar)

    def _extract_text(
        self,
        pdf_path: Path,
        output_dir: Path,
        *,
        force_full_page_ocr: bool,
        use_vlm: bool,
        progress_callback: ProgressCallback | None,
        timeout_seconds: float | None,
    ) -> str:
        """Purpose: run Docling in-process for tests or subprocess for hard timeouts."""

        if self.converter_factory:
            return self._extract_text_inline(pdf_path, output_dir, force_full_page_ocr=force_full_page_ocr, use_vlm=use_vlm)
        return self._extract_text_subprocess(
            pdf_path,
            output_dir,
            force_full_page_ocr=force_full_page_ocr,
            use_vlm=use_vlm,
            progress_callback=progress_callback,
            timeout_seconds=timeout_seconds,
        )

    def _extract_text_inline(self, pdf_path: Path, output_dir: Path, *, force_full_page_ocr: bool, use_vlm: bool) -> str:
        """Purpose: run Docling directly when tests inject a fake converter."""

        os.environ.setdefault("DOCLING_CACHE_DIR", str(output_dir / "cache"))
        converter = self._converter(force_full_page_ocr=force_full_page_ocr, use_vlm=use_vlm)
        result = converter.convert(str(pdf_path))
        return self._result_text(result)

    def _extract_text_subprocess(
        self,
        pdf_path: Path,
        output_dir: Path,
        *,
        force_full_page_ocr: bool,
        use_vlm: bool,
        progress_callback: ProgressCallback | None,
        timeout_seconds: float | None,
    ) -> str:
        """Purpose: isolate Docling so a hung converter can be timed out and observed."""

        mode = self._mode_name(force_full_page_ocr, use_vlm)
        output_json = output_dir / f"{pdf_path.name}.{mode}.worker.json"
        stdout_log = output_dir / f"{pdf_path.name}.{mode}.worker.stdout.log"
        stderr_log = output_dir / f"{pdf_path.name}.{mode}.worker.stderr.log"
        timeout_seconds = self._timeout_seconds(force_full_page_ocr, use_vlm, timeout_seconds)
        command = self._worker_command(pdf_path, output_json, force_full_page_ocr, use_vlm)
        env = os.environ.copy()
        env["DOCLING_CACHE_DIR"] = str(output_dir / "cache")
        env["PCW_DOCLING_FULL_PAGE_OCR_BACKEND"] = self.full_page_ocr_backend
        env["PCW_DOCLING_IMAGES_SCALE"] = str(self._images_scale())
        env["PCW_DOCLING_VLM_PRESET"] = self._vlm_preset()
        process = self._start_worker(command, env, stdout_log, stderr_log)
        if not self._report_progress(
            progress_callback,
            f"Docling {mode} worker PID {process.pid} started; timeout_seconds={timeout_seconds:g}",
        ):
            kill_detail = self._kill_worker(process)
            raise DoclingExtractionError(f"Docling {mode} worker stopped because the run is no longer active; {kill_detail}")
        start = time.monotonic()
        deadline = start + timeout_seconds
        progress_interval = self._worker_progress_interval()
        next_progress = start + progress_interval
        while True:
            return_code = process.poll()
            if return_code is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                kill_detail = self._kill_worker(process)
                detail = self._worker_log_detail(stderr_log, stdout_log)
                suffix = f"; {kill_detail}: {detail}" if detail else f"; {kill_detail}"
                raise DoclingExtractionError(f"Docling {mode} timed out after {timeout_seconds:g} seconds{suffix}")
            if now >= next_progress:
                elapsed = int(now - start)
                if not self._report_progress(
                    progress_callback,
                    f"Docling {mode} worker PID {process.pid} still running after {elapsed}s; timeout_seconds={timeout_seconds:g}",
                ):
                    kill_detail = self._kill_worker(process)
                    raise DoclingExtractionError(f"Docling {mode} worker stopped because the run is no longer active; {kill_detail}")
                next_progress = now + progress_interval
            time.sleep(0.5)
        if return_code != 0:
            detail = self._worker_log_detail(stderr_log, stdout_log)
            raise DoclingExtractionError(f"Docling {mode} worker exited {return_code}: {detail}")
        try:
            payload = json.loads(output_json.read_text(encoding="utf-8"))
        except Exception as exc:
            raise DoclingExtractionError(f"Docling {mode} worker did not write readable output") from exc
        text = payload.get("text")
        if not isinstance(text, str):
            raise DoclingExtractionError(f"Docling {mode} worker output did not contain text")
        return text

    def _start_worker(
        self,
        command: list[str],
        env: dict[str, str],
        stdout_log: Path,
        stderr_log: Path,
    ) -> subprocess.Popen:
        """Purpose: launch the Docling child while keeping its raw logs on disk."""

        stdout_fh = stdout_log.open("w", encoding="utf-8")
        stderr_fh = stderr_log.open("w", encoding="utf-8")
        try:
            return subprocess.Popen(
                command,
                stdout=stdout_fh,
                stderr=stderr_fh,
                env=env,
                text=True,
                **self._worker_process_group_kwargs(),
            )
        finally:
            stdout_fh.close()
            stderr_fh.close()

    def _kill_worker(self, process: subprocess.Popen) -> str:
        """Purpose: stop a stuck Docling child before moving to the next fallback."""

        if process.poll() is not None:
            return "worker already exited"
        detail = self._kill_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return f"{detail}; worker still present after kill wait"
        return detail

    def _worker_process_group_kwargs(self) -> dict[str, Any]:
        """Purpose: isolate Docling so timeout cleanup can kill descendants."""

        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if flags or no_window:
                return {"creationflags": flags | no_window}
            return {}
        return {"start_new_session": True}

    def _kill_process_tree(self, process: subprocess.Popen) -> str:
        """Purpose: terminate child processes that Docling may spawn internally."""

        if os.name == "nt":
            return self._kill_windows_process_tree(process)
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return f"sent SIGKILL to worker process group {process.pid}"
        except Exception as exc:
            return f"process-group kill failed: {exc}; {self._kill_direct_process(process)}"

    def _kill_windows_process_tree(self, process: subprocess.Popen) -> str:
        """Purpose: use Windows taskkill to stop the worker and its children."""

        pid = process.pid
        command = ["taskkill", "/PID", str(pid), "/T", "/F"]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
        except Exception as exc:
            return f"taskkill failed for worker process tree {pid}: {exc}; {self._kill_direct_process(process)}"
        detail = (completed.stderr or completed.stdout or "").strip()
        if completed.returncode == 0:
            return f"terminated worker process tree {pid}"
        return f"taskkill exited {completed.returncode} for worker process tree {pid}: {detail[-300:]}; {self._kill_direct_process(process)}"

    def _kill_direct_process(self, process: subprocess.Popen) -> str:
        """Purpose: provide a last-resort kill for the direct worker process."""

        try:
            process.kill()
            return "direct worker kill requested"
        except Exception as exc:
            return f"direct worker kill failed: {exc}"

    def _report_progress(self, progress_callback: ProgressCallback | None, message: str) -> bool:
        """Purpose: let callers keep DB heartbeats alive during long Docling work."""

        if not progress_callback:
            return True
        return progress_callback(message) is not False

    def _worker_progress_interval(self) -> float:
        """Purpose: throttle visible worker pings so logs stay useful."""

        try:
            value = float(os.getenv("PCW_DOCLING_WORKER_PROGRESS_SECONDS", "30"))
        except ValueError:
            return 30.0
        return max(5.0, value)

    def _worker_log_detail(self, *paths: Path) -> str:
        """Purpose: include the most useful worker stderr/stdout tail on failure."""

        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except FileNotFoundError:
                continue
            if text:
                return text[-1200:]
        return "no worker output"

    def _worker_command(self, pdf_path: Path, output_json: Path, force_full_page_ocr: bool, use_vlm: bool) -> list[str]:
        """Purpose: invoke the Docling worker in source or frozen desktop builds."""

        args = [
            "--docling-worker",
            "--pdf",
            str(pdf_path),
            "--output-json",
            str(output_json),
            "--mode",
            self._mode_name(force_full_page_ocr, use_vlm),
        ]
        if getattr(sys, "frozen", False):
            return [sys.executable, *args]
        return [sys.executable, "-m", "plan_commission_workbench.docling_worker", *args[1:]]

    def _timeout_seconds(
        self,
        force_full_page_ocr: bool,
        use_vlm: bool = False,
        override_seconds: float | None = None,
    ) -> float:
        """Purpose: bound Docling work so retry logic can proceed."""

        if override_seconds is not None:
            return max(10.0, override_seconds)
        if use_vlm:
            name = "PCW_DOCLING_VLM_TIMEOUT_SECONDS"
            default = 900.0
        elif force_full_page_ocr:
            name = "PCW_DOCLING_FULL_PAGE_TIMEOUT_SECONDS"
            default = 600.0
        else:
            name = "PCW_DOCLING_TIMEOUT_SECONDS"
            default = 120.0
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError:
            return default
        return max(10.0, value)

    def _converter(self, *, force_full_page_ocr: bool = False, use_vlm: bool = False) -> Any:
        """Purpose: instantiate Docling lazily so tests can mock it."""

        if self.converter_factory:
            return self._factory_converter(force_full_page_ocr, use_vlm)
        try:
            from docling.document_converter import DocumentConverter  # type: ignore
        except Exception as exc:
            raise DoclingExtractionError("Docling is not installed in this environment") from exc
        if use_vlm:
            return self._vlm_converter(DocumentConverter)
        if not force_full_page_ocr:
            return DocumentConverter()
        return self._full_page_ocr_converter(DocumentConverter)

    def _factory_converter(self, force_full_page_ocr: bool, use_vlm: bool) -> Any:
        """Purpose: support zero-arg and mode-aware converter test factories."""

        assert self.converter_factory is not None
        signature = inspect.signature(self.converter_factory)
        accepts_mode = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values()
        ) or bool(signature.parameters)
        if accepts_mode:
            return self.converter_factory(self._mode_name(force_full_page_ocr, use_vlm))
        return self.converter_factory()

    def _vlm_converter(self, document_converter_cls: Any) -> Any:
        """Purpose: configure Docling's VLM conversion fallback."""

        try:
            from docling.datamodel.base_models import InputFormat  # type: ignore
            from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions  # type: ignore
            from docling.document_converter import PdfFormatOption  # type: ignore
            from docling.pipeline.vlm_pipeline import VlmPipeline  # type: ignore
        except Exception as exc:
            raise DoclingExtractionError("Docling VLM options are unavailable") from exc
        pipeline_options = VlmPipelineOptions()
        pipeline_options.document_timeout = self._timeout_seconds(False, use_vlm=True)
        pipeline_options.images_scale = self._images_scale()
        try:
            pipeline_options.vlm_options = VlmConvertOptions.from_preset(self._vlm_preset())
        except Exception as exc:
            raise DoclingExtractionError(f"Docling VLM preset '{self._vlm_preset()}' is unavailable") from exc
        return document_converter_cls(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_cls=VlmPipeline, pipeline_options=pipeline_options),
            }
        )

    def _full_page_ocr_converter(self, document_converter_cls: Any) -> Any:
        """Purpose: configure Docling's heavier full-page OCR path."""

        try:
            from docling.datamodel.base_models import InputFormat  # type: ignore
            from docling.datamodel.pipeline_options import PdfPipelineOptions, TableStructureOptions  # type: ignore
            from docling.document_converter import PdfFormatOption  # type: ignore
        except Exception as exc:
            raise DoclingExtractionError("Docling full-page OCR options are unavailable") from exc
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = True
        pipeline_options.do_table_structure = True
        pipeline_options.images_scale = self._images_scale()
        pipeline_options.table_structure_options = TableStructureOptions(do_cell_matching=True)
        pipeline_options.ocr_options = self._ocr_options()
        return document_converter_cls(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            }
        )

    def _ocr_options(self) -> Any:
        """Purpose: choose a packaged OCR backend for full-page retry."""

        try:
            from docling.datamodel import pipeline_options as options  # type: ignore
        except Exception as exc:
            raise DoclingExtractionError("Docling OCR options are unavailable") from exc
        backend_classes = {
            "rapidocr": "RapidOcrOptions",
            "easyocr": "EasyOcrOptions",
            "tesseract_cli": "TesseractCliOcrOptions",
            "tesseract": "TesseractOcrOptions",
            "ocrmac": "OcrMacOptions",
        }
        class_name = backend_classes.get(self.full_page_ocr_backend)
        if not class_name:
            choices = ", ".join(sorted(backend_classes))
            raise DoclingExtractionError(f"Unsupported OCR backend '{self.full_page_ocr_backend}'. Choose one of: {choices}")
        option_cls = getattr(options, class_name)
        return option_cls(force_full_page_ocr=True)

    def _images_scale(self) -> float:
        """Purpose: let operators increase OCR image resolution when needed."""

        try:
            value = float(os.getenv("PCW_DOCLING_IMAGES_SCALE", "2.0"))
        except ValueError:
            return 2.0
        return max(1.0, min(value, 4.0))

    def full_page_ocr_summary(self) -> str:
        """Purpose: describe the configured retry mode in run logs."""

        return (
            f"backend={self.full_page_ocr_backend}, images_scale={self._images_scale():g}, "
            f"timeout_seconds={self._timeout_seconds(True):g}"
        )

    def vlm_summary(self) -> str:
        """Purpose: describe the configured VLM fallback in run logs."""

        return f"preset={self._vlm_preset()}, images_scale={self._images_scale():g}, timeout_seconds={self._timeout_seconds(False, use_vlm=True):g}"

    def mode_timeout_summary(
        self,
        force_full_page_ocr: bool,
        use_vlm: bool = False,
        timeout_seconds: float | None = None,
    ) -> str:
        """Purpose: describe Docling timeout settings in run logs."""

        mode = self._mode_name(force_full_page_ocr, use_vlm)
        return f"mode={mode}, timeout_seconds={self._timeout_seconds(force_full_page_ocr, use_vlm, timeout_seconds):g}"

    def _mode_name(self, force_full_page_ocr: bool, use_vlm: bool = False) -> str:
        """Purpose: normalize Docling mode names across parent and worker."""

        if use_vlm:
            return "vlm"
        return "full_page_ocr" if force_full_page_ocr else "default"

    def _vlm_preset(self) -> str:
        """Purpose: choose Docling's VLM preset for last-resort extraction."""

        return os.getenv("PCW_DOCLING_VLM_PRESET", "granite_docling").strip() or "granite_docling"

    def _result_text(self, result: Any) -> str:
        """Purpose: support common Docling document export shapes."""

        document = getattr(result, "document", result)
        for method_name in ("export_to_markdown", "export_to_text"):
            method = getattr(document, method_name, None)
            if callable(method):
                value = method()
                if value:
                    return str(value)
        return str(document or "")

    def _file_context(self, pdf_path: Path) -> str:
        """Purpose: include disk evidence when Docling rejects an input file."""

        try:
            stat = pdf_path.stat()
            with pdf_path.open("rb") as fh:
                first_bytes = fh.read(32)
            return f"file_bytes={stat.st_size}, first_bytes={first_bytes.hex()}"
        except Exception as exc:
            return f"file_context_unavailable={exc}"
