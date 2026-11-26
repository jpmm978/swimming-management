from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import os
from typing import Any, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from db.schema import (
    Athlete,
    AuditLog,
    Base,
    CourseType,
    DisqualificationReport,
    DQStatus,
    Entry,
    Facility,
    GenderMode,
    Meet,
    MeetStatus,
    OfficialResult,
    Organization,
    OrgType,
    ResultRevision,
    ResultState,
    RoleAssignment,
    RoleCode,
    RuleProfile,
    RuleVersion,
    Season,
    SeasonRoster,
    Team,
    User,
)
from importers.roster_importer import RosterImportRow, resolve_existing_athlete

ADMIN_ROLES = {
    RoleCode.PLATFORM_ADMIN,
    RoleCode.LEAGUE_ADMIN,
    RoleCode.ORG_ADMIN,
}

COACH_ROLES = ADMIN_ROLES | {
    RoleCode.HEAD_COACH,
    RoleCode.ASSISTANT_COACH,
}

MEET_OP_ROLES = ADMIN_ROLES | {
    RoleCode.MEET_DIRECTOR,
    RoleCode.SCORING_OPERATOR,
}

OFFICIAL_ROLES = ADMIN_ROLES | {
    RoleCode.REFEREE,
    RoleCode.OFFICIAL,
}


class PermissionDeniedError(Exception):
    """Raised when a user lacks a required role or organization/team scope."""


class MeetLockedError(Exception):
    """Raised when attempting a disallowed change on a locked or finalized meet."""


def hash_password(raw_password: str, salt: Optional[str] = None) -> str:
    active_salt = salt or os.environ.get("SWIM_AUTH_SALT", "miaa-swim-dev-salt")
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        raw_password.encode("utf-8"),
        active_salt.encode("utf-8"),
        100_000,
    ).hex()
    return f"{active_salt}${digest}"


