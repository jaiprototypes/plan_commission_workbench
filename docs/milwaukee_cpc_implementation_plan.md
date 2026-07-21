# Milwaukee CPC Implementation Plan

## Status

Madison Plan Commission is complete as the v1 production module. Milwaukee CPC
will be implemented as a separate city module. The two city modules should come
together only after extraction through the normalized SQLite database, operator
review, and accepted export layer.

The current Milwaukee step is an MVP learning loop, not full integration. The
goal is to learn where Milwaukee CPC exposes the Madison-equivalent fields,
test the most likely evidence locations, and refine the search/extraction path
until it reliably produces reviewable contact data. Shared database blending,
combined review rails, and cross-city export behavior are intentionally later
work.

Do not make Milwaukee a Madison clone. Milwaukee uses Legistar, but its useful
contact evidence is spread across City Plan Commission staff reports, exhibits,
project narratives, affidavits, title-sheet continuations, and related
attachments. The module must rank and bundle evidence documents instead of
assuming a standardized application PDF with fixed sections.

## Implementation Goals

- Add Milwaukee CPC as an independent source module.
- Preserve Madison behavior without broad refactors or source-rule drift.
- Extract potential client contacts from Milwaukee CPC development items.
- Build a local MVP that shows where applicant, owner, developer, project
  contact, project description, location, and unit-count fields were found.
- Keep downloads and Docling sidecars temporary; persist only normalized DB rows,
  evidence snippets, review state, exports, logs, and diagnostics.
- Test locally for workability and debugging before treating Milwaukee as a
  production source.
- Analyze UI expansion only after the local Milwaukee pipeline works.
- Integrate Madison and Milwaukee into one UI rail only after both modules write
  reliable normalized rows.

## Module Boundary

Create city modules under a city boundary such as:

- `plan_commission_workbench/cities/madison/`
- `plan_commission_workbench/cities/milwaukee/`

Shared services remain outside city modules:

- SQLite persistence
- exports
- diagnostics
- Docling worker/runtime safeguards
- OpenAI JSON client plumbing
- review UI primitives
- quality issue helpers where city-neutral

Madison keeps its current source behavior. Milwaukee gets its own discovery,
attachment ranking, evidence bundling, prompt, and quality rules.

## Shared Contract

Introduce a small city profile contract with methods equivalent to:

- `source_name`
- `legistar_tenant`
- `body_name`
- `discover_events(date_from, date_to)`
- `discover_items(event)`
- `rank_evidence_documents(item)`
- `build_evidence_bundle(item, documents)`
- `extract_contacts(bundle)`
- `quality_issues(extraction)`

The DB layer should receive normalized rows from both modules, not raw
city-specific objects.

For the MVP, the Milwaukee module can return an in-memory smoke-test payload to
the browser instead of writing normalized rows. That payload should still use
the same conceptual fields Madison ultimately exports, so the later database
contract is informed by real Milwaukee evidence rather than assumptions.

## Database Work

The existing `runs.source` column is the right merge point for multi-city work.
The schema should be extended carefully so city-specific identities cannot
collide:

- Add `source` to `agenda_items` if it is not already present.
- Add or preserve `source`/city identity on `source_items`.
- Change agenda uniqueness from `(event_id, city_item_id)` to
  `(source, event_id, city_item_id)`.
- Preserve accepted export shape so downstream import still sees one reviewed
  contact row format.
- Keep source URL, attachment ID, file number, Legistar matter ID, and content
  hash as dedupe evidence.

## Milwaukee Discovery

Use the Milwaukee Legistar tenant:

- tenant: `milwaukee`
- body name: `CITY PLAN COMMISSION`

Discovery should use Legistar event/item metadata first. Milwaukee agenda PDFs
are useful for context, but the meeting detail and item attachment metadata are
more direct for source document selection.

Candidate item classification should start broad and cautious:

- likely target: multifamily, mixed-use, office, planned development, detailed
  planned development, site plan review overlay, riverwalk development,
  residential development, major redevelopment
- likely non-target: hearing notices, maps alone, public comments, street/alley
  vacations without a target building, comprehensive plan updates, procedural
  ordinance changes
- uncertain: route to review, not failure

## Milwaukee Evidence Ranking

Rank attachments before downloading/extraction:

High confidence:

- `CPC Staff Report`
- `Staff Report`
- `Exhibit A`
- `Project Narrative`
- `Deviation Narrative`
- `Plan of Operation`

