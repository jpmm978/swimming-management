from __future__ import annotations

from sqlalchemy import select
import streamlit as st

from core_logic.meet_engine import EVENT_BY_CODE, MIAA_STANDARD_EVENTS, format_time_ms
from db.repository import SwimRepository
from db.schema import (
    Athlete,
    DisqualificationReport,
    DQStatus,
    Entry,
    Meet,
    OfficialResult,
    Organization,
    RuleProfile,
    RuleVersion,
    Season,
    Team,
    User,
)

COMMON_NFHS_DQ_CODES: list[str] = [
    "NFHS 8-1-1: False start",
    "NFHS 8-2-1: Non-simultaneous / one-hand touch (Fly/Breast)",
    "NFHS 8-2-3: Illegal underwater kick / pullout",
    "NFHS 8-2-4: Backstroke past vertical at turn / finish",
    "NFHS 8-3-4: Relay early takeoff",
    "NFHS 8-1-3: Did not finish / missed wall",
    "Other (Specify in description)",
]


def render_officials_center_view(
    repo: SwimRepository,
    *,
    active_user: User | None,
    selected_org: Organization | None,
    selected_season: Season | None,
    selected_meet: Meet | None,
) -> None:
    st.markdown("## Officials Center")

    tab_dq, tab_verify, tab_rules, tab_reopen = st.tabs(
        [
            "Digital DQ Slip & Review",
            "Event Verification Queue",
            "Versioned Rules Engine",
            "Controlled Meet Reopening",
        ]
    )

    with tab_rules:
        with repo.session() as session:
            profiles = list(session.scalars(select(RuleProfile)).all())
            versions = list(session.scalars(select(RuleVersion).order_by(RuleVersion.effective_from.desc())).all())
        for p in profiles:
            st.subheader(f"{p.name} ({p.governing_body})")
            for v in [ver for ver in versions if ver.rule_profile_id == p.id]:
                badge = "VERIFIED" if v.is_verified else "PROVISIONAL"
                st.markdown(f"**Version `{v.version_label}`** · Effective `{v.effective_from}` · `{badge}`")
                st.json(v.rules_payload)

    if not selected_meet:
        for tab in (tab_dq, tab_verify, tab_reopen):
            with tab:
                st.info("Select an Active Meet in the top context bar first.")
        return

    with repo.session() as session:
        entries = list(session.scalars(select(Entry).where(Entry.meet_id == selected_meet.id)).all())
        teams = {t.id: t for t in session.scalars(select(Team)).all()}
        athletes = {a.id: a for a in session.scalars(select(Athlete)).all()}
        dq_rows = list(
            session.scalars(
                select(DisqualificationReport)
                .where(DisqualificationReport.meet_id == selected_meet.id)
                .order_by(DisqualificationReport.id.desc())
            ).all()
        )
        results = list(
            session.scalars(select(OfficialResult).where(OfficialResult.meet_id == selected_meet.id)).all()
        )

    entry_labels: dict[int, str] = {}
    entry_event_map: dict[int, str] = {}
    for e in entries:
        t = teams.get(e.team_id)
        a = athletes.get(e.athlete_id) if e.athlete_id else None
        ev = EVENT_BY_CODE.get(e.event_code)
        who = f"{a.last_name}, {a.first_name}" if a else f"{t.code if t else 'Team'} Relay"
        entry_labels[e.id] = f"#{ev.event_number if ev else '?'} {e.event_code} · {t.code if t else ''} — {who}"
        entry_event_map[e.id] = e.event_code

    with tab_dq:
        if active_user and not selected_meet.is_locked and entries:
            with st.expander("Submit Digital Disqualification Slip", expanded=True):
                with st.form("submit_dq_slip_form"):
                    selected_entry_id = st.selectbox(
                        "Entry / Swimmer / Relay",
                        options=list(entry_labels.keys()),
                        format_func=lambda eid: entry_labels[eid],
                    )
                    c1, c2, c3 = st.columns([1, 1, 2])
                    heat_num = c1.number_input("Heat", min_value=1, max_value=20, value=1)
                    lane_num = c2.number_input("Lane", min_value=1, max_value=8, value=3)
                    rule_choice = c3.selectbox("NFHS Rule Reference", options=COMMON_NFHS_DQ_CODES)
                    desc = st.text_area("Violation Observation", placeholder="Describe observed infraction clearly...")
                    if st.form_submit_button("Submit DQ for Referee Review", type="primary"):
                        if not desc.strip():
                            st.error("Violation observation text is required.")
                        else:
                            try:
                                repo.submit_dq_report(
                                    actor_user_id=active_user.id,
                                    meet_id=selected_meet.id,
                                    entry_id=selected_entry_id,
                                    event_code=entry_event_map[selected_entry_id],
                                    rule_code=rule_choice,
                                    description=desc,
                                    heat_number=int(heat_num),
                                    lane_number=int(lane_num),
                                )
                                st.success("DQ slip submitted to Referee queue.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Could not submit DQ: {exc}")

        st.subheader("Disqualification Log & Referee Queue")
        if not dq_rows:
            st.info("No disqualifications reported for this meet.")
        else:
            for dq in dq_rows:
                label = entry_labels.get(dq.entry_id, f"Entry #{dq.entry_id}")
                with st.container(border=True):
                    st.markdown(
                        f"**DQ #{dq.id}** · `{dq.status.value.upper()}` · {label} "
                        f"(Heat {dq.heat_number or '-'}, Lane {dq.lane_number or '-'})"
                    )
                    st.write(f"**Rule:** {dq.rule_code} — {dq.description}")
                    if dq.status == DQStatus.SUBMITTED and active_user and not selected_meet.is_locked:
                        r_notes = st.text_input("Referee Review Notes", key=f"dq_note_{dq.id}")
                        b1, b2 = st.columns(2)
                        if b1.button("Approve DQ (Apply to Results)", key=f"approve_dq_{dq.id}", type="primary"):
                            repo.review_dq_report(
                                referee_user_id=active_user.id,
                                dq_id=dq.id,
                                approve=True,
                                review_notes=r_notes,
                            )
                            st.rerun()
                        if b2.button("Reject DQ (Overturn)", key=f"reject_dq_{dq.id}"):
                            repo.review_dq_report(
                                referee_user_id=active_user.id,
                                dq_id=dq.id,
                                approve=False,
                                review_notes=r_notes,
                            )
                            st.rerun()

    with tab_verify:
        st.subheader("Event Verification Queue")
        by_event: dict[str, list[OfficialResult]] = {}
        for r in results:
            by_event.setdefault(r.event_code, []).append(r)

        if not by_event:
            st.info("No recorded results in this meet yet.")
        else:
            for ev in MIAA_STANDARD_EVENTS:
                ev_res = by_event.get(ev.code)
                if not ev_res:
                    continue
                prelim_count = sum(1 for r in ev_res if r.state.value == "preliminary")
                official_count = sum(1 for r in ev_res if r.state.value == "official")
                with st.expander(
                    f"#{ev.event_number} {ev.name} — Official: {official_count}, Preliminary: {prelim_count}",
                    expanded=prelim_count > 0,
                ):
                    st.dataframe(
                        [
                            {
                                "Entry": entry_labels.get(r.entry_id, str(r.entry_id)),
                                "Mark": str(r.diving_score) if r.diving_score is not None else format_time_ms(r.final_time_ms),
                                "Place": r.place or "-",
                                "Points": f"{r.points_awarded:.2f}",
                                "State": r.state.value.upper(),
                            }
                            for r in ev_res
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )
                    if prelim_count > 0 and active_user and not selected_meet.is_locked:
                        if st.button(f"Verify All Results for {ev.name}", key=f"verify_{ev.code}", type="primary"):
                            count = repo.verify_event_results(
                                official_user_id=active_user.id,
                                meet_id=selected_meet.id,
                                event_code=ev.code,
                            )
                            st.success(f"Verified {count} result(s) as OFFICIAL.")
                            st.rerun()

    with tab_reopen:
        st.subheader("Meet Lock & Controlled Reopening")
        st.write(f"Current Meet Status: **`{selected_meet.status.value}`** · Locked: **`{selected_meet.is_locked}`**")
        if selected_meet.is_locked and active_user:
            reason = st.text_input(
                "Mandatory Audit Reason for Reopening",
                placeholder="e.g., Referee approved timing correction on Event 14 Heat 2",
            )
            if st.button("Reopen Locked Meet", type="primary"):
                try:
                    repo.reopen_locked_meet(
                        actor_user_id=active_user.id,
                        meet_id=selected_meet.id,
                        reopen_reason=reason,
                    )
                    st.success("Meet reopened and logged in audit trail.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not reopen meet: {exc}")
        elif not selected_meet.is_locked:
            st.info("This meet is currently open for live operations and verification.")
