"""Per-occurrence provenance for one recurring series (one ``UID``).

Where :mod:`app.expander` reports only the final instances inside a window,
this module explains *why* each candidate occurrence of a single series did
or did not survive:

* **generation** — the candidate was produced by the master event's
  ``RRULE`` or ``RDATE`` properties, or is the ``DTSTART`` occurrence
  itself (per RFC 5545 ``DTSTART`` always belongs to the recurrence set);
* **exclusion** — a matching ``EXDATE`` removed the candidate;
* **override** — a ``RECURRENCE-ID`` override rescheduled the occurrence
  (new ``DTSTART``/``DTEND``) or cancelled it (``STATUS:CANCELLED``);
* **final state** — the effective ``DTSTART``/``DTEND``/``STATUS`` after
  those steps, exactly as ``/expand`` reports the surviving instance.

The trace mirrors the expander step for step: candidates are generated in
the same duration-padded window, the surviving (post-``EXDATE``) set comes
from the shared :func:`app.expander._expand_master` engine, and overrides
are keyed by ``RECURRENCE-ID`` with the last one winning. An override
therefore determines the final state of its occurrence even when an
``EXDATE`` also matched it (a rescheduled override resurrects an excluded
occurrence, exactly as ``/expand`` does).

Only the requested ``UID``'s VEVENTs are parsed and validated; unrelated
events in the same calendar are ignored. A request whose ``uid`` has no
VEVENT at all, or only ``RECURRENCE-ID`` overrides and no master event to
generate from, raises :class:`ProvenanceError` (the series source is
missing), which the HTTP layer maps to 422 alongside the expander's
:class:`~app.expander.ExpansionError`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from dateutil.rrule import rruleset, rrulestr

from .expander import (
    MAX_OCCURRENCES_PER_UID,
    ExpansionError,
    _datetimes,
    _event_times,
    _expand_master,
    _overlaps,
    _parse_calendar,
    _props,
    _recurrence_id,
    _status,
    _to_utc,
    _validate_tzids,
)

UTC = dt.timezone.utc

# Generation-source labels. When several mechanisms produce the same
# instant (DTSTART usually coincides with the first RRULE occurrence, an
# RDATE may restate either), the most specific source wins.
SOURCE_DTSTART = "DTSTART"
SOURCE_RRULE = "RRULE"
SOURCE_RDATE = "RDATE"
_SOURCE_PRIORITY = {SOURCE_RRULE: 0, SOURCE_RDATE: 1, SOURCE_DTSTART: 2}

# What a RECURRENCE-ID override did to its occurrence.
OVERRIDE_RESCHEDULED = "RESCHEDULED"
OVERRIDE_CANCELLED = "CANCELLED"


class ProvenanceError(Exception):
    """The requested series has no usable source; surfaced as HTTP 422."""


@dataclass(frozen=True)
class OccurrenceProvenance:
    """The provenance chain of one candidate occurrence of a series."""

    uid: str
    recurrence_id: dt.datetime | None  # aware UTC; None for non-recurring events
    source: str | None  # DTSTART | RRULE | RDATE; None for orphan overrides
    excluded: bool  # an EXDATE matched the candidate occurrence
    override: str | None  # RESCHEDULED | CANCELLED; None when no override applies
    # Final effective values after exclusion/override resolution. An
    # EXDATEd occurrence leaves no trace (all three None); a cancelled one
    # keeps only its STATUS. A rescheduled occurrence carries the
    # override's own DTSTART/DTEND.
    start: dt.datetime | None  # aware UTC
    end: dt.datetime | None  # aware UTC
    status: str | None


def occurrence_provenance(
    ics_text: str,
    uid: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> list[OccurrenceProvenance]:
    """Trace every candidate occurrence of ``uid`` touching the UTC window.

    A candidate is reported when its original slot or its final instance
    overlaps ``[window_start, window_end)``; an override whose
    RECURRENCE-ID matches no in-window candidate is still reported when
    its rescheduled instance lands in the window (mirroring ``/expand``,
    where such an override produces an instance). Records are stably
    sorted by final instance time (candidates without one sort by their
    original occurrence time), then by ``recurrence_id``.
    """
    calendar = _parse_calendar(ics_text)
    events = []
    for component in calendar.walk("VEVENT"):
        component_uid = component.get("UID")
        if component_uid is not None and str(component_uid) == uid:
            events.append(component)
    if not events:
        raise ProvenanceError(f"no VEVENT with UID {uid!r}")
    for event in events:
        _validate_tzids(event)

    masters = [e for e in events if "RECURRENCE-ID" not in e]
    overrides = [e for e in events if "RECURRENCE-ID" in e]
    if not masters:
        raise ProvenanceError(
            f"UID {uid!r} has no master event: "
            "no DTSTART/RRULE/RDATE source to trace"
        )
    master = masters[0]
    master_status = _status(master)
    m_start, m_end, _ = _event_times(master)
    duration = m_end - m_start

    # Overrides are keyed by RECURRENCE-ID; like the expander, the last
    # override for a given RECURRENCE-ID wins. A cancelled override is
    # dropped before its own DTSTART/DTEND are read (they may be absent),
    # exactly as the expander skips them.
    override_by_rid: dict[
        dt.datetime, tuple[bool, dt.datetime | None, dt.datetime | None, str]
    ] = {}
    for override in overrides:
        rid = _to_utc(_recurrence_id(override, master))
        status = _status(override)
        if status == "CANCELLED":
            override_by_rid[rid] = (True, None, None, status)
        else:
            o_start, o_end, _ = _event_times(override, default_duration=duration)
            override_by_rid[rid] = (False, _to_utc(o_start), _to_utc(o_end), status)

    # Candidate generation is suppressed for a cancelled master (the whole
    # series is cancelled); overrides are still traced, mirroring /expand.
    # Recurrence properties of a cancelled master are never parsed, so an
    # invalid RRULE there cannot fail the request — again like /expand.
    recurring = bool(_props(master, "RRULE") or _props(master, "RDATE"))
    candidates: dict[dt.datetime, str] = {}
    surviving: set[dt.datetime] = set()
    rule_set = None
    rdate_utc: set[dt.datetime] = set()
    exdate_utc: set[dt.datetime] = set()
    if master_status != "CANCELLED":
        occurrences, _ = _expand_master(
            master, m_start, duration, window_start, window_end
        )
        surviving = {_to_utc(occ) for occ in occurrences}
        if recurring:
            rule_set = _build_rule_set(master, m_start)
            rdate_utc = {_to_utc(v) for v in _rdate_values(master, m_start)}
            exdate_utc = _exdate_set(master, m_start)
            candidates = _window_candidates(
                master, m_start, duration, window_start, window_end,
                rule_set, rdate_utc,
            )
        else:
            candidates = {_to_utc(m_start): SOURCE_DTSTART}

    records: dict[dt.datetime, OccurrenceProvenance] = {}
    for candidate, source in candidates.items():
        excluded = candidate not in surviving
        override = override_by_rid.pop(candidate, None)
        # /expand gives an overridden occurrence a RECURRENCE-ID even when
        # the master is not recurring; an untouched single event has none.
        rid_out = candidate if (recurring or override is not None) else None
        include = _overlaps(
            candidate, candidate + duration, window_start, window_end
        )
        if override is None:
            override_kind = None
            if excluded:
                start = end = status = None
            else:
                start, end, status = candidate, candidate + duration, master_status
        else:
            cancelled, o_start, o_end, o_status = override
            if cancelled:
                override_kind = OVERRIDE_CANCELLED
                start = end = None
                status = "CANCELLED"
            else:
                override_kind = OVERRIDE_RESCHEDULED
                start, end, status = o_start, o_end, o_status
                include = include or _overlaps(
                    o_start, o_end, window_start, window_end
                )
        if include:
            records[candidate] = OccurrenceProvenance(
                uid=uid,
                recurrence_id=rid_out,
                source=source,
                excluded=excluded,
                override=override_kind,
                start=start,
                end=end,
                status=status,
            )

    # Leftover overrides match no in-window candidate: they belong to
    # occurrences outside the padded window (or to no occurrence at all).
    # Only a rescheduling override surfaces an instance in the window; a
    # cancellation of an out-of-window occurrence leaves no trace here.
    for rid, (cancelled, o_start, o_end, o_status) in override_by_rid.items():
        if cancelled or not _overlaps(o_start, o_end, window_start, window_end):
            continue
        records[rid] = OccurrenceProvenance(
            uid=uid,
            recurrence_id=rid,
            source=_series_source(m_start, rid, rdate_utc, rule_set),
            excluded=rid in exdate_utc,
            override=OVERRIDE_RESCHEDULED,
            start=o_start,
            end=o_end,
            status=o_status,
        )

    no_rid = dt.datetime.min.replace(tzinfo=UTC)
    return sorted(
        records.values(),
        key=lambda r: (
            r.start or r.recurrence_id or no_rid,
            r.recurrence_id or no_rid,
        ),
    )


# ---------------------------------------------------------------------------
# Candidate generation (mirrors app.expander._expand_master, with sources)
# ---------------------------------------------------------------------------


def _window_candidates(
    master,
    m_start: dt.datetime,
    duration: dt.timedelta,
    window_start: dt.datetime,
    window_end: dt.datetime,
    rule_set,
    rdate_utc: set[dt.datetime],
) -> dict[dt.datetime, str]:
    """All recurrence-set occurrences in the duration-padded window.

    Returns a map of occurrence start (aware UTC) to its generation
    source. The window is padded backwards by the event duration, exactly
    like the expander, so occurrences starting before the window but
    overlapping it are traced too.
    """
    after_utc = window_start - duration
    before_utc = window_end
    candidates: dict[dt.datetime, str] = {}

    if rule_set is not None:
        after, before = _padded_bounds(m_start, duration, window_start, window_end)
        try:
            generated = 0
            for occ in rule_set.between(after, before, inc=True):
                generated += 1
                if generated > MAX_OCCURRENCES_PER_UID:
                    raise ExpansionError(
                        f"recurrence expands to more than {MAX_OCCURRENCES_PER_UID} "
                        "occurrences within the query window"
                    )
                _record_source(candidates, _to_utc(occ), SOURCE_RRULE)
        except ExpansionError:
            raise
        except (ValueError, TypeError) as exc:
            raise ExpansionError(f"cannot expand recurrence: {exc}") from exc

    for value in rdate_utc:
        if after_utc <= value <= before_utc:
            _record_source(candidates, value, SOURCE_RDATE)

    # Per RFC 5545 the DTSTART itself always belongs to the recurrence set.
    start_utc = _to_utc(m_start)
    if after_utc <= start_utc <= before_utc:
        _record_source(candidates, start_utc, SOURCE_DTSTART)
    return candidates


def _build_rule_set(master, m_start: dt.datetime):
    """An rruleset of the master's RRULE properties only (None if there are none)."""
    rrule_props = _props(master, "RRULE")
    if not rrule_props:
        return None
    rset = rruleset()
    for prop in rrule_props:
        rule_text = prop.to_ical().decode()
        try:
            rset.rrule(rrulestr(rule_text, dtstart=m_start))
        except ExpansionError:
            raise
        except Exception as exc:
            raise ExpansionError(f"invalid RRULE {rule_text!r}: {exc}") from exc
    return rset


