from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from core_logic.meet_engine import EVENT_BY_CODE, format_time_ms


@dataclass(frozen=True)
class VerifiedSwimMark:
    result_id: int
    meet_id: int
    meet_name: str
    meet_date: date
    season_id: int
    course: str
    team_id: int
    athlete_id: int
    athlete_name: str
    event_code: str
    state: str
    final_time_ms: Optional[int] = None
    diving_score: Optional[Decimal] = None


@dataclass(frozen=True)
class AthleteEventSummary:
    athlete_id: int
    athlete_name: str
    event_code: str
    event_name: str
    lifetime_best_ms: Optional[int]
    lifetime_best_diving: Optional[Decimal]
    season_best_ms: Optional[int]
    season_best_diving: Optional[Decimal]
    first_season_ms: Optional[int]
    improvement_ms: Optional[int]
    improvement_pct: Optional[float]
    meet_count: int
    source_meet: str
    source_date: date
    verification_status: str


@dataclass(frozen=True)
class QualificationEvaluation:
    athlete_id: int
    athlete_name: str
    event_code: str
    event_name: str
    standard_label: str
    best_display: str
    cutoff_display: str
    status: str
    delta_display: str
    pct_diff: float


def compute_athlete_bests(
    marks: list[VerifiedSwimMark],
    *,
    active_season_id: int,
    course: str = "SCY",
    include_preliminary: bool = False,
) -> list[AthleteEventSummary]:
    allowed_states = {"official", "preliminary"} if include_preliminary else {"official"}
    filtered = [
        m
        for m in marks
        if m.course == course
        and m.state in allowed_states
        and (m.final_time_ms is not None or m.diving_score is not None)
    ]

    grouped: dict[tuple[int, str], list[VerifiedSwimMark]] = {}
    for m in filtered:
        grouped.setdefault((m.athlete_id, m.event_code), []).append(m)

    summaries: list[AthleteEventSummary] = []
    for (athlete_id, event_code), items in grouped.items():
        spec = EVENT_BY_CODE.get(event_code)
        is_diving = spec.is_diving if spec else False
        ev_name = spec.name if spec else event_code
        items_by_date = sorted(items, key=lambda x: (x.meet_date, x.result_id))
        season_items = [x for x in items_by_date if x.season_id == active_season_id]

        if is_diving:
            valid_all = [x for x in items_by_date if x.diving_score is not None]
            if not valid_all:
                continue
            best_all = max(valid_all, key=lambda x: x.diving_score or Decimal("0"))
            valid_season = [x for x in season_items if x.diving_score is not None]
            best_season = max(valid_season, key=lambda x: x.diving_score or Decimal("0")) if valid_season else None

            summaries.append(
                AthleteEventSummary(
                    athlete_id=athlete_id,
                    athlete_name=best_all.athlete_name,
                    event_code=event_code,
                    event_name=ev_name,
                    lifetime_best_ms=None,
                    lifetime_best_diving=best_all.diving_score,
                    season_best_ms=None,
                    season_best_diving=best_season.diving_score if best_season else None,
                    first_season_ms=None,
                    improvement_ms=None,
                    improvement_pct=None,
                    meet_count=len(valid_all),
                    source_meet=best_all.meet_name,
                    source_date=best_all.meet_date,
                    verification_status=best_all.state.upper(),
                )
            )
        else:
            valid_all = [x for x in items_by_date if x.final_time_ms and x.final_time_ms > 0]
            if not valid_all:
                continue
            best_all = min(valid_all, key=lambda x: x.final_time_ms or 999_999_999)
            valid_season = [x for x in season_items if x.final_time_ms and x.final_time_ms > 0]
            best_season = min(valid_season, key=lambda x: x.final_time_ms or 999_999_999) if valid_season else None
            first_season_ms = valid_season[0].final_time_ms if valid_season else None

            imp_ms: Optional[int] = None
            imp_pct: Optional[float] = None
            if first_season_ms and best_season and best_season.final_time_ms:
                imp_ms = first_season_ms - best_season.final_time_ms
                imp_pct = round((imp_ms / first_season_ms) * 100.0, 2)

            summaries.append(
                AthleteEventSummary(
                    athlete_id=athlete_id,
                    athlete_name=best_all.athlete_name,
                    event_code=event_code,
                    event_name=ev_name,
                    lifetime_best_ms=best_all.final_time_ms,
                    lifetime_best_diving=None,
                    season_best_ms=best_season.final_time_ms if best_season else None,
                    season_best_diving=None,
                    first_season_ms=first_season_ms,
                    improvement_ms=imp_ms,
                    improvement_pct=imp_pct,
                    meet_count=len(valid_all),
                    source_meet=best_all.meet_name,
                    source_date=best_all.meet_date,
                    verification_status=best_all.state.upper(),
                )
            )

    summaries.sort(key=lambda s: (s.athlete_name, s.event_code))
    return summaries


def evaluate_qualifying_standards(
    summaries: list[AthleteEventSummary],
    cutoffs: list[dict],
) -> list[QualificationEvaluation]:
    cutoff_by_event: dict[str, list[dict]] = {}
    for c in cutoffs:
        cutoff_by_event.setdefault(c["event_code"], []).append(c)

    evaluations: list[QualificationEvaluation] = []
    for s in summaries:
        for c in cutoff_by_event.get(s.event_code, []):
            if c.get("cutoff_diving_score") is not None and s.season_best_diving is not None:
                target = Decimal(str(c["cutoff_diving_score"]))
                diff = s.season_best_diving - target
                pct = float(((s.season_best_diving - target) / target) * Decimal("100"))
                status = "QUALIFIED" if diff >= 0 else ("CLOSE (<2%)" if pct >= -2.0 else "IN_PROGRESS")
                evaluations.append(
                    QualificationEvaluation(
                        athlete_id=s.athlete_id,
                        athlete_name=s.athlete_name,
                        event_code=s.event_code,
                        event_name=s.event_name,
                        standard_label=c["level_label"],
                        best_display=f"{s.season_best_diving:.2f}",
                        cutoff_display=f"{target:.2f}",
                        status=status,
                        delta_display=f"{diff:+.2f} pts",
                        pct_diff=round(pct, 2),
                    )
                )
            elif c.get("cutoff_time_ms") is not None and s.season_best_ms is not None:
                target_ms = int(c["cutoff_time_ms"])
                diff_ms = s.season_best_ms - target_ms
                pct = ((s.season_best_ms - target_ms) / target_ms) * 100.0
                status = "QUALIFIED" if diff_ms <= 0 else ("CLOSE (<2%)" if pct <= 2.0 else "IN_PROGRESS")
                sign = "-" if diff_ms < 0 else "+"
                delta_fmt = f"{sign}{format_time_ms(abs(diff_ms))}"
                evaluations.append(
                    QualificationEvaluation(
                        athlete_id=s.athlete_id,
                        athlete_name=s.athlete_name,
                        event_code=s.event_code,
                        event_name=s.event_name,
                        standard_label=c["level_label"],
                        best_display=format_time_ms(s.season_best_ms),
                        cutoff_display=format_time_ms(target_ms),
                        status=status,
                        delta_display=delta_fmt,
                        pct_diff=round(pct, 2),
                    )
                )

    return evaluations
