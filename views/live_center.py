from __future__ import annotations

from sqlalchemy import select
import streamlit as st

from core_logic.live_feed import build_live_meet_snapshot
from core_logic.meet_engine import MIAA_STANDARD_EVENTS
from db.repository import MEET_OP_ROLES, OFFICIAL_ROLES, SwimRepository
from db.schema import (
    Athlete,
    Entry,
    Meet,
    MeetPublicationSetting,
    NameVisibilityMode,
    OfficialResult,
    RoleCode,
    Team,
    User,
)


def _fetch_live_snapshot(repo: SwimRepository, meet: Meet):
    with repo.session() as session:
        pub = session.scalar(
            select(MeetPublicationSetting).where(MeetPublicationSetting.meet_id == meet.id)
        )
        is_public = pub.is_public if pub else True
        show_prelim = pub.show_preliminary_results if pub else True
        visibility = pub.name_visibility if pub else NameVisibilityMode.FULL_NAME

        stmt = (
            select(OfficialResult, Entry, Team, Athlete)
            .join(Entry, Entry.id == OfficialResult.entry_id)
            .join(Team, Team.id == Entry.team_id)
            .outerjoin(Athlete, Athlete.id == Entry.athlete_id)
            .where(OfficialResult.meet_id == meet.id)
        )
        raw_items = []
        for res, entry, team, athlete in session.execute(stmt).all():
            raw_items.append(
                {
                    "event_code": res.event_code,
                    "state": res.state.value,
                    "final_time_ms": res.final_time_ms,
                    "diving_score": res.diving_score,
                    "place": res.place,
                    "points_awarded": res.points_awarded,
                    "is_relay": entry.is_relay,
                    "is_exhibition": entry.is_exhibition,
                    "team_code": team.code,
                    "first_name": athlete.first_name if athlete else None,
                    "last_name": athlete.last_name if athlete else None,
                }
            )

    return build_live_meet_snapshot(
        meet_id=meet.id,
        meet_name=meet.name,
        meet_date=meet.meet_date.isoformat(),
        course=meet.course.value,
        lane_count=meet.lane_count,
        meet_status=meet.status.value,
        is_public=is_public,
        show_preliminary=show_prelim,
        name_visibility=visibility,
        raw_items=raw_items,
    )


