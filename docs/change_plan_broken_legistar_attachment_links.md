# Change Plan: Production Resilience Updates

## Problem

The workbench can encounter a Legistar matter attachment that is advertised in
event item metadata but whose file endpoint returns a durable not-found response.
The first confirmed case was found in state bundle
`pcw_state_bundle_20260627T234347268341Z`:

- Run: `24`
- Status: `failed_application_download`
- Agenda item: `636`
- Event ID: `27224`
- Matter/city item ID: `93350`
- File ID: `84450`
- Attachment ID: `168761`
- URL:
  `https://webapi.legistar.com/v1/madison/Matters/93350/Attachments/168761/File`
- HTTP result: `404 Client Error: Not Found`

This should not stop the whole scrape. A confirmed broken Legistar file link is
an external source defect, not a Docling or LLM processing failure.

## Desired Behavior

When a selected application attachment returns a confirmed durable missing-file
response:

1. Log the failure with enough source evidence to reproduce it.
2. Mark the application source as unavailable so future runs do not retry it.
3. Continue processing later agenda hits in the same run.
4. Do not create an `application_extractions` row, because no application PDF was
   available to extract.
5. Keep Docling and LLM failures as stop-the-run errors.

Only durable missing-file responses should be bypassed. Transient network and
service errors should still fail loudly so the operator does not accept a
silently incomplete scrape.

## Error Classification

Bypass and persist as unavailable:

- HTTP `404 Not Found`
- HTTP `410 Gone`

Keep as stop-the-run download failures:

- Timeout
- Connection failure
- HTTP `429`
- HTTP `500`, `502`, `503`, `504`
- Non-PDF or corrupted PDF response
- Any download response that is ambiguous or likely temporary

## Data Model

Add a durable source status for broken external application links, for example:

- `application_unavailable`

The existing `source_items` identity fields are enough to make this skip durable:

- `source_kind`
- `event_id`
- `file_id`
- `attachment_id`
- `source_url`
- `content_hash`

For unavailable sources, `content_hash` should stay `NULL` because no file was
downloaded.

## Pipeline Changes

In the application pipeline:

1. Before downloading, check whether the selected `source_url` and
   `attachment_id` are already marked `application_unavailable`.
2. If unavailable, log `application_skip_unavailable_source` and continue.
3. During download, catch the specific durable missing-file error.
4. Update the `source_items` row to `application_unavailable`.
5. Log `application_unavailable` with agenda item ID, event ID, matter ID,
   file ID, attachment ID, URL, and HTTP status.
6. Continue to the next agenda hit.

Do not treat `application_missing` and `application_unavailable` as the same
thing:

- `application_missing` means Legistar metadata did not expose a standardized
  Land Use Application attachment.
- `application_unavailable` means metadata exposed an attachment, but the file
  endpoint was dead.

## Counter/UI Expectations

Current run counters can show `applications_total == applications_extracted`
while some agenda hits remain unprocessed, because those counters are based on
existing `application_extractions` rows in the date range.

The implementation should avoid presenting unavailable links as successful
extractions. A later UI/counter pass should consider separating:

- agenda hits
- extracted applications
- missing standardized applications
- unavailable external application links
- failed processing errors

## Tests

Add regression coverage for:

1. Application attachment download returns `404`.
2. Source row is marked `application_unavailable`.
3. Run does not become `failed_application_download`.
4. Later agenda hits continue processing.
5. Future runs skip the same unavailable `source_url` and `attachment_id`.
6. Timeout or HTTP `500` still raises `failed_application_download`.

## Acceptance Criteria

The same broken link from run `24` should no longer fail the run after the
change. It should produce a durable unavailable-source log entry and future runs
should bypass the same Legistar source identity without attempting the download
again.

## Production Update Channel

The current Windows delivery model is a PyInstaller `--onedir` folder compressed
as `PlanCommissionWorkbench-windows.zip`. That is useful for test distribution,
but it does not provide a production update channel. The next production build
path should move to MSIX packaging with an App Installer file.

Microsoft's App Installer model uses an `.appinstaller` XML file to point
Windows at the package location and update behavior. The user installs from the
App Installer file rather than manually replacing an extracted zip folder.

