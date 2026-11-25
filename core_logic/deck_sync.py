from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

from sqlalchemy import select

from db.repository import MeetLockedError, SwimRepository
from db.schema import Meet, OfficialResult, ResultState


@dataclass(frozen=True)
class OutboxItem:
    id: int
    meet_id: int
    action_type: str
    payload: dict[str, Any]
    created_at_utc: str
    status: str
    conflict_reason: Optional[str] = None


@dataclass(frozen=True)
class SyncBatchReport:
    synced_count: int
    conflict_count: int
    remaining_pending: int
    messages: list[str]


class DeckOfflineOutbox:
    def __init__(self, sqlite_path: str | Path = "./deck_local_outbox.db") -> None:
        self.db_path = str(sqlite_path)
        self._init_local_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_local_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deck_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meet_id INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    conflict_reason TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_deck_outbox_meet_status ON deck_outbox (meet_id, status)"
            )
            conn.commit()

    def enqueue_result_write(
        self,
        *,
        actor_user_id: int,
        meet_id: int,
        entry_id: int,
        event_code: str,
        state: str,
        final_time_ms: Optional[int] = None,
        diving_score: Optional[Decimal] = None,
        place: Optional[int] = None,
        points_awarded: Decimal = Decimal("0.00"),
        revision_reason: str = "Pool-deck offline entry",
    ) -> int:
        payload = {
            "actor_user_id": actor_user_id,
            "meet_id": meet_id,
            "entry_id": entry_id,
            "event_code": event_code,
            "state": state,
            "final_time_ms": final_time_ms,
            "diving_score": str(diving_score) if diving_score is not None else None,
            "place": place,
            "points_awarded": str(points_awarded),
            "revision_reason": revision_reason,
        }
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO deck_outbox (meet_id, action_type, payload_json, created_at_utc, status)
                VALUES (?, 'UPSERT_RESULT', ?, ?, 'PENDING')
                """,
                (meet_id, json.dumps(payload), now_iso),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_items(
        self,
        *,
        meet_id: Optional[int] = None,
        statuses: Optional[tuple[str, ...]] = None,
    ) -> list[OutboxItem]:
        query = "SELECT id, meet_id, action_type, payload_json, created_at_utc, status, conflict_reason FROM deck_outbox"
        clauses: list[str] = []
        params: list[Any] = []

        if meet_id is not None:
            clauses.append("meet_id = ?")
            params.append(meet_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)

        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id ASC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            OutboxItem(
                id=row["id"],
                meet_id=row["meet_id"],
                action_type=row["action_type"],
                payload=json.loads(row["payload_json"]),
                created_at_utc=row["created_at_utc"],
                status=row["status"],
                conflict_reason=row["conflict_reason"],
            )
            for row in rows
        ]

    def sync_pending_to_repository(
        self,
        repo: SwimRepository,
        *,
        meet_id: Optional[int] = None,
    ) -> SyncBatchReport:
        pending = self.list_items(meet_id=meet_id, statuses=("PENDING",))
        synced = 0
        conflicts = 0
        messages: list[str] = []

        for item in pending:
            p = item.payload
            try:
                with repo.session() as session:
                    db_meet = session.get(Meet, p["meet_id"])
                    if db_meet is None:
                        self._mark_conflict(item.id, f"Meet #{p['meet_id']} no longer exists on server.")
                        conflicts += 1
                        messages.append(f"Item #{item.id}: Meet not found.")
                        continue

                    if db_meet.is_locked:
                        self._mark_conflict(
                            item.id,
                            "Meet is LOCKED on server. Reopen meet via Officials Center before applying deck change.",
                        )
                        conflicts += 1
                        messages.append(f"Item #{item.id} ({p['event_code']}): Blocked — meet is locked.")
                        continue

                    existing_res = session.scalar(
                        select(OfficialResult).where(
                            OfficialResult.meet_id == p["meet_id"],
                            OfficialResult.entry_id == p["entry_id"],
                        )
                    )
                    if existing_res is not None and existing_res.state == ResultState.OFFICIAL:
                        self._mark_conflict(
                            item.id,
                            "Server result is already verified OFFICIAL by Referee; manual approval required.",
                        )
                        conflicts += 1
                        messages.append(
                            f"Item #{item.id} ({p['event_code']}): Conflict — server result already OFFICIAL."
                        )
                        continue

                repo.upsert_official_result(
                    actor_user_id=p["actor_user_id"],
                    meet_id=p["meet_id"],
                    entry_id=p["entry_id"],
                    event_code=p["event_code"],
                    state=ResultState(p["state"]),
                    final_time_ms=p["final_time_ms"],
                    diving_score=Decimal(p["diving_score"]) if p["diving_score"] is not None else None,
                    place=p["place"],
                    points_awarded=Decimal(p["points_awarded"]),
                    revision_reason=f"[Offline Deck Sync #{item.id}] {p['revision_reason']}",
                    mark_verified=False,
                )
                self._mark_synced(item.id)
                synced += 1
                messages.append(f"Item #{item.id} ({p['event_code']}): Synced.")
            except MeetLockedError as exc:
                self._mark_conflict(item.id, str(exc))
                conflicts += 1
                messages.append(f"Item #{item.id}: {exc}")
            except Exception as exc:
                messages.append(f"Sync paused on item #{item.id} (connection/server error): {exc}")
                break

        remaining = len(self.list_items(meet_id=meet_id, statuses=("PENDING",)))
        return SyncBatchReport(
            synced_count=synced,
            conflict_count=conflicts,
            remaining_pending=remaining,
            messages=messages,
        )

    def resolve_conflict_discard(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM deck_outbox WHERE id = ?", (item_id,))
            conn.commit()

    def resolve_conflict_force_apply(
        self,
        repo: SwimRepository,
        *,
        item_id: int,
        referee_user_id: int,
        override_reason: str,
    ) -> None:
        items = [i for i in self.list_items(statuses=("CONFLICT",)) if i.id == item_id]
        if not items:
            raise ValueError(f"Conflict item #{item_id} not found.")
        p = items[0].payload
        repo.upsert_official_result(
            actor_user_id=referee_user_id,
            meet_id=p["meet_id"],
            entry_id=p["entry_id"],
            event_code=p["event_code"],
            state=ResultState(p["state"]),
            final_time_ms=p["final_time_ms"],
            diving_score=Decimal(p["diving_score"]) if p["diving_score"] is not None else None,
            place=p["place"],
            points_awarded=Decimal(p["points_awarded"]),
            revision_reason=f"[Conflict Override #{item_id}] {override_reason.strip()}",
            mark_verified=False,
        )
        self._mark_synced(item_id)

    def _mark_synced(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE deck_outbox SET status = 'SYNCED', conflict_reason = NULL WHERE id = ?",
                (item_id,),
            )
            conn.commit()

    def _mark_conflict(self, item_id: int, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE deck_outbox SET status = 'CONFLICT', conflict_reason = ? WHERE id = ?",
                (reason, item_id),
            )
            conn.commit()


def save_scored_result_resilient(
    repo: SwimRepository,
    outbox: DeckOfflineOutbox,
    *,
    force_offline: bool = False,
    **kwargs,
) -> str:
    if not force_offline:
        try:
            repo.upsert_official_result(**kwargs)
            return "saved_primary"
        except Exception:
            pass

    outbox.enqueue_result_write(
        actor_user_id=kwargs["actor_user_id"],
        meet_id=kwargs["meet_id"],
        entry_id=kwargs["entry_id"],
        event_code=kwargs["event_code"],
        state=kwargs["state"].value,
        final_time_ms=kwargs.get("final_time_ms"),
        diving_score=kwargs.get("diving_score"),
        place=kwargs.get("place"),
        points_awarded=kwargs["points_awarded"],
        revision_reason=kwargs.get("revision_reason", "Pool-deck entry"),
    )
    return "queued_offline"
