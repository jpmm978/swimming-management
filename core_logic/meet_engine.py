from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


@dataclass(frozen=True)
class EventSpec:
    event_number: int
    code: str
    name: str
    gender: str
    is_relay: bool
    is_diving: bool = False
    stroke_code: str = "1"


MIAA_STANDARD_EVENTS: list[EventSpec] = [
    EventSpec(1, "G_200_MR", "Girls 200 Yard Medley Relay", "G", True, False, "7"),
    EventSpec(2, "B_200_MR", "Boys 200 Yard Medley Relay", "B", True, False, "7"),
    EventSpec(3, "G_200_FREE", "Girls 200 Yard Freestyle", "G", False, False, "1"),
    EventSpec(4, "B_200_FREE", "Boys 200 Yard Freestyle", "B", False, False, "1"),
    EventSpec(5, "G_200_IM", "Girls 200 Yard IM", "G", False, False, "5"),
    EventSpec(6, "B_200_IM", "Boys 200 Yard IM", "B", False, False, "5"),
    EventSpec(7, "G_50_FREE", "Girls 50 Yard Freestyle", "G", False, False, "1"),
    EventSpec(8, "B_50_FREE", "Boys 50 Yard Freestyle", "B", False, False, "1"),
    EventSpec(9, "G_1M_DIVING", "Girls 1 Meter Diving", "G", False, True, "1"),
    EventSpec(10, "B_1M_DIVING", "Boys 1 Meter Diving", "B", False, True, "1"),
    EventSpec(11, "G_100_FLY", "Girls 100 Yard Butterfly", "G", False, False, "4"),
    EventSpec(12, "B_100_FLY", "Boys 100 Yard Butterfly", "B", False, False, "4"),
    EventSpec(13, "G_100_FREE", "Girls 100 Yard Freestyle", "G", False, False, "1"),
    EventSpec(14, "B_100_FREE", "Boys 100 Yard Freestyle", "B", False, False, "1"),
    EventSpec(15, "G_500_FREE", "Girls 500 Yard Freestyle", "G", False, False, "1"),
    EventSpec(16, "B_500_FREE", "Boys 500 Yard Freestyle", "B", False, False, "1"),
    EventSpec(17, "G_200_FR", "Girls 200 Yard Freestyle Relay", "G", True, False, "6"),
    EventSpec(18, "B_200_FR", "Boys 200 Yard Freestyle Relay", "B", True, False, "6"),
    EventSpec(19, "G_100_BACK", "Girls 100 Yard Backstroke", "G", False, False, "2"),
    EventSpec(20, "B_100_BACK", "Boys 100 Yard Backstroke", "B", False, False, "2"),
    EventSpec(21, "G_100_BREAST", "Girls 100 Yard Breaststroke", "G", False, False, "3"),
    EventSpec(22, "B_100_BREAST", "Boys 100 Yard Breaststroke", "B", False, False, "3"),
    EventSpec(23, "G_400_FR", "Girls 400 Yard Freestyle Relay", "G", True, False, "6"),
    EventSpec(24, "B_400_FR", "Boys 400 Yard Freestyle Relay", "B", True, False, "6"),
]

EVENT_BY_CODE: dict[str, EventSpec] = {ev.code: ev for ev in MIAA_STANDARD_EVENTS}


def format_event_label(event_code: str) -> str:
    spec = EVENT_BY_CODE.get(event_code)
    return f"Event #{spec.event_number} · {spec.name}" if spec else event_code


INDIVIDUAL_POINTS_6_LANE: list[Decimal] = [
    Decimal("6.00"),
    Decimal("4.00"),
    Decimal("3.00"),
    Decimal("2.00"),
    Decimal("1.00"),
]

RELAY_POINTS_6_LANE: list[Decimal] = [
    Decimal("8.00"),
    Decimal("4.00"),
    Decimal("2.00"),
]


@dataclass
class EntryCandidate:
    entry_id: int
    team_id: int
    team_code: str
    event_code: str
    athlete_id: Optional[int] = None
    athlete_name: str = ""
    relay_label: str = ""
    relay_member_ids: list[int] = field(default_factory=list)
    is_relay: bool = False
    is_exhibition: bool = False
    seed_time_ms: Optional[int] = None