Preferred deployment target:

- Package format: MSIX or MSIX bundle.
- Update controller: App Installer `.appinstaller` file.
- Hosting: GitHub Releases stable feed, private HTTPS storage, or another stable
  HTTPS endpoint controlled by GECG.
- Install scope: current user unless a machine-wide install becomes necessary.
- Runtime data: keep using `%LOCALAPPDATA%\PlanCommissionWorkbench\data`.
- Secrets: keep using Windows Credential Manager for the OpenAI key and any
  future support/upload token.

Implementation status: implemented in the Windows build path. The build now
keeps the fallback zip and adds:

- `artifacts\PlanCommissionWorkbench.msix`
- `artifacts\PlanCommissionWorkbench.appinstaller`
- optional `artifacts\PlanCommissionWorkbench-test.cer` for local test signing
- GitHub Actions artifact verification that unpacks the MSIX, compares package
  identity against the `.appinstaller`, checks update settings, and verifies that
  the MSIX is signed
- GitHub Releases stable update feed at `pcw-windows-stable` when persistent
  signing secrets are configured
- retained versioned release tags in the form `pcw-windows-v<MSIX_VERSION>` for
  rollback

Implemented steps:

1. Add an application version source that can be read by the UI, build scripts,
   and MSIX manifest.
2. Extend the Windows build workflow to produce an MSIX package in addition to
   or instead of the current zip artifact.
3. Generate an `.appinstaller` file that points at the hosted MSIX package and
   configures update checks.
4. Sign the package with a code-signing certificate trusted by the target
   machine.
5. Publish the MSIX and `.appinstaller` files through the release workflow.
6. Update the README with first-install and update behavior.
7. Keep a manual zip artifact temporarily as a fallback until the MSIX path has
   been tested on the production machine.
8. Add a rollback workflow that republishes a retained versioned release to the
   stable App Installer feed.

Update behavior expectations:

- The user installs once from the `.appinstaller` file.
- Future app launches check the hosted App Installer update settings.
- Updating the application replaces app binaries only.
- The local SQLite database, logs, diagnostics folder, and Credential Manager
  secrets remain intact.
- Update failure should not corrupt the existing app data directory.
- The stable feed must not be published from a newly generated temporary
  certificate. GitHub Actions must use the persistent PFX secret so certificate
  trust remains a one-time target-machine action.
- The stable feed release tag is `pcw-windows-stable` unless explicitly
  overridden. Windows should never be pointed at per-run Actions artifacts for
  update checks.
- Production installs must start from the stable `.appinstaller` file. Direct
  `.msix` installs are useful for testing, but they do not subscribe Windows to
  the App Installer update feed.

Validation:

1. Install version `A` from the `.appinstaller` file on a clean Windows machine.
2. Run a scrape and confirm the data directory is created under
   `%LOCALAPPDATA%`.
3. Publish version `B` with a higher version number.
4. Launch the installed app and confirm Windows detects and applies the update.
5. Confirm the existing database, OpenAI key, logs, and exports remain available.
6. Confirm the packaged app can still spawn run workers and Docling workers.
7. Run the rollback workflow with version `A`, launch the app again, and confirm
   Windows restores the retained package without deleting local workbench data.

References:

- Microsoft App Installer auto-update and repair overview:
  `https://learn.microsoft.com/en-us/windows/msix/app-installer/auto-update-and-repair--overview`
- Microsoft App Installer file overview:
  `https://learn.microsoft.com/en-us/windows/msix/app-installer/app-installer-file-overview`
- Microsoft App Installer update settings:
  `https://learn.microsoft.com/en-us/windows/msix/app-installer/update-settings`

## Diagnostic Return Channel

Production support should not depend on the operator manually finding and
emailing state bundles by hand. The app should use a configured email service to
send compact diagnostic reports back to GECG, with a controlled path for full
state bundles when needed.

This keeps the production footprint smaller than a hosted listener service. It
does mean the desktop app must store email-service credentials or an app-specific
SMTP password on the user's machine, so those credentials must live in Windows
Credential Manager and never be embedded in the executable.

Preferred communication path:

`Desktop app -> configured SMTP/email service -> GECG diagnostics inbox`

