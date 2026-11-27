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
    assign_dual_meet_odd_even_lanes,
    format_time_ms,
    get_relay_seeding_notices,
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
    render_meet_program_html,
    render_printable_results_html,
    render_swimmer_checkin_csv,
    render_swimmer_checkin_html,
    render_timer_sheets_html,
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
        meet_audit_logs = list(
            session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.action.in_(["meet.relay_squad_saved", "meet.result_decision_note"]),
                    AuditLog.entity_type == "entry",
                )
                .order_by(AuditLog.id)
            ).all()
        )

    athlete_ids_by_team: dict[int, set[int]] = {}
    for sr in season_rosters:
        athlete_ids_by_team.setdefault(sr.team_id, set()).add(sr.athlete_id)

    relay_meta_by_entry: dict[int, dict] = {}
    decision_meta_by_entry: dict[int, dict] = {}
    for log in meet_audit_logs:
        if log.entity_id and log.entity_id.isdigit():
            eid = int(log.entity_id)
            details = log.details or {}
            if details.get("meet_id") == selected_meet.id:
                if log.action == "meet.relay_squad_saved":
                    relay_meta_by_entry[eid] = {
                        "squad": details.get("squad", "A"),
                        "member_ids": details.get("member_ids", []),
                    }
                elif log.action == "meet.result_decision_note":
                    decision_meta_by_entry[eid] = {
                        "is_jd": bool(details.get("is_jd", False)),
                        "decision_note": str(details.get("decision_note", "")),
                    }

    heat_lane_overrides: dict[int, dict[str, int]] = st.session_state.setdefault(
        f"heat_lane_overrides_meet_{selected_meet.id}", {}
    )
    jd_overrides: dict[int, int] = st.session_state.setdefault(
        f"jd_overrides_meet_{selected_meet.id}", {}
    )
    place_overrides: dict[int, int] = st.session_state.setdefault(
        f"place_overrides_meet_{selected_meet.id}", {}
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

    def _get_seeded_rows_for_event(
        ev_code: str,
        *,
        seeding_mode: str = "Dual-Meet Odd / Even Lanes",
        odd_team_id: int | None = None,
        even_team_id: int | None = None,
    ) -> list[dict]:
        ev_entries = [c for c in candidates if c.event_code == ev_code]
        if not ev_entries:
            return []
        if seeding_mode == "Dual-Meet Odd / Even Lanes" and odd_team_id is not None:
            rows = assign_dual_meet_odd_even_lanes(
                ev_entries,
                odd_team_id=odd_team_id,
                even_team_id=even_team_id,
                lane_count=selected_meet.lane_count,
            )
        else:
            rows = assign_center_out_lanes(ev_entries, lane_count=selected_meet.lane_count)

        db_by_id = {e.id: e for e in db_entries if e.event_code == ev_code}
        for r in rows:
            eid = int(r["entry_id"])
            db_e = db_by_id.get(eid)
            ov = heat_lane_overrides.get(eid, {})
            if "heat" in ov:
                r["heat"] = ov["heat"]
            elif db_e is not None and getattr(db_e, "heat_number", None) is not None:
                r["heat"] = db_e.heat_number

            if "lane" in ov:
                r["lane"] = ov["lane"]
            elif db_e is not None and getattr(db_e, "lane_number", None) is not None:
                r["lane"] = db_e.lane_number

            if db_e is not None:
                r["is_exhibition"] = bool(db_e.is_exhibition)

        rows.sort(key=lambda item: (int(item["heat"]), int(item["lane"])))
        return rows

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
                        _, tot_c = _athlete_event_counts(a.id)
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

            with st.expander("3. Direct Event Slot Entry (Event → Heat & Lane → Swimmer)", expanded=False):
                slot_e1, slot_h1, slot_l1, slot_g1 = st.columns([2.0, 0.8, 0.8, 1.0])
                slot_event = slot_e1.selectbox(
                    "Event",
                    options=[ev for ev in MIAA_STANDARD_EVENTS if not ev.is_relay],
                    format_func=lambda ev: f"#{ev.event_number} · {ev.name}",
                    key=f"slot_entry_event_{selected_meet.id}",
                )
                slot_heat = int(
                    slot_h1.number_input(
                        "Heat",
                        min_value=1,
                        max_value=12,
                        value=1,
                        key=f"slot_entry_heat_{selected_meet.id}",
                    )
                )
                slot_lane = int(
                    slot_l1.number_input(
                        "Lane",
                        min_value=1,
                        max_value=selected_meet.lane_count,
                        value=1,
                        key=f"slot_entry_lane_{selected_meet.id}",
                    )
                )
                default_gen_filter = (
                    "Girls" if slot_event.gender == "G" else ("Boys" if slot_event.gender == "B" else "All")
                )
                gen_filter_opts = ["Girls", "Boys", "All"]
                slot_gen_filter = slot_g1.selectbox(
                    "Swimmer List",
                    options=gen_filter_opts,
                    index=gen_filter_opts.index(default_gen_filter),
                    key=f"slot_entry_gen_{selected_meet.id}_{slot_event.code}",
                )

                athlete_team_id_map: dict[int, int] = {}
                for sr in season_rosters:
                    athlete_team_id_map.setdefault(sr.athlete_id, sr.team_id)

                if slot_gen_filter == "Girls":
                    pool_athletes = [a for a in athletes if a.gender == "F"]
                elif slot_gen_filter == "Boys":
                    pool_athletes = [a for a in athletes if a.gender == "M"]
                else:
                    pool_athletes = list(athletes)

                if not pool_athletes:
                    st.info(f"No swimmers found in the '{slot_gen_filter}' list.")
                else:
                    sw_col, tm_col, sd_col, ex_col = st.columns([2.2, 1.3, 1.0, 0.7])
                    chosen_swimmer = sw_col.selectbox(
                        "Select Swimmer",
                        options=pool_athletes,
                        format_func=lambda a: (
                            f"{a.last_name}, {a.first_name} "
                            f"({team_map[athlete_team_id_map[a.id]].code if a.id in athlete_team_id_map and athlete_team_id_map[a.id] in team_map else 'No Team'} · "
                            f"{a.grad_year or '—'} · {a.gender or '?'})"
                        ),
                        key=f"slot_entry_swimmer_{selected_meet.id}_{slot_event.code}_{slot_gen_filter}",
                    )

                    inferred_team_id = athlete_team_id_map.get(chosen_swimmer.id)
                    default_t_idx = next(
                        (i for i, t in enumerate(teams) if t.id == inferred_team_id),
                        teams.index(selected_team) if selected_team in teams else 0,
                    )
                    chosen_slot_team = tm_col.selectbox(
                        "Team",
                        options=teams,
                        index=default_t_idx,
                        format_func=lambda t: f"{t.code} — {t.name}",
                        key=f"slot_entry_team_{selected_meet.id}_{chosen_swimmer.id}",
                    )
                    slot_seed_str = sd_col.text_input(
                        "Seed Time",
                        value="NT",
                        disabled=slot_event.is_diving,
                        key=f"slot_entry_seed_{selected_meet.id}_{slot_event.code}_{chosen_swimmer.id}",
                    )
                    slot_is_exh = ex_col.checkbox(
                        "EXH",
                        value=False,
                        key=f"slot_entry_exh_{selected_meet.id}_{slot_event.code}_{chosen_swimmer.id}",
                    )

                    already_entered = any(
                        not c.is_relay and c.athlete_id == chosen_swimmer.id and c.event_code == slot_event.code
                        for c in candidates
                    )
                    if already_entered:
                        st.warning(
                            f"{chosen_swimmer.first_name} {chosen_swimmer.last_name} already has an entry in "
                            f"#{slot_event.event_number} · {slot_event.name}."
                        )

                    if st.button(
                        f"Assign to Heat {slot_heat}, Lane {slot_lane}",
                        type="primary",
                        key=f"save_slot_entry_{selected_meet.id}_{slot_event.code}_{chosen_swimmer.id}",
                    ):
                        with repo.session() as session:
                            new_entry = Entry(
                                meet_id=selected_meet.id,
                                team_id=chosen_slot_team.id,
                                athlete_id=chosen_swimmer.id,
                                event_code=slot_event.code,
                                is_relay=False,
                                is_exhibition=slot_is_exh,
                                seed_time_ms=(
                                    None if slot_event.is_diving else parse_time_to_ms(slot_seed_str)
                                ),
                            )
                            if hasattr(new_entry, "heat_number"):
                                new_entry.heat_number = slot_heat
                            if hasattr(new_entry, "lane_number"):
                                new_entry.lane_number = slot_lane
                            session.add(new_entry)
                            session.flush()
                            created_id = new_entry.id
                            session.commit()

                        heat_lane_overrides[created_id] = {"heat": slot_heat, "lane": slot_lane}
                        st.success(
                            f"Added {chosen_swimmer.first_name} {chosen_swimmer.last_name} "
                            f"to #{slot_event.event_number} · {slot_event.name} "
                            f"(Heat {slot_heat}, Lane {slot_lane})."
                        )
                        st.rerun()

        violations = validate_nfhs_entries(candidates, lane_count=selected_meet.lane_count)
        relay_notices = get_relay_seeding_notices(candidates)
        if violations:
            st.error("NFHS Entry-Limit Validation Warnings:")
            for v in violations:
                st.write(f"- {v}")
        elif candidates:
            st.success(f"All {len(candidates)} entries pass NFHS dual-meet limits.")

        if relay_notices:
            for notice in relay_notices:
                st.info(notice)

        st.divider()
        st.subheader("Heat & Lane Seeding")
        sm1, sm2, sm3 = st.columns([1.4, 1.3, 1.3])
        seeding_mode = sm1.selectbox(
            "Seeding Mode",
            options=["Dual-Meet Odd / Even Lanes", "Center-Out (All Lanes)"],
            key=f"seeding_mode_{selected_meet.id}",
        )
        default_odd_idx = teams.index(selected_team) if selected_team in teams else 0
        odd_team = sm2.selectbox(
            "Odd Lanes Team (1, 3, 5...)",
            options=teams,
            index=default_odd_idx,
            disabled=(seeding_mode != "Dual-Meet Odd / Even Lanes"),
            format_func=lambda t: f"{t.code} — {t.name}",
            key=f"odd_lanes_team_{selected_meet.id}",
        )
        even_candidates = [t for t in teams if t.id != odd_team.id] or teams
        even_team = sm3.selectbox(
            "Even Lanes Team (2, 4, 6...)",
            options=even_candidates,
            index=0,
            disabled=(seeding_mode != "Dual-Meet Odd / Even Lanes"),
            format_func=lambda t: f"{t.code} — {t.name}",
            key=f"even_lanes_team_{selected_meet.id}",
        )

        ev_filter = st.selectbox(
            "Preview Heats & Lane Seeding for Event",
            options=MIAA_STANDARD_EVENTS,
            format_func=lambda ev: f"#{ev.event_number} · {ev.name}",
            key="seed_preview_event",
        )
        seeded = _get_seeded_rows_for_event(
            ev_filter.code,
            seeding_mode=seeding_mode,
            odd_team_id=odd_team.id,
            even_team_id=even_team.id,
        )
        db_entry_by_id = {e.id: e for e in db_entries if e.event_code == ev_filter.code}

        if seeded:
            heat_numbers = sorted({int(r["heat"]) for r in seeded})
            st.caption(f"Total Heats in Event: **{len(heat_numbers)}** ({', '.join(f'Heat {h}' for h in heat_numbers)})")
            display_seeded = [
                {
                    "Heat": r["heat"],
                    "Lane": r["lane"],
                    "Team": r["team_code"],
                    "Athlete / Relay": r["athlete_name"],
                    "Seed Time": r["seed_time"],
                    "EXH": r["is_exhibition"],
                }
                for r in seeded
            ]
            st.dataframe(display_seeded, width="stretch", hide_index=True)

            if active_user and not selected_meet.is_locked:
                with st.expander("Edit Saved Entry (Exhibition EXH, Heat, Lane & Seed Time)", expanded=False):
                    target_row = st.selectbox(
                        "Select Saved Entry in Event",
                        options=seeded,
                        format_func=lambda r: (
                            f"Heat {r['heat']} · Lane {r['lane']} — "
                            f"{r['team_code']} {r['athlete_name']} "
                            f"({'EXH · ' if r['is_exhibition'] else ''}Seed: {r['seed_time']})"
                        ),
                        key=f"edit_entry_sel_{selected_meet.id}_{ev_filter.code}",
                    )
                    eid = int(target_row["entry_id"])
                    db_target = db_entry_by_id.get(eid)

                    ed1, ed2, ed3, ed4 = st.columns([1.0, 1.0, 1.2, 1.0])
                    new_heat = int(
                        ed1.number_input(
                            "Heat",
                            min_value=1,
                            max_value=12,
                            value=int(target_row["heat"]),
                            key=f"edit_heat_{selected_meet.id}_{ev_filter.code}_{eid}",
                        )
                    )
                    new_lane = int(
                        ed2.number_input(
                            "Lane",
                            min_value=1,
                            max_value=selected_meet.lane_count,
                            value=int(target_row["lane"]),
                            key=f"edit_lane_{selected_meet.id}_{ev_filter.code}_{eid}",
                        )
                    )
                    default_seed_str = (
                        ""
                        if ev_filter.is_diving
                        else (
                            format_time_ms(db_target.seed_time_ms)
                            if (db_target and db_target.seed_time_ms)
                            else "NT"
                        )
                    )
                    new_seed_str = ed3.text_input(
                        "Seed Time",
                        value=default_seed_str,
                        disabled=ev_filter.is_diving,
                        key=f"edit_seed_{selected_meet.id}_{ev_filter.code}_{eid}",
                    )
                    new_exh = ed4.checkbox(
                        "Exhibition (EXH)",
                        value=bool(target_row["is_exhibition"]),
                        key=f"edit_exh_{selected_meet.id}_{ev_filter.code}_{eid}",
                    )

                    if st.button("Save Entry Changes", type="primary", key=f"save_entry_edits_{ev_filter.code}_{eid}"):
                        heat_lane_overrides[eid] = {"heat": new_heat, "lane": new_lane}
                        with repo.session() as session:
                            db_entry = session.get(Entry, eid)
                            if db_entry is not None:
                                db_entry.is_exhibition = new_exh
                                if not ev_filter.is_diving:
                                    db_entry.seed_time_ms = parse_time_to_ms(new_seed_str)
                                if hasattr(db_entry, "heat_number"):
                                    db_entry.heat_number = new_heat
                                if hasattr(db_entry, "lane_number"):
                                    db_entry.lane_number = new_lane
                                session.commit()
                        st.success(
                            f"Updated {target_row['athlete_name']} — "
                            f"Heat {new_heat}, Lane {new_lane}, "
                            f"{'EXH' if new_exh else 'Scoring'}."
                        )
                        st.rerun()
        else:
            st.info("No entries in this event yet.")

    with tab_live:
        if "live_score_event" not in st.session_state:
            st.session_state["live_score_event"] = MIAA_STANDARD_EVENTS[0]

        current_ev_obj = st.session_state["live_score_event"]
        current_ev_idx = next(
            (i for i, ev in enumerate(MIAA_STANDARD_EVENTS) if ev.code == getattr(current_ev_obj, "code", None)),
            0,
        )

        nav_ev_col, prev_btn_col, next_btn_col, top_heat_col = st.columns([2.2, 0.55, 0.55, 1.5])
        with prev_btn_col:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("◀ Prev", disabled=(current_ev_idx <= 0), key="live_prev_event_btn"):
                prev_ev = MIAA_STANDARD_EVENTS[current_ev_idx - 1]
                st.session_state["live_score_event"] = prev_ev
                st.session_state[f"live_heat_filter_{selected_meet.id}_{prev_ev.code}"] = "All Heats"
                st.rerun()
        with next_btn_col:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("Next ▶", disabled=(current_ev_idx >= len(MIAA_STANDARD_EVENTS) - 1), key="live_next_event_btn"):
                next_ev = MIAA_STANDARD_EVENTS[current_ev_idx + 1]
                st.session_state["live_score_event"] = next_ev
                st.session_state[f"live_heat_filter_{selected_meet.id}_{next_ev.code}"] = "All Heats"
                st.rerun()

        active_ev = nav_ev_col.selectbox(
            "Event",
            options=MIAA_STANDARD_EVENTS,
            index=current_ev_idx,
            format_func=lambda ev: f"#{ev.event_number} · {ev.name}",
            key="live_score_event_select",
        )
        st.session_state["live_score_event"] = active_ev

        current_seeding_mode = st.session_state.get(
            f"seeding_mode_{selected_meet.id}", "Dual-Meet Odd / Even Lanes"
        )
        current_odd_team = st.session_state.get(f"odd_lanes_team_{selected_meet.id}", selected_team)
        current_even_team = st.session_state.get(f"even_lanes_team_{selected_meet.id}", None)
        ev_seeded_rows = _get_seeded_rows_for_event(
            active_ev.code,
            seeding_mode=current_seeding_mode,
            odd_team_id=current_odd_team.id if current_odd_team else None,
            even_team_id=current_even_team.id if current_even_team else None,
        )

        if not ev_seeded_rows:
            st.info("Add entries for this event in Tab 2 first.")
        else:
            avail_heats = sorted({int(r["heat"]) for r in ev_seeded_rows})
            heat_filter_choice = top_heat_col.selectbox(
                "Heat",
                options=["All Heats"] + [f"Heat {h}" for h in avail_heats],
                key=f"live_heat_filter_{selected_meet.id}_{active_ev.code}",
            )

            cand_by_id = {c.entry_id: c for c in candidates}
            visible_rows = (
                ev_seeded_rows
                if heat_filter_choice == "All Heats"
                else [
                    r
                    for r in ev_seeded_rows
                    if f"Heat {r['heat']}" == heat_filter_choice
                ]
            )

            all_perf_inputs: list[PerformanceInput] = []
            visible_entry_ids = {int(r["entry_id"]) for r in visible_rows}

            h_cols = st.columns([0.55, 0.75, 2.35, 0.95, 1.25, 0.55, 0.65, 1.0, 0.65, 0.75])
            h_cols[0].caption("LANE")
            h_cols[1].caption("TEAM")
            h_cols[2].caption("ATHLETE / RELAY")
            h_cols[3].caption("SEED")
            h_cols[4].caption("PTS" if active_ev.is_diving else "RESULT TIME")
            h_cols[5].caption("DQ")
            h_cols[6].caption("SCR")
            h_cols[7].caption("OFFICIAL")
            h_cols[8].caption("PLACE")
            h_cols[9].caption("PTS")

            for r in ev_seeded_rows:
                eid = int(r["entry_id"])
                c = cand_by_id.get(eid)
                if not c:
                    continue
                existing_res = db_results.get(eid)
                default_mark = (
                    str(existing_res.diving_score)
                    if (active_ev.is_diving and existing_res and existing_res.diving_score is not None)
                    else format_time_ms(existing_res.final_time_ms)
                    if (existing_res and existing_res.final_time_ms)
                    else ""
                )
                default_dq = bool(existing_res and existing_res.state == ResultState.DISQUALIFIED)
                default_scr = bool(existing_res and existing_res.state == ResultState.SCRATCH)

                if eid in visible_entry_ids:
                    row_cols = st.columns([0.55, 0.75, 2.35, 0.95, 1.25, 0.55, 0.65, 1.0, 0.65, 0.75])
                    row_cols[0].markdown(f"**{r['lane']}**")
                    row_cols[1].markdown(f"`{c.team_code}`")
                    exh_tag = " *(EXH)*" if c.is_exhibition else ""
                    row_cols[2].markdown(f"{c.athlete_name}{exh_tag}")
                    row_cols[3].caption(r["seed_time"])

                    mark_val = row_cols[4].text_input(
                        "Time",
                        value=default_mark,
                        key=f"mark_{eid}",
                        label_visibility="collapsed",
                    )
                    is_dq = row_cols[5].checkbox(
                        "DQ",
                        value=default_dq,
                        key=f"dq_{eid}",
                        label_visibility="collapsed",
                    )
                    is_scr = row_cols[6].checkbox(
                        "SCR",
                        value=default_scr and not is_dq,
                        key=f"scr_{eid}",
                        label_visibility="collapsed",
                    )
                    status_val = "DQ" if is_dq else ("SCRATCH" if is_scr else "OK")
                else:
                    mark_val = st.session_state.get(f"mark_{eid}", default_mark)
                    is_dq = st.session_state.get(f"dq_{eid}", default_dq)
                    is_scr = st.session_state.get(f"scr_{eid}", default_scr)
                    status_val = "DQ" if is_dq else ("SCRATCH" if is_scr else "OK")

                diving_pts = Decimal(mark_val) if (active_ev.is_diving and mark_val.strip()) else None
                time_ms = None if active_ev.is_diving else parse_time_to_ms(mark_val)
                perf = PerformanceInput(
                    entry_id=eid,
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
                    jd_priority=jd_overrides.get(eid),
                    place_override=place_overrides.get(eid),
                )
                all_perf_inputs.append(perf)

            scored_all = score_event_performances(
                all_perf_inputs, is_relay=active_ev.is_relay, is_diving=active_ev.is_diving
            )
            scored_by_id = {s.entry_id: s for s in scored_all}

            for r in visible_rows:
                eid = int(r["entry_id"])
                s = scored_by_id.get(eid)
                if not s:
                    continue
                off_cols = st.columns([0.55, 0.75, 2.35, 0.95, 1.25, 0.55, 0.65, 1.0, 0.65, 0.75])
                if s.status == "DQ":
                    off_str = "DQ"
                    place_str = "DQ"
                elif s.status == "SCRATCH":
                    off_str = "SCR"
                    place_str = "-"
                else:
                    off_str = (
                        str(s.diving_score)
                        if (active_ev.is_diving and s.diving_score is not None)
                        else format_time_ms(s.final_time_ms)
                        if s.final_time_ms
                        else "-"
                    )
                    if s.is_exhibition:
                        place_str = "EXH"
                    elif s.place:
                        place_str = f"{s.place} (JD)" if s.is_jd else str(s.place)
                    else:
                        place_str = "-"

                off_cols[7].markdown(f"**{off_str}**")
                off_cols[8].markdown(place_str)
                off_cols[9].markdown(f"**{s.points:.2f}**")
                if s.decision_note:
                    st.caption(f"Lane {r['lane']} ({s.team_code} {s.athlete_name}): {s.decision_note}")

            if active_user and not selected_meet.is_locked:
                with st.expander("Judge's Decision (JD) & Place Override", expanded=False):
                    jd_c1, jd_c2, jd_c3, jd_c4 = st.columns([2.2, 1.0, 1.0, 1.1])
                    jd_target_row = jd_c1.selectbox(
                        "Swimmer / Relay",
                        options=ev_seeded_rows,
                        format_func=lambda r: (
                            f"H{r['heat']} · L{r['lane']} — {r['team_code']} {r['athlete_name']}"
                        ),
                        key=f"jd_target_sel_{selected_meet.id}_{active_ev.code}",
                    )
                    jd_eid = int(jd_target_row["entry_id"])
                    jd_rank_input = int(
                        jd_c2.number_input(
                            "JD Order (0 = None)",
                            min_value=0,
                            max_value=8,
                            value=int(jd_overrides.get(jd_eid, 0)),
                            help="Set 1 for JD winner on a tie, 2 for runner-up, etc. Set 0 to clear.",
                            key=f"jd_rank_num_{selected_meet.id}_{active_ev.code}_{jd_eid}",
                        )
                    )
                    place_ov_input = int(
                        jd_c3.number_input(
                            "Place Override (0 = Auto)",
                            min_value=0,
                            max_value=12,
                            value=int(place_overrides.get(jd_eid, 0)),
                            help="Override finishing place directly (1, 2, 3...). Set 0 for automatic.",
                            key=f"place_ov_num_{selected_meet.id}_{active_ev.code}_{jd_eid}",
                        )
                    )
                    jd_c4.markdown("<br>", unsafe_allow_html=True)
                    if jd_c4.button("Apply JD / Override", key=f"apply_jd_btn_{active_ev.code}_{jd_eid}"):
                        if jd_rank_input > 0:
                            jd_overrides[jd_eid] = jd_rank_input
                        else:
                            jd_overrides.pop(jd_eid, None)

                        if place_ov_input > 0:
                            place_overrides[jd_eid] = place_ov_input
                        else:
                            place_overrides.pop(jd_eid, None)

                        st.success(f"Updated JD / place override for {jd_target_row['athlete_name']}.")
                        st.rerun()

            if active_user and not selected_meet.is_locked and st.button("Save Event Results", type="primary"):
                saved_primary = 0
                queued_offline = 0
                for s in scored_all:
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
                        revision_reason=s.decision_note or f"Saved event {active_ev.code} from Meet Center",
                    )
                    if outcome == "saved_primary":
                        saved_primary += 1
                    else:
                        queued_offline += 1

                with repo.session() as session:
                    for s in scored_all:
                        if s.decision_note or s.is_jd:
                            repo.record_audit(
                                session,
                                action="meet.result_decision_note",
                                entity_type="entry",
                                entity_id=s.entry_id,
                                org_id=selected_meet.host_org_id,
                                actor_user_id=active_user.id,
                                details={
                                    "meet_id": selected_meet.id,
                                    "event_code": s.event_code,
                                    "is_jd": s.is_jd,
                                    "decision_note": s.decision_note,
                                },
                            )
                    session.commit()

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
            d_meta = decision_meta_by_entry.get(c.entry_id, {})
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
                is_jd=bool(d_meta.get("is_jd", False)),
                decision_note=str(d_meta.get("decision_note", "")),
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

        st.divider()
        st.subheader("Pre-Meet & Pool-Deck Sheets (Check-In, Heat Sheet & Timer Sheets)")

        checkin_roster: list[dict] = []
        for sr in season_rosters:
            ath = athlete_map.get(sr.athlete_id)
            tm = team_map.get(sr.team_id)
            if ath:
                checkin_roster.append(
                    {
                        "athlete_id": ath.id,
                        "team_code": tm.code if tm else "",
                        "first_name": ath.first_name,
                        "last_name": ath.last_name,
                        "preferred_name": ath.preferred_name,
                        "grad_year": ath.grad_year,
                        "gender": ath.gender,
                    }
                )

        db_entry_attrs = {
            e.id: {
                "heat_number": getattr(e, "heat_number", None),
                "lane_number": getattr(e, "lane_number", None),
                "is_exhibition": bool(e.is_exhibition),
            }
            for e in db_entries
        }
        athlete_grad_map = {a.id: a.grad_year for a in athletes}
        athlete_name_map = {a.id: f"{a.first_name} {a.last_name}" for a in athletes}

        rep_seeding_mode = st.session_state.get(
            f"seeding_mode_{selected_meet.id}", "Dual-Meet Odd / Even Lanes"
        )
        rep_odd_team = st.session_state.get(f"odd_lanes_team_{selected_meet.id}", selected_team)
        rep_even_team = st.session_state.get(f"even_lanes_team_{selected_meet.id}", None)
        rep_odd_id = rep_odd_team.id if rep_odd_team is not None else None
        rep_even_id = rep_even_team.id if rep_even_team is not None else None

        rep_c1, rep_c2, rep_c3 = st.columns(3)

        checkin_team_code = rep_c1.selectbox(
            "Check-In Team Filter",
            options=["All Teams"] + [t.code for t in teams],
            key=f"checkin_team_filter_{selected_meet.id}",
        )
        active_checkin_filter = None if checkin_team_code == "All Teams" else checkin_team_code

        rep_c1.download_button(
            "Download Swimmer Check-In (HTML)",
            data=render_swimmer_checkin_html(
                meet_name=selected_meet.name,
                meet_date=selected_meet.meet_date,
                roster_rows=checkin_roster,
                candidates=candidates,
                team_code_filter=active_checkin_filter,
            ),
            file_name=f"meet_{selected_meet.id}_checkin.html",
            mime="text/html",
        )
        rep_c1.download_button(
            "Download Swimmer Check-In (CSV)",
            data=render_swimmer_checkin_csv(
                roster_rows=checkin_roster,
                candidates=candidates,
                team_code_filter=active_checkin_filter,
            ),
            file_name=f"meet_{selected_meet.id}_checkin.csv",
            mime="text/csv",
        )

        rep_c2.markdown("**Meet Program (Heat Sheet)**")
        rep_c2.caption("2-column portrait heat sheet with heats, lanes, relay legs & seed times.")
        rep_c2.download_button(
            "Download Meet Program / Heat Sheet (HTML)",
            data=render_meet_program_html(
                meet_name=selected_meet.name,
                meet_date=selected_meet.meet_date,
                course_label=selected_meet.course.value,
                lane_count=selected_meet.lane_count,
                candidates=candidates,
                athlete_grad_map=athlete_grad_map,
                athlete_name_map=athlete_name_map,
                seeding_mode=rep_seeding_mode,
                odd_team_id=rep_odd_id,
                even_team_id=rep_even_id,
                heat_lane_overrides=heat_lane_overrides,
                db_entry_attrs=db_entry_attrs,
            ),
            file_name=f"meet_{selected_meet.id}_program_heat_sheet.html",
            mime="text/html",
        )

        timer_version_label = rep_c3.selectbox(
            "Timer Sheet Version",
            options=[
                "By Lane (1 Sheet per Lane)",
                "By Event & Heat (All Lanes)",
                "Clipboard Fill-In (Compact)",
            ],
            key=f"timer_sheet_version_{selected_meet.id}",
        )
        timer_ver_key = (
            "by_lane"
            if timer_version_label.startswith("By Lane")
            else "by_event"
            if timer_version_label.startswith("By Event")
            else "clipboard"
        )
        rep_c3.download_button(
            "Download Timer Sheets (HTML)",
            data=render_timer_sheets_html(
                meet_name=selected_meet.name,
                meet_date=selected_meet.meet_date,
                lane_count=selected_meet.lane_count,
                candidates=candidates,
                version=timer_ver_key,
                seeding_mode=rep_seeding_mode,
                odd_team_id=rep_odd_id,
                even_team_id=rep_even_id,
                heat_lane_overrides=heat_lane_overrides,
                db_entry_attrs=db_entry_attrs,
            ),
            file_name=f"meet_{selected_meet.id}_timer_sheets_{timer_ver_key}.html",
            mime="text/html",
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