def _series_source(
    m_start: dt.datetime,
    rid: dt.datetime,
    rdate_utc: set[dt.datetime],
    rule_set,
) -> str | None:
    """Source label for a RECURRENCE-ID that no in-window candidate matched.

    The occurrence lies outside the padded window (or outside the rule
    grid entirely); membership is tested against the series definition.
    """
    if rid == _to_utc(m_start):
        return SOURCE_DTSTART
    if rid in rdate_utc:
        return SOURCE_RDATE
    if rule_set is not None:
        if m_start.tzinfo is None:  # floating series: compare as UTC-naive
            key = rid.replace(tzinfo=None)
        else:
            key = rid.astimezone(m_start.tzinfo)
        if rule_set.between(key, key, inc=True):
            return SOURCE_RRULE
    return None


def _record_source(
    candidates: dict[dt.datetime, str], when: dt.datetime, source: str
) -> None:
    current = candidates.get(when)
    if current is None or _SOURCE_PRIORITY[source] > _SOURCE_PRIORITY[current]:
        candidates[when] = source


def _rdate_values(master, m_start: dt.datetime) -> list[dt.datetime]:
    values = []
    for prop in _props(master, "RDATE"):
        values.extend(_datetimes(prop, m_start))
    return values


def _exdate_set(master, m_start: dt.datetime) -> set[dt.datetime]:
    out = set()
    for prop in _props(master, "EXDATE"):
        for value in _datetimes(prop, m_start):
            out.add(_to_utc(value))
    return out


def _padded_bounds(
    m_start: dt.datetime,
    duration: dt.timedelta,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> tuple[dt.datetime, dt.datetime]:
    """The expander's generation window, expressed in the master's tz."""
    if m_start.tzinfo is None:  # floating time: interpreted as UTC
        return (
            (window_start - duration).replace(tzinfo=None),
            window_end.replace(tzinfo=None),
        )
    return (
        (window_start - duration).astimezone(m_start.tzinfo),
        window_end.astimezone(m_start.tzinfo),
    )
