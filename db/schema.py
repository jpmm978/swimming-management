from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class OrgType(str, Enum):
    ASSOCIATION = "association"
    LEAGUE = "league"
    DISTRICT = "district"
    SCHOOL = "school"
    COOP = "coop"
    CLUB = "club"


class GenderMode(str, Enum):
    BOYS = "boys"
    GIRLS = "girls"
    MIXED = "mixed"


class CourseType(str, Enum):
    SCY = "SCY"
    SCM = "SCM"
    LCM = "LCM"


class MeetStatus(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    PENDING_VERIFICATION = "pending_verification"
    OFFICIAL = "official"
    LOCKED = "locked"


class ResultState(str, Enum):
    PRELIMINARY = "preliminary"
    OFFICIAL = "official"
    DISQUALIFIED = "disqualified"
    SCRATCH = "scratch"
    EXHIBITION = "exhibition"


class RoleCode(str, Enum):
    PLATFORM_ADMIN = "platform_admin"
    LEAGUE_ADMIN = "league_admin"
    ORG_ADMIN = "org_admin"
    HEAD_COACH = "head_coach"
    ASSISTANT_COACH = "assistant_coach"
    MEET_DIRECTOR = "meet_director"
    SCORING_OPERATOR = "scoring_operator"
    REFEREE = "referee"
    OFFICIAL = "official"
    TIMER = "timer"
    ATHLETE = "athlete"
    PARENT = "parent"
    SPECTATOR = "spectator"


class DQStatus(str, Enum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class NameVisibilityMode(str, Enum):
    FULL_NAME = "full_name"
    FIRST_LAST_INITIAL = "first_last_initial"
    TEAM_ONLY = "team_only"


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("name", "org_type", name="uq_org_name_type"),
        Index("ix_organizations_type", "org_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    org_type: Mapped[OrgType] = mapped_column(SAEnum(OrgType), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    teams: Mapped[list[Team]] = relationship(back_populates="organization")
    seasons: Mapped[list[Season]] = relationship(back_populates="organization")


class Facility(Base):
    __tablename__ = "facilities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    default_course: Mapped[CourseType] = mapped_column(SAEnum(CourseType), default=CourseType.SCY, nullable=False)
    lane_count: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    city: Mapped[Optional[str]] = mapped_column(String(100))
    state: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (
        UniqueConstraint("org_id", "label", name="uq_season_org_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    start_date: Mapped[Optional[date]] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="seasons")


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_team_org_code"),
        Index("ix_teams_org_active", "org_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    home_facility_id: Mapped[Optional[int]] = mapped_column(ForeignKey("facilities.id"))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    gender_mode: Mapped[GenderMode] = mapped_column(SAEnum(GenderMode), default=GenderMode.MIXED, nullable=False)
    division_label: Mapped[Optional[str]] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="teams")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    role_assignments: Mapped[list[RoleAssignment]] = relationship(back_populates="user")


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        Index("ix_role_assignments_user", "user_id"),
        Index("ix_role_assignments_scope", "org_id", "team_id", "season_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[RoleCode] = mapped_column(SAEnum(RoleCode), nullable=False)
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"))
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id"))
    season_id: Mapped[Optional[int]] = mapped_column(ForeignKey("seasons.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    user: Mapped[User] = relationship(back_populates="role_assignments")


class Athlete(Base):
    __tablename__ = "athletes"
    __table_args__ = (
        Index("ix_athletes_org_name", "primary_org_id", "last_name", "first_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    persistent_uid: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: str(uuid4()), nullable=False)
    primary_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    preferred_name: Mapped[Optional[str]] = mapped_column(String(100))
    grad_year: Mapped[Optional[int]] = mapped_column(Integer)
    gender: Mapped[Optional[str]] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SeasonRoster(Base):
    __tablename__ = "season_rosters"
    __table_args__ = (
        UniqueConstraint("team_id", "season_id", "athlete_id", name="uq_roster_team_season_athlete"),
        Index("ix_roster_team_season", "team_id", "season_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    eligibility_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class RuleProfile(Base):
    __tablename__ = "rule_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    governing_body: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class RuleVersion(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (
        UniqueConstraint("rule_profile_id", "version_label", name="uq_rule_profile_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_profile_id: Mapped[int] = mapped_column(ForeignKey("rule_profiles.id"), nullable=False)
    version_label: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    rules_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Meet(Base):
    __tablename__ = "meets"
    __table_args__ = (
        Index("ix_meets_season_date", "season_id", "meet_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    host_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    facility_id: Mapped[Optional[int]] = mapped_column(ForeignKey("facilities.id"))
    rule_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("rule_versions.id"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    meet_date: Mapped[date] = mapped_column(Date, nullable=False)
    course: Mapped[CourseType] = mapped_column(SAEnum(CourseType), default=CourseType.SCY, nullable=False)
    lane_count: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    status: Mapped[MeetStatus] = mapped_column(SAEnum(MeetStatus), default=MeetStatus.DRAFT, nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        Index("ix_entries_meet_team", "meet_id", "team_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meet_id: Mapped[int] = mapped_column(ForeignKey("meets.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    athlete_id: Mapped[Optional[int]] = mapped_column(ForeignKey("athletes.id"))
    event_code: Mapped[str] = mapped_column(String(32), nullable=False)
    is_relay: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_exhibition: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    seed_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class OfficialResult(Base):
    __tablename__ = "official_results"
    __table_args__ = (
        UniqueConstraint("meet_id", "entry_id", name="uq_official_result_meet_entry"),
        Index("ix_official_results_meet_event", "meet_id", "event_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meet_id: Mapped[int] = mapped_column(ForeignKey("meets.id"), nullable=False)
    entry_id: Mapped[int] = mapped_column(ForeignKey("entries.id"), nullable=False)
    event_code: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[ResultState] = mapped_column(SAEnum(ResultState), default=ResultState.PRELIMINARY, nullable=False)
    final_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    diving_score: Mapped[Optional[Numeric]] = mapped_column(Numeric(6, 2))
    place: Mapped[Optional[int]] = mapped_column(Integer)
    points_awarded: Mapped[Numeric] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    verified_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    revisions: Mapped[list[ResultRevision]] = relationship(back_populates="official_result")


class ResultRevision(Base):
    __tablename__ = "result_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    official_result_id: Mapped[int] = mapped_column(ForeignKey("official_results.id"), nullable=False)
    changed_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    previous_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    new_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    official_result: Mapped[OfficialResult] = relationship(back_populates="revisions")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id"))
    actor_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[Optional[str]] = mapped_column(String(80))
    details: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class QualifyingStandardSet(Base):
    __tablename__ = "qualifying_standard_sets"
    __table_args__ = (
        UniqueConstraint("governing_body", "season_label", "name", name="uq_qual_set"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    governing_body: Mapped[str] = mapped_column(String(64), nullable=False)
    season_label: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    course: Mapped[CourseType] = mapped_column(SAEnum(CourseType), default=CourseType.SCY, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class QualifyingStandard(Base):
    __tablename__ = "qualifying_standards"
    __table_args__ = (
        UniqueConstraint("standard_set_id", "event_code", "level_code", name="uq_qual_std_event_level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    standard_set_id: Mapped[int] = mapped_column(ForeignKey("qualifying_standard_sets.id"), nullable=False)
    event_code: Mapped[str] = mapped_column(String(32), nullable=False)
    level_code: Mapped[str] = mapped_column(String(32), nullable=False)
    level_label: Mapped[str] = mapped_column(String(80), nullable=False)
    cutoff_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    cutoff_diving_score: Mapped[Optional[Numeric]] = mapped_column(Numeric(6, 2))


class VerifiedTeamRecord(Base):
    __tablename__ = "verified_team_records"
    __table_args__ = (
        UniqueConstraint("team_id", "event_code", "course", name="uq_team_record_event_course"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    event_code: Mapped[str] = mapped_column(String(32), nullable=False)
    course: Mapped[CourseType] = mapped_column(SAEnum(CourseType), default=CourseType.SCY, nullable=False)
    holder_name: Mapped[str] = mapped_column(String(160), nullable=False)
    athlete_id: Mapped[Optional[int]] = mapped_column(ForeignKey("athletes.id"))
    record_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    record_diving_score: Mapped[Optional[Numeric]] = mapped_column(Numeric(6, 2))
    meet_name: Mapped[str] = mapped_column(String(200), nullable=False)
    achieved_date: Mapped[date] = mapped_column(Date, nullable=False)
    verified_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class PracticePlan(Base):
    __tablename__ = "practice_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    practice_date: Mapped[date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    training_group: Mapped[str] = mapped_column(String(80), default="Varsity", nullable=False)
    total_yards: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warmup_text: Mapped[Optional[str]] = mapped_column(Text)
    main_set_text: Mapped[Optional[str]] = mapped_column(Text)
    cooldown_text: Mapped[Optional[str]] = mapped_column(Text)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class PracticeAttendance(Base):
    __tablename__ = "practice_attendance"
    __table_args__ = (
        UniqueConstraint("practice_id", "athlete_id", name="uq_practice_athlete"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    practice_id: Mapped[int] = mapped_column(ForeignKey("practice_plans.id"), nullable=False)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="present", nullable=False)


class AthleteMeetAvailability(Base):
    __tablename__ = "athlete_meet_availability"
    __table_args__ = (
        UniqueConstraint("meet_id", "athlete_id", name="uq_meet_athlete_avail"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meet_id: Mapped[int] = mapped_column(ForeignKey("meets.id"), nullable=False)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(200))


class LineupScenario(Base):
    __tablename__ = "lineup_scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meet_id: Mapped[int] = mapped_column(ForeignKey("meets.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    assignments_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    estimated_points: Mapped[Numeric] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class OfficialAssignment(Base):
    __tablename__ = "official_assignments"
    __table_args__ = (
        UniqueConstraint("meet_id", "user_id", "meet_role", name="uq_official_meet_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meet_id: Mapped[int] = mapped_column(ForeignKey("meets.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    meet_role: Mapped[str] = mapped_column(String(64), nullable=False)
    certification_ref: Mapped[Optional[str]] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class DisqualificationReport(Base):
    __tablename__ = "disqualification_reports"
    __table_args__ = (
        Index("ix_dq_reports_meet_status", "meet_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meet_id: Mapped[int] = mapped_column(ForeignKey("meets.id"), nullable=False)
    entry_id: Mapped[int] = mapped_column(ForeignKey("entries.id"), nullable=False)
    event_code: Mapped[str] = mapped_column(String(32), nullable=False)
    heat_number: Mapped[Optional[int]] = mapped_column(Integer)
    lane_number: Mapped[Optional[int]] = mapped_column(Integer)
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reported_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[DQStatus] = mapped_column(SAEnum(DQStatus), default=DQStatus.SUBMITTED, nullable=False)
    reviewed_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    review_notes: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class MeetPublicationSetting(Base):
    __tablename__ = "meet_publication_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meet_id: Mapped[int] = mapped_column(ForeignKey("meets.id"), unique=True, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    show_preliminary_results: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    name_visibility: Mapped[NameVisibilityMode] = mapped_column(
        SAEnum(NameVisibilityMode), default=NameVisibilityMode.FULL_NAME, nullable=False
    )
    updated_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class League(Base):
    __tablename__ = "leagues"
    __table_args__ = (
        UniqueConstraint("governing_org_id", "name", name="uq_league_org_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    governing_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    division_structure: Mapped[Optional[str]] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class LeagueMembership(Base):
    __tablename__ = "league_memberships"
    __table_args__ = (
        UniqueConstraint("league_id", "season_id", "team_id", name="uq_league_season_team"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    conference_label: Mapped[Optional[str]] = mapped_column(String(80))


class LeagueScheduleItem(Base):
    __tablename__ = "league_schedule_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    meet_id: Mapped[Optional[int]] = mapped_column(ForeignKey("meets.id"))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False)
    facility_id: Mapped[Optional[int]] = mapped_column(ForeignKey("facilities.id"))
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