Future optional path:

`Desktop app -> private HTTPS support endpoint -> email notification to GECG`

Diagnostic levels:

1. Manual report:
   - Add a `Send Diagnostics` action in the UI.
   - Operator confirms before sending.
   - App emails a compact diagnostic report.
   - Operator can choose whether to attach a full state bundle.
2. Automatic failure summary:
   - On run failure, app sends a compact email automatically if enabled.
   - Report includes run metadata, failure status, last error, relevant
     `run_events`, relevant `source_items`, and log tails.
   - Report excludes OpenAI keys and other credentials.
3. Full state bundle email:
   - The app prompts before attaching the larger bundle.
   - The email body should warn that the bundle may contain contact data.
   - If the bundle is too large for the provider, the app should fall back to
     saving the bundle locally and instructing the operator where it is.

Install identity:

- Generate a random support installation ID on first launch.
- Store it under the local app data directory or Windows Credential Manager.
- Do not use machine name, Windows username, or email as the primary identity.
- Include app version, build channel, and Windows version in reports.

Email configuration:

- Support recipient email.
- SMTP host and port.
- TLS mode.
- SMTP username.
- SMTP app password or service credential.
- Optional reply-to address.
- Automatic failure email enabled/disabled.
- Full state bundle attachment enabled only by explicit operator confirmation.
- Clear stored OpenAI API key.
- Clear stored email-service credential.
- Clear all stored workbench secrets.

The SMTP password or email-service token must be saved in Windows Credential
Manager. It must not be stored in SQLite, logs, state bundles, source control, or
the packaged executable.

Credential clearing:

- Add a settings action to clear the OpenAI API key from Windows Credential
  Manager.
- Add a settings action to clear the SMTP/email-service credential from Windows
  Credential Manager.
- Add a combined `Clear All Stored Secrets` action for production support
  handoff or credential rotation.
- Confirm before clearing secrets because future runs or diagnostic emails may
  stop working until credentials are entered again.
- Clearing secrets should not delete the database, logs, exports, diagnostics,
  support install ID, or app configuration that does not contain credentials.

Security and privacy:

- Redact environment variables and secrets.
- Never include the OpenAI API key.
- Never include the SMTP password or email-service token.
- Prefer compact JSON or text reports for automatic sending.
- Require operator approval before attaching the full SQLite database.
- Rate-limit automatic failure reporting to avoid repeated reports for the same
  failing source identity.
- Store email-service credentials in Windows Credential Manager.
- Make stored secrets removable from the UI without requiring the operator to
  manually open Windows Credential Manager.

Direct email responsibilities:

1. Validate SMTP configuration before enabling automatic failure reports.
2. Send a small test email from the settings screen.
3. Generate deterministic subject lines for grouping failures.
4. Attach compact JSON/text diagnostics automatically.
5. Attach full state bundles only with operator approval.
6. Record send success/failure in local logs without logging credentials.
7. Track duplicate failures by source identity, run status, and error hash.

Diagnostic report contents:

- App version and build channel.
- Install support ID.
- Run ID, date range, status, and last error.
- Heartbeat stage/source.
- Recent run events for the failing run.
- Relevant source item rows for the failing source.
- Relevant agenda/application rows when available.
- Tail of `server.err.log`, `server.log`, and the active run worker logs.
- State bundle filename if a full bundle was generated.

Acceptance criteria:

1. A failed run creates a compact diagnostic email without operator file
   handling when automatic reporting is enabled.
2. GECG receives an email containing the failure type, run ID, app version, and
   compact diagnostic attachment/body.
3. A full bundle email attachment requires operator approval.
4. Secrets are redacted in tests and in generated diagnostic payloads.
5. Duplicate failures for the same unavailable Legistar attachment do not spam
   repeated emails.
6. The operator can clear the stored OpenAI key, stored email credential, or all
   stored workbench secrets from the app settings UI.

## Future Hiccups To Plan For

Legistar source variability:

- A `404` may be durable for one attachment but temporary during a Legistar
  publishing window. The implementation should record first-seen and last-seen
  timestamps, then allow a manual retry/reset of unavailable sources.
