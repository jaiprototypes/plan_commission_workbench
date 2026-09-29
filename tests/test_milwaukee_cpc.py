from __future__ import annotations

from plan_commission_workbench.cities.milwaukee import MilwaukeeCpcMvpService


def _attachment(attachment_id: str, name: str, suffix: str = "pdf") -> dict:
    """Purpose: keep Milwaukee ranking fixtures compact."""

    return {
        "MatterAttachmentId": attachment_id,
        "MatterAttachmentName": name,
        "MatterAttachmentHyperlink": f"https://milwaukee.legistar1.com/milwaukee/attachments/{attachment_id}.{suffix}",
    }


def test_milwaukee_evidence_ranking_prefers_staff_report_and_exhibits() -> None:
    raw_item = {
        "EventItemMatterId": 73730,
        "EventItemMatterAttachments": [
            _attachment("1", "Map"),
            _attachment("2", "CPC Public Hearing Notice"),
            _attachment("3", "Patrick Barker Support"),
            _attachment("4", "Exhibit A"),
            _attachment("5", "CPC Staff Report"),
            _attachment("6", "Affidavit for Zoning Change"),
        ],
    }

    documents = MilwaukeeCpcMvpService().rank_evidence_documents(raw_item, documents_per_item=2)
    selected = [document.name for document in documents if document.selected]

    assert selected == ["CPC Staff Report", "Exhibit A"]
    assert "Map" not in [document.name for document in documents]
    assert "CPC Public Hearing Notice" not in [document.name for document in documents]
    assert "Patrick Barker Support" not in [document.name for document in documents]


def test_milwaukee_evidence_ranking_prefers_affidavit_over_continued_exhibit() -> None:
    raw_item = {
        "EventItemMatterId": 73730,
        "EventItemMatterAttachments": [
            _attachment("1", "CPC Staff Report"),
            _attachment("2", "Exhibit A"),
            _attachment("3", "Exhibit A Continued"),
            _attachment("4", "Affidavit for Zoning Change"),
        ],
    }

    documents = MilwaukeeCpcMvpService().rank_evidence_documents(raw_item, documents_per_item=3)
    selected = [document.name for document in documents if document.selected]

    assert selected == ["CPC Staff Report", "Exhibit A", "Affidavit for Zoning Change"]


def test_milwaukee_evidence_ranking_requires_direct_pdf() -> None:
    raw_item = {
        "EventItemMatterId": 73730,
        "EventItemMatterAttachments": [
            _attachment("1", "CPC Staff Report", suffix="docx"),
            _attachment("2", "Exhibit A"),
        ],
    }

    documents = MilwaukeeCpcMvpService().rank_evidence_documents(raw_item, documents_per_item=3)

    assert [document.name for document in documents] == ["Exhibit A"]


def test_milwaukee_evidence_ranking_excludes_drawing_packets() -> None:
    raw_item = {
        "EventItemMatterId": 73730,
        "EventItemMatterAttachments": [
            _attachment("1", "CPC Staff Report"),
            _attachment("2", "Exhibit A Drawings"),
            _attachment("3", "Exhibit A Continued"),
        ],
    }

    documents = MilwaukeeCpcMvpService().rank_evidence_documents(raw_item, documents_per_item=3)

    assert [document.name for document in documents] == ["CPC Staff Report", "Exhibit A Continued"]


