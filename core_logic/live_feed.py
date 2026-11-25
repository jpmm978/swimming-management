from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from core_logic.meet_engine import EVENT_BY_CODE, MIAA_STANDARD_EVENTS, format_time_ms
from db.schema import NameVisibilityMode


@dataclass(frozen=True)
class LiveResultRow:
    event_number: int
    event_code: str
    event_name: str
    gender: str
    place_display: str
    team_code: str
    competitor_display: str
    mark_display: str
    points_display: str
    verification_badge: str


@dataclass(frozen=True)
class LiveMeetSnapshot:
    meet_id: int
    meet_name: str
    meet_date: str
    course: str
    lane_count: int
    meet_status: str
    is_public: bool
    boys_team_scores: dict[str, Decimal]
    girls_team_scores: dict[str, Decimal]
    completed_events: int
    total_scheduled_events: int
    rows: list[LiveResultRow]


def format_public_competitor_name(
    *,
    first_name: Optional[str],
    last_name: Optional[str],
    team_code: str,
    is_relay: bool,
    visibility: NameVisibilityMode,
) -> str:
    if is_relay:
        return f"{team_code} Relay"
    if visibility == NameVisibilityMode.TEAM_ONLY or not first_name:
        return f"{team_code} Swimmer"
    if visibility == NameVisibilityMode.FIRST_LAST_INITIAL:
        initial = f" {last_name[0]}." if last_name else ""
        return f"{first_name}{initial}"
    return f"{first_name} {last_name or ''}".strip()


def build_live_meet_snapshot(
    *,
    meet_id: int,
    meet_name: str,
    meet_date: str,
    course: str,
    lane_count: int,
    meet_status: str,
    is_public: bool,
    show_preliminary: bool,
    name_visibility: NameVisibilityMode,
    raw_items: list[dict],
) -> LiveMeetSnapshot:
    boys_scores: dict[str, Decimal] = {}
    girls_scores: dict[str, Decimal] = {}
    rows: list[LiveResultRow] = []
    events_with_results: set[str] = set()

    for item in raw_items:
        state = item["state"]
        if state == "scratch":
            continue
        if state == "preliminary" and not show_preliminary:
            continue

        ev_code = item["event_code"]
        spec = EVENT_BY_CODE.get(ev_code)
        ev_num = spec.event_number if spec else 999
        ev_name = spec.name if spec else ev_code
        gender = spec.gender if spec else "X"
        is_relay = spec.is_relay if spec else item.get("is_relay", False)
        is_diving = spec.is_diving if spec else False

        events_with_results.add(ev_code)
        pts = Decimal(str(item.get("points_awarded", "0.00")))
        team_code = item["team_code"]

        if gender == "B":
            boys_scores[team_code] = boys_scores.get(team_code, Decimal("0.00")) + pts
        elif gender == "G":
            girls_scores[team_code] = girls_scores.get(team_code, Decimal("0.00")) + pts

        if state == "disqualified":
            mark = "DQ"
            badge = "DQ"
            place_disp = "-"
        else:
            if is_diving and item.get("diving_score") is not None:
                mark = f"{Decimal(str(item['diving_score'])):.2f}"
            else:
                mark = format_time_ms(item.get("final_time_ms"))

            if item.get("is_exhibition") or state == "exhibition":
                badge = "EXHIBITION"
                place_disp = "EXH"
            elif state == "official":
                badge = "OFFICIAL"
                place_disp = str(item["place"]) if item.get("place") else "-"
            else:
                badge = "PRELIMINARY"
                place_disp = str(item["place"]) if item.get("place") else "-"

        comp_name = format_public_competitor_name(
            first_name=item.get("first_name"),
            last_name=item.get("last_name"),
            team_code=team_code,
            is_relay=is_relay,
            visibility=name_visibility,
        )

        rows.append(
            LiveResultRow(
                event_number=ev_num,
                event_code=ev_code,
                event_name=ev_name,
                gender=gender,
                place_display=place_disp,
                team_code=team_code,
                competitor_display=comp_name,
                mark_display=mark,
                points_display=f"{pts:.2f}",
                verification_badge=badge,
            )
        )

    rows.sort(key=lambda r: (r.event_number, r.place_display == "-", r.place_display))
    return LiveMeetSnapshot(
        meet_id=meet_id,
        meet_name=meet_name,
        meet_date=meet_date,
        course=course,
        lane_count=lane_count,
        meet_status=meet_status,
        is_public=is_public,
        boys_team_scores=boys_scores,
        girls_team_scores=girls_scores,
        completed_events=len(events_with_results),
        total_scheduled_events=len(MIAA_STANDARD_EVENTS),
        rows=rows,
    )
