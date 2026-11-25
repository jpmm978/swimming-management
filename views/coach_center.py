from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
import streamlit as st

from core_logic.lineup_optimizer import (
    AthleteSeedProfile,
    build_optimal_dual_lineup,
)
from core_logic.meet_engine import parse_time_to_ms
from core_logic.performance_engine import compute_athlete_bests
from db.repository import COACH_ROLES, SwimRepository
from db.schema import (
    AthleteMeetAvailability,
    Entry,
    LineupScenario,
    Meet,
    Organization,
    PracticePlan,
    Season,
    Team,
    User,
)
from views.performance_center import _load_team_marks


def render_coach_center_view(
    repo: SwimRepository,
    *,
    active_user: User | None,
    selected_org: Organization | None,
    selected_team: Team | None,
    selected_season: Season | None,
    selected_meet: Meet | None,
) -> None:
    st.markdown("## Coach Center")
    if not (selected_org and selected_team and selected_season):
        st.warning("Select an Organization, Team, and Season in the top context bar.")
        return

    tab_practice, tab_avail, tab_lineup = st.tabs(
        [
            "Practice Plans & Yardage",
            "Meet Availability",
            "Lineup Optimizer & Estimated Projections",
        ]
    )

    with tab_practice:
        with repo.session() as session:
            plans = list(
                session.scalars(
                    select(PracticePlan)
                    .where(
                        PracticePlan.team_id == selected_team.id,
                        PracticePlan.season_id == selected_season.id,
                    )
                    .order_by(PracticePlan.practice_date.desc())
                ).all()
            )

        total_season_yards = sum(p.total_yards for p in plans)
        c1, c2 = st.columns(2)
        c1.metric("Logged Practices", len(plans))
        c2.metric("Season Total Yardage", f"{total_season_yards:,} yds")

        if active_user:
            with st.expander("Create Practice Workout", expanded=len(plans) == 0):
                with st.form("create_practice_form"):
                    p1, p2, p3 = st.columns(3)
                    p_date = p1.date_input("Practice Date", value=date.today())
                    title = p2.text_input("Workout Focus", placeholder="e.g., Threshold Free & Turns")
                    group = p3.text_input("Training Group", value="Varsity")
                    yards = st.number_input("Total Distance (Yards)", min_value=0, max_value=15000, value=4200, step=100)
                    warmup = st.text_area("Warm-Up", height=68)
                    main_set = st.text_area("Main Set", height=100)
                    cooldown = st.text_area("Cool-Down", height=68)
                    if st.form_submit_button("Save Practice Plan", type="primary") and title.strip():
                        with repo.session() as session:
                            repo.require_roles(
                                session,
                                active_user.id,
                                COACH_ROLES,
                                org_id=selected_org.id,
                                team_id=selected_team.id,
                                action_label="create practice plans",
                            )
                            session.add(
                                PracticePlan(
                                    team_id=selected_team.id,
                                    season_id=selected_season.id,
                                    practice_date=p_date,
                                    title=title.strip(),
                                    training_group=group.strip() or "Varsity",
                                    total_yards=int(yards),
                                    warmup_text=warmup.strip() or None,
                                    main_set_text=main_set.strip() or None,
                                    cooldown_text=cooldown.strip() or None,
                                    created_by_user_id=active_user.id,
                                )
                            )
                            session.commit()
                        st.success("Practice plan saved.")
                        st.rerun()

        for p in plans[:10]:
            with st.expander(f"{p.practice_date} · {p.title} ({p.training_group} — {p.total_yards:,} yds)"):
                if p.warmup_text:
                    st.markdown(f"**Warm-Up:** {p.warmup_text}")
                if p.main_set_text:
                    st.markdown(f"**Main Set:** {p.main_set_text}")
                if p.cooldown_text:
                    st.markdown(f"**Cool-Down:** {p.cooldown_text}")

    with tab_avail:
        if not selected_meet:
            st.info("Select an Active Meet in the top context bar to manage athlete availability.")
        else:
            roster = repo.list_active_roster(selected_team.id, selected_season.id)
            with repo.session() as session:
                avail_map = {
                    row.athlete_id: row.is_available
                    for row in session.scalars(
                        select(AthleteMeetAvailability).where(AthleteMeetAvailability.meet_id == selected_meet.id)
                    ).all()
                }

            if not roster:
                st.info("No active athletes on the roster yet.")
            else:
                with st.form("meet_availability_form"):
                    updated_avail: dict[int, bool] = {}
                    for r in roster:
                        aid = r["athlete_id"]
                        label = f"{r['last_name']}, {r['first_name']} ({r['gender'] or 'Unset'})"
                        updated_avail[aid] = st.checkbox(
                            label,
                            value=avail_map.get(aid, True),
                            key=f"avail_{selected_meet.id}_{aid}",
                        )
                    if active_user and st.form_submit_button("Save Meet Availability", type="primary"):
                        with repo.session() as session:
                            for aid, is_avail in updated_avail.items():
                                existing = session.scalar(
                                    select(AthleteMeetAvailability).where(
                                        AthleteMeetAvailability.meet_id == selected_meet.id,
                                        AthleteMeetAvailability.athlete_id == aid,
                                    )
                                )
                                if existing is None:
                                    session.add(
                                        AthleteMeetAvailability(
                                            meet_id=selected_meet.id,
                                            athlete_id=aid,
                                            is_available=is_avail,
                                        )
                                    )
                                else:
                                    existing.is_available = is_avail
                            session.commit()
                        st.success("Athlete availability updated.")
                        st.rerun()

    with tab_lineup:
        if not selected_meet:
            st.info("Select an Active Meet in the top context bar to build and simulate a lineup.")
            return

        roster = repo.list_active_roster(selected_team.id, selected_season.id)
        marks = _load_team_marks(repo, selected_team.id)
        summaries = compute_athlete_bests(
            marks,
            active_season_id=selected_season.id,
            course=selected_meet.course.value,
            include_preliminary=True,
        )

        with repo.session() as session:
            avail_map = {
                row.athlete_id: row.is_available
                for row in session.scalars(
                    select(AthleteMeetAvailability).where(AthleteMeetAvailability.meet_id == selected_meet.id)
                ).all()
            }

        best_times_by_ath: dict[int, dict[str, int]] = {}
        best_div_by_ath: dict[int, Decimal] = {}
        for s in summaries:
            if s.season_best_ms is not None:
                best_times_by_ath.setdefault(s.athlete_id, {})[s.event_code] = s.season_best_ms
            elif s.lifetime_best_ms is not None:
                best_times_by_ath.setdefault(s.athlete_id, {})[s.event_code] = s.lifetime_best_ms
            if s.season_best_diving is not None:
                best_div_by_ath[s.athlete_id] = s.season_best_diving

        profiles = [
            AthleteSeedProfile(
                athlete_id=r["athlete_id"],
                athlete_name=f"{r['first_name']} {r['last_name']}",
                gender=r["gender"] or "M",
                is_available=avail_map.get(r["athlete_id"], True),
                best_times_ms=best_times_by_ath.get(r["athlete_id"], {}),
                best_diving_score=best_div_by_ath.get(r["athlete_id"]),
            )
            for r in roster
        ]

        projection = build_optimal_dual_lineup(profiles, opponent_benchmarks=[])
        st.warning(projection.disclaimer)

        m1, m2, m3 = st.columns(3)
        m1.metric("Estimated Boys Points", f"{projection.estimated_boys_points:.1f}")
        m2.metric("Estimated Girls Points", f"{projection.estimated_girls_points:.1f}")
        m3.metric("Estimated Combined Points", f"{projection.estimated_total_points:.1f}")

        if projection.slots:
            st.dataframe(
                [
                    {
                        "Event": slot.event_name,
                        "Assignment": slot.display_label,
                        "Seed": slot.seed_display,
                        "Est. Place": slot.estimated_place,
                        "Est. Points": f"{slot.estimated_points:.2f}",
                    }
                    for slot in projection.slots
                ],
                use_container_width=True,
                hide_index=True,
            )

            if active_user and not selected_meet.is_locked:
                if st.button("Push Optimized Lineup to Meet Center Entries", type="primary"):
                    with repo.session() as session:
                        repo.require_roles(
                            session,
                            active_user.id,
                            COACH_ROLES,
                            org_id=selected_org.id,
                            team_id=selected_team.id,
                            action_label="publish lineup entries to meet",
                        )
                        for slot in projection.slots:
                            session.add(
                                Entry(
                                    meet_id=selected_meet.id,
                                    team_id=selected_team.id,
                                    athlete_id=None if slot.is_relay else slot.athlete_ids[0],
                                    event_code=slot.event_code,
                                    is_relay=slot.is_relay,
                                    is_exhibition=False,
                                    seed_time_ms=parse_time_to_ms(slot.seed_display),
                                )
                            )
                        session.add(
                            LineupScenario(
                                meet_id=selected_meet.id,
                                team_id=selected_team.id,
                                name=f"Optimized Lineup ({date.today().isoformat()})",
                                assignments_json={
                                    "slots": [
                                        {
                                            "event_code": s.event_code,
                                            "athlete_ids": s.athlete_ids,
                                            "label": s.display_label,
                                        }
                                        for s in projection.slots
                                    ]
                                },
                                estimated_points=projection.estimated_total_points,
                                created_by_user_id=active_user.id,
                            )
                        )
                        session.commit()
                    st.success("Published lineup entries directly into Meet Center.")
        else:
            st.info("Add verified/preliminary athlete times in Performance Center (or Meet Center) so the optimizer can seed events.")