def test_milwaukee_candidate_score_demotes_snapshot_false_positives() -> None:
    service = MilwaukeeCpcMvpService()

    industrial_score, _ = service._candidate_score(
        "A substitute ordinance relating to the change in zoning from Industrial Light, IL1, "
        "to Industrial Office, IO1, for the western 50 feet of 1215 West Layton Avenue.",
        64,
    )
    self_storage_score, _ = service._candidate_score(
        "Resolution approving a deviation to allow an indoor self-storage facility and a data "
        "processing/computer services/computational research facility in the former Walmart building.",
        120,
    )
    alley_score, _ = service._candidate_score(
        "Resolution to vacate the east-west alley, north of West Cherry Street, between North 12th Street and North 12th Lane.",
        120,
    )
    minor_mod_score, _ = service._candidate_score(
        "Substitute resolution relating to a Minor Modification to a Detailed Planned Development "
        "to allow exterior alterations to the existing residential structure.",
        120,
    )
    document_only_score, _ = service._candidate_score("Annual CPC report with no project signal.", 120)
    housing_score, _ = service._candidate_score(
        "A substitute ordinance allowing The Everett Multifamily, a multi-family residential development.",
        120,
    )

    assert industrial_score <= 0
    assert self_storage_score <= 0
    assert alley_score <= 0
    assert minor_mod_score < 20
    assert document_only_score == 0
    assert housing_score > 80


def test_milwaukee_candidate_score_demotes_2026_false_positives() -> None:
    service = MilwaukeeCpcMvpService()

    cases = [
        "A substitute ordinance relating to notification requirements for city plan commission public hearings on zoning map amendments.",
        "Substitute resolution relating to a minor modification to allow additional signage on the mixed-use building located at 1132 East North Avenue.",
        "A substitute ordinance relating to a General Planned Development to add Assembly Hall and Catering Service as permitted uses.",
        "Resolution approving exterior building modifications to an existing multi-tenant building located at 1010 West Layton Avenue.",
        "A substitute ordinance relating to the change in zoning from Two-Family Residential, RT4, to Local Business, LB2.",
        "A substitute ordinance relating to the change in zoning from Industrial Heavy, IH, to Industrial Mixed, IM.",
    ]

    for title in cases:
        score, _ = service._candidate_score(title, 120)
        assert score < 20


def test_milwaukee_project_groups_collapse_same_building_actions() -> None:
    service = MilwaukeeCpcMvpService()
    first = _project_item("252190", "The Everett Multifamily", "234 South Water Street", 200)
    second = _project_item("260085", "The Everett", "234 S. Water Street, Milwaukee, WI", 200)

    projects = service._project_groups([first, second])

    assert len(projects) == 1
    assert projects[0]["item_count"] == 2
    assert "same_building_multiple_actions" in projects[0]["tags"]
    assert {item["city_file"] for item in projects[0]["related_files"]} == {"252190", "260085"}


def test_milwaukee_project_groups_can_collapse_address_only_actions() -> None:
    service = MilwaukeeCpcMvpService()
    first = _project_item("251592", "Office Building", "350 South Water Street", 0)
    first["staff_report_triage"]["unit_count"] = None
    first["staff_report_triage"]["project_name"] = None
    second = _project_item("251591", "Riverwalk Deviation", "350 S. Water Street", 0)
    second["staff_report_triage"]["unit_count"] = None
    second["staff_report_triage"]["project_name"] = None

    projects = service._project_groups([first, second])

    assert len(projects) == 1
    assert projects[0]["project_key"] == "address:350 south water street"


def test_milwaukee_project_groups_collapse_office_name_and_address_only_actions() -> None:
    service = MilwaukeeCpcMvpService()
    first = _project_item("251592", "Pontis LLC 5-Story Office Building", "350 South Water Street", 0)
    first["staff_report_triage"]["unit_count"] = None
    first["staff_report_triage"]["building_type"] = "office"
    second = _project_item("251591", "Riverwalk Deviation", "350 S. Water Street", 0)
    second["staff_report_triage"]["unit_count"] = None
    second["staff_report_triage"]["project_name"] = None
    second["staff_report_triage"]["building_type"] = "office"

    projects = service._project_groups([first, second])

    assert len(projects) == 1
    assert projects[0]["project_key"] == "address:350 south water street"
    assert projects[0]["item_count"] == 2