Medium confidence:

- `Affidavit for Zoning Change`
- `Overview of Zoning Change Request`
- `Zoning Review Matrix`

Exclude by default:

- opposition/support letters
- testimony
- public comments
- hearing notice lists
- public hearing notices
- drawing packets and plan sheets
- maps alone
- renderings alone
- generic city letters unless no better evidence exists

The first implementation should download the top two or three ranked evidence
documents per candidate item and concatenate bounded text with document names,
attachment IDs, source URLs, and content hashes.

The refined MVP should use a staff-report-first triage pass before deeper
contact extraction:

- Treat `CPC Staff Report` as the first scan document when present.
- Use the embedded text layer for Milwaukee staff reports before falling back to
  Docling. The 2026 sample reports exposed usable text in under a second, while
  Docling conversion took tens of seconds per report.
- Let one staff report return one or more `project_candidates`; do not assume
  every report maps to exactly one building.
- Tag each candidate as target or non-target before fetching secondary
  documents.
- Build a stable project key from normalized project name, address, and unit
  count.
- Collapse multiple CPC actions for the same building into one project group
  with related files underneath.
- Run deeper contact extraction once per grouped target project, using
  secondary documents only when the staff report leaves important fields blank.
- Treat `Exhibit A Continued` as a likely title-sheet/project-team source when
  staff reports expose only company names. These packets can be large and should
  use a fast text-layer pass before visual/OCR conversion.
- Treat `Exhibit A Narrative`, `Project Narrative`, and `Deviation Narrative`
  packets as the next fallback before zoning matrices. The Midtown Commons sample
  showed direct design-team phone/address details in an `Exhibit A Narrative`
  while the matrix did not produce useful outreach contacts.
- Do not spend the default contact pass on duplicate continued exhibits from
  related CPC actions or dated reuploads such as `Exhibit A Continued as of
  06.22.26.pdf`; one title-sheet packet plus the next best source is more useful
  than two copies of the same plan set.
- During secondary contact extraction, send compact staff-report context plus
  the focused secondary documents. Do not resend the full staff report after it
  has already established project identity; that wastes LLM tokens and latency.

## Milwaukee LLM Extraction

Milwaukee needs a new prompt. It should ask for:

- target project decision
- project name
- project address/location
- unit count when present
- applicant contacts
- owner contacts
- developer contacts
- project contact or design team contacts when present
- evidence snippet and confidence for each field
- review notes for missing or ambiguous fields

Missing phone, email, or mailing address should produce `needs_operator_review`,
not a run failure. Milwaukee staff reports often provide company/entity names
and unit counts but may not expose every mailing field.

## Quality Rules

Milwaukee quality rules should be less rigid than Madison:

- Require at least one useful contact entity for a target project.
- If no useful contact is found, return a `no_direct_contact_found` verification
  result with every checked document and the reason it failed. This separates a
  true source-data gap from a scraper miss.
- Do not let architect, contractor, engineer, or other project-team contacts
  satisfy the developer-contact requirement. They can remain supporting contacts,
  but projects still need developer/applicant enrichment when the CPC packet only
  names the developer company.
- Emit `external_enrichment_candidates` for CPC-identified developers,
  applicants, project contacts, and owners that lack a phone, email, or mailing
  address in the checked CPC PDFs.
- Prefer applicant/developer/owner over design consultants.
- Do not reject a target project only because person-level names are missing.
- Flag missing mailing address, phone, or email for review.
- Flag conflicting unit counts across documents for review.
- Flag extraction based only on low-confidence documents for review.
- Flag OCR-derived affidavit signature names and nonstandard phone numbers for
  manual verification before outreach.

Madison Section 3/Section 5 validation should remain Madison-only.

## Local Test Phase

Milwaukee should be tested locally before any production UI expansion:

1. Add a Milwaukee-only MVP page for live local smoke testing.
2. Add fixture tests for Milwaukee Legistar event/item payload normalization.
3. Add unit tests for Milwaukee attachment ranking.
4. Add local PDF text fixtures from representative public CPC staff reports and
   exhibits.
5. Add LLM responder tests using saved Milwaukee evidence snippets.
6. Run a live local smoke test over a narrow date range, such as one or two CPC
   meetings.
7. Inspect the MVP output for source document choices, missing contacts, bad
   unit counts, and review routing candidates.
