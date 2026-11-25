from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from core_logic.meet_engine import (
    EVENT_BY_CODE,
    INDIVIDUAL_POINTS_6_LANE,
    MIAA_STANDARD_EVENTS,
    RELAY_POINTS_6_LANE,
    format_time_ms,
)


@dataclass(frozen=True)
class AthleteSeedProfile:
    athlete_id: int
    athlete_name: str
    gender: str
    is_available: bool = True
    best_times_ms: dict[str, int] = field(default_factory=dict)
    best_diving_score: Optional[Decimal] = None


@dataclass(frozen=True)
class OpponentBenchmark:
    event_code: str
    seed_time_ms: Optional[int] = None
    diving_score: Optional[Decimal] = None


@dataclass(frozen=True)
class LineupSlot:
    event_code: str
    event_name: str
    is_relay: bool
    athlete_ids: list[int]
    display_label: str
    seed_display: str
    estimated_place: int
    estimated_points: Decimal
    is_locked_override: bool = False


@dataclass
class LineupProjectionResult:
    slots: list[LineupSlot]
    estimated_boys_points: Decimal
    estimated_girls_points: Decimal
    estimated_total_points: Decimal
    disclaimer: str = (
        "ESTIMATE ONLY: Projected placements and points are calculated from historical/seed marks "
        "and benchmark opponent seeds; actual meet outcomes depend on live race execution."
    )


def _estimate_individual_points(
    event_code: str,
    candidate_mark_ms: Optional[int],
    candidate_diving: Optional[Decimal],
    all_event_marks: list[tuple[bool, Optional[int], Optional[Decimal]]],
) -> tuple[int, Decimal]:
    spec = EVENT_BY_CODE.get(event_code)
    is_diving = spec.is_diving if spec else False

    if is_diving:
        sorted_marks = sorted(
            all_event_marks,
            key=lambda m: m[2] if m[2] is not None else Decimal("-1"),
            reverse=True,
        )
        target_key = candidate_diving
        place = 1
        for idx, (_, _, div_sc) in enumerate(sorted_marks, start=1):
            if div_sc == target_key:
                place = idx
                break
    else:
        sorted_marks = sorted(
            all_event_marks,
            key=lambda m: m[1] if m[1] is not None else 999_999_999,
        )
        target_key_ms = candidate_mark_ms
        place = 1
        for idx, (_, t_ms, _) in enumerate(sorted_marks, start=1):
            if t_ms == target_key_ms:
                place = idx
                break

    pts = INDIVIDUAL_POINTS_6_LANE[place - 1] if 1 <= place <= len(INDIVIDUAL_POINTS_6_LANE) else Decimal("0.00")
    return place, pts