def test_milwaukee_project_groups_allow_multiple_candidates_in_one_report() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("260999", "Mixed Staff Report", "100 Main Street", 120)
    item["staff_report_triage"]["project_candidates"] = [
        item["staff_report_triage"],
        {
            "target_project": True,
            "target_reason": "Second multifamily building",
            "project_name": "Second Building",
            "project_address": "200 Main Street",
            "unit_count": 80,
            "building_type": "multifamily",
            "tags": ["target_multifamily"],
            "confidence": 0.85,
        },
    ]

    projects = service._project_groups([item])

    assert len(projects) == 2
    assert {project["project_address"] for project in projects} == {"100 Main Street", "200 Main Street"}


def test_milwaukee_contact_dig_triggers_for_company_only_contacts() -> None:
    service = MilwaukeeCpcMvpService()

    assert service._needs_contact_dig({"contacts": [{"company": "Kaeding Development Group"}]})
    assert service._needs_contact_dig({"contacts": [{"role": "owner", "company": "236 WATER STREET ONE, LLC"}]})
    assert not service._needs_contact_dig({"contacts": [{"company": "Kaeding", "email": "lead@example.com"}]})
    assert not service._needs_contact_dig(
        {"contacts": [{"role": "developer", "company": "Kaeding Development Group", "phone": "952.229.4433"}]}
    )


def test_milwaukee_contact_dig_prefers_affidavit_documents() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("252190", "The Everett", "234 South Water Street", 200)
    item["evidence_documents"] = [
        {"name": "CPC Staff Report", "source_url": "https://example.test/staff.pdf", "selected": True},
        {"name": "Exhibit A", "source_url": "https://example.test/exhibit.pdf", "selected": False},
        {"name": "Affidavit for Zoning Change", "source_url": "https://example.test/affidavit.pdf", "selected": False},
    ]
    project = {"items": [item]}

    ranked = service._contact_dig_documents(project)

    assert ranked[0][1]["name"] == "Affidavit for Zoning Change"
    assert [document["name"] for _item, document in ranked] == ["Affidavit for Zoning Change"]


def test_milwaukee_contact_dig_checks_exhibit_narrative_before_matrix() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("252155", "Midtown Commons", "5825 West Hope Avenue", 200)
    item["evidence_documents"] = [
        {"name": "CPC Staff Report", "source_url": "https://example.test/staff.pdf", "selected": True},
        {"name": "Exhibit A Narrative as of 03.19.26", "source_url": "https://example.test/narrative.pdf", "selected": False},
        {"name": "Zoning Review Matrix as of 03.19.26", "source_url": "https://example.test/matrix.pdf", "selected": False},
    ]

    ranked = service._contact_dig_documents({"items": [item]}, document_limit=2)

    assert [document["name"] for _item, document in ranked] == [
        "Exhibit A Narrative as of 03.19.26",
        "Zoning Review Matrix as of 03.19.26",
    ]


def test_milwaukee_contact_dig_keeps_continued_exhibit_before_narrative() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("252190", "The Everett", "234 South Water Street", 200)
    item["evidence_documents"] = [
        {"name": "CPC Staff Report", "source_url": "https://example.test/staff.pdf", "selected": True},
        {"name": "Exhibit A Narrative", "source_url": "https://example.test/narrative.pdf", "selected": False},
        {"name": "Exhibit A Continued", "source_url": "https://example.test/continued.pdf", "selected": False},
        {"name": "Affidavit for Zoning Change", "source_url": "https://example.test/affidavit.pdf", "selected": False},
    ]

    ranked = service._contact_dig_documents({"items": [item]}, document_limit=3)

    assert [document["name"] for _item, document in ranked] == [
        "Exhibit A Continued",
        "Exhibit A Narrative",
        "Affidavit for Zoning Change",
    ]


def test_milwaukee_contact_dig_limit_can_expand_for_heavy_verification() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("252190", "The Everett", "234 South Water Street", 200)
    item["evidence_documents"] = [
        {"name": "CPC Staff Report", "source_url": "https://example.test/staff.pdf", "selected": True},
        {"name": "Exhibit A", "source_url": "https://example.test/exhibit.pdf", "selected": False},
        {"name": "Exhibit A Continued", "source_url": "https://example.test/continued.pdf", "selected": False},
        {"name": "Affidavit for Zoning Change", "source_url": "https://example.test/affidavit.pdf", "selected": False},
        {"name": "Plan of Operation", "source_url": "https://example.test/operation.pdf", "selected": False},
    ]

    ranked = service._contact_dig_documents({"items": [item]}, document_limit=3)

    assert [document["name"] for _item, document in ranked] == [
        "Exhibit A Continued",
        "Affidavit for Zoning Change",
        "Plan of Operation",
    ]