8. Save MVP JSON snapshots from the Milwaukee page into `data/diagnostics/` so
   Codex can read the exact findings and refine ranking, prompts, and quality
   rules from observed output.
9. Inspect grouped project output to verify duplicate CPC actions are collapsed
   and multi-building staff reports can produce multiple project candidates.
10. Iterate on ranking and quality rules until the local run produces reviewable
   rows without brittle assumptions.

This phase is explicitly for workability and debugging. The production Windows
release and shared city UI should wait until local Milwaukee runs are stable.

## UI Rail Later

After Milwaukee works locally:

- Add a source/city selector to run controls.
- Add source filters to agenda, applications, review, exports, and diagnostics.
- Keep one visual rail for the operator, but preserve city labels on every row.
- Default exports to all accepted rows, with optional city filtering.
- Keep city-specific pipeline errors readable in run logs and state bundles.

Do not build the shared UI rail first. The extraction module needs to prove its
source behavior locally before the interface is expanded.

## Initial Validation Evidence

The Milwaukee feasibility probe found development-like CPC files whose staff
reports and exhibits expose extractable contact evidence:

- File `252190`, The Everett Multifamily: staff report exposed applicant,
  owners, developer, and 200 units.
- File `260085`, The Everett Riverwalk/multifamily item: staff report and
  exhibits exposed applicant, owner, contractor/design contacts, and 200 units.
- File `251606`, multifamily rezoning: staff report/affidavit exposed owner,
  developer, and 100 senior housing units.

These samples support the module design, but they are not enough to skip local
debugging. Milwaukee should still start behind a local smoke-test workflow until
the ranking and review behavior are proven across more meetings.

## Contact Dig Evidence

The June 2026 Everett files show that Milwaukee contact evidence is split by
document type:

- `CPC Staff Report` identifies the target project, unit count, developer, and
  owner LLCs, but does not provide reliable person names, mailing addresses,
  phones, or emails for outreach.
- `Affidavit for Zoning Change` can expose a petitioner signature, but the
  observed Docling OCR text was damaged enough that person names must be flagged
  for manual verification.
- `Exhibit A Continued` contains the useful title-sheet/project-team block. The
  refined pass found Kaeding Development Group with mailing address and phone,
  VJS Construction Services, Brian Griebl AIA, Pierce Engineering, and Kapur.
- Address-named owner LLCs such as `236 WATER STREET ONE, LLC` should be kept as
  ownership evidence, not treated as useful outreach companies. They should only
  become outreach candidates if paired with a real person or direct contact
  route.
- `CPC Public Hearing Notice`, `DPW Comments`, and public support/opposition
  letters contain emails and phones, but those are city staff, public-notice, or
  public-comment contacts and should not be promoted as applicant/developer
  contacts.
- Drawing and rendering packets can be large, expensive, and low-value for
  outreach. They should be excluded from the MVP contact path unless a future
  inspected sample proves a specific packet type contains useful direct contact
  details.

The scalable Milwaukee extraction pattern is:

1. Use the staff report to decide whether the item is a target project and to
   establish project name, address, units, and related CPC files.
2. If the staff report only gives company names or owner LLCs, pull one
   `Exhibit A Continued` or equivalent title-sheet continuation packet with a
   fast text-layer pass.
3. If the continuation packet is unavailable or still lacks direct contact
   fields, check narrative packets before affidavits and zoning matrices.
4. Promote developer/applicant/project contact rows with address, phone, or
   email as primary outreach leads.
5. Keep contractor, architect, structural engineer, and civil engineer rows with
   direct contact details as secondary project-team evidence only. These rows do
   not mean the developer/applicant contact was found.
6. Queue developer/applicant/project-contact/company-only rows for external
   enrichment when CPC documents identify the party but do not publish phone,
   email, or mailing address.
7. Keep address-named owner LLCs, public-notice contacts, city staff contacts,
   and public-comment contacts out of the outreach lead set unless operator
   review explicitly promotes them.
8. Keep title-sheet contact blocks isolated by company. Do not assign a
   Pierce/VJS/Kapur header address or phone to the developer/applicant unless
   that same source block explicitly names the developer/applicant.
9. If the same primary company appears with conflicting direct contact details,
   keep the strongest developer/project-contact row and demote duplicate primary
   rows for operator review.
10. For projects like the inspected Bradley Road packet, where CPC documents name
   the owner/developer but publish no direct phone/email/address outside city
   boilerplate, emit `no_direct_contact_found` with the checked staff report and
   affidavit rows.

