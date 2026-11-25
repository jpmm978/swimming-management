from __future__ import annotations

from datetime import date
import os
from typing import Any

from sqlalchemy import func, select
import streamlit as st

from core_logic.deck_sync import DeckOfflineOutbox
from db.repository import (
    ADMIN_ROLES,
    COACH_ROLES,
    MEET_OP_ROLES,
    OFFICIAL_ROLES,
    SwimRepository,
    build_engine,
    create_session_factory,
)
from db.schema import (
    Athlete,
    AuditLog,
    Meet,
    Organization,
    RoleAssignment,
    RoleCode,
    Season,
    SeasonRoster,
    Team,
    User,
)
from views.coach_center import render_coach_center_view
from views.league_center import render_league_center_view
from views.live_center import render_live_center_view
from views.meet_center import render_meet_center_view
from views.officials_center import render_officials_center_view
from views.performance_center import render_performance_center_view
from views.team_center import render_team_center_view

NAV_CENTERS: list[tuple[str, str]] = [
    ("Dashboard", "Role-specific overview & quick actions"),
    ("Team Center", "Athletes, rosters, eligibility & team records"),
    ("Meet Center", "Meet setup, entries, seeding, timing, diving & scoring"),
    ("Coach Center", "Practices, lineups & projected meet scoring"),
    ("Performance Center", "Verified times, personal bests & MIAA standards"),
    ("Officials Center", "Rules versions, DQs, verification & sign-off"),
    ("Live Center", "Read-only public meet results & scoreboards"),
    ("League Center", "Multi-team schedules, standings & championships"),
    ("Administration Center", "Organizations, users, roles & audit logs"),
]


@st.cache_resource
def get_repository() -> SwimRepository:
    db_url = os.environ.get("DATABASE_URL") or "sqlite:///./miaa_swim_platform.db"
    engine = build_engine(db_url)
    repo = SwimRepository(create_session_factory(engine))
    repo.init_schema(engine)
    repo.seed_initial_program()
    return repo


@st.cache_resource
def get_deck_outbox() -> DeckOfflineOutbox:
    outbox_path = os.environ.get("DECK_OUTBOX_DB") or "./deck_local_outbox.db"
    return DeckOfflineOutbox(outbox_path)


