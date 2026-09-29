from __future__ import annotations

from plan_commission_workbench.contact_evidence import ContactEvidenceSelector


def _block(text: str, x: float, top: float, width: float = 170, method: str = "text_layer") -> dict:
    """Use ordinary bottom-origin PDF page coordinates."""
    return {"text": text, "bbox": [x, top - 10, x + width, top], "method": method}


def _panel(lines: list[str], x: float = 30, top: float = 750) -> list[dict]:
    """Keep source contact rows physically separate."""
    return [_block(text, x, top - 15 * index) for index, text in enumerate(lines)]


def _extract(blocks: list[dict], coverage_complete: bool = True) -> dict:
    """Run the public reader-to-selector interface."""
    return ContactEvidenceSelector().extract({
        "pages": [{"page": 3, "width": 1200, "height": 850, "blocks": blocks}],
        "coverage_complete": coverage_complete,
    })


def test_columns_preserve_owner_identity_and_malformed_engineer_phone() -> None:
    owner = ["OWNER", "KAEDING DEVELOPMENT GROUP", "7900 INTERNATIONAL DR, STE 910",
             "BLOOMINGTON, MN 55425", "P: 952.229.4433", "WWW.KAEDING-GROUP.COM"]
    engineer = ["STRUCTURAL ENGINEER", "PIERCE ENGINEERING, INC", "305 N PLANKINTON AVE, STE 101",
                "MILWAUKEE, WI 53203", "P: 414.278.90200", "pierceengineers.com"]
    result = _extract(_panel(owner) + _panel(engineer, x=300))

    by_role = {contact["role"]: contact for contact in result["contacts"]}
    assert set(by_role) == {"owner", "engineer"}
    assert by_role["owner"]["company"] == "KAEDING DEVELOPMENT GROUP"
    assert by_role["owner"]["phone"] == "952.229.4433"
    assert by_role["owner"]["mailing_address"] == "7900 INTERNATIONAL DR, STE 910\nBLOOMINGTON, MN 55425"
    assert by_role["owner"]["source_role"] == "OWNER"
    assert by_role["owner"]["useful_for_outreach"] is True
    assert "PIERCE" not in by_role["owner"]["evidence_snippet"]
    assert by_role["owner"]["website"] != "pierceengineers.com"
    assert by_role["engineer"]["phone"] == "414.278.90200"
    assert "nonstandard_phone_in_source" in by_role["engineer"]["review_flags"]
    assert by_role["engineer"]["useful_for_outreach"] is False
    assert by_role["engineer"]["pdf_page"] == 3


def test_stacked_role_panels_do_not_mix_addresses_or_contacts() -> None:
    result = _extract(_panel(["DEVELOPER", "Example Development LLC", "1 First Street",
                              "Milwaukee, WI 53202", "hello@example.com"]) +
                      _panel(["CIVIL ENGINEER", "Other Engineering Inc", "2 Second Street",
                              "Madison, WI 53703", "608-555-0123"], top=660))
    developer, engineer = result["contacts"]

    assert developer["email"] == "hello@example.com"
    assert developer["phone"] is None
    assert engineer["phone"] == "608-555-0123"
    assert "Second" not in developer["mailing_address"]
    assert "555" not in developer["evidence_snippet"]


def test_split_word_heading_and_person_with_company() -> None:
    blocks = [_block("PROJECT", 30, 750, 50), _block("ARCHITECT", 85, 750, 70)]
    blocks += _panel(["BRIAN GRIEBL, AIA", "VJS CONSTRUCTION SERVICES",
                      "W233 N2847 ROUNDY CIRCLE WEST", "PEWAUKEE, WI 53072",
                      "P: 262.542.9000", "VJSCS.COM"], top=735)
    contact = _extract(blocks)["contacts"][0]

    assert contact["source_role"] == "PROJECT ARCHITECT"
    assert contact["name"] == "BRIAN GRIEBL, AIA"
    assert contact["company"] == "VJS CONSTRUCTION SERVICES"
    assert contact["mailing_address"].startswith("W233 N2847")
    assert contact["useful_for_outreach"] is False


def test_two_line_heading_remains_one_role() -> None:
    blocks = _panel(["GENERAL", "CONTRACTOR", "Construction Example LLC", "262.555.1234"])
    contact = _extract(blocks)["contacts"][0]

    assert contact["source_role"] == "GENERAL CONTRACTOR"
    assert contact["role"] == "contractor"
    assert contact["company"] == "Construction Example LLC"


def test_unlabelled_and_missing_company_stay_unresolved() -> None:
    result = _extract(_panel(["Some Company", "414.555.0100", "hello@example.com"]))
    assert result["contacts"] == []
    assert result["unresolved_blocks"]
    assert "Some Company" in result["unresolved_blocks"][0]["evidence_snippet"]
    assert "414.555.0100" in "\n".join(block["evidence_snippet"] for block in result["unresolved_blocks"])

    result = _extract(_panel(["OWNER", "414.555.0100"]))
    assert result["contacts"] == []
    assert result["unresolved_blocks"][0]["review_flags"] == ["contact_association_unresolved"]


def test_city_staff_boilerplate_cannot_become_a_developer_lead() -> None:
    result = _extract(_panel(["APPLICANT", "CITY OF MILWAUKEE",
                             "DEPARTMENT OF CITY DEVELOPMENT", "414.555.0100"]))
    assert result["contacts"] == []
    assert "city_staff_not_developer" in result["unresolved_blocks"][0]["review_flags"]


