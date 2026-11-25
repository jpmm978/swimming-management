from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from core_logic.meet_engine import EVENT_BY_CODE, format_time_ms


@dataclass(frozen=True)
class TeamStandingRow:
    team_id: int
    team_code: str
    team_name: str
    conference: str
    wins: int
    losses: int
    ties: int
    win_pct: float
    points_for: Decimal
    points_against: Decimal


@dataclass(frozen=True)
class DualMeetOutcome:
    meet_id: int
    gender: str
    team_scores: dict[int, Decimal]


@dataclass(frozen=True)
class ScheduleConflict:
    conflict_type: str
    scheduled_date: date
    description: str


def compute_league_standings(
    member_teams: list[dict],
    outcomes: list[DualMeetOutcome],
    *,
    gender: str = "B",
) -> list[TeamStandingRow]:
    stats: dict[int, dict] = {
        t["team_id"]: {
            "team_id": t["team_id"],
            "team_code": t["team_code"],
            "team_name": t["team_name"],
            "conference": t.get("conference") or "General",
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "points_for": Decimal("0.00"),
            "points_against": Decimal("0.00"),
        }
        for t in member_teams
    }

    for outcome in outcomes:
        if outcome.gender != gender:
            continue
        participants = [tid for tid in outcome.team_scores if tid in stats]
        if len(participants) != 2:
            continue

        t1, t2 = participants[0], participants[1]
        s1 = outcome.team_scores[t1]
        s2 = outcome.team_scores[t2]

        stats[t1]["points_for"] += s1
        stats[t1]["points_against"] += s2
        stats[t2]["points_for"] += s2
        stats[t2]["points_against"] += s1

        if s1 > s2:
            stats[t1]["wins"] += 1
            stats[t2]["losses"] += 1
        elif s2 > s1:
            stats[t2]["wins"] += 1
            stats[t1]["losses"] += 1
        else:
            stats[t1]["ties"] += 1
            stats[t2]["ties"] += 1

    rows: list[TeamStandingRow] = []
    for item in stats.values():
        total_games = item["wins"] + item["losses"] + item["ties"]
        win_pct = ((item["wins"] + 0.5 * item["ties"]) / total_games) if total_games > 0 else 0.0
        rows.append(
            TeamStandingRow(
                team_id=item["team_id"],
                team_code=item["team_code"],
                team_name=item["team_name"],
                conference=item["conference"],
                wins=item["wins"],
                losses=item["losses"],
                ties=item["ties"],
                win_pct=round(win_pct, 3),
                points_for=item["points_for"],
                points_against=item["points_against"],
            )
        )

    rows.sort(key=lambda r: (r.win_pct, r.wins, r.points_for), reverse=True)
    return rows


def detect_schedule_conflicts(
    schedule_items: list[dict],
    team_code_map: dict[int, str],
    facility_name_map: dict[int, str],
) -> list[ScheduleConflict]:
    conflicts: list[ScheduleConflict] = []
    team_dates: dict[tuple[int, date], int] = {}
    facility_dates: dict[tuple[int, date], int] = {}

    for item in schedule_items:
        if item.get("status") == "postponed":
            continue
        d = item["scheduled_date"]
        for tid in (item["home_team_id"], item["away_team_id"]):
            key = (tid, d)
            team_dates[key] = team_dates.get(key, 0) + 1
            if team_dates[key] == 2:
                code = team_code_map.get(tid, f"Team #{tid}")
                conflicts.append(
                    ScheduleConflict(
                        conflict_type="TEAM_DOUBLE_BOOKED",
                        scheduled_date=d,
                        description=f"{code} is scheduled for multiple meets on {d.isoformat()}.",
                    )
                )

        fac_id = item.get("facility_id")
        if fac_id is not None:
            fkey = (fac_id, d)
            facility_dates[fkey] = facility_dates.get(fkey, 0) + 1
            if facility_dates[fkey] == 2:
                fname = facility_name_map.get(fac_id, f"Facility #{fac_id}")
                conflicts.append(
                    ScheduleConflict(
                        conflict_type="FACILITY_DOUBLE_BOOKED",
                        scheduled_date=d,
                        description=f"Facility '{fname}' has overlapping meets scheduled on {d.isoformat()}.",
                    )
                )

    return conflicts


def build_league_top_times(
    verified_rows: list[dict],
    *,
    event_code: str,
    limit: int = 15,
) -> list[dict]:
    spec = EVENT_BY_CODE.get(event_code)
    is_diving = spec.is_diving if spec else False

    best_by_athlete: dict[int, dict] = {}
    for row in verified_rows:
        if row["event_code"] != event_code:
            continue
        aid = row["athlete_id"]
        if is_diving:
            score = row.get("diving_score")
            if score is None:
                continue
            cur = best_by_athlete.get(aid)
            if cur is None or Decimal(str(score)) > Decimal(str(cur["diving_score"])):
                best_by_athlete[aid] = row
        else:
            t_ms = row.get("final_time_ms")
            if not t_ms or t_ms <= 0:
                continue
            cur = best_by_athlete.get(aid)
            if cur is None or t_ms < cur["final_time_ms"]:
                best_by_athlete[aid] = row

    ranked = list(best_by_athlete.values())
    if is_diving:
        ranked.sort(key=lambda r: Decimal(str(r["diving_score"])), reverse=True)
    else:
        ranked.sort(key=lambda r: r["final_time_ms"])

    output: list[dict] = []
    for idx, r in enumerate(ranked[:limit], start=1):
        mark = (
            f"{Decimal(str(r['diving_score'])):.2f}"
            if is_diving
            else format_time_ms(r["final_time_ms"])
        )
        output.append(
            {
                "Rank": idx,
                "Athlete": r["athlete_name"],
                "Team": r["team_code"],
                "Mark": mark,
                "Meet": r["meet_name"],
                "Date": r["meet_date"].isoformat() if hasattr(r["meet_date"], "isoformat") else str(r["meet_date"]),
            }
        )
    return output