def apply_theme() -> None:
    st.markdown(
        """
        <style>
            .main .block-container {
                padding-top: 1.25rem;
                padding-bottom: 2rem;
                max-width: 1280px;
            }
            .swim-banner {
                background: linear-gradient(90deg, #0b2545 0%, #134074 100%);
                color: #ffffff;
                padding: 0.85rem 1.2rem;
                border-radius: 10px;
                margin-bottom: 1rem;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            .swim-banner-title {
                font-size: 1.15rem;
                font-weight: 700;
                letter-spacing: 0.03em;
            }
            .swim-banner-meta {
                font-size: 0.88rem;
                opacity: 0.92;
            }
            .status-pill {
                display: inline-block;
                padding: 0.2rem 0.55rem;
                border-radius: 999px;
                font-size: 0.78rem;
                font-weight: 600;
                background-color: #e6f4ea;
                color: #137333;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def get_allowed_centers(roles: set[RoleCode]) -> list[str]:
    if roles & ADMIN_ROLES:
        return [name for name, _ in NAV_CENTERS]

    allowed = {"Dashboard", "Live Center"}
    if roles & COACH_ROLES:
        allowed |= {"Team Center", "Coach Center", "Performance Center", "Meet Center"}
    if roles & MEET_OP_ROLES:
        allowed |= {"Team Center", "Meet Center", "Officials Center", "Performance Center"}
    if roles & OFFICIAL_ROLES:
        allowed |= {"Meet Center", "Officials Center", "Performance Center"}
    if RoleCode.ATHLETE in roles or RoleCode.PARENT in roles:
        allowed |= {"Team Center", "Performance Center"}

    return [name for name, _ in NAV_CENTERS if name in allowed]


def load_context_options(repo: SwimRepository) -> dict[str, Any]:
    with repo.session() as session:
        orgs = session.scalars(select(Organization).where(Organization.is_active.is_(True)).order_by(Organization.name)).all()
        teams = session.scalars(select(Team).where(Team.is_active.is_(True)).order_by(Team.code)).all()
        seasons = session.scalars(select(Season).order_by(Season.id.desc())).all()
        meets = session.scalars(select(Meet).order_by(Meet.meet_date.desc())).all()
        users = session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id)).all()

        user_role_map: dict[int, list[RoleCode]] = {u.id: [] for u in users}
        for ra in session.scalars(select(RoleAssignment)).all():
            if ra.user_id in user_role_map:
                user_role_map[ra.user_id].append(ra.role)

    return {
        "orgs": orgs,
        "teams": teams,
        "seasons": seasons,
        "meets": meets,
        "users": users,
        "user_role_map": user_role_map,
    }


def render_dashboard(
    repo: SwimRepository,
    *,
    active_user: User | None,
    roles: set[RoleCode],
    selected_team: Team | None,
    selected_season: Season | None,
    selected_meet: Meet | None,
) -> None:
    primary_role = (
        "Platform / Org Admin"
        if (roles & ADMIN_ROLES)
        else "Coach"
        if (roles & COACH_ROLES)
        else "Meet Director / Scorer"
        if (roles & MEET_OP_ROLES)
        else "Official / Referee"
        if (roles & OFFICIAL_ROLES)
        else "Spectator / Member"
    )

    st.markdown(f"## {primary_role} Dashboard")
    if selected_team and selected_season:
        div_text = f" · {selected_team.division_label}" if selected_team.division_label else ""
        st.caption(f"{selected_team.name} ({selected_team.code}){div_text} · Season {selected_season.label}")

    with repo.session() as session:
        roster_count = 0
        if selected_team and selected_season:
            roster_count = session.scalar(
                select(func.count(SeasonRoster.id)).where(
                    SeasonRoster.team_id == selected_team.id,
                    SeasonRoster.season_id == selected_season.id,
                    SeasonRoster.status == "active",
                )
            ) or 0

        upcoming_meets_count = session.scalar(
            select(func.count(Meet.id)).where(Meet.meet_date >= date.today())
        ) or 0
        total_athletes = session.scalar(select(func.count(Athlete.id))) or 0

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Active Roster", roster_count)
    with c2:
        st.metric("Upcoming Meets", upcoming_meets_count)
    with c3:
        st.metric("Tracked Athletes", total_athletes)
    with c4:
        st.metric("2026-27 Season", selected_season.label if selected_season else "—")

    st.divider()
    left_col, right_col = st.columns([1.4, 1])

    with left_col:
        st.subheader("Active Meet & Program Context")
        if selected_meet:
            st.info(
                f"**Active Meet:** {selected_meet.name} ({selected_meet.meet_date}) · "
                f"Course: {selected_meet.course.value} · Lanes: {selected_meet.lane_count} · "
                f"Status: `{selected_meet.status.value}`"
            )
        else:
            st.info("No active meet selected yet. Create or select a meet in **Meet Center**.")

    with right_col:
        st.subheader("Quick Actions")
        if st.button("Open Meet Center", use_container_width=True, type="primary"):
            st.session_state["active_center"] = "Meet Center"
            st.rerun()
        if st.button("Manage Roster in Team Center", use_container_width=True):
            st.session_state["active_center"] = "Team Center"
            st.rerun()
        if active_user:
            st.caption(f"Signed in as `{active_user.display_name}` ({active_user.email})")


def render_admin_center(repo: SwimRepository, selected_org: Organization | None) -> None:
    st.markdown("## Administration Center")
    with repo.session() as session:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(35)
        if selected_org:
            stmt = stmt.where(AuditLog.org_id == selected_org.id)
        logs = session.scalars(stmt).all()

    rows = [
        {
            "id": log.id,
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "actor_user_id": log.actor_user_id,
            "created_at": log.created_at.isoformat() if log.created_at else "",
        }
        for log in logs
    ]
    st.subheader("Recent Audit Log")
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No audit records found.")


def main() -> None:
    st.set_page_config(
        page_title="Swim Management Platform",
        page_icon="🏊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_theme()

    repo = get_repository()
    outbox = get_deck_outbox()
    ctx = load_context_options(repo)

    orgs: list[Organization] = ctx["orgs"]
    teams: list[Team] = ctx["teams"]
    seasons: list[Season] = ctx["seasons"]
    meets: list[Meet] = ctx["meets"]
    users: list[User] = ctx["users"]
    user_role_map: dict[int, list[RoleCode]] = ctx["user_role_map"]

    with st.sidebar:
        st.markdown("### SWIM MANAGEMENT")
        user_options = {u.id: f"{u.display_name} ({u.email})" for u in users}
        default_user_id = users[0].id if users else None
        active_user_id = st.selectbox(
            "Active User / Role Context",
            options=list(user_options.keys()) if user_options else [None],
            format_func=lambda uid: user_options.get(uid, "Guest Spectator"),
            index=0 if default_user_id else 0,
        )
        active_user = next((u for u in users if u.id == active_user_id), None)

        st.markdown("**WORKSPACE**")
        if "active_center" not in st.session_state:
            st.session_state["active_center"] = "Dashboard"

        selected_org_id = st.session_state.get("selected_org_id", orgs[0].id if orgs else None)
        selected_team_id = st.session_state.get("selected_team_id", teams[0].id if teams else None)
        selected_season_id = st.session_state.get("selected_season_id", seasons[0].id if seasons else None)

        with repo.session() as session:
            active_roles = repo.get_user_roles(
                session,
                active_user.id if active_user else None,
                org_id=selected_org_id,
                team_id=selected_team_id,
                season_id=selected_season_id,
            )

        allowed_centers = get_allowed_centers(active_roles)
        if st.session_state["active_center"] not in allowed_centers:
            st.session_state["active_center"] = allowed_centers[0]

        for center_name, _ in NAV_CENTERS:
            if center_name in allowed_centers:
                is_current = st.session_state["active_center"] == center_name
                if st.button(
                    center_name,
                    key=f"nav_{center_name}",
                    use_container_width=True,
                    type="primary" if is_current else "secondary",
                ):
                    st.session_state["active_center"] = center_name
                    st.rerun()

        if active_user and active_user.id in user_role_map:
            role_labels = ", ".join(r.value for r in user_role_map[active_user.id]) or "spectator"
            st.caption(f"Roles: `{role_labels}`")

    c_org, c_team, c_season, c_meet = st.columns([1.4, 1.3, 1.0, 1.5])

    selected_org = next((o for o in orgs if o.id == st.session_state.get("selected_org_id")), orgs[0] if orgs else None)
    with c_org:
        if orgs:
            org_choice = st.selectbox(
                "Organization",
                options=orgs,
                index=orgs.index(selected_org) if selected_org in orgs else 0,
                format_func=lambda o: o.name,
            )
            st.session_state["selected_org_id"] = org_choice.id
            selected_org = org_choice

    org_teams = [t for t in teams if selected_org is None or t.org_id == selected_org.id] or teams
    selected_team = next((t for t in org_teams if t.id == st.session_state.get("selected_team_id")), org_teams[0] if org_teams else None)
    with c_team:
        if org_teams:
            team_choice = st.selectbox(
                "Team",
                options=org_teams,
                index=org_teams.index(selected_team) if selected_team in org_teams else 0,
                format_func=lambda t: f"{t.name} ({t.code})",
            )
            st.session_state["selected_team_id"] = team_choice.id
            selected_team = team_choice

    org_seasons = [s for s in seasons if selected_org is None or s.org_id == selected_org.id] or seasons
    selected_season = next((s for s in org_seasons if s.id == st.session_state.get("selected_season_id")), org_seasons[0] if org_seasons else None)
    with c_season:
        if org_seasons:
            season_choice = st.selectbox(
                "Season",
                options=org_seasons,
                index=org_seasons.index(selected_season) if selected_season in org_seasons else 0,
                format_func=lambda s: s.label,
            )
            st.session_state["selected_season_id"] = season_choice.id
            selected_season = season_choice

    season_meets = [m for m in meets if selected_season is None or m.season_id == selected_season.id]
    selected_meet = next((m for m in season_meets if m.id == st.session_state.get("selected_meet_id")), season_meets[0] if season_meets else None)
    with c_meet:
        meet_options: list[Meet | None] = [None] + season_meets
        current_idx = meet_options.index(selected_meet) if selected_meet in meet_options else 0
        meet_choice = st.selectbox(
            "Active Meet",
            options=meet_options,
            index=current_idx,
            format_func=lambda m: "None selected" if m is None else f"{m.name} ({m.meet_date})",
        )
        st.session_state["selected_meet_id"] = meet_choice.id if meet_choice else None
        selected_meet = meet_choice

    pending_count = len(outbox.list_items(statuses=("PENDING",)))
    sync_pill = f"Queue: {pending_count} pending" if pending_count else "DB & Outbox Ready"

    st.markdown(
        f"""
        <div class="swim-banner">
            <div class="swim-banner-title">{st.session_state["active_center"]}</div>
            <div class="swim-banner-meta">
                {selected_team.code if selected_team else "NO TEAM"} ·
                {selected_season.label if selected_season else "NO SEASON"} ·
                <span class="status-pill">{sync_pill}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    current_center = st.session_state["active_center"]
    if current_center == "Dashboard":
        render_dashboard(
            repo,
            active_user=active_user,
            roles=active_roles,
            selected_team=selected_team,
            selected_season=selected_season,
            selected_meet=selected_meet,
        )
    elif current_center == "Team Center":
        render_team_center_view(
            repo,
            active_user=active_user,
            selected_org=selected_org,
            selected_team=selected_team,
            selected_season=selected_season,
        )
    elif current_center == "Meet Center":
        render_meet_center_view(
            repo,
            outbox=outbox,
            active_user=active_user,
            selected_org=selected_org,
            selected_season=selected_season,
            selected_team=selected_team,
            selected_meet=selected_meet,
        )
    elif current_center == "Coach Center":
        render_coach_center_view(
            repo,
            active_user=active_user,
            selected_org=selected_org,
            selected_team=selected_team,
            selected_season=selected_season,
            selected_meet=selected_meet,
        )
    elif current_center == "Performance Center":
        render_performance_center_view(
            repo,
            active_user=active_user,
            selected_org=selected_org,
            selected_team=selected_team,
            selected_season=selected_season,
        )
    elif current_center == "Officials Center":
        render_officials_center_view(
            repo,
            active_user=active_user,
            selected_org=selected_org,
            selected_season=selected_season,
            selected_meet=selected_meet,
        )
    elif current_center == "Live Center":
        render_live_center_view(
            repo,
            active_user=active_user,
            active_roles=active_roles,
            selected_meet=selected_meet,
        )
    elif current_center == "League Center":
        render_league_center_view(
            repo,
            active_user=active_user,
            active_roles=active_roles,
            selected_org=selected_org,
            selected_season=selected_season,
        )
    elif current_center == "Administration Center":
        render_admin_center(repo, selected_org)


if __name__ == "__main__":
    main()
