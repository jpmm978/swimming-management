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