def test_milwaukee_contact_dig_keeps_one_continued_exhibit_before_affidavit() -> None:
    service = MilwaukeeCpcMvpService()
    first = _project_item("252190", "The Everett", "234 South Water Street", 200)
    first["evidence_documents"] = [
        {"name": "CPC Staff Report", "source_url": "https://example.test/staff-1.pdf", "selected": True},
        {"name": "Exhibit A Continued", "source_url": "https://example.test/continued-1.pdf", "selected": False},
        {"name": "Affidavit for Zoning Change", "source_url": "https://example.test/affidavit.pdf", "selected": False},
    ]
    second = _project_item("260085", "The Everett", "234 South Water Street", 200)
    second["evidence_documents"] = [
        {"name": "CPC Staff Report", "source_url": "https://example.test/staff-2.pdf", "selected": True},
        {"name": "Exhibit A Continued", "source_url": "https://example.test/continued-2.pdf", "selected": False},
    ]

    ranked = service._contact_dig_documents({"items": [first, second]}, document_limit=2)

    assert [document["name"] for _item, document in ranked] == ["Exhibit A Continued", "Affidavit for Zoning Change"]


def test_milwaukee_contact_dig_skips_dated_continued_exhibit_copies() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("252190", "The Everett", "234 South Water Street", 200)
    item["evidence_documents"] = [
        {"name": "CPC Staff Report", "source_url": "https://example.test/staff.pdf", "selected": True},
        {"name": "Exhibit A Continued", "source_url": "https://example.test/continued.pdf", "selected": False},
        {"name": "Exhibit A Continued as of 06.22.26.pdf", "source_url": "https://example.test/continued-as-of.pdf", "selected": False},
        {"name": "Affidavit for Zoning Change", "source_url": "https://example.test/affidavit.pdf", "selected": False},
    ]

    ranked = service._contact_dig_documents({"items": [item]}, document_limit=3)

    assert [document["name"] for _item, document in ranked] == ["Exhibit A Continued", "Affidavit for Zoning Change"]


def test_milwaukee_secondary_contact_evidence_uses_compact_staff_context() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("252190", "The Everett", "234 South Water Street", 200)
    item["_evidence_bundle"] = "FULL STAFF REPORT TEXT " * 500
    project = service._project_groups([item])[0]
    initial = {
        "project_name": "The Everett Multifamily",
        "project_address": "234 South Water Street",
        "unit_count": 200,
        "contacts": [
            {
                "role": "applicant",
                "company": "Kaeding Development Group",
                "outreach_priority": "needs_contact_detail",
            }
        ],
    }

    evidence = service._secondary_contact_evidence(project, initial, "TITLE SHEET CONTACT DETAILS")

    assert "PROJECT CONTEXT FROM STAFF REPORT" in evidence
    assert "Kaeding Development Group" in evidence
    assert "TITLE SHEET CONTACT DETAILS" in evidence
    assert "FULL STAFF REPORT TEXT" not in evidence