def render_live_center_view(
    repo: SwimRepository,
    *,
    active_user: User | None,
    active_roles: set[RoleCode],
    selected_meet: Meet | None,
) -> None:
    st.markdown("## Live Center")
    if not selected_meet:
        st.info("Select an Active Meet in the top context bar to view live results.")
        return

    snapshot = _fetch_live_snapshot(repo, selected_meet)
    can_manage_pub = bool(active_roles & (MEET_OP_ROLES | OFFICIAL_ROLES))

    if not snapshot.is_public and not can_manage_pub:
        st.warning("This meet is currently restricted to authorized team and meet personnel.")
        return

    mode = st.radio(
        "Display Mode",
        options=["Spectator & Mobile View", "Facility Scoreboard / Lobby Display"],
        horizontal=True,
    )

    if mode == "Facility Scoreboard / Lobby Display":
        st.markdown(
            f"""
            <div style="background:#0b2545;color:#fff;padding:1.25rem 1.5rem;border-radius:12px;margin-bottom:1rem;">
              <div style="font-size:1.5rem;font-weight:800;">{snapshot.meet_name}</div>
              <div style="font-size:1rem;opacity:0.9;">
                {snapshot.meet_date} · {snapshot.course} ({snapshot.lane_count} Lanes) ·
                Events Scored: {snapshot.completed_events} / {snapshot.total_scheduled_events} ·
                Status: {snapshot.meet_status.upper()}
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Boys Team Score")
            if snapshot.boys_team_scores:
                for code, pts in sorted(snapshot.boys_team_scores.items(), key=lambda x: x[1], reverse=True):
                    st.metric(code, f"{pts:.1f}")
            else:
                st.write("Awaiting scored boys events.")
        with c2:
            st.subheader("Girls Team Score")
            if snapshot.girls_team_scores:
                for code, pts in sorted(snapshot.girls_team_scores.items(), key=lambda x: x[1], reverse=True):
                    st.metric(code, f"{pts:.1f}")
            else:
                st.write("Awaiting scored girls events.")

        st.divider()
        st.subheader("Latest Event Results")
        if snapshot.rows:
            st.dataframe(
                [
                    {
                        "Event": f"#{r.event_number} {r.event_name}",
                        "Place": r.place_display,
                        "Team": r.team_code,
                        "Competitor": r.competitor_display,
                        "Mark": r.mark_display,
                        "Points": r.points_display,
                        "Status": r.verification_badge,
                    }
                    for r in snapshot.rows[-18:]
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No results published yet.")
    else:
        st.caption(
            f"{snapshot.meet_name} · {snapshot.meet_date} · "
            f"Progress: {snapshot.completed_events}/{snapshot.total_scheduled_events} events"
        )
        s1, s2 = st.columns(2)
        s1.metric(
            "Boys Leader",
            max(snapshot.boys_team_scores.items(), key=lambda x: x[1])[0]
            if snapshot.boys_team_scores
            else "—",
            f"{max(snapshot.boys_team_scores.values()):.1f} pts" if snapshot.boys_team_scores else "0.0 pts",
        )
        s2.metric(
            "Girls Leader",
            max(snapshot.girls_team_scores.items(), key=lambda x: x[1])[0]
            if snapshot.girls_team_scores
            else "—",
            f"{max(snapshot.girls_team_scores.values()):.1f} pts" if snapshot.girls_team_scores else "0.0 pts",
        )

        ev_options = ["All Events"] + [f"#{e.event_number} {e.name}" for e in MIAA_STANDARD_EVENTS]
        chosen_ev = st.selectbox("Filter by Event", options=ev_options)
        filtered_rows = (
            snapshot.rows
            if chosen_ev == "All Events"
            else [r for r in snapshot.rows if f"#{r.event_number} {r.event_name}" == chosen_ev]
        )
        if filtered_rows:
            st.dataframe(
                [
                    {
                        "Event": f"#{r.event_number} {r.event_name}",
                        "Place": r.place_display,
                        "Team": r.team_code,
                        "Competitor": r.competitor_display,
                        "Result": r.mark_display,
                        "Pts": r.points_display,
                        "Verification": r.verification_badge,
                    }
                    for r in filtered_rows
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No published results match the current filter.")

    if can_manage_pub and active_user:
        with st.expander("Publication & Privacy Controls (Authorized Meet/Org Staff)", expanded=False):
            with repo.session() as session:
                pub = session.scalar(
                    select(MeetPublicationSetting).where(MeetPublicationSetting.meet_id == selected_meet.id)
                )
            with st.form("live_pub_settings_form"):
                is_pub = st.checkbox("Publicly Accessible Meet Page", value=pub.is_public if pub else True)
                show_pre = st.checkbox(
                    "Show Preliminary Results Before Referee Sign-Off",
                    value=pub.show_preliminary_results if pub else True,
                )
                vis_modes = [m.value for m in NameVisibilityMode]
                cur_vis = pub.name_visibility.value if pub else NameVisibilityMode.FULL_NAME.value
                vis_choice = st.selectbox(
                    "Athlete Name Display Policy",
                    options=vis_modes,
                    index=vis_modes.index(cur_vis),
                )
                if st.form_submit_button("Save Publication Settings"):
                    with repo.session() as session:
                        existing = session.scalar(
                            select(MeetPublicationSetting).where(
                                MeetPublicationSetting.meet_id == selected_meet.id
                            )
                        )
                        if existing is None:
                            session.add(
                                MeetPublicationSetting(
                                    meet_id=selected_meet.id,
                                    is_public=is_pub,
                                    show_preliminary_results=show_pre,
                                    name_visibility=NameVisibilityMode(vis_choice),
                                    updated_by_user_id=active_user.id,
                                )
                            )
                        else:
                            existing.is_public = is_pub
                            existing.show_preliminary_results = show_pre
                            existing.name_visibility = NameVisibilityMode(vis_choice)
                            existing.updated_by_user_id = active_user.id
                        repo.record_audit(
                            session,
                            action="live.publication_updated",
                            entity_type="meet",
                            entity_id=selected_meet.id,
                            org_id=selected_meet.host_org_id,
                            actor_user_id=active_user.id,
                            details={
                                "is_public": is_pub,
                                "show_preliminary": show_pre,
                                "name_visibility": vis_choice,
                            },
                        )
                        session.commit()
                    st.success("Live publication settings updated.")
                    st.rerun()