@dataclass
class PerformanceInput:
    entry_id: int
    team_id: int
    team_code: str
    event_code: str
    athlete_name: str
    is_relay: bool = False
    is_exhibition: bool = False
    is_diving: bool = False
    status: str = "OK"
    final_time_ms: Optional[int] = None
    diving_score: Optional[Decimal] = None
    jd_priority: Optional[int] = None
    place_override: Optional[int] = None


@dataclass
class ScoredPerformance:
    entry_id: int
    team_id: int
    team_code: str
    event_code: str
    athlete_name: str
    status: str
    is_exhibition: bool
    final_time_ms: Optional[int]
    diving_score: Optional[Decimal]
    place: Optional[int]
    points: Decimal
    is_jd: bool = False
    decision_note: str = ""


def format_time_ms(ms: Optional[int]) -> str:
    if ms is None or ms <= 0:
        return "NT"
    total_hundredths = round(ms / 10)
    minutes, rem_hundredths = divmod(total_hundredths, 6000)
    seconds, hundredths = divmod(rem_hundredths, 100)
    if minutes > 0:
        return f"{minutes}:{seconds:02d}.{hundredths:02d}"
    return f"{seconds}.{hundredths:02d}"


def parse_time_to_ms(raw: str) -> Optional[int]:
    text = raw.strip().upper()
    if not text or text in {"NT", "NS", "DQ", "SCR"}:
        return None
    if ":" not in text and "." not in text and text.isdigit():
        if len(text) <= 2:
            hundredths = int(text)
            return hundredths * 10
        hundredths = int(text[-2:])
        rem = text[:-2]
        if len(rem) > 2:
            minutes = int(rem[:-2])
            seconds = int(rem[-2:])
        else:
            minutes = 0
            seconds = int(rem)
        return int(round((minutes * 60 + seconds) * 1000)) + (hundredths * 10)
    if ":" in text:
        mins_str, secs_str = text.split(":", 1)
        minutes = int(mins_str)
        seconds = float(secs_str)
    else:
        minutes = 0
        seconds = float(text)
    return int(round((minutes * 60 + seconds) * 1000))


def validate_nfhs_entries(entries: list[EntryCandidate], lane_count: int = 6) -> list[str]:
    violations: list[str] = []
    athlete_total: dict[int, int] = {}
    athlete_indiv: dict[int, int] = {}
    athlete_names: dict[int, str] = {}
    team_indiv_scoring_count: dict[tuple[int, str], int] = {}
    team_relay_total_count: dict[tuple[int, str], int] = {}
    team_code_map: dict[int, str] = {}

    max_total_relays = 2 if lane_count <= 4 else 3
    max_scoring_indiv = 2 if lane_count <= 4 else 3

    for e in entries:
        team_code_map[e.team_id] = e.team_code
        spec = EVENT_BY_CODE.get(e.event_code)
        is_relay = spec.is_relay if spec else e.is_relay

        if is_relay:
            r_key = (e.team_id, e.event_code)
            team_relay_total_count[r_key] = team_relay_total_count.get(r_key, 0) + 1
            for aid in e.relay_member_ids:
                athlete_total[aid] = athlete_total.get(aid, 0) + 1
        else:
            if not e.is_exhibition:
                key = (e.team_id, e.event_code)
                team_indiv_scoring_count[key] = team_indiv_scoring_count.get(key, 0) + 1
                if team_indiv_scoring_count[key] > max_scoring_indiv:
                    violations.append(
                        f"Team {e.team_code} exceeds {max_scoring_indiv} scoring entries in "
                        f"{format_event_label(e.event_code)}."
                    )
            if e.athlete_id is not None:
                athlete_names[e.athlete_id] = e.athlete_name or f"Athlete #{e.athlete_id}"
                athlete_total[e.athlete_id] = athlete_total.get(e.athlete_id, 0) + 1
                athlete_indiv[e.athlete_id] = athlete_indiv.get(e.athlete_id, 0) + 1

    for (tid, ev_code), total_relays in team_relay_total_count.items():
        if total_relays > max_total_relays:
            t_code = team_code_map.get(tid, f"Team #{tid}")
            violations.append(
                f"NFHS Rule 3-2: Team {t_code} entered {total_relays} relays in "
                f"{format_event_label(ev_code)} (max {max_total_relays} relay teams per school "
                f"in a {lane_count}-lane pool)."
            )

    for aid, total in athlete_total.items():
        name = athlete_names.get(aid, f"Athlete #{aid}")
        if total > 4:
            violations.append(f"NFHS Rule 3-2: {name} is entered in {total} events (max 4 total).")
        if athlete_indiv.get(aid, 0) > 2:
            violations.append(
                f"NFHS Rule 3-2: {name} is entered in {athlete_indiv[aid]} individual events (max 2)."
            )

    return violations


