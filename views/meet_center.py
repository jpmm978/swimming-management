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
    AuditLog,
    CourseType,
    Entry,
    Meet,
    MeetStatus,
    OfficialResult,
    Organization,
    ResultState,
    Season,
    SeasonRoster,
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
        teams = list(session.scalars(select(Team).where(Team.is_active.is_(True)).order_by(Team.code)).all())
        team_map = {t.id: t for t in teams}
        athletes = list(
            session.scalars(
                select(Athlete).where(Athlete.is_active.is_(True)).order_by(Athlete.last_name, Athlete.first_name)
            ).all()
        )
        athlete_map = {a.id: a for a in athletes}
        season_rosters = list(
            session.scalars(
                select(SeasonRoster).where(
                    SeasonRoster.season_id == selected_meet.season_id,
                    SeasonRoster.status == "active",
                )
            ).all()
        )
        db_entries = list(
            session.scalars(select(Entry).where(Entry.meet_id == selected_meet.id).order_by(Entry.id)).all()
        )
        db_results = {
            r.entry_id: r
            for r in session.scalars(select(OfficialResult).where(OfficialResult.meet_id == selected_meet.id)).all()
        }
        relay_audit_logs = list(
            session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.action == "meet.relay_squad_saved",
                    AuditLog.entity_type == "entry",
                )
                .order_by(AuditLog.id)
            ).all()
        )

    athlete_ids_by_team: dict[int, set[int]] = {}
    for sr in season_rosters:
        athlete_ids_by_team.setdefault(sr.team_id, set()).add(sr.athlete_id)

    # Reconstruct saved relay squad letter (A/B/C/D) & 4 legs per Entry ID
    relay_meta_by_entry: dict[int, dict] = {}
    for log in relay_audit_logs:
        if log.entity_id and log.entity_id.isdigit():
            eid = int(log.entity_id)
            details = log.details or {}
            if details.get("meet_id") == selected_meet.id:
                relay_meta_by_entry[eid] = {
                    "squad": details.get("squad", "A"),
                    "member_ids": details.get("member_ids", []),
                }

    lane_overrides: dict[int, int] = st.session_state.setdefault(
        f"lane_overrides_meet_{selected_meet.id}", {}
    )

    auto_squad_counter: dict[tuple[int, str], int] = {}
    squad_letters = ["A", "B", "C", "D"]

    candidates: list[EntryCandidate] = []
    for e in db_entries:
        t = team_map.get(e.team_id)
        t_code = t.code if t else "TEAM"
        a = athlete_map.get(e.athlete_id) if e.athlete_id else None
        spec = EVENT_BY_CODE.get(e.event_code)
        is_relay = spec.is_relay if spec else e.is_relay

        if is_relay:
            meta = relay_meta_by_entry.get(e.id)
            if meta:
                squad = meta["squad"]
                relay_members = [int(mid) for mid in meta.get("member_ids", []) if int(mid) in athlete_map]
            else:
                idx = auto_squad_counter.get((e.team_id, e.event_code), 0)
                squad = squad_letters[idx] if idx < len(squad_letters) else f"#{idx + 1}"
                auto_squad_counter[(e.team_id, e.event_code)] = idx + 1
                relay_members = list(getattr(e, "relay_member_ids", None) or [])

            leg_labels = [
                f"{i + 1}. {athlete_map[mid].first_name} {athlete_map[mid].last_name}"
                for i, mid in enumerate(relay_members)
                if mid in athlete_map
            ]
            relay_title = f"{t_code} Relay {squad}"
            if leg_labels:
                relay_title += f" ({', '.join(leg_labels)})"

            candidates.append(
                EntryCandidate(
                    entry_id=e.id,
                    team_id=e.team_id,
                    team_code=t_code,
                    event_code=e.event_code,
                    athlete_id=None,
                    athlete_name=relay_title,
                    relay_label=f"{t_code} {squad}",
                    relay_member_ids=relay_members,
                    is_relay=True,
                    is_exhibition=e.is_exhibition,
                    seed_time_ms=e.seed_time_ms,
                )
            )
        else:
            candidates.append(
                EntryCandidate(
                    entry_id=e.id,
                    team_id=e.team_id,
                    team_code=t_code,
                    event_code=e.event_code,
                    athlete_id=e.athlete_id,
                    athlete_name=f"{a.first_name} {a.last_name}" if a else f"Athlete #{e.athlete_id}",
                    is_relay=False,
                    is_exhibition=e.is_exhibition,
                    seed_time_ms=e.seed_time_ms,
                )
            )

    def _athlete_event_counts(athlete_id: int) -> tuple[int, int]:
        indiv = 0
        relays = 0
        for c in candidates:
            if not c.is_relay and c.athlete_id == athlete_id:
                indiv += 1
            elif c.is_relay and athlete_id in c.relay_member_ids:
                relays += 1
        return indiv, indiv + relays

    with tab_entries:
        if active_user and not selected_meet.is_locked and teams:
            with st.expander("1. Individual Event Checkboxes (SwimTopia Style)", expanded=True):
                sw_c1, sw_c2 = st.columns([1.4, 2.0])
                matrix_team = sw_c1.selectbox(
                    "Entry Team",
                    options=teams,
                    index=teams.index(selected_team) if selected_team in teams else 0,
                    format_func=lambda t: f"{t.name} ({t.code})",
                    key="matrix_entry_team",
                )
                roster_ids = athlete_ids_by_team.get(matrix_team.id, set())
                team_athletes = [a for a in athletes if a.id in roster_ids] if roster_ids else []

                if not team_athletes:
                    st.info(
                        f"No active athletes found on **{matrix_team.name} ({matrix_team.code})** for this season. "
                        "Switch to this team in **Team Center** to add or import its swimmers first."
                    )
                else:
                    matrix_athlete = sw_c2.selectbox(
                        "Select Swimmer",
                        options=team_athletes,
                        format_func=lambda a: f"{a.last_name}, {a.first_name} ({a.gender or '?'})",
                        key="matrix_entry_athlete",
                    )

                    existing_indiv_by_code = {
                        c.event_code: c
                        for c in candidates
                        if not c.is_relay and c.athlete_id == matrix_athlete.id
                    }
                    cur_indiv, cur_total = _athlete_event_counts(matrix_athlete.id)

                    def _gender_matches(ev_gender: str) -> bool:
                        if matrix_athlete.gender is None or matrix_athlete.gender == "X":
                            return True
                        if matrix_athlete.gender == "M":
                            return ev_gender == "B"
                        if matrix_athlete.gender == "F":
                            return ev_gender == "G"
                        return True

                    indiv_events = [
                        ev for ev in MIAA_STANDARD_EVENTS if not ev.is_relay and _gender_matches(ev.gender)
                    ]

                    selected_event_seeds: dict[str, str] = {}
                    selected_event_exh: dict[str, bool] = {}
                    checked_indiv_events: list[str] = []

                    st.markdown("**Check individual events (NFHS limit: max 2 individual / 4 total events):**")
                    hdr_cols = st.columns([2.3, 1.1, 0.7, 0.7])
                    hdr_cols[0].caption("Event")
                    hdr_cols[1].caption("Seed Time / Pts")
                    hdr_cols[2].caption("EXH")
                    hdr_cols[3].caption("Type")

                    for ev in indiv_events:
                        r_cols = st.columns([2.3, 1.1, 0.7, 0.7])
                        existing_entry = existing_indiv_by_code.get(ev.code)
                        already_in = existing_entry is not None
                        existing_suffix = (
                            " *(already entered — EXH)*"
                            if (existing_entry and existing_entry.is_exhibition)
                            else " *(already entered)*"
                            if already_in
                            else ""
                        )
                        is_checked = r_cols[0].checkbox(
                            f"#{ev.event_number} · {ev.name}{existing_suffix}",
                            value=False,
                            disabled=already_in,
                            key=f"chk_ind_{selected_meet.id}_{matrix_athlete.id}_{ev.code}",
                        )
                        seed_val = r_cols[1].text_input(
                            f"Seed for {ev.code}",
                            value="NT",
                            disabled=not is_checked,
                            key=f"seed_ind_{selected_meet.id}_{matrix_athlete.id}_{ev.code}",
                            label_visibility="collapsed",
                        )
                        is_ev_exh = r_cols[2].checkbox(
                            "EXH",
                            value=existing_entry.is_exhibition if existing_entry else False,
                            disabled=not is_checked,
                            key=f"exh_ind_{selected_meet.id}_{matrix_athlete.id}_{ev.code}",
                        )
                        r_cols[3].caption("Diving" if ev.is_diving else "Swim")
                        if is_checked and not already_in:
                            checked_indiv_events.append(ev.code)
                            selected_event_seeds[ev.code] = seed_val
                            selected_event_exh[ev.code] = is_ev_exh

                    proj_indiv = cur_indiv + len(checked_indiv_events)
                    proj_total = cur_total + len(checked_indiv_events)
                    st.caption(
                        f"Swimmer Event Load after save: **{proj_indiv}/2 Individual** · "
                        f"**{proj_total}/4 Total Events**"
                    )
                    if proj_indiv > 2 or proj_total > 4:
                        st.warning("Heads up: This selection exceeds standard NFHS per-swimmer entry limits.")

                    if st.button("Save Individual Events", type="primary", key="save_checkbox_entries"):
                        if not checked_indiv_events:
                            st.info("Check at least one event above before saving.")
                        else:
                            with repo.session() as session:
                                for ev_code in checked_indiv_events:
                                    ev_spec = EVENT_BY_CODE[ev_code]
                                    session.add(
                                        Entry(
                                            meet_id=selected_meet.id,
                                            team_id=matrix_team.id,
                                            athlete_id=matrix_athlete.id,
                                            event_code=ev_code,
                                            is_relay=False,
                                            is_exhibition=selected_event_exh.get(ev_code, False),
                                            seed_time_ms=(
                                                None
                                                if ev_spec.is_diving
                                                else parse_time_to_ms(selected_event_seeds.get(ev_code, "NT"))
                                            ),
                                        )
                                    )
                                session.commit()
                            st.success(
                                f"Added {len(checked_indiv_events)} individual event(s) for "
                                f"{matrix_athlete.first_name} {matrix_athlete.last_name}."
                            )
                            st.rerun()

            with st.expander("2. Relay Squad Builder (A, B, C, D — Select 4 Swimmers)", expanded=True):
                rc1, rc2, rc3, rc4 = st.columns([1.3, 1.8, 0.8, 1.0])
                relay_team = rc1.selectbox(
                    "Relay Team",
                    options=teams,
                    index=teams.index(selected_team) if selected_team in teams else 0,
                    format_func=lambda t: f"{t.name} ({t.code})",
                    key="relay_squad_team",
                )
                relay_ev = rc2.selectbox(
                    "Relay Event",
                    options=[ev for ev in MIAA_STANDARD_EVENTS if ev.is_relay],
                    format_func=lambda ev: f"#{ev.event_number} · {ev.name}",
                    key="relay_squad_event",
                )
                relay_squad = rc3.selectbox(
                    "Relay Squad",
                    options=["A", "B", "C", "D"],
                    key="relay_squad_letter",
                )
                relay_seed = rc4.text_input(
                    "Seed Time",
                    value="NT",
                    key="relay_squad_seed",
                )

                r_roster_ids = athlete_ids_by_team.get(relay_team.id, set())
                eligible_relay_swimmers = [
                    a
                    for a in athletes
                    if a.id in r_roster_ids
                    and (
                        a.gender is None
                        or a.gender == "X"
                        or (relay_ev.gender == "B" and a.gender == "M")
                        or (relay_ev.gender == "G" and a.gender == "F")
                    )
                ]

                if not eligible_relay_swimmers:
                    st.info(
                        f"No eligible swimmers on **{relay_team.name} ({relay_team.code})** for {relay_ev.name}."
                    )
                else:
                    existing_squad_entry: Entry | None = None
                    existing_legs: list[int] = []
                    for c in candidates:
                        if (
                            c.is_relay
                            and c.team_id == relay_team.id
                            and c.event_code == relay_ev.code
                            and c.relay_label == f"{relay_team.code} {relay_squad}"
                        ):
                            existing_squad_entry = next(
                                (e for e in db_entries if e.id == c.entry_id), None
                            )
                            existing_legs = list(c.relay_member_ids)
                            break

                    swimmer_options: list[Athlete | None] = [None] + eligible_relay_swimmers
                    option_ids = [None] + [a.id for a in eligible_relay_swimmers]

                    def _fmt_leg_swimmer(a: Athlete | None) -> str:
                        if a is None:
                            return "-- Select Swimmer --"
                        ind_c, tot_c = _athlete_event_counts(a.id)
                        return f"{a.last_name}, {a.first_name} ({tot_c}/4 events)"

                    leg_cols = st.columns(4)
                    selected_legs: list[Athlete | None] = []
                    for leg_idx in range(4):
                        default_id = existing_legs[leg_idx] if leg_idx < len(existing_legs) else None
                        default_idx = option_ids.index(default_id) if default_id in option_ids else 0
                        chosen = leg_cols[leg_idx].selectbox(
                            f"Leg {leg_idx + 1}",
                            options=swimmer_options,
                            index=default_idx,
                            format_func=_fmt_leg_swimmer,
                            key=f"relay_leg_{selected_meet.id}_{relay_team.id}_{relay_ev.code}_{relay_squad}_{leg_idx}",
                        )
                        selected_legs.append(chosen)

                    ex_col, btn_col = st.columns([1.2, 1.8])
                    relay_exh = ex_col.checkbox(
                        "Exhibition Relay",
                        value=existing_squad_entry.is_exhibition if existing_squad_entry else False,
                        key=f"relay_exh_{selected_meet.id}_{relay_team.id}_{relay_ev.code}_{relay_squad}",
                    )

                    if btn_col.button(
                        f"Save {relay_team.code} Relay {relay_squad}",
                        type="primary",
                        key="save_relay_squad_btn",
                    ):
                        chosen_ids = [a.id for a in selected_legs if a is not None]
                        if len(chosen_ids) != 4:
                            st.error("Please select all 4 swimmers (Legs 1–4) for the relay.")
                        elif len(set(chosen_ids)) != 4:
                            st.error("A swimmer cannot swim multiple legs on the same relay.")
                        else:
                            seed_ms = parse_time_to_ms(relay_seed)
                            with repo.session() as session:
                                if existing_squad_entry is not None:
                                    db_rel = session.get(Entry, existing_squad_entry.id)
                                    if db_rel is not None:
                                        db_rel.is_exhibition = relay_exh
                                        db_rel.seed_time_ms = seed_ms
                                        target_entry_id = db_rel.id
                                else:
                                    new_rel = Entry(
                                        meet_id=selected_meet.id,
                                        team_id=relay_team.id,
                                        athlete_id=None,
                                        event_code=relay_ev.code,
                                        is_relay=True,
                                        is_exhibition=relay_exh,
                                        seed_time_ms=seed_ms,
                                    )
                                    session.add(new_rel)
                                    session.flush()
                                    target_entry_id = new_rel.id

                                repo.record_audit(
                                    session,
                                    action="meet.relay_squad_saved",
                                    entity_type="entry",
                                    entity_id=target_entry_id,
                                    org_id=selected_meet.host_org_id,
                                    actor_user_id=active_user.id,
                                    details={
                                        "meet_id": selected_meet.id,
                                        "team_id": relay_team.id,
                                        "event_code": relay_ev.code,
                                        "squad": relay_squad,
                                        "member_ids": chosen_ids,
                                    },
                                )
                                session.commit()

                            st.success(
                                f"Saved {relay_team.code} Relay {relay_squad} for {relay_ev.name} "
                                "(assigned to its own lane)."
                            )
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

        db_lane_by_entry = {
            e.id: getattr(e, "lane_number", None)
            for e in db_entries
            if e.event_code == ev_filter.code
        }
        for row in seeded:
            eid = row["entry_id"]
            if eid in lane_overrides:
                row["lane"] = lane_overrides[eid]
            elif db_lane_by_entry.get(eid) is not None:
                row["lane"] = db_lane_by_entry[eid]

        if seeded:
            st.dataframe(seeded, use_container_width=True, hide_index=True)

            if active_user and not selected_meet.is_locked:
                with st.expander("Edit Assigned Lane for Entry", expanded=False):
                    le_cols = st.columns([2.4, 1.0, 1.2])
                    target_row = le_cols[0].selectbox(
                        "Assigned Swimmer / Relay",
                        options=seeded,
                        format_func=lambda r: (
                            f"Heat {r['heat']} · Lane {r['lane']} — "
                            f"{r['team_code']} {r['athlete_name']} ({r['seed_time']})"
                        ),
                        key=f"edit_lane_entry_{selected_meet.id}_{ev_filter.code}",
                    )
                    new_lane = int(
                        le_cols[1].number_input(
                            "New Lane",
                            min_value=1,
                            max_value=selected_meet.lane_count,
                            value=int(target_row["lane"]),
                            key=f"edit_lane_num_{selected_meet.id}_{ev_filter.code}_{target_row['entry_id']}",
                        )
                    )
                    le_cols[2].markdown("<br>", unsafe_allow_html=True)
                    if le_cols[2].button("Update Lane", type="primary", key=f"save_lane_btn_{ev_filter.code}"):
                        eid = int(target_row["entry_id"])
                        lane_overrides[eid] = new_lane
                        with repo.session() as session:
                            db_entry = session.get(Entry, eid)
                            if db_entry is not None and hasattr(db_entry, "lane_number"):
                                db_entry.lane_number = new_lane
                                session.commit()
                        st.success(f"Updated {target_row['athlete_name']} to Lane {new_lane}.")
                        st.rerun()
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