Milwaukee-specific efficiency rules from the 2026 scale pass:

- Demote procedural notification-rule items before LLM extraction.
- Demote signage-only minor modifications, use-permission additions, and
  exterior modifications to existing buildings before downloading secondary
  attachments.
- Demote local-business, industrial-heavy, and industrial-mixed rezonings unless
  the staff report or title explicitly describes a target new building.
- Group related office/development actions by normalized address when the staff
  report lacks a project name or unit count, so riverwalk/deviation companions do
  not produce duplicate extraction work.
- Exclude drawing packets and plan sheets from evidence ranking and contact
  digging. `Exhibit A Continued` remains eligible because inspected samples show
  useful title-sheet contact blocks there.
- Collapse dated copies of `Exhibit A Continued` into one contact-document
  family before download. This avoids transferring multiple 9-14 MB title-sheet
  packets for the same project.
- Keep generic `Exhibit A` out of the secondary contact-dig pass. It may remain
  initial project evidence, but it is too broad to download by default after the
  staff report has already identified the project.
- Keep `Exhibit A Narrative` in the secondary contact-dig pass because it is a
  narrower narrative packet and the inspected Midtown sample had usable direct
  contact signals there.
- Keep the second LLM contact pass compact: known project name/address/units,
  missing-contact companies from the staff report, and the selected secondary
  document text. This preserves contact grounding while reducing prompt size.
- Use fast text-layer extraction for `CPC Staff Report` and `Exhibit A
  Continued`; also use it for verified narrative packet names. Reserve Docling
  for affidavits, zoning matrices, and other PDFs where embedded text is not yet
  verified.

## MVP Tracking Counters

The Milwaukee MVP payload includes `tracking_summary` and flattened
`contact_evidence_rows` so scrape outputs can be fed back into Codex for pattern
analysis.

- `Candidate limit` is the UI cap for how many high-scoring Legistar files the
  MVP will process after agenda scoring. This was previously labeled `Items`.
- `Initial evidence docs` is the UI cap for how many top-ranked PDFs are
  extracted per candidate before staff-report triage. This was previously
  labeled `Docs`.
- `tracking_summary.items` is the number of candidate Legistar files actually
  processed.
- `tracking_summary.ranked_docs` is every high-signal attachment retained in the
  evidence ranking list, whether extracted or not.
- `tracking_summary.selected_initial_docs` is the subset of ranked docs selected
  for the initial staff-report/project triage pass.
- `tracking_summary.extracted_docs` is the number of ranked docs with text,
  hashes, or extraction errors from the initial pass.
- `tracking_summary.contact_dig_docs` is the number of secondary PDFs pulled
  after the staff report left direct contact details blank.
- `tracking_summary.contact_evidence_rows` is the flattened contact evidence row
  count, one row per extracted contact with source document metadata and text
  offsets when available.
- `tracking_summary.verified_no_contact_projects` is the number of target
  projects whose checked CPC PDFs did not produce direct usable contact details.
- `tracking_summary.primary_contact_projects` is the number of projects with a
  direct developer/applicant/project-contact route.
- `tracking_summary.project_team_only_projects` is the number of projects where
  CPC PDFs only exposed secondary project-team contacts such as architects or
  contractors.
- `tracking_summary.external_enrichment_candidates` is the number of
  CPC-identified developer/applicant/owner parties that need lookup outside the
  CPC packet.
- `project.contact_search_verification.checked_documents` lists each PDF actually
  checked for contacts and why it did or did not produce a usable lead.

## Public Source References

- Milwaukee CPC: `https://city.milwaukee.gov/DCD/Planning/CPC`
- Milwaukee Legistar calendar: `https://milwaukee.legistar.com/Calendar.aspx`
- Example CPC meeting: `https://milwaukee.legistar.com/MeetingDetail.aspx?GUID=8505CD5A-87FD-4F5E-A535-AF2D44C37BC2&ID=1355790&Options=info%7C&Search=`
- Example Everett file: `https://milwaukee.legistar.com/LegislationDetail.aspx?GUID=C3A14CD7-3CB3-49FF-BBA1-F8F09CFD268F&ID=8041024&Options=&Search=`
- Example CPC staff report: `https://milwaukee.legistar.com/View.ashx?GUID=B4163330-2E43-4410-AC06-94F001098679&ID=15552910&M=F`