def get_relay_seeding_notices(entries: list[EntryCandidate]) -> list[str]:
    notices: list[str] = []
    team_relay_counts: dict[tuple[int, str], int] = {}
    team_code_map: dict[int, str] = {}

    for e in entries:
        team_code_map[e.team_id] = e.team_code
        spec = EVENT_BY_CODE.get(e.event_code)
        is_relay = spec.is_relay if spec else e.is_relay
        if is_relay and not e.is_exhibition:
            key = (e.team_id, e.event_code)
            team_relay_counts[key] = team_relay_counts.get(key, 0) + 1

    for (tid, ev_code), count in team_relay_counts.items():
        if count > 2:
            t_code = team_code_map.get(tid, f"Team #{tid}")
            notices.append(
                f"Team {t_code} has {count} relays entered in {format_event_label(ev_code)}: "
                "all 3 entries are seeded, and only the fastest 2 non-DQ relays will score points."
            )

    return notices


def assign_center_out_lanes(
    entries: list[EntryCandidate],
    lane_count: int = 6,
) -> list[dict]:
    lane_order_map = {
        4: [2, 3, 1, 4],
        6: [3, 4, 2, 5, 1, 6],
        8: [4, 5, 3, 6, 2, 7, 1, 8],
    }
    lane_order = lane_order_map.get(lane_count, list(range(1, lane_count + 1)))

    sorted_fast = sorted(
        entries,
        key=lambda e: (e.seed_time_ms is None, e.seed_time_ms if e.seed_time_ms is not None else 999_999_999),
    )
    if not sorted_fast:
        return []

    heats_fast_first: list[list[EntryCandidate]] = [
        sorted_fast[i : i + lane_count] for i in range(0, len(sorted_fast), lane_count)
    ]
    heats_ordered = list(reversed(heats_fast_first))

    assignments: list[dict] = []
    for heat_idx, heat_entries in enumerate(heats_ordered, start=1):
        for seed_pos, entry in enumerate(heat_entries):
            lane = lane_order[seed_pos]
            assignments.append(
                {
                    "heat": heat_idx,
                    "lane": lane,
                    "entry_id": entry.entry_id,
                    "team_code": entry.team_code,
                    "athlete_name": entry.athlete_name or entry.relay_label,
                    "seed_time": format_time_ms(entry.seed_time_ms),
                    "is_exhibition": entry.is_exhibition,
                }
            )
    return assignments


