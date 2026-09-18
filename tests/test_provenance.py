"""Key tests for the /provenance endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//provenance-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def provenance(
    ics: str,
    uid="evt-1",
    window_start="2026-01-01T00:00:00Z",
    window_end="2026-02-01T00:00:00Z",
):
    return client.post(
        "/provenance",
        json={
            "ics": ics,
            "uid": uid,
            "window_start": window_start,
            "window_end": window_end,
        },
    )


def occurrences(resp) -> list[dict]:
    return resp.json()["occurrences"]


# ---------------------------------------------------------------------------
# Generation sources: DTSTART / RRULE / RDATE
# ---------------------------------------------------------------------------

def test_rrule_series_marks_dtstart_and_rrule_sources():
    # DTSTART coincides with the first RRULE occurrence: DTSTART wins.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
    ))
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    occs = occurrences(resp)
    assert [(o["recurrence_id"], o["source"]) for o in occs] == [
        ("2026-01-05T09:00:00Z", "DTSTART"),
        ("2026-01-06T09:00:00Z", "RRULE"),
        ("2026-01-07T09:00:00Z", "RRULE"),
    ]
    for occ in occs:
        assert occ["excluded"] is False
        assert occ["override"] is None
        assert occ["status"] == "CONFIRMED"
    assert [o["start"] for o in occs] == [o["recurrence_id"] for o in occs]
    assert [o["end"] for o in occs] == [
        "2026-01-05T10:00:00Z",
        "2026-01-06T10:00:00Z",
        "2026-01-07T10:00:00Z",
    ]


def test_rdate_series_marks_rdate_sources():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RDATE:20260120T090000Z",
        "RDATE:20260121T090000Z,20260122T090000Z",
    ))
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    assert [(o["recurrence_id"], o["source"]) for o in occurrences(resp)] == [
        ("2026-01-05T09:00:00Z", "DTSTART"),
        ("2026-01-20T09:00:00Z", "RDATE"),
        ("2026-01-21T09:00:00Z", "RDATE"),
        ("2026-01-22T09:00:00Z", "RDATE"),
    ]


def test_rdate_restating_an_rrule_occurrence_reports_rdate():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
        "RDATE:20260106T090000Z",  # same instant as the second RRULE occurrence
    ))
    resp = provenance(ics)
    assert [(o["recurrence_id"], o["source"]) for o in occurrences(resp)] == [
        ("2026-01-05T09:00:00Z", "DTSTART"),
        ("2026-01-06T09:00:00Z", "RDATE"),
    ]


def test_non_recurring_event_is_a_single_dtstart_candidate():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "STATUS:TENTATIVE",
    ))
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    assert occurrences(resp) == [
        {
            "uid": "evt-1",
            "recurrence_id": None,
            "source": "DTSTART",
            "excluded": False,
            "override": None,
            "start": "2026-01-05T09:00:00Z",
            "end": "2026-01-05T10:00:00Z",
            "status": "TENTATIVE",
        }
    ]


def test_dst_series_reports_utc_candidate_times():
    # America/New_York: EST (UTC-5) until 2026-03-08, then EDT (UTC-4).
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=America/New_York:20260301T090000",
        "DTEND;TZID=America/New_York:20260301T100000",
        "RRULE:FREQ=WEEKLY;COUNT=3",
    ))
    resp = provenance(ics, window_start="2026-02-25T00:00:00Z", window_end="2026-04-01T00:00:00Z")
    assert resp.status_code == 200, resp.text
    assert [(o["recurrence_id"], o["start"]) for o in occurrences(resp)] == [
        ("2026-03-01T14:00:00Z", "2026-03-01T14:00:00Z"),  # EST
        ("2026-03-08T13:00:00Z", "2026-03-08T13:00:00Z"),  # EDT
        ("2026-03-15T13:00:00Z", "2026-03-15T13:00:00Z"),  # EDT
    ]


# ---------------------------------------------------------------------------
# EXDATE exclusion
# ---------------------------------------------------------------------------

def test_exdate_marks_candidate_excluded_with_null_finals():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE:20260106T090000Z",
    ))
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    occs = occurrences(resp)
    assert [o["recurrence_id"] for o in occs] == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T09:00:00Z",
        "2026-01-07T09:00:00Z",
    ]
    excluded = occs[1]
    assert excluded["source"] == "RRULE"
    assert excluded["excluded"] is True
    assert excluded["override"] is None
    assert excluded["start"] is None
    assert excluded["end"] is None
    assert excluded["status"] is None
    assert occs[0]["excluded"] is False
    assert occs[2]["excluded"] is False


def test_exdate_can_remove_the_dtstart_occurrence():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
        "EXDATE:20260105T090000Z",
    ))
    resp = provenance(ics)
    occs = occurrences(resp)
    assert [(o["source"], o["excluded"]) for o in occs] == [
        ("DTSTART", True),
        ("RRULE", False),
    ]


# ---------------------------------------------------------------------------
# RECURRENCE-ID overrides: reschedule and cancel
# ---------------------------------------------------------------------------

def weekly_with_override(*override_lines: str) -> str:
    master = event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",  # Mondays 09:00 UTC
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",  # Jan 5, 12, 19
    )
    override = event("UID:evt-1", *override_lines)
    return cal(master, override)


def test_override_reschedule_reports_new_final_times():
    ics = weekly_with_override(
        "RECURRENCE-ID:20260112T090000Z",
        "DTSTART:20260113T100000Z",
        "DTEND:20260113T110000Z",
        "STATUS:TENTATIVE",
    )
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    occs = occurrences(resp)
    moved = occs[1]
    assert moved["recurrence_id"] == "2026-01-12T09:00:00Z"
    assert moved["source"] == "RRULE"
    assert moved["excluded"] is False
    assert moved["override"] == "RESCHEDULED"
    assert moved["start"] == "2026-01-13T10:00:00Z"
    assert moved["end"] == "2026-01-13T11:00:00Z"
    assert moved["status"] == "TENTATIVE"
    # Untouched occurrences keep their master-derived finals.
    assert occs[0]["start"] == "2026-01-05T09:00:00Z"
    assert occs[2]["start"] == "2026-01-19T09:00:00Z"


def test_override_cancel_reports_cancelled_status_and_null_times():
    ics = weekly_with_override(
        "RECURRENCE-ID:20260112T090000Z",
        "DTSTART:20260112T090000Z",
        "DTEND:20260112T100000Z",
        "STATUS:CANCELLED",
    )
    resp = provenance(ics)
    occs = occurrences(resp)
    cancelled = occs[1]
    assert cancelled["recurrence_id"] == "2026-01-12T09:00:00Z"
    assert cancelled["source"] == "RRULE"
    assert cancelled["excluded"] is False
    assert cancelled["override"] == "CANCELLED"
    assert cancelled["start"] is None
    assert cancelled["end"] is None
    assert cancelled["status"] == "CANCELLED"


def test_cancelled_override_without_dtstart_is_accepted():
    # Like /expand, a cancellation is dropped before its DTSTART is read.
    ics = weekly_with_override(
        "RECURRENCE-ID:20260112T090000Z",
        "STATUS:CANCELLED",
    )
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    cancelled = occurrences(resp)[1]
    assert cancelled["override"] == "CANCELLED"
    assert cancelled["status"] == "CANCELLED"


def test_excluded_occurrence_resurrected_by_override():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=3",
            "EXDATE:20260106T090000Z",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T120000Z",
            "DTEND:20260106T130000Z",
        ),
    )
    resp = provenance(ics)
    occs = occurrences(resp)
    revived = occs[1]
    assert revived["recurrence_id"] == "2026-01-06T09:00:00Z"
    assert revived["excluded"] is True
    assert revived["override"] == "RESCHEDULED"
    assert revived["start"] == "2026-01-06T12:00:00Z"
    assert revived["end"] == "2026-01-06T13:00:00Z"
    assert revived["status"] == "CONFIRMED"


def test_override_moved_into_window_from_outside():
    # The March 2 occurrence (outside the January window) is moved into it.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=10",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260302T090000Z",
            "DTSTART:20260115T120000Z",
            "DTEND:20260115T130000Z",
        ),
    )
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    occs = occurrences(resp)
    moved = [o for o in occs if o["recurrence_id"] == "2026-03-02T09:00:00Z"]
    assert len(moved) == 1
    moved = moved[0]
    assert moved["source"] == "RRULE"  # the series does generate March 2
    assert moved["override"] == "RESCHEDULED"
    assert moved["start"] == "2026-01-15T12:00:00Z"
    assert moved["end"] == "2026-01-15T13:00:00Z"
    # The record sorts by its final instance time, between Jan 12 and Jan 19.
    assert [o["start"] for o in occs if o["start"]] == [
        "2026-01-05T09:00:00Z",
        "2026-01-12T09:00:00Z",
        "2026-01-15T12:00:00Z",
        "2026-01-19T09:00:00Z",
        "2026-01-26T09:00:00Z",
    ]


def test_override_moved_out_of_window_keeps_record():
    # The January 12 occurrence is moved to March: the candidate is still
    # traced (its original slot lies in the window), with out-of-window finals.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=WEEKLY;COUNT=3",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260310T090000Z",
            "DTEND:20260310T100000Z",
        ),
    )
    resp = provenance(ics)
    occs = occurrences(resp)
    assert len(occs) == 3
    moved = [o for o in occs if o["recurrence_id"] == "2026-01-12T09:00:00Z"]
    assert len(moved) == 1
    assert moved[0]["override"] == "RESCHEDULED"
    assert moved[0]["start"] == "2026-03-10T09:00:00Z"


def test_orphan_override_reports_null_source():
    # RECURRENCE-ID that no RRULE/RDATE/DTSTART generates: /expand still
    # produces the instance; provenance reports it with a null source.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260220T090000Z",
            "DTSTART:20260110T150000Z",
            "DTEND:20260110T160000Z",
        ),
    )
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    orphan = [o for o in occurrences(resp) if o["recurrence_id"] == "2026-02-20T09:00:00Z"]
    assert len(orphan) == 1
    assert orphan[0]["source"] is None
    assert orphan[0]["override"] == "RESCHEDULED"
    assert orphan[0]["start"] == "2026-01-10T15:00:00Z"


def test_override_with_tzid_recurrence_id_matches_utc_candidate():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID;TZID=Europe/Berlin:20260106T100000",  # = 09:00Z
            "DTSTART:20260107T150000Z",
            "DTEND:20260107T160000Z",
        ),
    )
    resp = provenance(ics)
    occs = occurrences(resp)
    assert len(occs) == 2
    moved = occs[1]
    assert moved["recurrence_id"] == "2026-01-06T09:00:00Z"
    assert moved["override"] == "RESCHEDULED"
    assert moved["start"] == "2026-01-07T15:00:00Z"


def test_cancelled_master_suppresses_candidates_but_not_overrides():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=3",
            "STATUS:CANCELLED",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T110000Z",
            "DTEND:20260106T120000Z",
        ),
    )
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    occs = occurrences(resp)
    assert occs == [
        {
            "uid": "evt-1",
            "recurrence_id": "2026-01-06T09:00:00Z",
            "source": None,  # a cancelled master never parses its RRULE
            "excluded": False,
            "override": "RESCHEDULED",
            "start": "2026-01-06T11:00:00Z",
            "end": "2026-01-06T12:00:00Z",
            "status": "CONFIRMED",
        }
    ]


def test_cancelled_master_without_overrides_yields_empty_trace():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "STATUS:CANCELLED",
    ))
    resp = provenance(ics)
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 0
    assert occurrences(resp) == []


def test_override_of_non_recurring_event():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260105T090000Z",
            "DTSTART:20260105T140000Z",
            "DTEND:20260105T150000Z",
        ),
    )
    resp = provenance(ics)
    occs = occurrences(resp)
    assert occs == [
        {
            "uid": "evt-1",
            "recurrence_id": "2026-01-05T09:00:00Z",
            "source": "DTSTART",
            "excluded": False,
            "override": "RESCHEDULED",
            "start": "2026-01-05T14:00:00Z",
            "end": "2026-01-05T15:00:00Z",
            "status": "CONFIRMED",
        }
    ]


# ---------------------------------------------------------------------------
# Windowing and ordering
# ---------------------------------------------------------------------------

def test_window_selects_overlapping_candidates():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=10",
    ))
    resp = provenance(ics, window_start="2026-01-07T00:00:00Z", window_end="2026-01-10T00:00:00Z")
    assert [o["recurrence_id"] for o in occurrences(resp)] == [
        "2026-01-07T09:00:00Z",
        "2026-01-08T09:00:00Z",
        "2026-01-09T09:00:00Z",
    ]


def test_candidate_overlapping_window_start_is_traced():
    # A 3-hour occurrence starting before the window but ending inside it.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260104T220000Z",
        "DTEND:20260105T010000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
    ))
    resp = provenance(ics, window_start="2026-01-05T00:00:00Z", window_end="2026-01-06T00:00:00Z")
    assert [o["start"] for o in occurrences(resp)] == [
        "2026-01-04T22:00:00Z",
        "2026-01-05T22:00:00Z",
    ]


def test_other_uids_in_the_calendar_are_ignored():
    ics = cal(
        event("UID:evt-1", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("UID:evt-2", "DTSTART:20260106T090000Z", "DTEND:20260106T100000Z"),
    )
    resp = provenance(ics, uid="evt-2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["uid"] == "evt-2"
    assert [o["uid"] for o in body["occurrences"]] == ["evt-2"]
    assert body["occurrences"][0]["start"] == "2026-01-06T09:00:00Z"


# ---------------------------------------------------------------------------
# Errors: 422 for missing source or invalid window/calendar
# ---------------------------------------------------------------------------

def test_unknown_uid_returns_422():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z"))
    resp = provenance(ics, uid="no-such-uid")
    assert resp.status_code == 422
    assert "no-such-uid" in resp.json()["detail"]


def test_uid_without_master_returns_422():
    # Only RECURRENCE-ID overrides: no DTSTART/RRULE/RDATE source exists.
    ics = cal(event(
        "UID:evt-1",
        "RECURRENCE-ID:20260105T090000Z",
        "DTSTART:20260105T100000Z",
        "DTEND:20260105T110000Z",
    ))
    resp = provenance(ics)
    assert resp.status_code == 422
    assert "no master event" in resp.json()["detail"]


def test_invalid_window_returns_422():
    ics = cal(event("UID:evt-1", "DTSTART:20260105T090000Z"))
    resp = provenance(ics, window_start="2026-02-01T00:00:00Z", window_end="2026-01-01T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_missing_uid_field_returns_422():
    resp = client.post(
        "/provenance",
        json={
            "ics": cal(event("UID:evt-1", "DTSTART:20260105T090000Z")),
            "window_start": "2026-01-01T00:00:00Z",
            "window_end": "2026-02-01T00:00:00Z",
        },
    )
    assert resp.status_code == 422


def test_unparseable_ics_returns_422():
    resp = provenance("this is not an iCalendar document")
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_invalid_rrule_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "RRULE:FREQ=BOGUS;COUNT=2",
    ))
    resp = provenance(ics)
    assert resp.status_code == 422
    assert "invalid RRULE" in resp.json()["detail"]


def test_unknown_tzid_returns_422():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T090000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T100000",
    ))
    resp = provenance(ics)
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_missing_dtstart_returns_422():
    ics = cal(event("UID:evt-1", "RRULE:FREQ=DAILY;COUNT=2"))
    resp = provenance(ics)
    assert resp.status_code == 422
    assert "DTSTART" in resp.json()["detail"]


def test_thisandfuture_range_returns_422():
    ics = weekly_with_override(
        "RECURRENCE-ID;RANGE=THISANDFUTURE:20260112T090000Z",
        "DTSTART:20260112T100000Z",
    )
    resp = provenance(ics)
    assert resp.status_code == 422
    assert "THISANDFUTURE" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Consistency with /expand
# ---------------------------------------------------------------------------

def test_surviving_instances_match_expand_exactly():
    # Every /expand instance must appear in the trace with identical final
    # values, and every traced candidate with a non-null start must be an
    # /expand instance.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART;TZID=America/New_York:20260301T090000",
            "DTEND;TZID=America/New_York:20260301T100000",
            "RRULE:FREQ=WEEKLY;COUNT=4",
            "EXDATE;TZID=America/New_York:20260308T090000",
            "SUMMARY:weekly sync",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID;TZID=America/New_York:20260315T090000",
            "DTSTART;TZID=America/New_York:20260316T110000",
            "DTEND;TZID=America/New_York:20260316T120000",
            "STATUS:TENTATIVE",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID;TZID=America/New_York:20260322T090000",
            "STATUS:CANCELLED",
        ),
    )
    window = {"window_start": "2026-02-25T00:00:00Z", "window_end": "2026-04-01T00:00:00Z"}
    resp = provenance(ics, **window)
    assert resp.status_code == 200, resp.text
    expanded = client.post("/expand", json={"ics": ics, **window})
    assert expanded.status_code == 200, expanded.text

    surviving = [
        (o["recurrence_id"], o["start"], o["end"], o["status"])
        for o in occurrences(resp)
        if o["start"] is not None
    ]
    expanded_tuples = [
        (i["recurrence_id"], i["start"], i["end"], i["status"])
        for i in expanded.json()["instances"]
    ]
    assert surviving == expanded_tuples
    # The trace additionally explains the two lost candidates.
    lost = [o for o in occurrences(resp) if o["start"] is None]
    assert [(o["recurrence_id"], o["excluded"], o["override"]) for o in lost] == [
        ("2026-03-08T13:00:00Z", True, None),
        ("2026-03-22T13:00:00Z", False, "CANCELLED"),
    ]