def test_milwaukee_payload_includes_contact_evidence_tracking_rows() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("252190", "The Everett", "234 South Water Street", 200)
    snippet = "KAEDING DEVELOPMENT GROUP 7900 INTERNATIONAL DR, STE 910 BLOOMINGTON, MN 55425 P: 952.229.4433"
    item["evidence_documents"] = [
        {
            "name": "Exhibit A Continued",
            "attachment_id": "245957",
            "source_url": "https://example.test/continued.pdf",
            "selected": True,
            "content_hash": "hash-continued",
            "docling_mode": "text_layer",
            "text_chars": 5434,
            "_tracking_text": f"title sheet\n{snippet}\nfooter",
        }
    ]
    project = {
        "project_key": "address:234 south water street|units:200",
        "project_name": "The Everett",
        "project_address": "234 South Water Street",
        "unit_count": 200,
        "related_files": [{"city_file": "252190"}],
        "items": [item],
        "contacts": [
            {
                "role": "developer",
                "company": "Kaeding Development Group",
                "mailing_address": "7900 International Dr, Ste 910, Bloomington, MN 55425",
                "phone": "952.229.4433",
                "outreach_priority": "primary",
                "useful_for_outreach": True,
                "evidence_snippet": snippet,
            }
        ],
    }

    payload = service._payload([], [item], include_text=True, include_llm=True, projects=[project])

    assert payload["tracking_summary"]["items"] == 1
    assert payload["tracking_summary"]["ranked_docs"] == 1
    assert payload["tracking_summary"]["contact_evidence_rows"] == 1
    row = payload["contact_evidence_rows"][0]
    assert row["document_name"] == "Exhibit A Continued"
    assert row["document_family"] == "exhibit a continued"
    assert row["content_hash"] == "hash-continued"
    assert row["text_offset_start"] is not None
    assert row["text_offset_end"] is not None
    assert row["company"] == "Kaeding Development Group"
    assert "_tracking_text" not in payload["items"][0]["evidence_documents"][0]
    assert "_tracking_text" not in payload["projects"][0]["items"][0]["evidence_documents"][0]


def test_milwaukee_fast_text_layer_is_scoped_to_verified_text_packets() -> None:
    service = MilwaukeeCpcMvpService()

    assert service._should_use_fast_text_layer({"name": "CPC Staff Report"})
    assert service._should_use_fast_text_layer({"name": "Exhibit A Continued"})
    assert service._should_use_fast_text_layer({"name": "Exhibit A Narrative as of 03.19.26"})
    assert service._should_use_fast_text_layer({"name": "Project Narrative"})
    assert not service._should_use_fast_text_layer({"name": "Exhibit A Drawings"})
    assert not service._should_use_fast_text_layer({"name": "Affidavit for Zoning Change"})


def test_milwaukee_no_contact_verification_lists_checked_documents() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("251606", "Bradley Road", "11919 West Bradley Road", 100)
    item["evidence_documents"] = [
        {
            "name": "CPC Staff Report",
            "attachment_id": "staff",
            "source_url": "https://example.test/staff.pdf",
            "selected": True,
            "content_hash": "staff-hash",
            "docling_mode": "text_layer",
            "text_chars": 5599,
            "_tracking_text": "809 North Broadway Phone (414) 286-5800 Scott Crawford, Inc.",
        }
    ]
    project = {
        "items": [item],
        "contacts": [
            {
                "role": "developer",
                "company": "Scott Crawford, Inc.",
                "outreach_priority": "needs_contact_detail",
                "useful_for_outreach": False,
            }
        ],
        "contact_dig_documents": [
            {
                "document_role": "contact_dig",
                "name": "Affidavit for Zoning Change",
                "attachment_id": "affidavit",
                "source_url": "https://example.test/affidavit.pdf",
                "content_hash": "affidavit-hash",
                "docling_mode": "default",
                "text_chars": 3507,
                "_tracking_text": "Petitioner signature with no phone or email.",
            }
        ],
    }

    service._attach_contact_search_verification(project, {"contacts": project["contacts"]})

    verification = project["contact_search_verification"]
    assert verification["status"] == "no_direct_contact_found"
    assert verification["primary_contact_count"] == 0
    assert verification["checked_document_count"] == 2
    assert [row["document_name"] for row in verification["checked_documents"]] == [
        "CPC Staff Report",
        "Affidavit for Zoning Change",
    ]
    assert verification["checked_documents"][0]["checked_result"] == "city_boilerplate_contact_only"
    assert verification["checked_documents"][1]["checked_result"] == "no_direct_phone_or_email_signal_detected"
    assert project["external_enrichment_candidates"][0]["company"] == "Scott Crawford, Inc."