def test_ocr_and_partial_coverage_are_reported() -> None:
    blocks = _panel(["DEVELOPER", "Example LLC", "414.555.0100"])
    for block in blocks:
        block["method"] = "ocr"
    result = _extract(blocks, coverage_complete=False)
    assert "incomplete_document_coverage" in result["warnings"]
    assert "ocr_evidence_requires_review" in result["contacts"][0]["review_flags"]
    assert result["contacts"][0]["useful_for_outreach"] is False


def test_multiline_source_block_preserves_fields_and_source_phone() -> None:
    result = _extract([{"text": "OWNER\nExample LLC\n1 Main Street\nMilwaukee, WI 53202\n414.555.0100",
                        "bbox": [30, 650, 200, 750], "method": "text_layer"}])
    contact = result["contacts"][0]
    assert contact["company"] == "Example LLC"
    assert contact["phone"] == "414.555.0100"
    assert "414.555.0100" not in contact["mailing_address"]


def test_narrative_role_mention_does_not_assign_a_contact() -> None:
    result = _extract(_panel(["The developer will contact the engineer.", "Example LLC", "414.555.0100"]))
    assert result["contacts"] == []
    assert result["unresolved_blocks"]


def test_multiple_phone_numbers_require_review_instead_of_silent_selection() -> None:
    result = _extract(_panel(["DEVELOPER", "Example LLC", "414.555.0100", "608.555.0101"]))
    contact = result["contacts"][0]
    assert "multiple_phone_numbers_require_review" in contact["review_flags"]
    assert "608.555.0101" in contact["evidence_snippet"]
    assert contact["useful_for_outreach"] is False


def test_unlabelled_city_staff_context_is_flagged() -> None:
    result = _extract(_panel(["CITY OF MILWAUKEE", "PLANNING STAFF", "414.555.0100"]))
    assert result["contacts"] == []
    assert "city_staff_not_developer" in result["unresolved_blocks"][0]["review_flags"]


def test_parenthesized_phone_extension_and_suite_remain_exact() -> None:
    result = _extract(_panel(["DEVELOPER", "Example LLC", "1 Main Street", "Suite 200",
                             "Milwaukee, WI 53202", "(414)555-0100 ext. 42"]))
    contact = result["contacts"][0]
    assert contact["phone"] == "(414)555-0100 ext. 42"
    assert "Suite 200" in contact["mailing_address"]
    assert contact["useful_for_outreach"] is True


def test_address_only_contact_is_reviewable_but_not_outreach_ready() -> None:
    result = _extract(_panel(["OWNER", "Example LLC", "1 Main Street", "Milwaukee, WI 53202"]))
    contact = result["contacts"][0]
    assert contact["mailing_address"] == "1 Main Street\nMilwaukee, WI 53202"
    assert contact["useful_for_outreach"] is False
    assert "no_verified_direct_contact" in contact["review_flags"]


def test_unlabelled_neighbor_does_not_supply_owner_phone() -> None:
    blocks = [_block("OWNER", 30, 750, 50), _block("Example LLC", 30, 735, 80),
              _block("1 Main Street", 30, 720, 90), _block("Other Company", 220, 735, 50),
              _block("414.555.0100", 220, 720, 50)]
    result = _extract(blocks)
    assert result["contacts"][0]["phone"] is None
    assert result["contacts"][0]["useful_for_outreach"] is False
    assert "414.555.0100" in result["unresolved_blocks"][0]["evidence_snippet"]


def test_short_labelled_phone_is_preserved_for_review() -> None:
    contact = _extract(_panel(["OWNER", "Example LLC", "PHONE: 414.555.010"]))["contacts"][0]
    assert contact["phone"] == "414.555.010"
    assert "nonstandard_phone_in_source" in contact["review_flags"]
    assert contact["useful_for_outreach"] is False


def test_low_ocr_confidence_is_retained_and_flagged() -> None:
    blocks = _panel(["OWNER", "Example LLC", "414.555.0100"])
    for block in blocks:
        block.update(method="ocr", confidence=.6)
    contact = _extract(blocks)["contacts"][0]
    assert contact["ocr_confidence_min"] == .6
    assert "low_ocr_confidence" in contact["review_flags"]
    assert contact["useful_for_outreach"] is False


def test_real_everett_split_domain_rejoins_without_changing_evidence() -> None:
    rows = [
        ("OWNER", [143.8869, 1795.3574, 198.0537, 1809.6141]),
        ("KAEDING DEVELOPMENT GROUP", [143.9136, 1773.7456, 300.4627, 1783.2076]),
        ("7900 INTERNATIONAL DR, STE 910", [143.7630, 1757.6763, 299.3622, 1767.9677]),
        ("BLOOMINGTON, MN 55425", [143.9337, 1742.5563, 272.2417, 1752.8477]),
        ("P: 952.229.4433", [143.9136, 1728.1456, 215.1819, 1737.6077]),
        ("WWW.KAEDING", [143.5119, 1713.0256, 218.3849, 1722.4877]),
        ("-GROUP.COM", [219.2013, 1713.0256, 286.4767, 1722.4877]),
    ]
    contact = _extract([{"text": text, "bbox": bbox, "method": "text_layer"}
                        for text, bbox in rows])["contacts"][0]
    assert contact["website"].lower() == "www.kaeding-group.com"
    assert "WWW.KAEDING -GROUP.COM" in contact["evidence_snippet"]
    assert contact["phone"] == "952.229.4433"
    assert contact["company"] == "KAEDING DEVELOPMENT GROUP"


def test_url_suffix_is_not_extracted_from_an_unresolved_domain_fragment() -> None:
    contact = _extract(_panel(["OWNER", "Example LLC", "414.555.0100", "-GROUP.COM"]))["contacts"][0]
    assert contact["website"] is None