def build_optimal_dual_lineup(
    athletes: list[AthleteSeedProfile],
    opponent_benchmarks: list[OpponentBenchmark],
    *,
    locked_assignments: Optional[dict[int, list[str]]] = None,
    excluded_events_by_athlete: Optional[dict[int, set[str]]] = None,
) -> LineupProjectionResult:
    locked = locked_assignments or {}
    excluded = excluded_events_by_athlete or {}
    available_athletes = [a for a in athletes if a.is_available]
    athlete_map = {a.athlete_id: a for a in available_athletes}

    opp_by_event: dict[str, list[OpponentBenchmark]] = {}
    for ob in opponent_benchmarks:
        opp_by_event.setdefault(ob.event_code, []).append(ob)

    indiv_counts: dict[int, int] = {a.athlete_id: 0 for a in available_athletes}
    total_counts: dict[int, int] = {a.athlete_id: 0 for a in available_athletes}
    event_entries: dict[str, list[tuple[int, Optional[int], Optional[Decimal], bool]]] = {
        ev.code: [] for ev in MIAA_STANDARD_EVENTS
    }

    for aid, ev_codes in locked.items():
        ath = athlete_map.get(aid)
        if not ath:
            continue
        for code in ev_codes:
            spec = EVENT_BY_CODE.get(code)
            if not spec or spec.is_relay:
                continue
            if indiv_counts[aid] >= 2 or total_counts[aid] >= 4:
                continue
            if len(event_entries[code]) >= 3:
                continue
            mark_ms = ath.best_times_ms.get(code)
            div_pts = ath.best_diving_score if spec.is_diving else None
            event_entries[code].append((aid, mark_ms, div_pts, True))
            indiv_counts[aid] += 1
            total_counts[aid] += 1

    individual_events = [ev for ev in MIAA_STANDARD_EVENTS if not ev.is_relay]
    while True:
        best_gain = Decimal("-1.00")
        best_choice: Optional[tuple[int, str, Optional[int], Optional[Decimal]]] = None

        for ev in individual_events:
            if len(event_entries[ev.code]) >= 3:
                continue
            expected_gender = "M" if ev.gender == "B" else "F"

            for ath in available_athletes:
                if ath.gender != expected_gender:
                    continue
                if indiv_counts[ath.athlete_id] >= 2 or total_counts[ath.athlete_id] >= 4:
                    continue
                if ev.code in excluded.get(ath.athlete_id, set()):
                    continue
                if any(existing_aid == ath.athlete_id for existing_aid, _, _, _ in event_entries[ev.code]):
                    continue

                if ev.is_diving:
                    if ath.best_diving_score is None:
                        continue
                    cand_ms, cand_div = None, ath.best_diving_score
                else:
                    cand_ms = ath.best_times_ms.get(ev.code)
                    if cand_ms is None:
                        continue
                    cand_div = None

                combined = [
                    (True, m_ms, d_sc) for _, m_ms, d_sc, _ in event_entries[ev.code]
                ] + [(True, cand_ms, cand_div)]
                for ob in opp_by_event.get(ev.code, []):
                    combined.append((False, ob.seed_time_ms, ob.diving_score))

                _, est_pts = _estimate_individual_points(ev.code, cand_ms, cand_div, combined)
                if est_pts > best_gain:
                    best_gain = est_pts
                    best_choice = (ath.athlete_id, ev.code, cand_ms, cand_div)

        if best_choice is None or best_gain <= Decimal("0.00"):
            break

        chosen_aid, chosen_code, c_ms, c_div = best_choice
        event_entries[chosen_code].append((chosen_aid, c_ms, c_div, False))
        indiv_counts[chosen_aid] += 1
        total_counts[chosen_aid] += 1

    relay_stroke_map = {
        "G_200_MR": ["G_100_BACK", "G_100_BREAST", "G_100_FLY", "G_100_FREE"],
        "B_200_MR": ["B_100_BACK", "B_100_BREAST", "B_100_FLY", "B_100_FREE"],
        "G_200_FR": ["G_50_FREE", "G_50_FREE", "G_50_FREE", "G_50_FREE"],
        "B_200_FR": ["B_50_FREE", "B_50_FREE", "B_50_FREE", "B_50_FREE"],
        "G_400_FR": ["G_100_FREE", "G_100_FREE", "G_100_FREE", "G_100_FREE"],
        "B_400_FR": ["B_100_FREE", "B_100_FREE", "B_100_FREE", "B_100_FREE"],
    }

    slots: list[LineupSlot] = []
    boys_pts = Decimal("0.00")
    girls_pts = Decimal("0.00")

    for ev in individual_events:
        entries = event_entries[ev.code]
        if not entries:
            continue
        combined_all = [(True, m_ms, d_sc) for _, m_ms, d_sc, _ in entries] + [
            (False, ob.seed_time_ms, ob.diving_score) for ob in opp_by_event.get(ev.code, [])
        ]
        for aid, m_ms, d_sc, is_lock in entries:
            ath = athlete_map[aid]
            place, pts = _estimate_individual_points(ev.code, m_ms, d_sc, combined_all)
            seed_disp = f"{d_sc:.2f}" if ev.is_diving and d_sc is not None else format_time_ms(m_ms)
            slots.append(
                LineupSlot(
                    event_code=ev.code,
                    event_name=ev.name,
                    is_relay=False,
                    athlete_ids=[aid],
                    display_label=ath.athlete_name,
                    seed_display=seed_disp,
                    estimated_place=place,
                    estimated_points=pts,
                    is_locked_override=is_lock,
                )
            )
            if ev.gender == "B":
                boys_pts += pts
            else:
                girls_pts += pts

    for ev in [e for e in MIAA_STANDARD_EVENTS if e.is_relay]:
        expected_gender = "M" if ev.gender == "B" else "F"
        leg_events = relay_stroke_map.get(ev.code, [])
        used_in_this_event: set[int] = set()

        for relay_idx, relay_label in enumerate(["A Relay", "B Relay"]):
            picked_ids: list[int] = []
            est_relay_ms = 0
            for leg_ev in leg_events:
                candidates = [
                    a
                    for a in available_athletes
                    if a.gender == expected_gender
                    and a.athlete_id not in used_in_this_event
                    and total_counts[a.athlete_id] < 4
                    and ev.code not in excluded.get(a.athlete_id, set())
                ]
                candidates.sort(key=lambda a: a.best_times_ms.get(leg_ev, 999_999_999))
                if not candidates:
                    break
                chosen = candidates[0]
                picked_ids.append(chosen.athlete_id)
                used_in_this_event.add(chosen.athlete_id)
                base_ms = chosen.best_times_ms.get(leg_ev, 30_000)
                est_relay_ms += (base_ms // 2) if ("200_MR" in ev.code) else base_ms

            if len(picked_ids) == 4:
                for aid in picked_ids:
                    total_counts[aid] += 1
                names = ", ".join(athlete_map[aid].athlete_name for aid in picked_ids)
                place = relay_idx + 1
                pts = RELAY_POINTS_6_LANE[place - 1] if place <= len(RELAY_POINTS_6_LANE) else Decimal("0.00")
                slots.append(
                    LineupSlot(
                        event_code=ev.code,
                        event_name=ev.name,
                        is_relay=True,
                        athlete_ids=picked_ids,
                        display_label=f"{relay_label}: {names}",
                        seed_display=format_time_ms(est_relay_ms),
                        estimated_place=place,
                        estimated_points=pts,
                    )
                )
                if ev.gender == "B":
                    boys_pts += pts
                else:
                    girls_pts += pts

    return LineupProjectionResult(
        slots=slots,
        estimated_boys_points=boys_pts,
        estimated_girls_points=girls_pts,
        estimated_total_points=boys_pts + girls_pts,
    )
