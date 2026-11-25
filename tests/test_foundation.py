from __future__ import annotations

from datetime import date
from decimal import Decimal
import pytest

from db.repository import (
    MeetLockedError,
    PermissionDeniedError,
    SwimRepository,
    build_engine,
    create_session_factory,
)
from db.schema import (
    AuditLog,
    CourseType,
    Entry,
    Meet,
    ResultRevision,
    ResultState,
    RoleAssignment,
    RoleCode,
    User,
)


@pytest.fixture
def repo_and_seed() -> tuple[SwimRepository, dict[str, int]]:
    engine = build_engine("sqlite:///:memory:")
    repo = SwimRepository(create_session_factory(engine))
    repo.init_schema(engine)
    seed_ids = repo.seed_initial_program()
    return repo, seed_ids


def test_seed_is_idempotent_and_creates_gar_context(
    repo_and_seed: tuple[SwimRepository, dict[str, int]],
) -> None:
    repo, first_seed = repo_and_seed
    second_seed = repo.seed_initial_program()

    assert first_seed == second_seed
    assert first_seed["team_id"] > 0
    assert first_seed["season_id"] > 0


def test_coach_can_add_athlete_and_list_roster(
    repo_and_seed: tuple[SwimRepository, dict[str, int]],
) -> None:
    repo, seed = repo_and_seed

    athlete = repo.add_athlete_to_roster(
        actor_user_id=seed["admin_user_id"],
        org_id=seed["org_id"],
        team_id=seed["team_id"],
        season_id=seed["season_id"],
        first_name="Jordan",
        last_name="Avery",
        grad_year=2028,
        gender="F",
        eligibility_verified=True,
    )

    roster = repo.list_active_roster(seed["team_id"], seed["season_id"])
    assert len(roster) == 1
    assert roster[0]["athlete_id"] == athlete.id
    assert roster[0]["first_name"] == "Jordan"
    assert roster[0]["eligibility_verified"] is True


def test_unauthorized_user_cannot_modify_roster(
    repo_and_seed: tuple[SwimRepository, dict[str, int]],
) -> None:
    repo, seed = repo_and_seed

    with repo.session() as session:
        parent = User(
            email="parent@gar-swim.local",
            display_name="Parent User",
            password_hash="not-used-in-test",
            is_active=True,
        )
        session.add(parent)
        session.flush()
        session.add(
            RoleAssignment(
                user_id=parent.id,
                role=RoleCode.PARENT,
                org_id=seed["org_id"],
                team_id=seed["team_id"],
                season_id=seed["season_id"],
            )
        )
        session.commit()
        parent_id = parent.id

    with pytest.raises(PermissionDeniedError):
        repo.add_athlete_to_roster(
            actor_user_id=parent_id,
            org_id=seed["org_id"],
            team_id=seed["team_id"],
            season_id=seed["season_id"],
            first_name="Taylor",
            last_name="Reed",
        )


def test_official_result_upsert_records_revision_history_and_blocks_locked_meets(
    repo_and_seed: tuple[SwimRepository, dict[str, int]],
) -> None:
    repo, seed = repo_and_seed
    admin_id = seed["admin_user_id"]

    athlete = repo.add_athlete_to_roster(
        actor_user_id=admin_id,
        org_id=seed["org_id"],
        team_id=seed["team_id"],
        season_id=seed["season_id"],
        first_name="Alex",
        last_name="Morgan",
        grad_year=2027,
        gender="M",
        eligibility_verified=True,
    )

    meet = repo.create_meet(
        actor_user_id=admin_id,
        season_id=seed["season_id"],
        host_org_id=seed["org_id"],
        name="GAR vs. Visiting Dual Meet",
        meet_date=date(2026, 12, 12),
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
            event_code="B_50_FREE",
            is_relay=False,
            is_exhibition=False,
            seed_time_ms=23450,
        )
        session.add(entry)
        session.commit()
        entry_id = entry.id

    res_v1 = repo.upsert_official_result(
        actor_user_id=admin_id,
        meet_id=meet.id,
        entry_id=entry_id,
        event_code="B_50_FREE",
        state=ResultState.PRELIMINARY,
        final_time_ms=23120,
        place=1,
        points_awarded=Decimal("6.00"),
        revision_reason="Initial heat touch time",
        mark_verified=False,
    )
    assert res_v1.final_time_ms == 23120

    res_v2 = repo.upsert_official_result(
        actor_user_id=admin_id,
        meet_id=meet.id,
        entry_id=entry_id,
        event_code="B_50_FREE",
        state=ResultState.OFFICIAL,
        final_time_ms=23140,
        place=1,
        points_awarded=Decimal("6.00"),
        revision_reason="Backup watch reconciliation and referee approval",
        mark_verified=True,
    )
    assert res_v2.final_time_ms == 23140
    assert res_v2.verified_by_user_id == admin_id

    with repo.session() as session:
        revisions = (
            session.query(ResultRevision)
            .filter(ResultRevision.official_result_id == res_v2.id)
            .order_by(ResultRevision.id)
            .all()
        )
        assert len(revisions) == 2
        assert revisions[0].previous_snapshot == {}
        assert revisions[1].previous_snapshot["final_time_ms"] == 23120
        assert revisions[1].new_snapshot["final_time_ms"] == 23140

        audit_actions = [row.action for row in session.query(AuditLog).all()]
        assert "result.upserted" in audit_actions

        db_meet = session.get(Meet, meet.id)
        assert db_meet is not None
        db_meet.is_locked = True
        session.commit()

    with pytest.raises(MeetLockedError):
        repo.upsert_official_result(
            actor_user_id=admin_id,
            meet_id=meet.id,
            entry_id=entry_id,
            event_code="B_50_FREE",
            state=ResultState.OFFICIAL,
            final_time_ms=23000,
            revision_reason="Attempted edit on locked meet",
        )
