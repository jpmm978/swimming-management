from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
import streamlit as st

from core_logic.league_engine import (
    DualMeetOutcome,
    build_league_top_times,
    compute_league_standings,
    detect_schedule_conflicts,
)
from core_logic.meet_engine import EVENT_BY_CODE, MIAA_STANDARD_EVENTS
from db.repository import ADMIN_ROLES, SwimRepository
from db.schema import (
    Athlete,
    Entry,
    Facility,
    League,
    LeagueMembership,
    LeagueScheduleItem,
    Meet,
    OfficialResult,
    Organization,
    ResultState,
    RoleCode,
    Season,
    Team,
    User,
)


def render_league_center_view(
    repo: SwimRepository,
    *,
    active_user: User | None,
    active_roles: set[RoleCode],
    selected_org: Organization | None,
    selected_season: Season | None,
) -> None:
    st.markdown("## League Center")
    if not (selected_org and selected_season):
        st.warning("Select an Organization and Season in the top context bar.")
        return

    can_admin_league = bool(active_roles & ADMIN_ROLES)

    with repo.session() as session:
        leagues = list(
            session.scalars(
                select(League).where(League.governing_org_id == selected_org.id, League.is_active.is_(True))
            ).all()
        )
        all_teams = list(session.scalars(select(Team).where(Team.is_active.is_(True)).order_by(Team.code)).all())
        all_facilities = list(session.scalars(select(Facility).order_by(Facility.name)).all())

    team_map = {t.id: t for t in all_teams}
    fac_map = {f.id: f for f in all_facilities}

    if not leagues:
        st.info("No leagues configured for this organization yet.")
        if can_admin_league and active_user:
            with st.form("create_league_form"):
                l_name = st.text_input("League / Conference Name", placeholder="e.g., Mid-Wach League")
                div_struct = st.text_input("Division Structure", value="Division 1 / Central-West")
                if st.form_submit_button("Create League", type="primary") and l_name.strip():
                    with repo.session() as session:
                        session.add(
                            League(
                                governing_org_id=selected_org.id,
                                name=l_name.strip(),
                                division_structure=div_struct.strip() or None,
                                is_active=True,
                            )
                        )
                        session.commit()
                    st.rerun()
        return

    selected_league = st.selectbox(
        "Active League",
        options=leagues,
        format_func=lambda lg: f"{lg.name} ({lg.division_structure or 'League'})",
    )

    with repo.session() as session:
        memberships = list(
            session.scalars(
                select(LeagueMembership).where(
                    LeagueMembership.league_id == selected_league.id,
                    LeagueMembership.season_id == selected_season.id,
                )
            ).all()
        )
        sched_items = list(
            session.scalars(
                select(LeagueScheduleItem)
                .where(
                    LeagueScheduleItem.league_id == selected_league.id,
                    LeagueScheduleItem.season_id == selected_season.id,
                )
                .order_by(LeagueScheduleItem.scheduled_date)
            ).all()
        )
        member_team_ids = {m.team_id for m in memberships}
        official_rows = session.execute(
            select(OfficialResult, Entry, Meet, Athlete)
            .join(Entry, Entry.id == OfficialResult.entry_id)
            .join(Meet, Meet.id == OfficialResult.meet_id)
            .outerjoin(Athlete, Athlete.id == Entry.athlete_id)
            .where(
                Meet.season_id == selected_season.id,
                OfficialResult.state == ResultState.OFFICIAL,
            )
        ).all()

    member_dicts = [
        {
            "team_id": m.team_id,
            "team_code": team_map[m.team_id].code if m.team_id in team_map else f"T{m.team_id}",
            "team_name": team_map[m.team_id].name if m.team_id in team_map else f"Team #{m.team_id}",
            "conference": m.conference_label or "General",
        }
        for m in memberships
    ]

    scores_by_meet_gender: dict[tuple[int, str], dict[int, Decimal]] = {}
    verified_indiv_marks: list[dict] = []

    for res, entry, meet, athlete in official_rows:
        if entry.team_id not in member_team_ids:
            continue
        spec = EVENT_BY_CODE.get(res.event_code)
        gender = spec.gender if spec else "X"
        if gender in {"B", "G"}:
            bucket = scores_by_meet_gender.setdefault((meet.id, gender), {})
            bucket[entry.team_id] = bucket.get(entry.team_id, Decimal("0.00")) + Decimal(str(res.points_awarded))

        if not entry.is_relay and athlete is not None:
            verified_indiv_marks.append(
                {
                    "athlete_id": athlete.id,
                    "athlete_name": f"{athlete.first_name} {athlete.last_name}",
                    "team_code": team_map[entry.team_id].code if entry.team_id in team_map else "TEAM",
                    "event_code": res.event_code,
                    "final_time_ms": res.final_time_ms,
                    "diving_score": res.diving_score,
                    "meet_name": meet.name,
                    "meet_date": meet.meet_date,
                }
            )

    outcomes = [
        DualMeetOutcome(meet_id=mid, gender=g, team_scores=t_scores)
        for (mid, g), t_scores in scores_by_meet_gender.items()
    ]

    tab_standings, tab_schedule, tab_top, tab_members = st.tabs(
        [
            "Standings & Win/Loss",
            "League Schedule & Conflicts",
            "League Top Times",
            "Member Teams",
        ]
    )

    with tab_standings:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Boys Standings")
            b_rows = compute_league_standings(member_dicts, outcomes, gender="B")
            if b_rows:
                st.dataframe(
                    [
                        {
                            "Team": f"{r.team_code} — {r.team_name}",
                            "W": r.wins,
                            "L": r.losses,
                            "T": r.ties,
                            "Win %": f"{r.win_pct:.3f}",
                            "PF": f"{r.points_for:.1f}",
                            "PA": f"{r.points_against:.1f}",
                        }
                        for r in b_rows
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("Add member teams to calculate standings.")

        with c2:
            st.subheader("Girls Standings")
            g_rows = compute_league_standings(member_dicts, outcomes, gender="G")
            if g_rows:
                st.dataframe(
                    [
                        {
                            "Team": f"{r.team_code} — {r.team_name}",
                            "W": r.wins,
                            "L": r.losses,
                            "T": r.ties,
                            "Win %": f"{r.win_pct:.3f}",
                            "PF": f"{r.points_for:.1f}",
                            "PA": f"{r.points_against:.1f}",
                        }
                        for r in g_rows
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("Add member teams to calculate standings.")

    with tab_schedule:
        sched_dicts = [
            {
                "home_team_id": s.home_team_id,
                "away_team_id": s.away_team_id,
                "facility_id": s.facility_id,
                "scheduled_date": s.scheduled_date,
                "status": s.status,
            }
            for s in sched_items
        ]
        conflicts = detect_schedule_conflicts(
            sched_dicts,
            {tid: t.code for tid, t in team_map.items()},
            {fid: f.name for fid, f in fac_map.items()},
        )
        if conflicts:
            st.error(f"Detected {len(conflicts)} schedule conflict(s):")
            for c in conflicts:
                st.write(f"- **{c.conflict_type}**: {c.description}")

        if sched_items:
            st.dataframe(
                [
                    {
                        "Date": s.scheduled_date.isoformat(),
                        "Home": team_map[s.home_team_id].code if s.home_team_id in team_map else str(s.home_team_id),
                        "Away": team_map[s.away_team_id].code if s.away_team_id in team_map else str(s.away_team_id),
                        "Facility": fac_map[s.facility_id].name if (s.facility_id and s.facility_id in fac_map) else "TBD",
                        "Status": s.status,
                    }
                    for s in sched_items
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No league dual meets scheduled yet.")

        if can_admin_league and active_user and len(member_dicts) >= 2:
            with st.expander("Schedule League Dual Meet", expanded=False):
                with st.form("add_league_schedule_form"):
                    member_options = [m["team_id"] for m in member_dicts]
                    c1, c2, c3, c4 = st.columns(4)
                    home_id = c1.selectbox(
                        "Home Team",
                        options=member_options,
                        format_func=lambda tid: team_map[tid].code if tid in team_map else str(tid),
                    )
                    away_id = c2.selectbox(
                        "Away Team",
                        options=member_options,
                        index=1 if len(member_options) > 1 else 0,
                        format_func=lambda tid: team_map[tid].code if tid in team_map else str(tid),
                    )
                    fac_choice = c3.selectbox(
                        "Facility",
                        options=[None] + all_facilities,
                        format_func=lambda f: "TBD" if f is None else f.name,
                    )
                    s_date = c4.date_input("Date", value=date.today())
                    if st.form_submit_button("Add to League Schedule", type="primary"):
                        if home_id == away_id:
                            st.error("Home and Away teams must be different.")
                        else:
                            with repo.session() as session:
                                session.add(
                                    LeagueScheduleItem(
                                        league_id=selected_league.id,
                                        season_id=selected_season.id,
                                        home_team_id=home_id,
                                        away_team_id=away_id,
                                        facility_id=fac_choice.id if fac_choice else None,
                                        scheduled_date=s_date,
                                        status="scheduled",
                                    )
                                )
                                session.commit()
                            st.rerun()

    with tab_top:
        ev_spec = st.selectbox(
            "Select Event for League Top Times",
            options=[e for e in MIAA_STANDARD_EVENTS if not e.is_relay],
            format_func=lambda e: e.name,
            key="league_top_times_event",
        )
        top_rows = build_league_top_times(verified_indiv_marks, event_code=ev_spec.code)
        if top_rows:
            st.dataframe(top_rows, use_container_width=True, hide_index=True)
        else:
            st.info("No official marks recorded across league teams for this event yet.")

    with tab_members:
        st.dataframe(member_dicts, use_container_width=True, hide_index=True)
        if can_admin_league and active_user and all_teams:
            unassigned = [t for t in all_teams if t.id not in member_team_ids]
            if unassigned:
                with st.form("add_league_member_form"):
                    c1, c2 = st.columns(2)
                    t_add = c1.selectbox(
                        "Add Team to League",
                        options=unassigned,
                        format_func=lambda t: f"{t.name} ({t.code})",
                    )
                    conf = c2.text_input("Conference / Division Label", value="Division 1")
                    if st.form_submit_button("Enroll Team"):
                        with repo.session() as session:
                            session.add(
                                LeagueMembership(
                                    league_id=selected_league.id,
                                    season_id=selected_season.id,
                                    team_id=t_add.id,
                                    conference_label=conf.strip() or None,
                                )
                            )
                            session.commit()
                        st.rerun()