def assign_dual_meet_odd_even_lanes(
    entries: list[EntryCandidate],
    *,
    odd_team_id: int,
    even_team_id: Optional[int] = None,
    lane_count: int = 6,
) -> list[dict]:
    odd_lane_order_map = {
        4: [3, 1],
        6: [3, 1, 5],
        8: [5, 3, 1, 7],
    }
    even_lane_order_map = {
        4: [2, 4],
        6: [4, 2, 6],
        8: [4, 6, 2, 8],
    }
    odd_lanes = odd_lane_order_map.get(
        lane_count, [l for l in range(1, lane_count + 1) if l % 2 == 1]
    )
    even_lanes = even_lane_order_map.get(
        lane_count, [l for l in range(1, lane_count + 1) if l % 2 == 0]
    )

    if not entries:
        return []

    def _seed_sort_key(e: EntryCandidate) -> tuple[bool, int]:
        return (
            e.seed_time_ms is None,
            e.seed_time_ms if e.seed_time_ms is not None else 999_999_999,
        )

    odd_entries = sorted(
        [e for e in entries if e.team_id == odd_team_id],
        key=_seed_sort_key,
    )
    if even_team_id is not None and even_team_id != odd_team_id:
        even_entries = sorted(
            [e for e in entries if e.team_id == even_team_id],
            key=_seed_sort_key,
        )
        extra_entries = sorted(
            [e for e in entries if e.team_id not in {odd_team_id, even_team_id}],
            key=_seed_sort_key,
        )
    else:
        even_entries = sorted(
            [e for e in entries if e.team_id != odd_team_id],
            key=_seed_sort_key,
        )
        extra_entries = []

    odd_chunks = [
        odd_entries[i : i + len(odd_lanes)] for i in range(0, len(odd_entries), len(odd_lanes))
    ]
    even_chunks = [
        even_entries[i : i + len(even_lanes)] for i in range(0, len(even_entries), len(even_lanes))
    ]
    num_heats = max(len(odd_chunks), len(even_chunks), 1 if extra_entries else 0)

    heats_fast_first: list[list[tuple[int, EntryCandidate]]] = []
    for h_idx in range(num_heats):
        heat_pairs: list[tuple[int, EntryCandidate]] = []
        o_group = odd_chunks[h_idx] if h_idx < len(odd_chunks) else []
        e_group = even_chunks[h_idx] if h_idx < len(even_chunks) else []

        for pos, entry in enumerate(o_group):
            heat_pairs.append((odd_lanes[pos], entry))
        for pos, entry in enumerate(e_group):
            heat_pairs.append((even_lanes[pos], entry))

        heats_fast_first.append(heat_pairs)

    if extra_entries:
        all_lanes_center = [l for pair in zip(odd_lanes, even_lanes) for l in pair]
        for entry in extra_entries:
            placed = False
            for heat_pairs in heats_fast_first:
                used = {l for l, _ in heat_pairs}
                avail = [l for l in all_lanes_center if l not in used]
                if avail:
                    heat_pairs.append((avail[0], entry))
                    placed = True
                    break
            if not placed:
                heats_fast_first.append([(all_lanes_center[0], entry)])

    heats_ordered = list(reversed(heats_fast_first))

    assignments: list[dict] = []
    for heat_idx, heat_pairs in enumerate(heats_ordered, start=1):
        for lane, entry in sorted(heat_pairs, key=lambda item: item[0]):
            assignments.append(
                {
                    "heat": heat_idx,
                    "lane": lane,
                    "entry_id": entry.entry_id,
                    "team_code": entry.team_code,
                    "athlete_name": entry.athlete_name or entry.relay_label,
                    "seed_time": format_time_ms(entry.seed_time_ms),
                    "is_exhibition": entry.is_exhibition,
                }
            )
    return assignments


