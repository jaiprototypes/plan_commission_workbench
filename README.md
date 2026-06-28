# Plan Commission Workbench

Standalone Madison Plan Commission scrape, review, and export workbench.

This project owns Madison Legistar event access, agenda PDF processing,
agenda-hit detection, standardized Land Use Application extraction, operator
review, and accepted-row export. It is intentionally separate from the customer
DBMS. The customer DBMS should only import reviewed `.csv` or `.xlsx` output.
Reviewed accepted contacts can also be exported to `.docx` Avery 5160/8160
mailing labels for outreach.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Set `OPENAI_API_KEY` to a credited OpenAI API key before live runs. LLM calls
are required for agenda classification and application extraction. If the key is
missing, terminal run/serve startup prompts for it when possible, and the local
Run screen prompts in the browser. On Windows, entered keys are saved to that
Windows user's Credential Manager and loaded on later launches. The key is not
embedded in the executable, committed to git, written to the workbench database,
or included in state bundles. Docling is required for live PDF text extraction.

## CLI

```bash
pcw run madison --from 2026-01-01 --to 2026-01-31
pcw serve --host 127.0.0.1 --port 8010
pcw export --status accepted --output data/exports/madison_review.xlsx
pcw export --status accepted --output data/exports/madison_labels.docx
pcw retry --run-id 1
```

Equivalent module form:

```bash
python -m plan_commission_workbench.cli run madison --from 2026-01-01 --to 2026-01-31
```

## Server

```bash
pcw serve --host 127.0.0.1 --port 8010
```

Open `http://127.0.0.1:8010/`.

API endpoints:

- `GET /health`
- `POST /runs/madison`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/events`
- `GET /agenda-items`
- `GET /application-extractions`
- `PATCH /application-extractions/{id}/review`
- `POST /exports`
- `GET /exports/{id}/download`
- `POST /runs/{run_id}/retry`

DOCX label exports use accepted rows only. Before a contact is printed, the
workbench checks for a populated name, company, and mailing address, rejects raw
form-label text, deduplicates repeated contacts, and reports skipped contacts in
the export response. Address interpretation is handled by the LLM extraction and
operator acceptance steps, not by deterministic export parsing.

## Data

SQLite lives at `data/workbench.db`. Downloaded PDFs and Docling sidecars are
kept only in per-run temp folders under `data/tmp/` and are removed when the run
ends. Durable state is the SQLite data plus reviewed exports.

Use the Run screen's **State Bundle** button to download a diagnostics zip with
`workbench.db`, `server.log`, `server.err.log`, and a small manifest. Restoring
that `workbench.db` into another machine's data folder reproduces the same
dedupe/skip state, so overlapping test runs resume near the failing item instead
of scraping every completed agenda and application again.

## Runtime Safeguards

Long Docling conversions run in isolated child worker process groups with hard
timeouts, process-tree cleanup, and visible run-log heartbeats. Useful controls:

- `PCW_DOCLING_WORKER_PROGRESS_SECONDS`: worker progress ping interval, default
  `30`.
- `PCW_DOCLING_TIMEOUT_SECONDS`: default Docling timeout, default `120`.
- `PCW_DOCLING_APPLICATION_TIMEOUT_SECONDS`: application default-Docling
  timeout before moving to OCR/VLM fallback, default `45`.
- `PCW_DOCLING_FULL_PAGE_TIMEOUT_SECONDS`: full-page OCR retry timeout, default
  `600`.
- `PCW_DOCLING_VLM_TIMEOUT_SECONDS`: VLM fallback timeout, default `900`.
- `PCW_LEGISTAR_TIMEOUT_SECONDS`: per-attempt Legistar HTTP timeout, default
  `30`.
- `PCW_LEGISTAR_JSON_ATTEMPTS`: visible JSON metadata attempts, default `4`.
- `PCW_RUN_STALE_SECONDS`: watchdog stale-run threshold, default `900`.

## Windows Desktop Build

The Windows launcher starts the local FastAPI server, opens the browser only
after `/health` responds at `http://127.0.0.1:8010/`, and stores runtime data under
`%LOCALAPPDATA%\PlanCommissionWorkbench\data`. It prompts for a credited OpenAI
API key when the key is missing. The key is saved for that Windows user in
Credential Manager when available, then loaded automatically on later launches.
It is not embedded in the executable, committed to git, written to the workbench
database, or included in state bundles.
Packaged startup logs are written to `server.log` and `server.err.log` in that
same data folder so server failures can be diagnosed without an IDE.

Build locally on Windows:

```powershell
.\scripts\build_windows.ps1
```

The build writes three desktop artifacts:

- `artifacts\PlanCommissionWorkbench-windows.zip`: fallback portable folder.
- `artifacts\PlanCommissionWorkbench.msix`: Windows package.
- `artifacts\PlanCommissionWorkbench.appinstaller`: update entry point.

The zip remains available as a fallback. For production installs, publish the
`.msix` and `.appinstaller` files to stable HTTPS URLs, then install from the
`.appinstaller` file. Windows App Installer will check that same App Installer
URI on launch and apply newer MSIX versions when they are published.

The GitHub Actions workflow publishes the update feed to GitHub Releases when a
persistent signing certificate secret is configured. The stable feed assets live
on release tag `pcw-windows-stable` by default:

