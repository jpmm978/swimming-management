from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
import streamlit as st

from core_logic.deck_sync import DeckOfflineOutbox, save_scored_result_resilient
from core_logic.meet_engine import (
    EVENT_BY_CODE,
    MIAA_STANDARD_EVENTS,
    EntryCandidate,
    PerformanceInput,
    ScoredPerformance,
    assign_center_out_lanes,
    format_time_ms,
    parse_time_to_ms,
    score_event_performances,
    validate_nfhs_entries,
)
from db.repository import SwimRepository
from db.schema import (
    Athlete,
    CourseType,
    Entry,
    Meet,
    MeetStatus,
    OfficialResult,
    Organization,
    ResultState,
    Season,
    Team,
    User,
)
from integrations.meet_exporters import (
    export_hytek_sdif_results,
    export_meet_results_csv,
    render_printable_results_html,
)
from views.deck_sync_bar import render_deck_sync_bar


def render_meet_center_view(
    repo: SwimRepository,
    *,
    outbox: DeckOfflineOutbox | None = None,
    active_user: User | None,
    selected_org: Organization | None,
    selected_season: Season | None,
    selected_team: Team | None = None,
    selected_meet: Meet | None,
) -> None:
    st.markdown("## Meet Center")
    active_outbox = outbox or DeckOfflineOutbox()
    is_online = render_deck_sync_bar(
        repo,
        active_outbox,
        active_user=active_user,
        selected_meet=selected_meet,
    )
    force_offline = st.checkbox(
        "Force Offline Deck Mode (queue all writes to local SQLite outbox)",
        value=not is_online,
        key="force_offline_deck_mode",
    )

    tab_setup, tab_entries, tab_live, tab_reports = st.tabs(
        [
            "1. Meet Setup",
            "2. Entries, NFHS Check & Seeding",
            "3. Pool-Deck Timing, Diving & Scoring",
            "4. Printouts, HY-TEK Export & Sign-Off",
        ]
    )

    with tab_setup:
        if selected_meet:
            st.success(
                f"**Active Meet:** {selected_meet.name} ({selected_meet.meet_date}) · "
                f"{selected_meet.course.value} · {selected_meet.lane_count} lanes · "
                f"Status: `{selected_meet.status.value}`"
            )
        if active_user and selected_org and selected_season:
            with st.form("meet_center_create_meet"):
                meet_name = st.text_input("New Meet Title", placeholder="e.g., GAR vs. Opponent Dual Meet")
                c1, c2, c3 = st.columns(3)
                m_date = c1.date_input("Meet Date", value=date.today())
                course = c2.selectbox("Course", options=[c.value for c in CourseType], index=0)
                lanes = c3.number_input("Lanes", min_value=4, max_value=8, value=6)
                if st.form_submit_button("Create Dual Meet", type="primary") and meet_name.strip():
                    meet = repo.create_meet(
                        actor_user_id=active_user.id,
                        season_id=selected_season.id,
                        host_org_id=selected_org.id,
                        name=meet_name,
                        meet_date=m_date,
                        course=CourseType(course),
                        lane_count=int(lanes),
                    )
                    st.session_state["selected_meet_id"] = meet.id
                    st.rerun()

    if not selected_meet:
        for t in (tab_entries, tab_live, tab_reports):
            with t:
                st.info("Create or select an Active Meet in the top bar first.")
        return

    with repo.session() as session:
        teams = list(session.scalars(select(Team).where(Team.is_active.is_(True))).all())
        team_map = {t.id: t for t in teams}
        athletes = list(session.scalars(select(Athlete).where(Athlete.is_active.is_(True))).all())
        athlete_map = {a.id: a for a in athletes}
        db_entries = list(
            session.scalars(select(Entry).where(Entry.meet_id == selected_meet.id).order_by(Entry.id)).all()
        )
        db_results = {
            r.entry_id: r
            for r in session.scalars(select(OfficialResult).where(OfficialResult.meet_id == selected_meet.id)).all()
        }

    candidates: list[EntryCandidate] = []
    for e in db_entries:
        t = team_map.get(e.team_id)
        a = athlete_map.get(e.athlete_id) if e.athlete_id else None
        spec = EVENT_BY_CODE.get(e.event_code)
        candidates.append(
            EntryCandidate(
                entry_id=e.id,
                team_id=e.team_id,
                team_code=t.code if t else "TEAM",
                event_code=e.event_code,
                athlete_id=e.athlete_id,
                athlete_name=f"{a.first_name} {a.last_name}" if a else f"{t.code if t else 'Team'} Relay",
                is_relay=spec.is_relay if spec else e.is_relay,
                is_exhibition=e.is_exhibition,
                seed_time_ms=e.seed_time_ms,
            )
        )

    with tab_entries:
        if active_user and not selected_meet.is_locked:
            with st.expander("Add Individual or Relay Entry", expanded=len(candidates) == 0):
                with st.form("add_meet_entry_form"):
                    c1, c2, c3 = st.columns(3)
                    entry_team = c1.selectbox(
                        "Team",
                        options=teams,
                        index=teams.index(selected_team) if selected_team in teams else 0,
                        format_func=lambda t: f"{t.name} ({t.code})",
                    )
                    ev_spec = c2.selectbox(
                        "Event",
                        options=MIAA_STANDARD_EVENTS,
                        format_func=lambda ev: f"#{ev.event_number} · {ev.name}",
                    )
                    seed_str = c3.text_input("Seed Time (e.g. 24.15 or 1:02.40)", value="NT")

                    c4, c5 = st.columns([2, 1])
                    athlete_choice = c4.selectbox(
                        "Athlete (leave blank for Relay)",
                        options=[None] + athletes,
                        format_func=lambda a: "Relay Team Entry" if a is None else f"{a.last_name}, {a.first_name}",
                    )
                    is_exh = c5.checkbox("Exhibition Entry", value=False)

                    if st.form_submit_button("Add Entry", type="primary"):
                        with repo.session() as session:
                            session.add(
                                Entry(
                                    meet_id=selected_meet.id,
                                    team_id=entry_team.id,
                                    athlete_id=athlete_choice.id if (athlete_choice and not ev_spec.is_relay) else None,
                                    event_code=ev_spec.code,
                                    is_relay=ev_spec.is_relay,
                                    is_exhibition=is_exh,
                                    seed_time_ms=parse_time_to_ms(seed_str),
                                )
                            )
                            session.commit()
                        st.rerun()

        violations = validate_nfhs_entries(candidates)
        if violations:
            st.error("NFHS Entry-Limit Validation Warnings:")
            for v in violations:
                st.write(f"- {v}")
        elif candidates:
            st.success(f"All {len(candidates)} entries pass NFHS dual-meet limits.")

        ev_filter = st.selectbox(
            "Preview Heat & Lane Seeding for Event",
            options=MIAA_STANDARD_EVENTS,
            format_func=lambda ev: f"#{ev.event_number} · {ev.name}",
            key="seed_preview_event",
        )
        ev_entries = [c for c in candidates if c.event_code == ev_filter.code]
        seeded = assign_center_out_lanes(ev_entries, lane_count=selected_meet.lane_count)
        if seeded:
            st.dataframe(seeded, use_container_width=True, hide_index=True)
        else:
            st.info("No entries in this event yet.")

    with tab_live:
        active_ev = st.selectbox(
            "Select Event to Score",
            options=MIAA_STANDARD_EVENTS,
            format_func=lambda ev: f"#{ev.event_number} · {ev.name}",
            key="live_score_event",
        )
        ev_entries = [c for c in candidates if c.event_code == active_ev.code]
        if not ev_entries:
            st.info("Add entries for this event in Tab 2 first.")
        else:
            perf_inputs: list[PerformanceInput] = []
            for c in ev_entries:
                existing_res = db_results.get(c.entry_id)
                cols = st.columns([2.2, 1.2, 1.0, 0.8])
                cols[0].markdown(f"**{c.team_code}** — {c.athlete_name}" + (" *(EXH)*" if c.is_exhibition else ""))
                default_mark = (
                    str(existing_res.diving_score)
                    if (active_ev.is_diving and existing_res and existing_res.diving_score is not None)
                    else format_time_ms(existing_res.final_time_ms)
                    if (existing_res and existing_res.final_time_ms)
                    else ""
                )
                mark_val = cols[1].text_input(
                    "Diving Pts" if active_ev.is_diving else "Time",
                    value=default_mark,
                    key=f"mark_{c.entry_id}",
                    label_visibility="collapsed",
                )
                status_opts = ["OK", "DQ", "SCRATCH"]
                default_status = (
                    "DQ"
                    if (existing_res and existing_res.state == ResultState.DISQUALIFIED)
                    else "SCRATCH"
                    if (existing_res and existing_res.state == ResultState.SCRATCH)
                    else "OK"
                )
                status_val = cols[2].selectbox(
                    "Status",
                    options=status_opts,
                    index=status_opts.index(default_status),
                    key=f"status_{c.entry_id}",
                    label_visibility="collapsed",
                )
                cols[3].caption("EXH" if c.is_exhibition else "SCORING")

                diving_pts = Decimal(mark_val) if (active_ev.is_diving and mark_val.strip()) else None
                time_ms = None if active_ev.is_diving else parse_time_to_ms(mark_val)
                perf_inputs.append(
                    PerformanceInput(
                        entry_id=c.entry_id,
                        team_id=c.team_id,
                        team_code=c.team_code,
                        event_code=active_ev.code,
                        athlete_name=c.athlete_name,
                        is_relay=active_ev.is_relay,
                        is_exhibition=c.is_exhibition,
                        is_diving=active_ev.is_diving,
                        status=status_val,
                        final_time_ms=time_ms,
                        diving_score=diving_pts,
                    )
                )

            scored = score_event_performances(
                perf_inputs, is_relay=active_ev.is_relay, is_diving=active_ev.is_diving
            )
            st.dataframe(
                [
                    {
                        "Place": "EXH" if s.is_exhibition else (s.place or "-"),
                        "Team": s.team_code,
                        "Athlete / Relay": s.athlete_name,
                        "Result": str(s.diving_score) if active_ev.is_diving else format_time_ms(s.final_time_ms),
                        "Status": s.status,
                        "Points": f"{s.points:.2f}",
                    }
                    for s in scored
                ],
                use_container_width=True,
                hide_index=True,
            )

            if active_user and not selected_meet.is_locked and st.button("Save Event Results", type="primary"):
                saved_primary = 0
                queued_offline = 0
                for s in scored:
                    r_state = (
                        ResultState.DISQUALIFIED
                        if s.status == "DQ"
                        else ResultState.SCRATCH
                        if s.status == "SCRATCH"
                        else ResultState.EXHIBITION
                        if s.is_exhibition
                        else ResultState.PRELIMINARY
                    )
                    outcome = save_scored_result_resilient(
                        repo,
                        active_outbox,
                        force_offline=force_offline,
                        actor_user_id=active_user.id,
                        meet_id=selected_meet.id,
                        entry_id=s.entry_id,
                        event_code=s.event_code,
                        state=r_state,
                        final_time_ms=s.final_time_ms,
                        diving_score=s.diving_score,
                        place=s.place,
                        points_awarded=s.points,
                        revision_reason=f"Saved event {active_ev.code} from Meet Center",
                    )
                    if outcome == "saved_primary":
                        saved_primary += 1
                    else:
                        queued_offline += 1
                st.success(f"Saved directly: {saved_primary} · Queued offline: {queued_offline}.")
                st.rerun()

    with tab_reports:
        all_scored: list[ScoredPerformance] = []
        boys_totals: dict[str, Decimal] = {}
        girls_totals: dict[str, Decimal] = {}

        for c in candidates:
            res = db_results.get(c.entry_id)
            if not res:
                continue
            spec = EVENT_BY_CODE.get(c.event_code)
            sp = ScoredPerformance(
                entry_id=c.entry_id,
                team_id=c.team_id,
                team_code=c.team_code,
                event_code=c.event_code,
                athlete_name=c.athlete_name,
                status="DQ" if res.state == ResultState.DISQUALIFIED else "OK",
                is_exhibition=c.is_exhibition,
                final_time_ms=res.final_time_ms,
                diving_score=Decimal(str(res.diving_score)) if res.diving_score is not None else None,
                place=res.place,
                points=Decimal(str(res.points_awarded)),
            )
            all_scored.append(sp)
            if spec and spec.gender == "B":
                boys_totals[c.team_code] = boys_totals.get(c.team_code, Decimal("0")) + sp.points
            elif spec and spec.gender == "G":
                girls_totals[c.team_code] = girls_totals.get(c.team_code, Decimal("0")) + sp.points

        c1, c2 = st.columns(2)
        c1.subheader("Boys Team Scores")
        c1.write(boys_totals or "No scored boys events yet.")
        c2.subheader("Girls Team Scores")
        c2.write(girls_totals or "No scored girls events yet.")

        boys_rows = [r for r in all_scored if EVENT_BY_CODE.get(r.event_code) and EVENT_BY_CODE[r.event_code].gender == "B"]
        girls_rows = [r for r in all_scored if EVENT_BY_CODE.get(r.event_code) and EVENT_BY_CODE[r.event_code].gender == "G"]

        d1, d2, d3, d4 = st.columns(4)
        d1.download_button(
            "Download Boys 1-Page Report (HTML)",
            data=render_printable_results_html(
                meet_name=selected_meet.name,
                meet_date=selected_meet.meet_date,
                gender_label="Boys",
                team_totals=boys_totals,
                scored_rows=boys_rows,
            ),
            file_name=f"meet_{selected_meet.id}_boys_official.html",
            mime="text/html",
        )
        d2.download_button(
            "Download Girls 1-Page Report (HTML)",
            data=render_printable_results_html(
                meet_name=selected_meet.name,
                meet_date=selected_meet.meet_date,
                gender_label="Girls",
                team_totals=girls_totals,
                scored_rows=girls_rows,
            ),
            file_name=f"meet_{selected_meet.id}_girls_official.html",
            mime="text/html",
        )
        d3.download_button(
            "Export HY-TEK (.sd3)",
            data=export_hytek_sdif_results(
                meet_name=selected_meet.name,
                meet_date=selected_meet.meet_date,
                host_team_code=selected_team.code if selected_team else "GAR",
                scored_rows=all_scored,
            ),
            file_name=f"meet_{selected_meet.id}_results.sd3",
            mime="text/plain",
        )
        d4.download_button(
            "Export CSV Results",
            data=export_meet_results_csv(all_scored),
            file_name=f"meet_{selected_meet.id}_results.csv",
            mime="text/csv",
        )

        if active_user and not selected_meet.is_locked:
            if st.button("Official Referee Sign-Off & Lock Meet"):
                with repo.session() as session:
                    db_meet = session.get(Meet, selected_meet.id)
                    if db_meet:
                        db_meet.status = MeetStatus.LOCKED
                        db_meet.is_locked = True
                        repo.record_audit(
                            session,
                            action="meet.official_signoff_locked",
                            entity_type="meet",
                            entity_id=db_meet.id,
                            org_id=db_meet.host_org_id,
                            actor_user_id=active_user.id,
                        )
                        session.commit()
                st.success("Meet signed off and locked.")
                st.rerun()
