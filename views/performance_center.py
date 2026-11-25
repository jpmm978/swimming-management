from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
import streamlit as st

from core_logic.meet_engine import EVENT_BY_CODE, MIAA_STANDARD_EVENTS, format_time_ms, parse_time_to_ms
from core_logic.performance_engine import (
    VerifiedSwimMark,
    compute_athlete_bests,
    evaluate_qualifying_standards,
)
from db.repository import COACH_ROLES, SwimRepository
from db.schema import (
    Athlete,
    CourseType,
    Entry,
    Meet,
    OfficialResult,
    Organization,
    QualifyingStandard,
    QualifyingStandardSet,
    ResultState,
    Season,
    Team,
    User,
    VerifiedTeamRecord,
)


def _load_team_marks(repo: SwimRepository, team_id: int) -> list[VerifiedSwimMark]:
    with repo.session() as session:
        stmt = (
            select(OfficialResult, Entry, Meet, Athlete)
            .join(Entry, Entry.id == OfficialResult.entry_id)
            .join(Meet, Meet.id == OfficialResult.meet_id)
            .join(Athlete, Athlete.id == Entry.athlete_id)
            .where(
                Entry.team_id == team_id,
                Entry.is_relay.is_(False),
                OfficialResult.state.in_([ResultState.OFFICIAL, ResultState.PRELIMINARY]),
            )
            .order_by(Meet.meet_date, OfficialResult.id)
        )
        rows = session.execute(stmt).all()

    return [
        VerifiedSwimMark(
            result_id=res.id,
            meet_id=meet.id,
            meet_name=meet.name,
            meet_date=meet.meet_date,
            season_id=meet.season_id,
            course=meet.course.value,
            team_id=entry.team_id,
            athlete_id=athlete.id,
            athlete_name=f"{athlete.first_name} {athlete.last_name}",
            event_code=res.event_code,
            state=res.state.value,
            final_time_ms=res.final_time_ms,
            diving_score=Decimal(str(res.diving_score)) if res.diving_score is not None else None,
        )
        for res, entry, meet, athlete in rows
    ]