def test_milwaukee_project_team_contact_does_not_satisfy_primary_contact() -> None:
    service = MilwaukeeCpcMvpService()
    item = _project_item("252155", "Midtown Commons", "5825 West Hope Avenue", 200)
    item["evidence_documents"] = [
        {
            "name": "CPC Staff Report",
            "attachment_id": "staff",
            "source_url": "https://example.test/staff.pdf",
            "selected": True,
            "content_hash": "staff-hash",
            "docling_mode": "text_layer",
            "text_chars": 32196,
            "_tracking_text": "Applicant/Owner: Gorman & Company (developer)",
        }
    ]
    project = {
        "items": [item],
        "contacts": [
            {
                "role": "developer",
                "company": "Gorman & Company",
                "outreach_priority": "needs_contact_detail",
                "useful_for_outreach": False,
            },
            {
                "role": "architect",
                "company": "Korb",
                "phone": "414.273.8230",
                "mailing_address": "648 N. Plankinton Avenue, Suite 240, Milwaukee, WI 53203",
                "outreach_priority": "secondary_project_team",
                "useful_for_outreach": True,
            },
        ],
        "contact_dig_documents": [
            {
                "document_role": "contact_dig",
                "name": "Exhibit A Narrative",
                "attachment_id": "narrative",
                "source_url": "https://example.test/narrative.pdf",
                "content_hash": "narrative-hash",
                "docling_mode": "text_layer",
                "text_chars": 7911,
                "_tracking_text": "648 N. Plankinton Avenue, Suite 240 · Milwaukee, WI 53203 · 414.273.8230",
            }
        ],
    }

    service._attach_contact_search_verification(project, {"contacts": project["contacts"]})

    verification = project["contact_search_verification"]
    assert verification["status"] == "project_team_contact_only"
    assert verification["primary_contact_count"] == 0
    assert verification["useful_contact_count"] == 1
    assert project["external_enrichment_candidates"] == [
        {
            "role": "developer",
            "company": "Gorman & Company",
            "name": None,
            "project_key": None,
            "project_name": None,
            "project_address": None,
            "unit_count": None,
            "reason": "CPC documents identify this party but do not publish a direct contact route.",
            "evidence_snippet": None,
        }
    ]
    assert verification["checked_documents"][1]["checked_result"] == "project_team_direct_signal_only"


def test_milwaukee_primary_contact_marks_project_solved() -> None:
    service = MilwaukeeCpcMvpService()
    project = {
        "items": [],
        "contacts": [
            {
                "role": "developer",
                "company": "Kaeding Development Group",
                "phone": "952.229.4433",
                "outreach_priority": "primary",
                "useful_for_outreach": True,
            }
        ],
    }

    service._attach_contact_search_verification(project, {"contacts": project["contacts"]})

    assert project["contact_search_verification"]["status"] == "primary_contact_found"
    assert project["contact_search_verification"]["primary_contact_count"] == 1
    assert project["external_enrichment_candidates"] == []


def test_milwaukee_external_enrichment_skips_address_named_owner_llcs() -> None:
    service = MilwaukeeCpcMvpService()
    project = {
        "contacts": [
            {
                "role": "owner",
                "company": "236 WATER STREET ONE, LLC",
                "outreach_priority": "ownership_evidence_only",
                "useful_for_outreach": False,
            },
            {
                "role": "developer",
                "company": "Kaeding Development Group",
                "outreach_priority": "needs_contact_detail",
                "useful_for_outreach": False,
            },
        ],
    }

    candidates = service._external_enrichment_candidates(project)

    assert [candidate["company"] for candidate in candidates] == ["Kaeding Development Group"]


