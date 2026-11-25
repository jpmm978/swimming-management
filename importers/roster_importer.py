from __future__ import annotations

import csv
from dataclasses import dataclass, field
import io
import re
from typing import Optional

from db.schema import Athlete


@dataclass(frozen=True)
class RosterImportRow:
    row_number: int
    first_name: str
    last_name: str
    preferred_name: Optional[str] = None
    grad_year: Optional[int] = None
    gender: Optional[str] = None
    external_swimmer_id: Optional[str] = None
    eligibility_verified: bool = True


@dataclass
class RosterImportPreview:
    valid_rows: list[RosterImportRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def parse_gender(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    val = raw.strip().upper()
    if val in {"M", "MALE", "B", "BOYS"}:
        return "M"
    if val in {"F", "FEMALE", "G", "GIRLS"}:
        return "F"
    if val in {"X", "NB", "NONBINARY", "MIXED"}:
        return "X"
    return None


def parse_bool(raw: Optional[str], default: bool = True) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "verified", "eligible"}


def parse_roster_csv(csv_text: str) -> RosterImportPreview:
    preview = RosterImportPreview()
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    if not reader.fieldnames:
        preview.errors.append("CSV file appears empty or missing headers.")
        return preview

    header_map = {h.strip().lower(): h for h in reader.fieldnames if h}

    def pick(row: dict[str, str], *aliases: str) -> str:
        for alias in aliases:
            col = header_map.get(alias)
            if col and row.get(col) is not None:
                return row[col].strip()
        return ""

    for idx, raw_row in enumerate(reader, start=2):
        first = pick(raw_row, "first_name", "firstname", "first")
        last = pick(raw_row, "last_name", "lastname", "last")
        if not first or not last:
            preview.errors.append(f"Row {idx}: Missing required first_name or last_name.")
            continue

        preferred = pick(raw_row, "preferred_name", "preferred", "nickname") or None
        grad_raw = pick(raw_row, "grad_year", "graduation_year", "grad", "class")
        grad_year: Optional[int] = None
        if grad_raw:
            try:
                grad_year = int(grad_raw)
                if not (2020 <= grad_year <= 2040):
                    preview.errors.append(f"Row {idx}: grad_year '{grad_raw}' out of expected range (2020–2040).")
                    continue
            except ValueError:
                preview.errors.append(f"Row {idx}: Invalid grad_year '{grad_raw}'.")
                continue

        gender_raw = pick(raw_row, "gender", "sex", "category")
        gender = parse_gender(gender_raw) if gender_raw else None
        if gender_raw and gender is None:
            preview.errors.append(f"Row {idx}: Unrecognized gender '{gender_raw}'. Use M, F, or X.")
            continue

        ext_id = pick(raw_row, "swimmer_id", "uss_id", "external_id", "persistent_uid") or None
        eligible = parse_bool(pick(raw_row, "eligibility_verified", "eligible", "verified"), default=True)

        preview.valid_rows.append(
            RosterImportRow(
                row_number=idx,
                first_name=first,
                last_name=last,
                preferred_name=preferred,
                grad_year=grad_year,
                gender=gender,
                external_swimmer_id=ext_id,
                eligibility_verified=eligible,
            )
        )

    return preview


def parse_hytek_sdif_roster(sdif_text: str) -> RosterImportPreview:
    preview = RosterImportPreview()
    lines = [line.rstrip("\r\n") for line in sdif_text.splitlines() if line.strip()]
    d0_count = 0

    for line_num, line in enumerate(lines, start=1):
        if not line.startswith("D0"):
            continue
        d0_count += 1
        raw_name = line[11:39].strip() if len(line) >= 39 else ""
        ext_id = line[39:51].strip() if len(line) >= 51 else None
        raw_gender = line[63:64].strip() if len(line) >= 64 else ""

        if "," in raw_name:
            last_part, first_part = [p.strip() for p in raw_name.split(",", 1)]
            first_name = first_part.split()[0] if first_part else ""
            last_name = last_part
        else:
            parts = raw_name.split()
            if len(parts) < 2:
                preview.errors.append(f"Line {line_num}: Unable to parse swimmer name '{raw_name}'.")
                continue
            first_name, last_name = parts[0], " ".join(parts[1:])

        if not first_name or not last_name:
            preview.errors.append(f"Line {line_num}: Incomplete swimmer name '{raw_name}'.")
            continue

        preview.valid_rows.append(
            RosterImportRow(
                row_number=line_num,
                first_name=first_name,
                last_name=last_name,
                gender=parse_gender(raw_gender),
                external_swimmer_id=ext_id or None,
                eligibility_verified=True,
            )
        )

    if d0_count == 0:
        preview.errors.append("No SDIF D0 athlete records found in file.")
    return preview


def resolve_existing_athlete(
    row: RosterImportRow,
    existing_athletes: list[Athlete],
) -> Optional[Athlete]:
    norm_first = normalize_name(row.first_name)
    norm_last = normalize_name(row.last_name)

    if row.external_swimmer_id:
        for athlete in existing_athletes:
            if athlete.persistent_uid == row.external_swimmer_id:
                return athlete

    if row.grad_year is not None:
        for athlete in existing_athletes:
            if (
                normalize_name(athlete.first_name) == norm_first
                and normalize_name(athlete.last_name) == norm_last
                and athlete.grad_year == row.grad_year
            ):
                return athlete

    if row.gender is not None:
        for athlete in existing_athletes:
            if (
                normalize_name(athlete.first_name) == norm_first
                and normalize_name(athlete.last_name) == norm_last
                and athlete.gender == row.gender
            ):
                return athlete

    name_matches = [
        a
        for a in existing_athletes
        if normalize_name(a.first_name) == norm_first
        and normalize_name(a.last_name) == norm_last
    ]
    if len(name_matches) == 1:
        return name_matches[0]

    return None