def render_performance_center_view(
    repo: SwimRepository,
    *,
    active_user: User | None,
    selected_org: Organization | None,
    selected_team: Team | None,
    selected_season: Season | None,
) -> None:
    st.markdown("## Performance Center")
    if not (selected_org and selected_team and selected_season):
        st.warning("Select an Organization, Team, and Season in the top context bar.")
        return

    c_course, c_prelim = st.columns([1, 2])
    course = c_course.selectbox("Course", options=[c.value for c in CourseType], index=0)
    include_prelim = c_prelim.checkbox(
        "Include Preliminary (unverified) meet marks alongside Official results",
        value=True,
    )

    marks = _load_team_marks(repo, selected_team.id)
    summaries = compute_athlete_bests(
        marks,
        active_season_id=selected_season.id,
        course=course,
        include_preliminary=include_prelim,
    )

    tab_bests, tab_qual, tab_depth, tab_records = st.tabs(
        [
            "Athlete Bests & Progression",
            "MIAA Qualifying Standards",
            "Team Top Times & Depth",
            "Team Record Book",
        ]
    )

    with tab_bests:
        if not summaries:
            st.info("No individual swim or diving results recorded for this team/course yet.")
        else:
            st.dataframe(
                [
                    {
                        "Athlete": s.athlete_name,
                        "Event": s.event_name,
                        "Season Best": (
                            f"{s.season_best_diving:.2f}"
                            if s.season_best_diving is not None
                            else format_time_ms(s.season_best_ms)
                        ),
                        "Lifetime Best": (
                            f"{s.lifetime_best_diving:.2f}"
                            if s.lifetime_best_diving is not None
                            else format_time_ms(s.lifetime_best_ms)
                        ),
                        "Season Drop": (
                            f"-{format_time_ms(s.improvement_ms)} ({s.improvement_pct:.1f}%)"
                            if (s.improvement_ms and s.improvement_ms > 0 and s.improvement_pct is not None)
                            else "0.00"
                        ),
                        "Source Meet": f"{s.source_meet} ({s.source_date})",
                        "Status": s.verification_status,
                    }
                    for s in summaries
                ],
                use_container_width=True,
                hide_index=True,
            )

    with tab_qual:
        with repo.session() as session:
            std_sets = list(
                session.scalars(
                    select(QualifyingStandardSet).where(
                        QualifyingStandardSet.season_label == selected_season.label
                    )
                ).all()
            )
            cutoffs = []
            if std_sets:
                chosen_set = st.selectbox(
                    "Qualifying Standard Set",
                    options=std_sets,
                    format_func=lambda s: f"{s.governing_body} — {s.name} ({s.season_label})",
                )
                std_rows = session.scalars(
                    select(QualifyingStandard).where(QualifyingStandard.standard_set_id == chosen_set.id)
                ).all()
                cutoffs = [
                    {
                        "event_code": r.event_code,
                        "level_label": r.level_label,
                        "cutoff_time_ms": r.cutoff_time_ms,
                        "cutoff_diving_score": r.cutoff_diving_score,
                    }
                    for r in std_rows
                ]
            else:
                st.info(
                    f"No qualifying standard table loaded for `{selected_season.label}` yet. "
                    "Add verified MIAA Central/West or Division 1 cutoffs below."
                )

        if cutoffs and summaries:
            evals = evaluate_qualifying_standards(summaries, cutoffs)
            st.dataframe(
                [
                    {
                        "Athlete": e.athlete_name,
                        "Event": e.event_name,
                        "Standard": e.standard_label,
                        "Season Best": e.best_display,
                        "Cutoff": e.cutoff_display,
                        "Status": e.status,
                        "Delta": e.delta_display,
                    }
                    for e in evals
                ],
                use_container_width=True,
                hide_index=True,
            )

        if active_user:
            with st.expander("Add / Update Qualifying Cutoff Standard", expanded=False):
                with st.form("add_qual_cutoff_form"):
                    c1, c2, c3 = st.columns(3)
                    set_name = c1.text_input("Standard Set", value="MIAA Central/West & Div 1")
                    level_code = c2.selectbox("Level", options=["SECTIONAL", "STATE_D1"])
                    ev_spec = c3.selectbox(
                        "Event",
                        options=[e for e in MIAA_STANDARD_EVENTS if not e.is_relay],
                        format_func=lambda e: e.name,
                    )
                    cutoff_str = st.text_input("Cutoff Time (e.g. 23.80) or Diving Score (e.g. 210.00)")
                    if st.form_submit_button("Save Standard") and cutoff_str.strip():
                        with repo.session() as session:
                            repo.require_roles(
                                session,
                                active_user.id,
                                COACH_ROLES,
                                org_id=selected_org.id,
                                team_id=selected_team.id,
                                action_label="configure qualifying standards",
                            )
                            qset = session.scalar(
                                select(QualifyingStandardSet).where(
                                    QualifyingStandardSet.governing_body == "MIAA",
                                    QualifyingStandardSet.season_label == selected_season.label,
                                    QualifyingStandardSet.name == set_name.strip(),
                                )
                            )
                            if qset is None:
                                qset = QualifyingStandardSet(
                                    governing_body="MIAA",
                                    season_label=selected_season.label,
                                    name=set_name.strip(),
                                    course=CourseType(course),
                                    is_verified=True,
                                )
                                session.add(qset)
                                session.flush()

                            existing_std = session.scalar(
                                select(QualifyingStandard).where(
                                    QualifyingStandard.standard_set_id == qset.id,
                                    QualifyingStandard.event_code == ev_spec.code,
                                    QualifyingStandard.level_code == level_code,
                                )
                            )
                            label = "MIAA Sectional" if level_code == "SECTIONAL" else "MIAA Div 1 State"
                            t_ms = None if ev_spec.is_diving else parse_time_to_ms(cutoff_str)
                            d_pts = Decimal(cutoff_str) if ev_spec.is_diving else None

                            if existing_std is None:
                                session.add(
                                    QualifyingStandard(
                                        standard_set_id=qset.id,
                                        event_code=ev_spec.code,
                                        level_code=level_code,
                                        level_label=label,
                                        cutoff_time_ms=t_ms,
                                        cutoff_diving_score=d_pts,
                                    )
                                )
                            else:
                                existing_std.cutoff_time_ms = t_ms
                                existing_std.cutoff_diving_score = d_pts
                            session.commit()
                        st.success("Qualifying standard saved.")
                        st.rerun()

    with tab_depth:
        ev_choice = st.selectbox(
            "Select Event for Team Top-Times Depth Chart",
            options=[e for e in MIAA_STANDARD_EVENTS if not e.is_relay],
            format_func=lambda e: e.name,
            key="depth_event_select",
        )
        ev_rows = [s for s in summaries if s.event_code == ev_choice.code]
        if ev_choice.is_diving:
            ev_rows.sort(key=lambda s: s.season_best_diving or Decimal("0"), reverse=True)
        else:
            ev_rows.sort(key=lambda s: s.season_best_ms or 999_999_999)

        if ev_rows:
            st.dataframe(
                [
                    {
                        "Rank": idx,
                        "Athlete": s.athlete_name,
                        "Season Best": (
                            f"{s.season_best_diving:.2f}"
                            if ev_choice.is_diving
                            else format_time_ms(s.season_best_ms)
                        ),
                        "Lifetime Best": (
                            f"{s.lifetime_best_diving:.2f}"
                            if ev_choice.is_diving
                            else format_time_ms(s.lifetime_best_ms)
                        ),
                        "Meet": s.source_meet,
                    }
                    for idx, s in enumerate(ev_rows, start=1)
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No recorded marks for this event yet.")

    with tab_records:
        with repo.session() as session:
            records = list(
                session.scalars(
                    select(VerifiedTeamRecord)
                    .where(
                        VerifiedTeamRecord.team_id == selected_team.id,
                        VerifiedTeamRecord.course == CourseType(course),
                    )
                    .order_by(VerifiedTeamRecord.event_code)
                ).all()
            )
        if records:
            st.dataframe(
                [
                    {
                        "Event": EVENT_BY_CODE[r.event_code].name if r.event_code in EVENT_BY_CODE else r.event_code,
                        "Holder": r.holder_name,
                        "Record": (
                            f"{r.record_diving_score:.2f}"
                            if r.record_diving_score is not None
                            else format_time_ms(r.record_time_ms)
                        ),
                        "Meet": r.meet_name,
                        "Date": r.achieved_date.isoformat(),
                    }
                    for r in records
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No verified team records stored yet.")