- A matter can appear on multiple meetings with the same city item ID but
  different event context. Skip logic should key on source URL and attachment ID
  first, while preserving agenda item links for operator review.
- Attachment names may drift away from "Land Use Application" naming. The
  pipeline should log available attachment names when no standardized
  application is selected so scoring problems can be diagnosed later.
- Legistar may replace a broken attachment with a new attachment ID. A source
  marked unavailable should not block a newer URL or newer attachment ID for the
  same matter.
- Some dead links may return HTML error pages with HTTP `200`. Download
  validation should still reject non-PDF responses, but those should be treated
  cautiously until enough evidence proves they are durable.

Run semantics and counters:

- Current application counters can make a failed run look complete because they
  count existing extraction rows, not every agenda hit outcome. The UI should
  eventually show separate counts for extracted, missing, unavailable, skipped,
  and failed items.
- Marking unavailable sources as skipped could hide useful production issues if
  the UI only shows a green completed state. Completed runs with unavailable
  sources should be visibly "completed with warnings" or have a clear warning
  badge.
- Operator corrections must continue to materialize into canonical rows. Any new
  unavailable-source table/status should not bypass review/export paths for
  valid extracted applications.

MSIX and App Installer:

- MSIX packaging may require changing assumptions that currently work in a
  portable PyInstaller folder, especially writable paths, child process launch,
  bundled model files, and relative template/static asset paths.
- App Installer update checks depend on Windows App Installer availability,
  sideloading policy, HTTPS reachability, and package identity stability.
  Locked-down machines may need a one-time policy or certificate setup.
- Code signing is likely to be the hardest operational step. An unsigned or
  newly signed app can trigger SmartScreen warnings until reputation builds.
- Package identity and publisher must remain stable. Changing either after the
  first install can force uninstall/reinstall instead of a clean update.
- App versions must always increase. A bad release with the same or lower
  version may not update correctly.
- Updating while the local server or worker process is running may fail or leave
  old processes alive. The update flow should close the app cleanly before
  replacement and verify workers are stopped.
- Large Docling/OCR dependencies may make MSIX packages large. Update hosting
  should tolerate large downloads and interrupted resumes.
- The existing zip artifact should remain available until MSIX has survived at
  least one production update cycle.

Diagnostics and support communication:

- Automatic reporting can leak sensitive contact data if payload boundaries are
  too broad. Compact reports should prefer IDs, statuses, error messages, and
  log tails, with full DB attachment requiring explicit approval.
- A failing run can report repeatedly. The app should deduplicate by install ID,
  app version, run status, source identity, and error hash.
- If SMTP sending fails, the app should queue a small bounded report locally and
  retry later without blocking scrape work.
- Diagnostic emails may fail on customer networks with outbound SMTP
  restrictions.
- Email providers may block large attachments or mark automated diagnostics as
  suspicious. The app should detect send failure and preserve the local bundle
  path for manual retrieval.
- SMTP app passwords and service tokens can expire or be revoked. The settings
  UI should show a clear test/send failure and let the operator replace the
  stored credential.
- Email is notification and transport, not a durable support database. If
  support volume grows, move to the optional HTTPS endpoint/storage path.
- Full state bundle attachments may exceed provider limits. The compact report
  should remain useful even when the full bundle cannot be attached.
- Customer mailbox rules or spam filtering may hide diagnostics. Use a stable
  subject prefix and sender identity.
  Manual state bundle export must remain available.

Rollback and recovery:

- A bad update may need a rollback path. Keep every signed production MSIX
  available as `pcw-windows-v<MSIX_VERSION>`, and use the rollback workflow to
  republish the previous package to `pcw-windows-stable`.
- Keep `ForceUpdateFromAnyVersion=true` in the App Installer file so Windows can
  restore a previous package version from the stable feed.
- If a bad build cannot launch, rerun/open the stable `.appinstaller` file after
  the rollback workflow publishes the previous version to force package repair.
- Database migrations must be forward-safe and backed up before schema changes.
  A failed migration should preserve the pre-migration DB and surface the backup
  path in diagnostics.
- Any future automatic repair should operate on app files only. It should not
  delete the workbench database, exports, logs, diagnostics, or Credential
  Manager secrets.