- `https://github.com/jaiprototypes/plan_commission_workbench/releases/download/pcw-windows-stable/PlanCommissionWorkbench.appinstaller`
- `https://github.com/jaiprototypes/plan_commission_workbench/releases/download/pcw-windows-stable/PlanCommissionWorkbench.msix`

The Windows package icon is a star generated during the MSIX build. Its color,
tilt, and proportions vary slightly from the MSIX package version so a user can
visually confirm that an update has reached the machine.

Install production machines from the stable `.appinstaller` URL, not from a
per-run GitHub Actions artifact. Actions artifacts are useful verification
outputs, but Windows cannot poll them as an update feed. A direct `.msix`
install also bypasses the App Installer update subscription; use it only for
manual testing or emergency package repair.

Useful MSIX build settings:

```powershell
$env:PCW_APPINSTALLER_URI = "https://example.com/PlanCommissionWorkbench.appinstaller"
$env:PCW_MSIX_PACKAGE_URI = "https://example.com/PlanCommissionWorkbench.msix"
$env:PCW_MSIX_PUBLISHER = "CN=Your Signing Publisher"
$env:PCW_SIGNING_CERTIFICATE_PATH = "C:\certs\pcw-signing.pfx"
$env:PCW_SIGNING_CERTIFICATE_PASSWORD = "pfx-password"
.\scripts\build_windows.ps1
```

The MSIX must be signed before App Installer can install or update it. The
publisher in `PCW_MSIX_PUBLISHER` must match the signing certificate subject, and
future updates must use the same package name and publisher. For a local test
only, run `.\scripts\build_windows.ps1 -CreateTestCertificate`; it exports
`artifacts\PlanCommissionWorkbench-test.cer`, which must be trusted on the target
machine before installing.

For the production update feed, do not use `-CreateTestCertificate`. Generate a
persistent signing PFX once on Windows and store it in GitHub Secrets. The helper
uses the MSIX-required Code Signing EKU and Basic Constraints extensions:

```powershell
.\scripts\create_windows_signing_secret.ps1 -Publisher "CN=GECG" -Password "<pfx-password>"
gh secret set PCW_SIGNING_CERTIFICATE_BASE64 --body (Get-Content -Raw "artifacts\signing\PlanCommissionWorkbench-signing.pfx.base64.txt")
gh secret set PCW_SIGNING_CERTIFICATE_PASSWORD --body "<pfx-password>"
gh variable set PCW_MSIX_PUBLISHER --body "CN=GECG"
```

The stable release includes `Install-PlanCommissionWorkbench.cmd` and
`Install-PlanCommissionWorkbench.ps1`. Run the `.cmd` file as the production
first-install path when possible; it prompts for UAC, imports the stable
certificate into Local Machine Trusted People, then installs from the stable
`.appinstaller` feed.

Manual trust remains available if the script is blocked by local policy:

```powershell
Import-Certificate -FilePath "C:\path\PlanCommissionWorkbench-signing.cer" -CertStoreLocation Cert:\LocalMachine\TrustedPeople
```

The currently trusted one-off `PlanCommissionWorkbench-test.cer` from a previous
Actions artifact cannot sign future builds because it does not include the
private key. A durable PFX secret is required for hands-off updates.

Before testing on a production PC, use the GitHub Actions build as the first
gate. The workflow builds the zip/MSIX/App Installer artifacts, unpacks the MSIX,
checks that `AppxManifest.xml` and the `.appinstaller` identities match, confirms
update settings are present, and verifies that the MSIX has a signature. Set
repository variable `PCW_REQUIRE_TRUSTED_SIGNATURE=true` only when the CI runner
can validate the signing certificate chain; otherwise the gate verifies that a
signature is present but does not require chain trust.

Keep the portable folder contents together when using the zip fallback; the
build intentionally uses a directory layout so large native Docling/OCR
dependencies do not expand into `%TEMP%` on every launch. The GitHub Actions
workflow in `.github/workflows/windows-build.yml` builds the zip, MSIX, and App
Installer artifacts on pushes to `main`, pull requests, and manual workflow
dispatch. Set repository variables `PCW_APPINSTALLER_URI`,
`PCW_MSIX_PACKAGE_URI`, and `PCW_MSIX_PUBLISHER`, plus secrets
`PCW_SIGNING_CERTIFICATE_BASE64` and `PCW_SIGNING_CERTIFICATE_PASSWORD`, for a
signed CI build.

Each successful signed `main` build also publishes a retained versioned release
tagged `pcw-windows-v<MSIX_VERSION>`. If a bad update ships, run the
`Roll Back Windows Desktop Update Feed` workflow and provide the previous MSIX
version. The workflow republishes that retained package to `pcw-windows-stable`;
the `.appinstaller` file keeps `ForceUpdateFromAnyVersion=true` so Windows can
restore the previous package without touching `%LOCALAPPDATA%` data, logs,
exports, diagnostics, or Credential Manager secrets. If the bad app cannot
launch far enough to trigger its normal on-launch check, open the stable
`.appinstaller` file again after the rollback workflow finishes to force Windows
to repair the installed package from the stable feed.

## Tests

The test suite mocks Legistar, Docling, and OpenAI calls:

```bash
source .venv/bin/activate
pip install -e ".[test]"
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
```

## Python API

```python
from datetime import date
from plan_commission_workbench import PlanCommissionWorkbench

workbench = PlanCommissionWorkbench()
workbench.run_madison_range(date(2026, 1, 1), date(2026, 1, 31))
```
