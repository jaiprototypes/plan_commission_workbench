# Current Production State

## Madison V1 Complete

Madison Plan Commission is the completed v1 production module. It owns the
Madison-specific Legistar scrape, agenda PDF classification, standardized Land
Use Application extraction, operator review, diagnostics, state bundles, and
accepted export flow.

Future city work should not fold new source assumptions into the Madison module.
Milwaukee CPC work will be implemented as its own module and will merge with
Madison only at the normalized database, review, and export layer.

## Scrape Reliability

The Madison Legistar scrape distinguishes durable broken attachment links from
pipeline failures. A confirmed missing application file, such as HTTP `404` or
`410`, is recorded as an unavailable source and skipped on later runs. Temporary
download failures, corrupted PDFs, Docling failures, and LLM failures still fail
the run so the operator does not accept incomplete output silently.

The database remains the durable source of truth. Downloads and Docling sidecars
stay temporary; reviewed rows, unavailable source status, run events, exports,
and diagnostics remain under the workbench data directory.

## Diagnostics

The production Windows app exposes only `Send Diagnostics` to the operator. The
signed GitHub Actions build injects the support SMTP settings and app password
from repository variables and secrets before packaging. The support password is
not committed to source, stored in SQLite, written to local settings JSON, or
shown in the UI.

Manual diagnostics email a short readable summary to the GECG support inbox and
attach the same state bundle ZIP produced by the State Bundle action. Automatic
failure reporting can send compact text failure reports when enabled by the
build configuration and deduplicates repeated failures.

Windows Credential Manager is still used for the operator's OpenAI API key. The
app keeps support paths for clearing the OpenAI key, legacy diagnostic email
credentials from older builds, or all stored workbench secrets.

## Windows Updates

Production installs use the stable App Installer feed, not direct `.msix`
installs or per-run Actions artifacts:

`https://github.com/jaiprototypes/plan_commission_workbench/releases/download/pcw-windows-stable/PlanCommissionWorkbench.appinstaller`

The signed `main` build publishes:

- stable feed assets on `pcw-windows-stable`
- retained rollback assets on `pcw-windows-v<MSIX_VERSION>`
- installer helper scripts and the signing certificate
- a fallback portable zip for emergency/manual use

The MSIX package identity, publisher, App Installer URL, and signing certificate
chain must stay stable. Changing them can force manual reinstall or certificate
trust work on the production PC.

Rollback uses the `Roll Back Windows Desktop Update Feed` workflow to republish a
retained previous version to the stable feed. `ForceUpdateFromAnyVersion=true`
must remain in the App Installer file so Windows can restore a previous package
without deleting local data, logs, exports, diagnostics, or Credential Manager
secrets.

## Support Loop

The intended loop is:

`diagnostic email with state bundle -> inspect state -> patch in Codex -> push main -> signed App Installer update`

The package icon is a transparent Gould-style `G` mark derived from the company
browser icon style. MSIX builds include target-size and unplated shortcut assets
so Windows should show the `G` without a colored tile or bar behind it.

## Next Work

Milwaukee CPC implementation planning lives in
`docs/milwaukee_cpc_implementation_plan.md`. The first phase is local-only
workability and debugging of Milwaukee extraction before any shared UI rail is
expanded to include both cities.