def build_engine(database_url: Optional[str] = None) -> Engine:
    url = (
        database_url
        or os.environ.get("DATABASE_URL")
        or "sqlite:///./miaa_swim_platform.db"
    )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

    if url.startswith("sqlite"):
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class SwimRepository:
    """Shared data-access and governance repository across all eight Centers."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def session(self) -> Session:
        return self._session_factory()

    def init_schema(self, engine: Optional[Engine] = None) -> None:
        target_engine = engine or self._session_factory.kw["bind"]
        Base.metadata.create_all(bind=target_engine)

    def get_user_roles(
        self,
        session: Session,
        user_id: Optional[int],
        *,
        org_id: Optional[int] = None,
        team_id: Optional[int] = None,
        season_id: Optional[int] = None,
    ) -> set[RoleCode]:
        if user_id is None:
            return {RoleCode.SPECTATOR}

        assignments = session.scalars(select(RoleAssignment).where(RoleAssignment.user_id == user_id)).all()
        matched: set[RoleCode] = set()

        for ra in assignments:
            if ra.role == RoleCode.PLATFORM_ADMIN:
                matched.add(ra.role)
                continue
            if ra.org_id is not None and org_id is not None and ra.org_id != org_id:
                continue
            if ra.role not in ADMIN_ROLES:
                if ra.team_id is not None and team_id is not None and ra.team_id != team_id:
                    continue
                if ra.season_id is not None and season_id is not None and ra.season_id != season_id:
                    continue
            matched.add(ra.role)

        return matched

    def require_roles(
        self,
        session: Session,
        user_id: Optional[int],
        allowed_roles: set[RoleCode],
        *,
        org_id: Optional[int] = None,
        team_id: Optional[int] = None,
        season_id: Optional[int] = None,
        action_label: str = "perform this action",
    ) -> set[RoleCode]:
        roles = self.get_user_roles(
            session,
            user_id,
            org_id=org_id,
            team_id=team_id,
            season_id=season_id,
        )
        if not (roles & allowed_roles):
            raise PermissionDeniedError(
                f"User {user_id} is not authorized to {action_label}."
            )
        return roles

    def record_audit(
        self,
        session: Session,
        *,
        action: str,
        entity_type: str,
        entity_id: Optional[str | int] = None,
        org_id: Optional[int] = None,
        actor_user_id: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> AuditLog:
        entry = AuditLog(
            org_id=org_id,
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            details=details or {},
        )
        session.add(entry)
        return entry

    def seed_initial_program(
        self,
        *,
        team_name: str = "Gardner High Cooperative Swim Team",
        team_code: str = "GAR",
        season_label: str = "2026-27",
        facility_name: str = "Mount Wachusett Community College Pool",
    ) -> dict[str, int]:
        with self.session() as session:
            org = session.scalar(
                select(Organization).where(
                    Organization.name == "North Central MA Cooperative Athletics",
                    Organization.org_type == OrgType.COOP,
                )
            )
            if org is None:
                org = Organization(
                    name="North Central MA Cooperative Athletics",
                    org_type=OrgType.COOP,
                    timezone="America/New_York",
                    is_active=True,
                )
                session.add(org)
                session.flush()

            facility = session.scalar(
                select(Facility).where(Facility.name == facility_name)
            )
            if facility is None:
                facility = Facility(
                    org_id=org.id,
                    name=facility_name,
                    default_course=CourseType.SCY,
                    lane_count=6,
                    city="Gardner",
                    state="MA",
                )
                session.add(facility)
                session.flush()

            season = session.scalar(
                select(Season).where(
                    Season.org_id == org.id,
                    Season.label == season_label,
                )
            )
            if season is None:
                season = Season(
                    org_id=org.id,
                    label=season_label,
                    start_date=date(2026, 11, 15),
                    end_date=date(2027, 3, 15),
                    is_active=True,
                )
                session.add(season)
                session.flush()

            team = session.scalar(
                select(Team).where(
                    Team.org_id == org.id,
                    Team.code == team_code,
                )
            )
            if team is None:
                team = Team(
                    org_id=org.id,
                    home_facility_id=facility.id,
                    name=team_name,
                    code=team_code,
                    gender_mode=GenderMode.MIXED,
                    division_label="Division 1",
                    is_active=True,
                )
                session.add(team)
                session.flush()

            profile = session.scalar(
                select(RuleProfile).where(RuleProfile.code == "MIAA_NFHS_HS")
            )
            if profile is None:
                profile = RuleProfile(
                    code="MIAA_NFHS_HS",
                    name="MIAA / NFHS High School Swimming & Diving",
                    governing_body="MIAA / NFHS",
                )
                session.add(profile)
                session.flush()

            rule_ver = session.scalar(
                select(RuleVersion).where(
                    RuleVersion.rule_profile_id == profile.id,
                    RuleVersion.version_label == "2026-27-D1-CW",
                )
            )
            if rule_ver is None:
                rule_ver = RuleVersion(
                    rule_profile_id=profile.id,
                    version_label="2026-27-D1-CW",
                    effective_from=date(2026, 8, 1),
                    rules_payload={
                        "max_total_events_per_athlete": 4,
                        "max_individual_events_per_athlete": 2,
                        "division": "Division 1",
                        "section": "Central/West",
                    },
                    is_verified=True,
                )
                session.add(rule_ver)
                session.flush()

            admin_user = session.scalar(
                select(User).where(User.email == "admin@gar-swim.local")
            )
            if admin_user is None:
                admin_user = User(
                    email="admin@gar-swim.local",
                    display_name="GAR Platform Admin",
                    password_hash=hash_password("change-me-local"),
                    is_active=True,
                )
                session.add(admin_user)
                session.flush()
                session.add(
                    RoleAssignment(
                        user_id=admin_user.id,
                        role=RoleCode.ORG_ADMIN,
                        org_id=org.id,
                        team_id=team.id,
                        season_id=season.id,
                    )
                )

            self.record_audit(
                session,
                action="program.seeded",
                entity_type="team",
                entity_id=team.id,
                org_id=org.id,
                actor_user_id=admin_user.id,
                details={"team_code": team_code, "season": season_label},
            )
            session.commit()

            return {
                "org_id": org.id,
                "facility_id": facility.id,
                "season_id": season.id,
                "team_id": team.id,
                "rule_version_id": rule_ver.id,
                "admin_user_id": admin_user.id,
            }

    def create_team(
        self,
        *,
        actor_user_id: int,
        org_id: int,
        name: str,
        code: str,
        gender_mode: GenderMode = GenderMode.MIXED,
        division_label: Optional[str] = None,
        home_facility_id: Optional[int] = None,
    ) -> Team:
        clean_name = name.strip()
        clean_code = code.strip().upper()
        if not clean_name or not clean_code:
            raise ValueError("Team name and team code/abbreviation are required.")

        with self.session() as session:
            self.require_roles(
                session,
                actor_user_id,
                COACH_ROLES | MEET_OP_ROLES,
                org_id=org_id,
                action_label="create a team",
            )

            existing = session.scalar(
                select(Team).where(Team.org_id == org_id, Team.code == clean_code)
            )
            if existing is not None:
                return existing

            team = Team(
                org_id=org_id,
                home_facility_id=home_facility_id,
                name=clean_name,
                code=clean_code,
                gender_mode=gender_mode,
                division_label=division_label.strip() if division_label else None,
                is_active=True,
            )
            session.add(team)
            session.flush()

            self.record_audit(
                session,
                action="team.created",
                entity_type="team",
                entity_id=team.id,
                org_id=org_id,
                actor_user_id=actor_user_id,
                details={"name": team.name, "code": team.code},
            )
            session.commit()
            return team

    def add_athlete_to_roster(
        self,
        *,
        actor_user_id: int,
        org_id: int,
        team_id: int,
        season_id: int,
        first_name: str,
        last_name: str,
        grad_year: Optional[int] = None,
        gender: Optional[str] = None,
        preferred_name: Optional[str] = None,
        eligibility_verified: bool = False,
    ) -> Athlete:
        with self.session() as session:
            self.require_roles(
                session,
                actor_user_id,
                COACH_ROLES,
                org_id=org_id,
                team_id=team_id,
                season_id=season_id,
                action_label="add athletes to roster",
            )

            athlete = Athlete(
                primary_org_id=org_id,
                first_name=first_name.strip(),
                last_name=last_name.strip(),
                preferred_name=preferred_name.strip() if preferred_name else None,
                grad_year=grad_year,
                gender=gender,
                is_active=True,
            )
            session.add(athlete)
            session.flush()

            roster = SeasonRoster(
                team_id=team_id,
                season_id=season_id,
                athlete_id=athlete.id,
                status="active",
                eligibility_verified=eligibility_verified,
            )
            session.add(roster)

            self.record_audit(
                session,
                action="roster.athlete_added",
                entity_type="athlete",
                entity_id=athlete.id,
                org_id=org_id,
                actor_user_id=actor_user_id,
                details={"team_id": team_id, "season_id": season_id},
            )
            session.commit()
            return athlete

    def list_active_roster(self, team_id: int, season_id: int) -> list[dict[str, Any]]:
        with self.session() as session:
            stmt = (
                select(Athlete, SeasonRoster)
                .join(SeasonRoster, SeasonRoster.athlete_id == Athlete.id)
                .where(
                    SeasonRoster.team_id == team_id,
                    SeasonRoster.season_id == season_id,
                    SeasonRoster.status == "active",
                )
                .order_by(Athlete.last_name, Athlete.first_name)
            )
            rows = session.execute(stmt).all()
            return [
                {
                    "athlete_id": athlete.id,
                    "persistent_uid": athlete.persistent_uid,
                    "first_name": athlete.first_name,
                    "last_name": athlete.last_name,
                    "preferred_name": athlete.preferred_name,
                    "grad_year": athlete.grad_year,
                    "gender": athlete.gender,
                    "eligibility_verified": roster.eligibility_verified,
                }
                for athlete, roster in rows
            ]

    def bulk_import_roster(
        self,
        *,
        actor_user_id: int,
        org_id: int,
        team_id: int,
        season_id: int,
        rows: list[RosterImportRow],
    ) -> dict[str, int]:
        created_athletes = 0
        matched_athletes = 0
        added_rosters = 0
        updated_rosters = 0

        with self.session() as session:
            self.require_roles(
                session,
                actor_user_id,
                COACH_ROLES,
                org_id=org_id,
                team_id=team_id,
                season_id=season_id,
                action_label="bulk import team roster",
            )

            existing_athletes = list(
                session.scalars(select(Athlete).where(Athlete.primary_org_id == org_id)).all()
            )

            for row in rows:
                athlete = resolve_existing_athlete(row, existing_athletes)
                if athlete is None:
                    athlete = Athlete(
                        primary_org_id=org_id,
                        first_name=row.first_name,
                        last_name=row.last_name,
                        preferred_name=row.preferred_name,
                        grad_year=row.grad_year,
                        gender=row.gender,
                        is_active=True,
                    )
                    session.add(athlete)
                    session.flush()
                    existing_athletes.append(athlete)
                    created_athletes += 1
                else:
                    matched_athletes += 1
                    if row.preferred_name and not athlete.preferred_name:
                        athlete.preferred_name = row.preferred_name
                    if row.grad_year and not athlete.grad_year:
                        athlete.grad_year = row.grad_year
                    if row.gender and not athlete.gender:
                        athlete.gender = row.gender

                roster_entry = session.scalar(
                    select(SeasonRoster).where(
                        SeasonRoster.team_id == team_id,
                        SeasonRoster.season_id == season_id,
                        SeasonRoster.athlete_id == athlete.id,
                    )
                )
                if roster_entry is None:
                    session.add(
                        SeasonRoster(
                            team_id=team_id,
                            season_id=season_id,
                            athlete_id=athlete.id,
                            status="active",
                            eligibility_verified=row.eligibility_verified,
                        )
                    )
                    added_rosters += 1
                else:
                    roster_entry.status = "active"
                    roster_entry.eligibility_verified = row.eligibility_verified
                    updated_rosters += 1

            self.record_audit(
                session,
                action="roster.bulk_imported",
                entity_type="season_roster",
                entity_id=f"team:{team_id}:season:{season_id}",
                org_id=org_id,
                actor_user_id=actor_user_id,
                details={
                    "created_athletes": created_athletes,
                    "matched_athletes": matched_athletes,
                    "added_rosters": added_rosters,
                    "updated_rosters": updated_rosters,
                },
            )
            session.commit()

        return {
            "created_athletes": created_athletes,
            "matched_athletes": matched_athletes,
            "added_rosters": added_rosters,
            "updated_rosters": updated_rosters,
        }

    def rollover_season_roster(
        self,
        *,
        actor_user_id: int,
        org_id: int,
        team_id: int,
        source_season_id: int,
        target_season_id: int,
        graduating_cutoff_year: int,
    ) -> dict[str, int]:
        promoted = 0
        graduated_skipped = 0
        already_enrolled = 0

        with self.session() as session:
            self.require_roles(
                session,
                actor_user_id,
                COACH_ROLES,
                org_id=org_id,
                team_id=team_id,
                season_id=target_season_id,
                action_label="roll over season roster",
            )

            source_rows = session.execute(
                select(Athlete, SeasonRoster)
                .join(SeasonRoster, SeasonRoster.athlete_id == Athlete.id)
                .where(
                    SeasonRoster.team_id == team_id,
                    SeasonRoster.season_id == source_season_id,
                    SeasonRoster.status == "active",
                )
            ).all()

            for athlete, _ in source_rows:
                if athlete.grad_year is not None and athlete.grad_year <= graduating_cutoff_year:
                    graduated_skipped += 1
                    continue

                existing = session.scalar(
                    select(SeasonRoster).where(
                        SeasonRoster.team_id == team_id,
                        SeasonRoster.season_id == target_season_id,
                        SeasonRoster.athlete_id == athlete.id,
                    )
                )
                if existing is not None:
                    already_enrolled += 1
                    continue

                session.add(
                    SeasonRoster(
                        team_id=team_id,
                        season_id=target_season_id,
                        athlete_id=athlete.id,
                        status="active",
                        eligibility_verified=False,
                    )
                )
                promoted += 1

            self.record_audit(
                session,
                action="roster.season_rollover",
                entity_type="season_roster",
                entity_id=f"team:{team_id}:{source_season_id}->{target_season_id}",
                org_id=org_id,
                actor_user_id=actor_user_id,
                details={
                    "promoted": promoted,
                    "graduated_skipped": graduated_skipped,
                    "already_enrolled": already_enrolled,
                },
            )
            session.commit()

        return {
            "promoted": promoted,
            "graduated_skipped": graduated_skipped,
            "already_enrolled": already_enrolled,
        }

    def export_roster_csv(self, team_id: int, season_id: int) -> str:
        roster = self.list_active_roster(team_id, season_id)
        buf = io.StringIO()
        fieldnames = [
            "persistent_uid",
            "first_name",
            "last_name",
            "preferred_name",
            "grad_year",
            "gender",
            "eligibility_verified",
        ]
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for row in roster:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
        return buf.getvalue()

    def create_meet(
        self,
        *,
        actor_user_id: int,
        season_id: int,
        host_org_id: int,
        name: str,
        meet_date: date,
        course: CourseType = CourseType.SCY,
        lane_count: int = 6,
        facility_id: Optional[int] = None,
        rule_version_id: Optional[int] = None,
    ) -> Meet:
        with self.session() as session:
            self.require_roles(
                session,
                actor_user_id,
                MEET_OP_ROLES,
                org_id=host_org_id,
                season_id=season_id,
                action_label="create a meet",
            )
            meet = Meet(
                season_id=season_id,
                host_org_id=host_org_id,
                facility_id=facility_id,
                rule_version_id=rule_version_id,
                name=name.strip(),
                meet_date=meet_date,
                course=course,
                lane_count=lane_count,
                status=MeetStatus.DRAFT,
                is_locked=False,
            )
            session.add(meet)
            session.flush()

            self.record_audit(
                session,
                action="meet.created",
                entity_type="meet",
                entity_id=meet.id,
                org_id=host_org_id,
                actor_user_id=actor_user_id,
                details={"name": meet.name, "meet_date": meet_date.isoformat()},
            )
            session.commit()
            return meet

    def upsert_official_result(
        self,
        *,
        actor_user_id: int,
        meet_id: int,
        entry_id: int,
        event_code: str,
        state: ResultState,
        final_time_ms: Optional[int] = None,
        diving_score: Optional[Decimal] = None,
        place: Optional[int] = None,
        points_awarded: Decimal = Decimal("0.00"),
        revision_reason: str = "Result recording/update",
        mark_verified: bool = False,
    ) -> OfficialResult:
        with self.session() as session:
            meet = session.get(Meet, meet_id)
            if meet is None:
                raise ValueError(f"Meet {meet_id} not found.")

            required = OFFICIAL_ROLES if mark_verified else MEET_OP_ROLES
            self.require_roles(
                session,
                actor_user_id,
                required,
                org_id=meet.host_org_id,
                season_id=meet.season_id,
                action_label="record or verify official results",
            )

            if meet.is_locked:
                raise MeetLockedError(
                    f"Meet {meet_id} is locked; reopen it through Officials/Admin workflow first."
                )

            existing = session.scalar(
                select(OfficialResult).where(
                    OfficialResult.meet_id == meet_id,
                    OfficialResult.entry_id == entry_id,
                )
            )

            new_snapshot = {
                "event_code": event_code,
                "state": state.value,
                "final_time_ms": final_time_ms,
                "diving_score": str(diving_score) if diving_score is not None else None,
                "place": place,
                "points_awarded": str(points_awarded),
            }

            if existing is None:
                result = OfficialResult(
                    meet_id=meet_id,
                    entry_id=entry_id,
                    event_code=event_code,
                    state=state,
                    final_time_ms=final_time_ms,
                    diving_score=diving_score,
                    place=place,
                    points_awarded=points_awarded,
                    verified_by_user_id=actor_user_id if mark_verified else None,
                    verified_at=datetime.now(timezone.utc) if mark_verified else None,
                )
                session.add(result)
                session.flush()
                session.add(
                    ResultRevision(
                        official_result_id=result.id,
                        changed_by_user_id=actor_user_id,
                        reason=revision_reason,
                        previous_snapshot={},
                        new_snapshot=new_snapshot,
                    )
                )
            else:
                prev_snapshot = {
                    "event_code": existing.event_code,
                    "state": existing.state.value,
                    "final_time_ms": existing.final_time_ms,
                    "diving_score": (
                        str(existing.diving_score) if existing.diving_score is not None else None
                    ),
                    "place": existing.place,
                    "points_awarded": str(existing.points_awarded),
                }
                existing.event_code = event_code
                existing.state = state
                existing.final_time_ms = final_time_ms
                existing.diving_score = diving_score
                existing.place = place
                existing.points_awarded = points_awarded
                if mark_verified:
                    existing.verified_by_user_id = actor_user_id
                    existing.verified_at = datetime.now(timezone.utc)

                session.add(
                    ResultRevision(
                        official_result_id=existing.id,
                        changed_by_user_id=actor_user_id,
                        reason=revision_reason,
                        previous_snapshot=prev_snapshot,
                        new_snapshot=new_snapshot,
                    )
                )
                result = existing

            self.record_audit(
                session,
                action="result.upserted",
                entity_type="official_result",
                entity_id=result.id,
                org_id=meet.host_org_id,
                actor_user_id=actor_user_id,
                details={
                    "meet_id": meet_id,
                    "entry_id": entry_id,
                    "reason": revision_reason,
                    "verified": mark_verified,
                },
            )
            session.commit()
            return result

    def submit_dq_report(
        self,
        *,
        actor_user_id: int,
        meet_id: int,
        entry_id: int,
        event_code: str,
        rule_code: str,
        description: str,
        heat_number: Optional[int] = None,
        lane_number: Optional[int] = None,
    ) -> DisqualificationReport:
        with self.session() as session:
            meet = session.get(Meet, meet_id)
            if meet is None:
                raise ValueError(f"Meet {meet_id} not found.")

            self.require_roles(
                session,
                actor_user_id,
                OFFICIAL_ROLES | MEET_OP_ROLES,
                org_id=meet.host_org_id,
                season_id=meet.season_id,
                action_label="submit a disqualification report",
            )
            if meet.is_locked:
                raise MeetLockedError(f"Meet {meet_id} is locked.")

            dq = DisqualificationReport(
                meet_id=meet_id,
                entry_id=entry_id,
                event_code=event_code,
                heat_number=heat_number,
                lane_number=lane_number,
                rule_code=rule_code.strip(),
                description=description.strip(),
                reported_by_user_id=actor_user_id,
                status=DQStatus.SUBMITTED,
            )
            session.add(dq)
            session.flush()

            self.record_audit(
                session,
                action="dq.submitted",
                entity_type="disqualification_report",
                entity_id=dq.id,
                org_id=meet.host_org_id,
                actor_user_id=actor_user_id,
                details={"meet_id": meet_id, "entry_id": entry_id, "rule_code": dq.rule_code},
            )
            session.commit()
            return dq

    def review_dq_report(
        self,
        *,
        referee_user_id: int,
        dq_id: int,
        approve: bool,
        review_notes: str = "",
    ) -> DisqualificationReport:
        with self.session() as session:
            dq = session.get(DisqualificationReport, dq_id)
            if dq is None:
                raise ValueError(f"DQ report {dq_id} not found.")
            meet = session.get(Meet, dq.meet_id)
            if meet is None:
                raise ValueError(f"Meet {dq.meet_id} not found.")

            self.require_roles(
                session,
                referee_user_id,
                OFFICIAL_ROLES,
                org_id=meet.host_org_id,
                season_id=meet.season_id,
                action_label="review and rule on disqualifications",
            )
            if meet.is_locked:
                raise MeetLockedError(f"Meet {meet.id} is locked.")

            dq.status = DQStatus.APPROVED if approve else DQStatus.REJECTED
            dq.reviewed_by_user_id = referee_user_id
            dq.review_notes = review_notes.strip() or None
            dq.reviewed_at = datetime.now(timezone.utc)

            if approve:
                existing_res = session.scalar(
                    select(OfficialResult).where(
                        OfficialResult.meet_id == dq.meet_id,
                        OfficialResult.entry_id == dq.entry_id,
                    )
                )
                prev_snap = (
                    {
                        "state": existing_res.state.value,
                        "final_time_ms": existing_res.final_time_ms,
                        "place": existing_res.place,
                        "points_awarded": str(existing_res.points_awarded),
                    }
                    if existing_res
                    else {}
                )
                new_snap = {
                    "state": ResultState.DISQUALIFIED.value,
                    "final_time_ms": existing_res.final_time_ms if existing_res else None,
                    "place": None,
                    "points_awarded": "0.00",
                }
                if existing_res is None:
                    res = OfficialResult(
                        meet_id=dq.meet_id,
                        entry_id=dq.entry_id,
                        event_code=dq.event_code,
                        state=ResultState.DISQUALIFIED,
                        place=None,
                        points_awarded=Decimal("0.00"),
                        verified_by_user_id=referee_user_id,
                        verified_at=datetime.now(timezone.utc),
                    )
                    session.add(res)
                    session.flush()
                else:
                    existing_res.state = ResultState.DISQUALIFIED
                    existing_res.place = None
                    existing_res.points_awarded = Decimal("0.00")
                    existing_res.verified_by_user_id = referee_user_id
                    existing_res.verified_at = datetime.now(timezone.utc)
                    res = existing_res

                session.add(
                    ResultRevision(
                        official_result_id=res.id,
                        changed_by_user_id=referee_user_id,
                        reason=f"Referee approved DQ #{dq.id} ({dq.rule_code})",
                        previous_snapshot=prev_snap,
                        new_snapshot=new_snap,
                    )
                )

            self.record_audit(
                session,
                action="dq.approved" if approve else "dq.rejected",
                entity_type="disqualification_report",
                entity_id=dq.id,
                org_id=meet.host_org_id,
                actor_user_id=referee_user_id,
                details={"entry_id": dq.entry_id, "notes": review_notes},
            )
            session.commit()
            return dq

    def verify_event_results(
        self,
        *,
        official_user_id: int,
        meet_id: int,
        event_code: str,
    ) -> int:
        verified_count = 0
        with self.session() as session:
            meet = session.get(Meet, meet_id)
            if meet is None:
                raise ValueError(f"Meet {meet_id} not found.")

            self.require_roles(
                session,
                official_user_id,
                OFFICIAL_ROLES,
                org_id=meet.host_org_id,
                season_id=meet.season_id,
                action_label="verify official event results",
            )
            if meet.is_locked:
                raise MeetLockedError(f"Meet {meet_id} is locked.")

            results = list(
                session.scalars(
                    select(OfficialResult).where(
                        OfficialResult.meet_id == meet_id,
                        OfficialResult.event_code == event_code,
                        OfficialResult.state == ResultState.PRELIMINARY,
                    )
                ).all()
            )
            now = datetime.now(timezone.utc)
            for res in results:
                prev_snap = {
                    "state": res.state.value,
                    "final_time_ms": res.final_time_ms,
                    "place": res.place,
                    "points_awarded": str(res.points_awarded),
                }
                res.state = ResultState.OFFICIAL
                res.verified_by_user_id = official_user_id
                res.verified_at = now
                session.add(
                    ResultRevision(
                        official_result_id=res.id,
                        changed_by_user_id=official_user_id,
                        reason=f"Official verification of event {event_code}",
                        previous_snapshot=prev_snap,
                        new_snapshot={
                            "state": ResultState.OFFICIAL.value,
                            "final_time_ms": res.final_time_ms,
                            "place": res.place,
                            "points_awarded": str(res.points_awarded),
                        },
                    )
                )
                verified_count += 1

            self.record_audit(
                session,
                action="event.verified",
                entity_type="meet",
                entity_id=meet_id,
                org_id=meet.host_org_id,
                actor_user_id=official_user_id,
                details={"event_code": event_code, "verified_count": verified_count},
            )
            session.commit()
        return verified_count

    def reopen_locked_meet(
        self,
        *,
        actor_user_id: int,
        meet_id: int,
        reopen_reason: str,
    ) -> Meet:
        if not reopen_reason or not reopen_reason.strip():
            raise ValueError("A documented reason is required to reopen a locked meet.")

        with self.session() as session:
            meet = session.get(Meet, meet_id)
            if meet is None:
                raise ValueError(f"Meet {meet_id} not found.")

            self.require_roles(
                session,
                actor_user_id,
                OFFICIAL_ROLES,
                org_id=meet.host_org_id,
                season_id=meet.season_id,
                action_label="reopen a locked meet",
            )

            meet.is_locked = False
            meet.status = MeetStatus.PENDING_VERIFICATION
            self.record_audit(
                session,
                action="meet.reopened",
                entity_type="meet",
                entity_id=meet.id,
                org_id=meet.host_org_id,
                actor_user_id=actor_user_id,
                details={"reason": reopen_reason.strip()},
            )
            session.commit()
            return meet
