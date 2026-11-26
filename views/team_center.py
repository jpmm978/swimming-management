from __future__ import annotations

from sqlalchemy import select
import streamlit as st

from db.repository import SwimRepository
from db.schema import Organization, Season, Team, User
from importers.roster_importer import parse_hytek_sdif_roster, parse_roster_csv


def render_team_center_view(
    repo: SwimRepository,
    *,
    active_user: User | None,
    selected_org: Organization | None,
    selected_team: Team | None,
    selected_season: Season | None,
) -> None:
    st.markdown("## Team Center")
    if not (selected_org and selected_team and selected_season):
        st.warning("Select an Organization, Team, and Season in the top context bar.")
        return

    st.caption(
        f"**{selected_team.name} ({selected_team.code})** · "
        f"Division: `{selected_team.division_label or 'Unassigned'}` · "
        f"Season: **{selected_season.label}**"
    )

    if active_user is not None:
        with st.expander("Create New Team", expanded=False):
            with st.form("team_center_create_team"):
                t1, t2, t3 = st.columns([2, 1, 1.2])
                new_team_name = t1.text_input("Team Name", placeholder="e.g., Opposing High School")
                new_team_code = t2.text_input("Code / Abbr", placeholder="e.g., OPP", max_chars=10)
                new_division = t3.text_input("Division", value="Division 1")
                if st.form_submit_button("Create Team", type="primary"):
                    try:
                        created = repo.create_team(
                            actor_user_id=active_user.id,
                            org_id=selected_org.id,
                            name=new_team_name,
                            code=new_team_code,
                            division_label=new_division or None,
                        )
                        st.session_state["selected_team_id"] = created.id
                        st.success(f"Created team {created.name} ({created.code}).")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not create team: {exc}")

    tab_roster, tab_import, tab_rollover = st.tabs(
        ["Active Roster & Add", "Import CSV / HY-TEK", "Season Rollover & Export"]
    )

    with tab_roster:
        roster = repo.list_active_roster(selected_team.id, selected_season.id)
        eligible_count = sum(1 for r in roster if r["eligibility_verified"])
        m1, m2, m3 = st.columns(3)
        m1.metric("Active Athletes", len(roster))
        m2.metric("Eligibility Verified", eligible_count)
        m3.metric("Pending Eligibility", len(roster) - eligible_count)

        if roster:
            st.dataframe(
                [
                    {
                        "First Name": r["first_name"],
                        "Last Name": r["last_name"],
                        "Preferred": r["preferred_name"] or "",
                        "Grad Year": r["grad_year"] or "",
                        "Gender": r["gender"] or "",
                        "Eligibility Verified": r["eligibility_verified"],
                    }
                    for r in roster
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No active athletes on this season roster yet.")

        if active_user is not None:
            with st.expander("Add Single Athlete", expanded=False):
                with st.form("team_center_add_athlete"):
                    c1, c2, c3 = st.columns(3)
                    first_name = c1.text_input("First Name")
                    last_name = c2.text_input("Last Name")
                    preferred_name = c3.text_input("Preferred Name (Optional)")
                    c4, c5, c6 = st.columns(3)
                    grad_year = c4.number_input("Graduation Year", min_value=2025, max_value=2035, value=2028)
                    gender = c5.selectbox("Category / Gender", options=["", "M", "F", "X"])
                    eligible = c6.checkbox("Eligibility Verified", value=True)
                    if st.form_submit_button("Add Athlete", type="primary"):
                        if not first_name.strip() or not last_name.strip():
                            st.error("First and last name are required.")
                        else:
                            try:
                                athlete = repo.add_athlete_to_roster(
                                    actor_user_id=active_user.id,
                                    org_id=selected_org.id,
                                    team_id=selected_team.id,
                                    season_id=selected_season.id,
                                    first_name=first_name,
                                    last_name=last_name,
                                    preferred_name=preferred_name or None,
                                    grad_year=int(grad_year),
                                    gender=gender or None,
                                    eligibility_verified=eligible,
                                )
                                st.success(f"Added {athlete.first_name} {athlete.last_name}.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Could not add athlete: {exc}")

            if roster:
                with st.expander("Edit Swimmer Details", expanded=False):
                    roster_by_id = {int(r["athlete_id"]): r for r in roster}
                    selected_edit_id = st.selectbox(
                        "Select Swimmer to Edit",
                        options=[int(r["athlete_id"]) for r in roster],
                        format_func=lambda aid: (
                            f"{roster_by_id[aid]['last_name']}, {roster_by_id[aid]['first_name']} "
                            f"({roster_by_id[aid].get('grad_year') or '—'} · {roster_by_id[aid].get('gender') or '?'})"
                        ),
                        key=f"edit_swimmer_sel_{selected_team.id}_{selected_season.id}",
                    )
                    cur_row = roster_by_id[selected_edit_id]

                    ec1, ec2, ec3 = st.columns([1.4, 1.4, 1.2])
                    ed_first = ec1.text_input(
                        "First Name",
                        value=cur_row.get("first_name") or "",
                        key=f"ed_fname_{selected_edit_id}",
                    )
                    ed_last = ec2.text_input(
                        "Last Name",
                        value=cur_row.get("last_name") or "",
                        key=f"ed_lname_{selected_edit_id}",
                    )
                    ed_pref = ec3.text_input(
                        "Preferred Name",
                        value=cur_row.get("preferred_name") or "",
                        key=f"ed_pref_{selected_edit_id}",
                    )

                    ec4, ec5, ec6, ec7 = st.columns([1.0, 1.0, 1.1, 1.1])
                    ed_grad = ec4.number_input(
                        "Grad Year",
                        min_value=2020,
                        max_value=2035,
                        value=int(cur_row["grad_year"]) if cur_row.get("grad_year") else 2028,
                        key=f"ed_grad_{selected_edit_id}",
                    )
                    gender_opts = ["", "M", "F", "X"]
                    cur_gender = cur_row.get("gender") or ""
                    ed_gender = ec5.selectbox(
                        "Gender",
                        options=gender_opts,
                        index=gender_opts.index(cur_gender) if cur_gender in gender_opts else 0,
                        key=f"ed_gender_{selected_edit_id}",
                    )
                    ed_elig = ec6.checkbox(
                        "Eligibility Verified",
                        value=bool(cur_row.get("eligibility_verified", False)),
                        key=f"ed_elig_{selected_edit_id}",
                    )
                    status_opts = ["active", "inactive", "ineligible"]
                    ed_status = ec7.selectbox(
                        "Roster Status",
                        options=status_opts,
                        index=0,
                        key=f"ed_status_{selected_edit_id}",
                    )

                    if st.button("Save Swimmer Changes", type="primary", key=f"save_swimmer_edit_{selected_edit_id}"):
                        try:
                            repo.update_athlete(
                                actor_user_id=active_user.id,
                                org_id=selected_org.id,
                                athlete_id=selected_edit_id,
                                first_name=ed_first,
                                last_name=ed_last,
                                grad_year=int(ed_grad),
                                gender=ed_gender or None,
                                preferred_name=ed_pref or None,
                                team_id=selected_team.id,
                                season_id=selected_season.id,
                                eligibility_verified=ed_elig,
                                roster_status=ed_status,
                            )
                            st.success(f"Updated {ed_first} {ed_last}.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not update swimmer: {exc}")

    with tab_import:
        st.markdown("Upload a CSV roster (`first_name, last_name, grad_year, gender, eligibility_verified`) or HY-TEK `.sd3` file.")
        source_format = st.radio("Import Format", options=["CSV Roster", "HY-TEK SDIF (.sd3)"], horizontal=True)
        uploaded = st.file_uploader("Select Roster File", type=["csv", "sd3", "txt"])

        if uploaded is not None:
            raw_text = uploaded.getvalue().decode("utf-8", errors="replace")
            preview = (
                parse_roster_csv(raw_text)
                if source_format == "CSV Roster"
                else parse_hytek_sdif_roster(raw_text)
            )
            if preview.errors:
                st.warning(f"Found {len(preview.errors)} validation issue(s):")
                for err in preview.errors[:15]:
                    st.write(f"- {err}")

            if preview.valid_rows:
                st.success(f"Validated {len(preview.valid_rows)} athlete row(s) ready for import.")
                st.dataframe(
                    [
                        {
                            "row": r.row_number,
                            "first_name": r.first_name,
                            "last_name": r.last_name,
                            "grad_year": r.grad_year,
                            "gender": r.gender,
                            "eligible": r.eligibility_verified,
                        }
                        for r in preview.valid_rows
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
                if active_user and st.button("Commit Roster Import", type="primary"):
                    try:
                        summary = repo.bulk_import_roster(
                            actor_user_id=active_user.id,
                            org_id=selected_org.id,
                            team_id=selected_team.id,
                            season_id=selected_season.id,
                            rows=preview.valid_rows,
                        )
                        st.success(
                            f"Import complete — Created: {summary['created_athletes']}, "
                            f"Matched existing: {summary['matched_athletes']}, "
                            f"Added to season: {summary['added_rosters']}, "
                            f"Updated: {summary['updated_rosters']}."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Import failed: {exc}")

    with tab_rollover:
        st.subheader("Export Current Roster")
        csv_data = repo.export_roster_csv(selected_team.id, selected_season.id)
        st.download_button(
            label=f"Download {selected_team.code} {selected_season.label} Roster CSV",
            data=csv_data,
            file_name=f"{selected_team.code.lower()}_{selected_season.label}_roster.csv",
            mime="text/csv",
        )

        st.divider()
        st.subheader("Season Rollover")
        with repo.session() as session:
            all_seasons = list(
                session.scalars(
                    select(Season).where(Season.org_id == selected_org.id).order_by(Season.id.desc())
                ).all()
            )
        other_seasons = [s for s in all_seasons if s.id != selected_season.id]
        if not other_seasons:
            st.info("Create a second season in Administration Center to roll over returning athletes.")
        elif active_user:
            target_season = st.selectbox(
                "Target Season",
                options=other_seasons,
                format_func=lambda s: s.label,
            )
            cutoff_year = st.number_input(
                "Skip Graduated Class Year (<=)",
                min_value=2025,
                max_value=2035,
                value=2027,
            )
            if st.button("Promote Returning Athletes"):
                try:
                    res = repo.rollover_season_roster(
                        actor_user_id=active_user.id,
                        org_id=selected_org.id,
                        team_id=selected_team.id,
                        source_season_id=selected_season.id,
                        target_season_id=target_season.id,
                        graduating_cutoff_year=int(cutoff_year),
                    )
                    st.success(
                        f"Rollover complete — Promoted: {res['promoted']}, "
                        f"Graduated skipped: {res['graduated_skipped']}, "
                        f"Already enrolled: {res['already_enrolled']}."
                    )
                except Exception as exc:
                    st.error(f"Rollover failed: {exc}")
