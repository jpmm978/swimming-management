from __future__ import annotations

from sqlalchemy import text
import streamlit as st

from core_logic.deck_sync import DeckOfflineOutbox
from core_logic.meet_engine import format_time_ms
from db.repository import SwimRepository
from db.schema import Meet, User


def check_primary_db_online(repo: SwimRepository) -> bool:
    try:
        with repo.session() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def render_deck_sync_bar(
    repo: SwimRepository,
    outbox: DeckOfflineOutbox,
    *,
    active_user: User | None,
    selected_meet: Meet | None,
) -> bool:
    is_online = check_primary_db_online(repo)
    meet_id = selected_meet.id if selected_meet else None
    pending_items = outbox.list_items(meet_id=meet_id, statuses=("PENDING",))
    conflict_items = outbox.list_items(meet_id=meet_id, statuses=("CONFLICT",))

    c1, c2, c3, c4 = st.columns([1.4, 1.1, 1.1, 1.4])
    with c1:
        if is_online:
            st.success("Primary DB: ONLINE")
        else:
            st.warning("Primary DB: OFFLINE (Saving to Local Deck Outbox)")
    with c2:
        st.metric("Queued Writes", len(pending_items))
    with c3:
        st.metric("Sync Conflicts", len(conflict_items))
    with c4:
        if pending_items and is_online:
            if st.button("Sync Queued Results Now", type="primary", use_container_width=True):
                report = outbox.sync_pending_to_repository(repo, meet_id=meet_id)
                st.toast(f"Synced: {report.synced_count}, Conflicts: {report.conflict_count}")
                st.rerun()
        elif not is_online:
            st.caption("Writes stay safe locally until connection returns.")

    if conflict_items:
        with st.expander(f"Resolve {len(conflict_items)} Offline Sync Conflict(s)", expanded=True):
            for item in conflict_items:
                p = item.payload
                mark_disp = (
                    p["diving_score"]
                    if p.get("diving_score")
                    else format_time_ms(p.get("final_time_ms"))
                )
                st.warning(
                    f"**Queue #{item.id}** · Event `{p['event_code']}` · Entry #{p['entry_id']} · "
                    f"Deck Mark: `{mark_disp}` — **{item.conflict_reason}**"
                )
                if active_user:
                    col_reason, col_apply, col_drop = st.columns([2.2, 1.1, 1.1])
                    override_note = col_reason.text_input(
                        "Audit reason to apply deck mark",
                        key=f"sync_override_note_{item.id}",
                        label_visibility="collapsed",
                        placeholder="Reason for overriding server value...",
                    )
                    if col_apply.button("Apply Deck Value", key=f"sync_apply_{item.id}"):
                        if not override_note.strip():
                            st.error("Enter an audit reason before applying.")
                        else:
                            try:
                                outbox.resolve_conflict_force_apply(
                                    repo,
                                    item_id=item.id,
                                    referee_user_id=active_user.id,
                                    override_reason=override_note,
                                )
                                st.rerun()
                            except Exception as exc:
                                st.error(str(exc))
                    if col_drop.button("Keep Server Value", key=f"sync_drop_{item.id}"):
                        outbox.resolve_conflict_discard(item.id)
                        st.rerun()

    return is_online