def test_milwaukee_flags_affidavit_signature_ocr_contacts() -> None:
    service = MilwaukeeCpcMvpService()
    extraction = service._flag_contact_quality(
        {
            "contacts": [
                {
                    "role": "applicant",
                    "name": "Brady Norman Gerelog",
                    "company": "Kaeding Development Group",
                    "evidence_snippet": "Petitioner(signature) BndyNorMan Qerelog",
                    "confidence": 0.55,
                }
            ],
            "review_notes": [],
        }
    )

    assert extraction["contacts"][0]["review_flags"] == ["manual_verify_affidavit_signature_ocr"]
    assert "Verify OCR-derived affidavit signature names" in extraction["review_notes"][0]


def test_milwaukee_marks_address_named_owner_llc_as_evidence_only() -> None:
    service = MilwaukeeCpcMvpService()
    extraction = service._flag_contact_quality(
        {
            "contacts": [
                {
                    "role": "owner",
                    "company": "236 WATER STREET ONE, LLC",
                    "evidence_snippet": "236 WATER STREET ONE, LLC (owner/applicant)",
                    "confidence": 0.8,
                },
                {
                    "role": "developer",
                    "company": "Kaeding Development Group",
                    "mailing_address": "7900 International Dr, Ste 910, Bloomington, MN 55425",
                    "phone": "952.229.4433",
                    "evidence_snippet": "Kaeding Development Group",
                    "confidence": 0.9,
                },
            ],
            "review_notes": [],
        }
    )

    owner, developer = extraction["contacts"]
    assert owner["outreach_priority"] == "ownership_evidence_only"
    assert owner["useful_for_outreach"] is False
    assert owner["review_flags"] == ["ownership_evidence_only"]
    assert developer["outreach_priority"] == "primary"
    assert developer["useful_for_outreach"] is True


def test_milwaukee_demotes_duplicate_primary_company_contacts() -> None:
    service = MilwaukeeCpcMvpService()
    extraction = service._flag_contact_quality(
        {
            "contacts": [
                {
                    "role": "developer",
                    "company": "Kaeding Development Group",
                    "mailing_address": "7900 International Dr, Ste 910, Bloomington, MN 55425",
                    "phone": "952.229.4433",
                    "confidence": 0.9,
                },
                {
                    "role": "applicant",
                    "company": "Kaeding Development Group",
                    "mailing_address": "222 West Washington Ave, Suite 650, Madison, WI 53703",
                    "phone": "608.256.7304",
                    "confidence": 0.9,
                },
            ],
            "review_notes": [],
        }
    )

    developer, applicant = extraction["contacts"]
    assert developer["outreach_priority"] == "primary"
    assert developer["useful_for_outreach"] is True
    assert applicant["outreach_priority"] == "duplicate_company_contact_detail"
    assert applicant["useful_for_outreach"] is False
    assert applicant["review_flags"] == ["duplicate_company_contact_detail"]
    assert "Review duplicate primary contacts" in extraction["review_notes"][0]


def test_milwaukee_flags_nonstandard_phone_ocr_contacts() -> None:
    service = MilwaukeeCpcMvpService()
    extraction = service._flag_contact_quality(
        {
            "contacts": [
                {
                    "role": "other",
                    "company": "Pierce Engineering, Inc",
                    "phone": "414.278.90200",
                    "evidence_snippet": "Pierce Engineering, Inc P: 414.278.90200",
                    "confidence": 0.7,
                }
            ],
            "review_notes": [],
        }
    )

    assert extraction["contacts"][0]["review_flags"] == ["manual_verify_phone_ocr"]
    assert "Verify nonstandard OCR-derived phone numbers" in extraction["review_notes"][0]


def _project_item(city_file: str, name: str, address: str, units: int) -> dict:
    """Purpose: create a compact grouped-project fixture."""

    return {
        "event_id": "13555",
        "meeting_date": "2026-06-08",
        "matter_id": city_file,
        "city_file": city_file,
        "title": name,
        "candidate_score": 100,
        "staff_report_triage": {
            "target_project": True,
            "target_reason": "Multifamily project",
            "project_name": name,
            "project_address": address,
            "unit_count": units,
            "building_type": "multifamily",
            "tags": ["target_multifamily"],
            "confidence": 0.9,
        },
        "_evidence_bundle": "staff report text",
    }