def score_event_performances(
    performances: list[PerformanceInput],
    *,
    is_relay: bool = False,
    is_diving: bool = False,
) -> list[ScoredPerformance]:
    points_table = RELAY_POINTS_6_LANE if is_relay else INDIVIDUAL_POINTS_6_LANE
    max_scoring_per_team = 2 if is_relay else 3

    valid_contenders: list[PerformanceInput] = []
    non_scoring: list[PerformanceInput] = []

    for p in performances:
        has_mark = (p.diving_score is not None) if is_diving else (p.final_time_ms is not None and p.final_time_ms > 0)
        if p.status == "OK" and has_mark and not p.is_exhibition:
            valid_contenders.append(p)
        else:
            non_scoring.append(p)

    # Sort primarily by mark (ascending time or descending diving score),
    # then by manual place_override if provided, then by jd_priority
    if is_diving:
        valid_contenders.sort(
            key=lambda p: (
                -(p.diving_score or Decimal("0")),
                p.place_override if p.place_override is not None else 999,
                p.jd_priority if p.jd_priority is not None else 999,
            )
        )
    else:
        valid_contenders.sort(
            key=lambda p: (
                p.final_time_ms or 999_999_999,
                p.place_override if p.place_override is not None else 999,
                p.jd_priority if p.jd_priority is not None else 999,
            )
        )

    team_scoring_used: dict[int, int] = {}
    point_eligible_entries: set[int] = set()
    for p in valid_contenders:
        used = team_scoring_used.get(p.team_id, 0)
        if used < max_scoring_per_team:
            team_scoring_used[p.team_id] = used + 1
            point_eligible_entries.add(p.entry_id)

    results: list[ScoredPerformance] = []
    idx = 0
    n = len(valid_contenders)

    while idx < n:
        tie_end = idx + 1
        while tie_end < n:
            if is_diving:
                same_mark = valid_contenders[tie_end].diving_score == valid_contenders[idx].diving_score
            else:
                same_mark = valid_contenders[tie_end].final_time_ms == valid_contenders[idx].final_time_ms
            if not same_mark:
                break
            tie_end += 1

        tie_group = valid_contenders[idx:tie_end]
        tie_size = len(tie_group)
        mark_label = (
            str(tie_group[0].diving_score)
            if is_diving
            else format_time_ms(tie_group[0].final_time_ms)
        )

        # Check if a Judge's Decision (jd_priority) resolves this tied group
        has_jd = tie_size > 1 and any(p.jd_priority is not None for p in tie_group)

        if has_jd:
            tie_group_sorted = sorted(
                tie_group,
                key=lambda p: (
                    p.jd_priority if p.jd_priority is not None else 999,
                    p.place_override if p.place_override is not None else 999,
                ),
            )
            for offset, p in enumerate(tie_group_sorted):
                pos = idx + offset
                computed_place = pos + 1
                final_place = p.place_override if p.place_override is not None else computed_place
                is_eligible = p.entry_id in point_eligible_entries
                pts = (
                    points_table[final_place - 1]
                    if (is_eligible and 1 <= final_place <= len(points_table))
                    else Decimal("0.00")
                )
                note_parts = [
                    f"Judge's Decision (JD): placed #{final_place} on tied mark ({mark_label}); full place points"
                ]
                if p.place_override is not None:
                    note_parts.append(f"Manual place override (#{p.place_override})")
                if not is_eligible and is_relay:
                    note_parts.append("3rd team relay (non-scoring)")
                results.append(
                    ScoredPerformance(
                        entry_id=p.entry_id,
                        team_id=p.team_id,
                        team_code=p.team_code,
                        event_code=p.event_code,
                        athlete_name=p.athlete_name,
                        status=p.status,
                        is_exhibition=False,
                        final_time_ms=p.final_time_ms,
                        diving_score=p.diving_score,
                        place=final_place,
                        points=pts,
                        is_jd=True,
                        decision_note="; ".join(note_parts),
                    )
                )
        else:
            place = idx + 1
            eligible_in_group = [
                valid_contenders[pos]
                for pos in range(idx, tie_end)
                if valid_contenders[pos].entry_id in point_eligible_entries
            ]

            pooled_points = Decimal("0.00")
            for pos in range(idx, tie_end):
                if pos < len(points_table):
                    pooled_points += points_table[pos]

            split_points = (
                (pooled_points / Decimal(len(eligible_in_group))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                if eligible_in_group
                else Decimal("0.00")
            )

            for pos in range(idx, tie_end):
                p = valid_contenders[pos]
                is_eligible = p.entry_id in point_eligible_entries
                final_place = p.place_override if p.place_override is not None else place
                pts = split_points if is_eligible else Decimal("0.00")
                if p.place_override is not None and tie_size == 1:
                    pts = (
                        points_table[final_place - 1]
                        if (is_eligible and 1 <= final_place <= len(points_table))
                        else Decimal("0.00")
                    )

                note_parts: list[str] = []
                if tie_size > 1:
                    note_parts.append(
                        f"Tie for Place {place} ({mark_label}) — points split ({pts:.2f} pts each)"
                    )
                if p.place_override is not None:
                    note_parts.append(f"Manual place override (#{p.place_override})")
                if not is_eligible and is_relay:
                    note_parts.append("3rd team relay (non-scoring)")

                results.append(
                    ScoredPerformance(
                        entry_id=p.entry_id,
                        team_id=p.team_id,
                        team_code=p.team_code,
                        event_code=p.event_code,
                        athlete_name=p.athlete_name,
                        status=p.status,
                        is_exhibition=False,
                        final_time_ms=p.final_time_ms,
                        diving_score=p.diving_score,
                        place=final_place,
                        points=pts,
                        is_jd=False,
                        decision_note="; ".join(note_parts),
                    )
                )
        idx = tie_end

    for p in non_scoring:
        results.append(
            ScoredPerformance(
                entry_id=p.entry_id,
                team_id=p.team_id,
                team_code=p.team_code,
                event_code=p.event_code,
                athlete_name=p.athlete_name,
                status=p.status,
                is_exhibition=p.is_exhibition,
                final_time_ms=p.final_time_ms,
                diving_score=p.diving_score,
                place=None,
                points=Decimal("0.00"),
                is_jd=False,
                decision_note="",
            )
        )

    return results
