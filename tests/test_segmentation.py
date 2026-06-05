from __future__ import annotations

import datetime as dt

from plan_commission_workbench.segmentation import AgendaSegmenter, SectionClipper


def test_agenda_segmenter_enriches_visible_file_id_from_event_items() -> None:
    text = """
    1. 88001 Conditional Use for a 100-unit apartment building
       with underground parking.
    2. 88002 Informational report from planning staff
    """
    event_items = [
        {
            "EventItemMatterId": "96005",
            "EventItemMatterFile": "88001",
            "EventItemMatterName": "Construct 100-unit apartment building",
        },
        {
            "EventItemMatterId": "96006",
            "EventItemMatterFile": "88002",
            "EventItemMatterName": "Planning staff report",
        },
    ]

    segments = AgendaSegmenter().segment(
        text,
        event_id="27999",
        meeting_date=dt.date(2026, 6, 1),
        event_items=event_items,
    )

    assert segments[0].city_item_id == "96005"
    assert segments[0].file_id == "88001"
    assert "underground parking" in segments[0].description
    assert segments[1].city_item_id == "96006"


def test_agenda_segmenter_keeps_event_item_when_docling_text_omits_it() -> None:
    event_items = [
        {
            "EventItemMatterId": "96005",
            "EventItemMatterFile": "88001",
            "EventItemMatterName": "Construct 100-unit apartment building",
        }
    ]

    segments = AgendaSegmenter().segment(
        "No numbered rows survived extraction",
        event_id="27999",
        meeting_date=dt.date(2026, 6, 1),
        event_items=event_items,
    )

    assert len(segments) == 1
    assert segments[0].description == "Construct 100-unit apartment building"


def test_section_clipper_returns_only_sections_3_and_5() -> None:
    text = """
    Section 2. Property
    Ignore this section.
    Section 3. Applicant and Project Contact
    Applicant Name Jane Applicant
    Project contact person Pat Contact
    Section 4. Submittal
    Ignore this section too.
    Section 5. Project Information
    Project description: 48 dwelling units.
    Section 6. Signatures
    Ignore signatures.
    """

    clipped = SectionClipper().clip_sections_3_and_5(text)

    assert "Applicant Name Jane Applicant" in clipped
    assert "48 dwelling units" in clipped
    assert "Ignore this section" not in clipped
    assert "Ignore signatures" not in clipped


def test_section_clipper_handles_docling_markdown_and_compressed_headings() -> None:
    text = """
    ## 2.ReviewFees
    Ignore fee text.
    - 3.PropertyOwnerandAgentInformation
    Nameof Property Owner:Michaeland Lisa Schuster
    Firm Preparing Survey:
    Birrenkott Surveying
    - 4.Property InformationforProperties Located withinMadisonCity Limits
    Parcel Addresses:7021Cottage GroveRoad
    - 4a.Property InformationforForPropertiesLocatedOutsidetheMadisonCityLimits
    Ignore 4a.
    ## 5.SubdivisionContents andDescription.Completetableasit pertainstoyourrequest
    Land Use
    Residential
    PROJECTTOTALS 20.043 Acres
    ## 6.RequiredSubmittal Materials
    Ignore submittals.
    """

    clipped = SectionClipper().clip_sections_3_and_5(text)

    assert "Michaeland Lisa Schuster" in clipped
    assert "Birrenkott Surveying" in clipped
    assert "PROJECTTOTALS 20.043 Acres" in clipped
    assert "Ignore fee text" not in clipped
    assert "Ignore 4a" not in clipped
    assert "Ignore submittals" not in clipped
