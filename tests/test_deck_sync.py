from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import pytest

from core_logic.deck_sync import DeckOfflineOutbox, save_scored_result_resilient
from db.repository import (
    SwimRepository,
    build_engine,
    create_session_factory,
)
from db.schema import (
    CourseType,
    Entry,
    Meet,
    OfficialResult,
    ResultRevision,
    ResultState,
)


@pytest.fixture
def sync_env(tmp_path: Path):
    primary_url = f"sqlite:///{tmp_path / 'primary_platform.db'}"
    engine = build_engine(primary_url)
    repo = SwimRepository(create_session_factory(engine))
    repo.init_schema(engine)
    seed = repo.seed_initial_program()

    athlete = repo.add_athlete_to_roster(
        actor_user_id=seed["admin_user_id"],
        org_id=seed["org_id"],
        team_id=seed["team_id"],
        season_id=seed["season_id"],
        first_name="Sam",
        last_name="Carter",
        grad_year=2027,
        gender="M",
        eligibility_verified=True,
    )

    meet = repo.create_meet(
        actor_user_id=seed["admin_user_id"],
        season_id=seed["season_id"],
        host_org_id=seed["org_id"],
        name="GAR vs. Rival Dual Meet",
        meet_date=date(2026, 12, 19),
        course=CourseType.SCY,
        lane_count=6,
        facility_id=seed["facility_id"],
        rule_version_id=seed["rule_version_id"],
    )

    with repo.session() as session:
        entry = Entry(
            meet_id=meet.id,
            team_id=seed["team_id"],
            athlete_id=athlete.id,
            event_code="B_100_FREE",
            is_relay=False,
            is_exhibition=False,
            seed_time_ms=52400,
        )
        session.add(entry)
        session.commit()
        entry_id = entry.id

    outbox = DeckOfflineOutbox(tmp_path / "deck_local_outbox.db")
    return {
        "repo": repo,
        "seed": seed,
        "meet": meet,
        "entry_id": entry_id,
        "outbox": outbox,
    }


def test_offline_queue_and_clean_sync_creates_result_and_revision(sync_env) -> None:
    repo: SwimRepository = sync_env["repo"]
    outbox: DeckOfflineOutbox = sync_env["outbox"]
    seed = sync_env["seed"]
    meet = sync_env["meet"]
    entry_id = sync_env["entry_id"]

    mode = save_scored_result_resilient(
        repo,
        outbox,
        force_offline=True,
        actor_user_id=seed["admin_user_id"],
        meet_id=meet.id,
        entry_id=entry_id,
        event_code="B_100_FREE",
        state=ResultState.PRELIMINARY,
        final_time_ms=51890,
        place=1,
        points_awarded=Decimal("6.00"),
        revision_reason="Lane 3 deck touch time",
    )
    assert mode == "queued_offline"
    assert len(outbox.list_items(meet_id=meet.id, statuses=("PENDING",))) == 1

    report = outbox.sync_pending_to_repository(repo, meet_id=meet.id)
    assert report.synced_count == 1
    assert report.conflict_count == 0
    assert report.remaining_pending == 0

    with repo.session() as session:
        saved = session.query(OfficialResult).filter_by(meet_id=meet.id, entry_id=entry_id).one()
        assert saved.final_time_ms == 51890
        assert saved.state == ResultState.PRELIMINARY
        revisions = session.query(ResultRevision).filter_by(official_result_id=saved.id).all()
        assert len(revisions) == 1
        assert "[Offline Deck Sync #" in revisions[0].reason


def test_official_result_conflict_is_not_silently_overwritten_and_can_be_resolved(sync_env) -> None:
    repo: SwimRepository = sync_env["repo"]
    outbox: DeckOfflineOutbox = sync_env["outbox"]
    seed = sync_env["seed"]
    meet = sync_env["meet"]
    entry_id = sync_env["entry_id"]

    repo.upsert_official_result(
        actor_user_id=seed["admin_user_id"],
        meet_id=meet.id,
        entry_id=entry_id,
        event_code="B_100_FREE",
        state=ResultState.OFFICIAL,
        final_time_ms=52010,
        place=1,
        points_awarded=Decimal("6.00"),
        revision_reason="Referee verified official time",
        mark_verified=True,
    )

    outbox.enqueue_result_write(
        actor_user_id=seed["admin_user_id"],
        meet_id=meet.id,
        entry_id=entry_id,
        event_code="B_100_FREE",
        state=ResultState.PRELIMINARY,
        final_time_ms=51950,
        place=1,
        points_awarded=Decimal("6.00"),
        revision_reason="Offline tablet edit",
    )

    report = outbox.sync_pending_to_repository(repo, meet_id=meet.id)
    assert report.synced_count == 0
    assert report.conflict_count == 1

    with repo.session() as session:
        res = session.query(OfficialResult).filter_by(meet_id=meet.id, entry_id=entry_id).one()
        assert res.final_time_ms == 52010

    conflicts = outbox.list_items(meet_id=meet.id, statuses=("CONFLICT",))
    assert len(conflicts) == 1

    outbox.resolve_conflict_force_apply(
        repo,
        item_id=conflicts[0].id,
        referee_user_id=seed["admin_user_id"],
        override_reason="Referee confirmed backup button 51.95s after sync",
    )
    assert len(outbox.list_items(meet_id=meet.id, statuses=("CONFLICT",))) == 0

    with repo.session() as session:
        res_after = session.query(OfficialResult).filter_by(meet_id=meet.id, entry_id=entry_id).one()
        assert res_after.final_time_ms == 51950
        revs = session.query(ResultRevision).filter_by(official_result_id=res_after.id).all()
        assert len(revs) == 2


def test_locked_meet_blocks_sync_and_discard_clears_conflict(sync_env) -> None:
    repo: SwimRepository = sync_env["repo"]
    outbox: DeckOfflineOutbox = sync_env["outbox"]
    seed = sync_env["seed"]
    meet = sync_env["meet"]
    entry_id = sync_env["entry_id"]

    outbox.enqueue_result_write(
        actor_user_id=seed["admin_user_id"],
        meet_id=meet.id,
        entry_id=entry_id,
        event_code="B_100_FREE",
        state=ResultState.PRELIMINARY,
        final_time_ms=52100,
        place=1,
        points_awarded=Decimal("6.00"),
    )

    with repo.session() as session:
        db_meet = session.get(Meet, meet.id)
        assert db_meet is not None
        db_meet.is_locked = True
        session.commit()

    report = outbox.sync_pending_to_repository(repo, meet_id=meet.id)
    assert report.conflict_count == 1

    conflicts = outbox.list_items(meet_id=meet.id, statuses=("CONFLICT",))
    outbox.resolve_conflict_discard(conflicts[0].id)
    assert len(outbox.list_items(meet_id=meet.id, statuses=("CONFLICT",))) == 0
