from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
import html
import io
from typing import Any, Optional

from core_logic.meet_engine import (
    EVENT_BY_CODE,
    MIAA_STANDARD_EVENTS,
    EntryCandidate,
    ScoredPerformance,
    assign_center_out_lanes,
    assign_dual_meet_odd_even_lanes,
    format_time_ms,
)


def export_meet_results_csv(scored_rows: list[ScoredPerformance]) -> str:
    buf = io.StringIO()
    fieldnames = [
        "event_code",
        "place",
        "team_code",
        "athlete_or_relay",
        "status",
        "is_exhibition",
        "final_time",
        "diving_score",
        "points",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in scored_rows:
        spec = EVENT_BY_CODE.get(row.event_code)
        result_str = (
            str(row.diving_score)
            if (spec and spec.is_diving and row.diving_score is not None)
            else format_time_ms(row.final_time_ms)
        )
        writer.writerow(
            {
                "event_code": row.event_code,
                "place": "" if row.is_exhibition else (row.place or ""),
                "team_code": row.team_code,
                "athlete_or_relay": row.athlete_name,
                "status": row.status,
                "is_exhibition": row.is_exhibition,
                "final_time": result_str,
                "diving_score": str(row.diving_score) if row.diving_score is not None else "",
                "points": f"{row.points:.2f}",
            }
        )
    return buf.getvalue()


def export_hytek_sdif_results(
    *,
    meet_name: str,
    meet_date: date,
    host_team_code: str,
    scored_rows: list[ScoredPerformance],
) -> str:
    lines: list[str] = []
    date_str = meet_date.strftime("%m%d%Y")
    lines.append(f"A012026-27 MIAA/NFHS Dual Meet {meet_name[:30]:<30}{date_str}")
    lines.append(f"B01{host_team_code:<8}{meet_name[:40]:<40}{date_str}")
    for idx, row in enumerate(scored_rows, start=1):
        spec = EVENT_BY_CODE.get(row.event_code)
        ev_num = spec.event_number if spec else idx
        time_fmt = format_time_ms(row.final_time_ms)
        place_str = str(row.place or 0).rjust(2)
        pts_str = f"{row.points:.2f}".rjust(6)
        lines.append(
            f"E0{ev_num:02d}{row.team_code:<8}{row.athlete_name[:28]:<28}"
            f"{time_fmt:>10} PL:{place_str} PTS:{pts_str}"
        )
    lines.append(f"Z01{len(scored_rows):04d}")
    return "\n".join(lines) + "\n"


def render_printable_results_html(
    *,
    meet_name: str,
    meet_date: date,
    gender_label: str,
    team_totals: dict[str, Decimal],
    scored_rows: list[ScoredPerformance],
) -> str:
    totals_html = "".join(
        f"<li><strong>{html.escape(team)}</strong>: {pts:.2f} pts</li>"
        for team, pts in sorted(team_totals.items(), key=lambda item: item[1], reverse=True)
    ) or "<li>No scored events recorded.</li>"

    rows_html = []
    for r in scored_rows:
        spec = EVENT_BY_CODE.get(r.event_code)
        ev_label = f"#{spec.event_number} {spec.name}" if spec else r.event_code
        mark = (
            str(r.diving_score)
            if (spec and spec.is_diving and r.diving_score is not None)
            else format_time_ms(r.final_time_ms)
        )
        place_label = "EXH" if r.is_exhibition else (str(r.place) if r.place else "-")
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(ev_label)}</td>"
            f"<td>{html.escape(place_label)}</td>"
            f"<td>{html.escape(r.team_code)}</td>"
            f"<td>{html.escape(r.athlete_name)}</td>"
            f"<td>{html.escape(mark)}</td>"
            f"<td>{html.escape(r.status)}</td>"
            f"<td>{r.points:.2f}</td>"
            "</tr>"
        )

    body_rows = "\n".join(rows_html) or "<tr><td colspan='7'>No results recorded yet.</td></tr>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(meet_name)} — {html.escape(gender_label)} Official Report</title>
<style>
  @page {{ size: letter portrait; margin: 0.45in; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 11px; color: #111; margin: 0; }}
  h1 {{ font-size: 16px; margin: 0 0 4px 0; }}
  .meta {{ font-size: 11px; color: #444; margin-bottom: 10px; }}
  .scores {{ display: flex; gap: 20px; margin-bottom: 12px; padding: 8px; background: #f5f7fa; border: 1px solid #cbd5e1; }}
  .scores ul {{ list-style: none; padding: 0; margin: 0; display: flex; gap: 18px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 4px 6px; text-align: left; font-size: 10.5px; }}
  th {{ background: #e2e8f0; }}
  .signoff {{ margin-top: 18px; display: flex; justify-content: space-between; gap: 24px; }}
  .sig-line {{ flex: 1; border-top: 1px solid #111; padding-top: 4px; font-size: 10px; }}
</style>
</head>
<body>
  <h1>{html.escape(meet_name)} — {html.escape(gender_label)} Official Meet Report</h1>
  <div class="meta">Date: {html.escape(meet_date.isoformat())} · Governing Rules: MIAA / NFHS</div>
  <div class="scores"><strong>Final Score:</strong><ul>{totals_html}</ul></div>
  <table>
    <thead>
      <tr>
        <th>Event</th><th>Place</th><th>Team</th><th>Swimmer / Relay</th><th>Time / Score</th><th>Status</th><th>Pts</th>
      </tr>
    </thead>
    <tbody>
      {body_rows}
    </tbody>
  </table>
  <div class="signoff">
    <div class="sig-line">Referee Signature / Date</div>
    <div class="sig-line">Scoring / Meet Director Signature / Date</div>
  </div>
</body>
</html>"""


def _build_seeded_event_map(
    *,
    candidates: list[EntryCandidate],
    lane_count: int,
    seeding_mode: str = "Dual-Meet Odd / Even Lanes",
    odd_team_id: Optional[int] = None,
    even_team_id: Optional[int] = None,
    heat_lane_overrides: Optional[dict[int, dict[str, int]]] = None,
    db_entry_attrs: Optional[dict[int, dict[str, Any]]] = None,
) -> dict[str, list[dict]]:
    overrides = heat_lane_overrides or {}
    db_attrs = db_entry_attrs or {}
    event_map: dict[str, list[dict]] = {}

    for ev in MIAA_STANDARD_EVENTS:
        ev_entries = [c for c in candidates if c.event_code == ev.code]
        if not ev_entries:
            event_map[ev.code] = []
            continue

        if seeding_mode == "Dual-Meet Odd / Even Lanes" and odd_team_id is not None:
            seeded = assign_dual_meet_odd_even_lanes(
                ev_entries,
                odd_team_id=odd_team_id,
                even_team_id=even_team_id,
                lane_count=lane_count,
            )
        else:
            seeded = assign_center_out_lanes(ev_entries, lane_count=lane_count)

        for row in seeded:
            eid = int(row["entry_id"])
            ov = overrides.get(eid, {})
            attrs = db_attrs.get(eid, {})
            if "heat" in ov:
                row["heat"] = ov["heat"]
            elif attrs.get("heat_number") is not None:
                row["heat"] = attrs["heat_number"]

            if "lane" in ov:
                row["lane"] = ov["lane"]
            elif attrs.get("lane_number") is not None:
                row["lane"] = attrs["lane_number"]

            if "is_exhibition" in attrs:
                row["is_exhibition"] = bool(attrs["is_exhibition"])

        seeded.sort(key=lambda r: (int(r["heat"]), int(r["lane"])))
        event_map[ev.code] = seeded

    return event_map


def render_swimmer_checkin_csv(
    *,
    roster_rows: list[dict[str, Any]],
    candidates: list[EntryCandidate],
    team_code_filter: Optional[str] = None,
) -> str:
    buf = io.StringIO()
    fieldnames = [
        "checked_in",
        "team_code",
        "last_name",
        "first_name",
        "grad_year",
        "gender",
        "event_count",
        "events",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()

    for r in roster_rows:
        t_code = str(r.get("team_code") or "")
        if team_code_filter and t_code != team_code_filter:
            continue
        aid = r.get("athlete_id")
        swimmer_events: list[str] = []
        for c in candidates:
            spec = EVENT_BY_CODE.get(c.event_code)
            ev_short = f"#{spec.event_number} {spec.name}" if spec else c.event_code
            if not c.is_relay and c.athlete_id == aid:
                exh_tag = " (EXH)" if c.is_exhibition else ""
                swimmer_events.append(f"{ev_short}{exh_tag}")
            elif c.is_relay and aid in (c.relay_member_ids or []):
                rel_tag = f" [{c.relay_label}]" if c.relay_label else " [Relay]"
                swimmer_events.append(f"{ev_short}{rel_tag}")

        writer.writerow(
            {
                "checked_in": "",
                "team_code": t_code,
                "last_name": r.get("last_name", ""),
                "first_name": r.get("first_name", ""),
                "grad_year": r.get("grad_year") or "",
                "gender": r.get("gender") or "",
                "event_count": len(swimmer_events),
                "events": " | ".join(swimmer_events),
            }
        )

    return buf.getvalue()


def render_swimmer_checkin_html(
    *,
    meet_name: str,
    meet_date: date,
    roster_rows: list[dict[str, Any]],
    candidates: list[EntryCandidate],
    team_code_filter: Optional[str] = None,
) -> str:
    filtered = [
        r
        for r in roster_rows
        if not team_code_filter or str(r.get("team_code") or "") == team_code_filter
    ]
    filtered.sort(
        key=lambda r: (
            str(r.get("team_code") or ""),
            str(r.get("last_name") or ""),
            str(r.get("first_name") or ""),
        )
    )

    body_rows: list[str] = []
    for r in filtered:
        aid = r.get("athlete_id")
        swimmer_events: list[str] = []
        for c in candidates:
            spec = EVENT_BY_CODE.get(c.event_code)
            ev_short = f"#{spec.event_number} {spec.name}" if spec else c.event_code
            if not c.is_relay and c.athlete_id == aid:
                exh_tag = " <em>(EXH)</em>" if c.is_exhibition else ""
                swimmer_events.append(f"{html.escape(ev_short)}{exh_tag}")
            elif c.is_relay and aid in (c.relay_member_ids or []):
                rel_tag = f" <strong>[{html.escape(c.relay_label)}]</strong>" if c.relay_label else " <strong>[Relay]</strong>"
                swimmer_events.append(f"{html.escape(ev_short)}{rel_tag}")

        name_str = f"{r.get('last_name', '')}, {r.get('first_name', '')}"
        if r.get("preferred_name"):
            name_str += f" ({r['preferred_name']})"
        grad_str = str(r.get("grad_year") or "")
        gender_str = str(r.get("gender") or "")
        t_code = str(r.get("team_code") or "")
        events_html = "<br>".join(swimmer_events) if swimmer_events else "<span style='color:#64748b'>No entries</span>"

        body_rows.append(
            "<tr>"
            "<td class='chk'><span class='box'>&#9744;</span></td>"
            f"<td><strong>{html.escape(t_code)}</strong></td>"
            f"<td><strong>{html.escape(name_str)}</strong></td>"
            f"<td>{html.escape(grad_str)}</td>"
            f"<td>{html.escape(gender_str)}</td>"
            f"<td class='num'>{len(swimmer_events)}/4</td>"
            f"<td>{events_html}</td>"
            "</tr>"
        )

    rows_joined = "\n".join(body_rows) or "<tr><td colspan='7'>No roster swimmers found.</td></tr>"
    scope_label = f"Team: {team_code_filter}" if team_code_filter else "All Teams"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(meet_name)} — Swimmer Check-In List</title>
<style>
  @page {{ size: letter portrait; margin: 0.4in; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10.5px; color: #111; margin: 0; }}
  h1 {{ font-size: 15px; margin: 0 0 3px 0; }}
  .meta {{ font-size: 10.5px; color: #334155; margin-bottom: 10px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ border: 1px solid #94a3b8; padding: 4px 6px; vertical-align: top; }}
  th {{ background: #e2e8f0; text-align: left; font-size: 10px; }}
  .chk {{ width: 34px; text-align: center; }}
  .box {{ font-size: 15px; line-height: 1; }}
  .num {{ text-align: center; white-space: nowrap; }}
  tr {{ break-inside: avoid; }}
</style>
</head>
<body>
  <h1>{html.escape(meet_name)} — Swimmer Check-In Sheet</h1>
  <div class="meta">Date: {html.escape(meet_date.isoformat())} · {html.escape(scope_label)} · Total Swimmers: {len(filtered)}</div>
  <table>
    <thead>
      <tr>
        <th>In</th><th>Team</th><th>Swimmer Name</th><th>Class</th><th>Gen</th><th>Events</th><th>Entered Events</th>
      </tr>
    </thead>
    <tbody>
      {rows_joined}
    </tbody>
  </table>
</body>
</html>"""


def render_meet_program_html(
    *,
    meet_name: str,
    meet_date: date,
    course_label: str,
    lane_count: int,
    candidates: list[EntryCandidate],
    athlete_grad_map: Optional[dict[int, Optional[int]]] = None,
    athlete_name_map: Optional[dict[int, str]] = None,
    seeding_mode: str = "Dual-Meet Odd / Even Lanes",
    odd_team_id: Optional[int] = None,
    even_team_id: Optional[int] = None,
    heat_lane_overrides: Optional[dict[int, dict[str, int]]] = None,
    db_entry_attrs: Optional[dict[int, dict[str, Any]]] = None,
) -> str:
    grad_map = athlete_grad_map or {}
    name_map = athlete_name_map or {}
    cand_by_id = {c.entry_id: c for c in candidates}
    event_seeded = _build_seeded_event_map(
        candidates=candidates,
        lane_count=lane_count,
        seeding_mode=seeding_mode,
        odd_team_id=odd_team_id,
        even_team_id=even_team_id,
        heat_lane_overrides=heat_lane_overrides,
        db_entry_attrs=db_entry_attrs,
    )

    event_blocks: list[str] = []
    for ev in MIAA_STANDARD_EVENTS:
        rows = event_seeded.get(ev.code, [])
        if not rows:
            continue

        by_heat: dict[int, list[dict]] = {}
        for r in rows:
            by_heat.setdefault(int(r["heat"]), []).append(r)

        heat_nums = sorted(by_heat.keys())
        total_heats = len(heat_nums)
        heat_sections: list[str] = []

        for idx_pos, h_num in enumerate(heat_nums, start=1):
            h_entries = sorted(by_heat[h_num], key=lambda x: int(x["lane"]))
            if ev.is_relay:
                sub_hdr = (
                    f"<tr class='heat-hdr'><td colspan='4'>Heat {idx_pos} of {total_heats} Finals</td></tr>"
                    "<tr class='col-hdr'><th>Lane</th><th>Team</th><th>Relay</th><th>Seed Time</th></tr>"
                )
            else:
                sub_hdr = (
                    f"<tr class='heat-hdr'><td colspan='4'>Heat {idx_pos} of {total_heats} Finals</td></tr>"
                    "<tr class='col-hdr'><th>Lane</th><th>Name</th><th>Class / Team</th><th>Seed Time</th></tr>"
                )

            entry_tr_list: list[str] = []
            for ent in h_entries:
                cand = cand_by_id.get(int(ent["entry_id"]))
                is_exh = bool(ent.get("is_exhibition"))
                seed_display = "XNT" if (is_exh and ent["seed_time"] == "NT") else ent["seed_time"]
                if is_exh and seed_display != "XNT":
                    seed_display = f"{seed_display} (EXH)"

                if ev.is_relay:
                    squad_let = (cand.relay_label.split()[-1] if cand and cand.relay_label else "A")
                    leg_lines = ""
                    if cand and cand.relay_member_ids:
                        legs_formatted = []
                        for leg_i, mid in enumerate(cand.relay_member_ids, start=1):
                            m_name = name_map.get(mid, f"Swimmer #{mid}")
                            m_grad = grad_map.get(mid)
                            grad_txt = f" {m_grad}" if m_grad else ""
                            legs_formatted.append(f"{leg_i}) {html.escape(m_name)}{html.escape(grad_txt)}")
                        leg_lines = "<div class='relay-legs'>" + " · ".join(legs_formatted) + "</div>"

                    entry_tr_list.append(
                        "<tr>"
                        f"<td class='lane-c'>{ent['lane']}</td>"
                        f"<td><strong>{html.escape(ent['team_code'])}</strong></td>"
                        f"<td><strong>{html.escape(squad_let)}</strong>{leg_lines}</td>"
                        f"<td class='time-c'>{html.escape(seed_display)}</td>"
                        "</tr>"
                    )
                else:
                    sw_name = cand.athlete_name if cand else ent["athlete_name"]
                    grad_val = grad_map.get(cand.athlete_id) if (cand and cand.athlete_id) else None
                    class_team = f"{grad_val or ''} {ent['team_code']}".strip()
                    entry_tr_list.append(
                        "<tr>"
                        f"<td class='lane-c'>{ent['lane']}</td>"
                        f"<td>{html.escape(sw_name)}</td>"
                        f"<td>{html.escape(class_team)}</td>"
                        f"<td class='time-c'>{html.escape(seed_display)}</td>"
                        "</tr>"
                    )

            heat_sections.append(
                "<table class='ev-table'>"
                f"<thead>{sub_hdr}</thead>"
                f"<tbody>{''.join(entry_tr_list)}</tbody>"
                "</table>"
            )

        event_blocks.append(
            "<div class='ev-block'>"
            f"<div class='ev-title'>#{ev.event_number} {html.escape(ev.name)}</div>"
            f"{''.join(heat_sections)}"
            "</div>"
        )

    blocks_html = "".join(event_blocks) or "<p>No seeded entries available for this meet.</p>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(meet_name)} — Meet Program</title>
<style>
  @page {{ size: letter portrait; margin: 0.4in; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 9.5px; color: #0f172a; margin: 0; }}
  .top-bar {{ display: flex; justify-content: space-between; border-bottom: 2px solid #0f172a; padding-bottom: 4px; margin-bottom: 8px; font-size: 10.5px; }}
  .meet-hdr {{ text-align: center; font-size: 13.5px; font-weight: bold; margin-bottom: 10px; }}
  .grid {{ column-count: 2; column-gap: 18px; }}
  .ev-block {{ break-inside: avoid; margin-bottom: 11px; }}
  .ev-title {{ font-weight: bold; font-size: 10px; background: #e2e8f0; padding: 3px 5px; border-top: 1px solid #334155; border-bottom: 1px solid #334155; }}
  .ev-table {{ width: 100%; border-collapse: collapse; margin-bottom: 4px; }}
  .heat-hdr td {{ font-weight: bold; font-size: 9px; background: #f8fafc; padding: 2px 4px; border-bottom: 1px solid #cbd5e1; }}
  .col-hdr th {{ font-size: 8.5px; text-align: left; padding: 2px 4px; border-bottom: 1px solid #94a3b8; color: #334155; }}
  td {{ padding: 2px 4px; border-bottom: 1px dotted #cbd5e1; vertical-align: top; }}
  .lane-c {{ width: 26px; text-align: center; font-weight: bold; }}
  .time-c {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
  .relay-legs {{ font-size: 8px; color: #475569; line-height: 1.3; margin-top: 1px; }}
</style>
</head>
<body>
  <div class="top-bar">
    <span><strong>{html.escape(meet_name)}</strong> ({html.escape(course_label)})</span>
    <span>Date: {html.escape(meet_date.isoformat())} · {lane_count} Lanes</span>
  </div>
  <div class="meet-hdr">Meet Program (Heat Sheet)</div>
  <div class="grid">
    {blocks_html}
  </div>
</body>
</html>"""


def render_timer_sheets_html(
    *,
    meet_name: str,
    meet_date: date,
    lane_count: int,
    candidates: list[EntryCandidate],
    version: str = "by_lane",
    seeding_mode: str = "Dual-Meet Odd / Even Lanes",
    odd_team_id: Optional[int] = None,
    even_team_id: Optional[int] = None,
    heat_lane_overrides: Optional[dict[int, dict[str, int]]] = None,
    db_entry_attrs: Optional[dict[int, dict[str, Any]]] = None,
) -> str:
    event_seeded = _build_seeded_event_map(
        candidates=candidates,
        lane_count=lane_count,
        seeding_mode=seeding_mode,
        odd_team_id=odd_team_id,
        even_team_id=even_team_id,
        heat_lane_overrides=heat_lane_overrides,
        db_entry_attrs=db_entry_attrs,
    )

    sections: list[str] = []

    if version == "by_lane":
        for lane_num in range(1, lane_count + 1):
            lane_rows_html: list[str] = []
            for ev in MIAA_STANDARD_EVENTS:
                for row in event_seeded.get(ev.code, []):
                    if int(row["lane"]) == lane_num:
                        exh_badge = " [EXH]" if row.get("is_exhibition") else ""
                        lane_rows_html.append(
                            "<tr>"
                            f"<td>#{ev.event_number} {html.escape(ev.name)}</td>"
                            f"<td class='center'>H{row['heat']}</td>"
                            f"<td><strong>{html.escape(row['team_code'])}</strong> — {html.escape(row['athlete_name'])}{html.escape(exh_badge)}</td>"
                            f"<td class='center'>{html.escape(row['seed_time'])}</td>"
                            "<td class='blank'></td>"
                            "<td class='blank'></td>"
                            "<td class='blank'></td>"
                            "<td class='blank'></td>"
                            "</tr>"
                        )
            table_body = "\n".join(lane_rows_html) or f"<tr><td colspan='8'>No swims assigned to Lane {lane_num}.</td></tr>"
            sections.append(
                "<div class='sheet-page'>"
                f"<h2>{html.escape(meet_name)} — Lane {lane_num} Timer Sheet</h2>"
                f"<div class='sub'>Date: {html.escape(meet_date.isoformat())} · Watch / Backup Timer Copy</div>"
                "<table>"
                "<thead><tr><th>Event</th><th>Heat</th><th>Swimmer / Relay</th><th>Seed</th><th>Watch 1</th><th>Watch 2</th><th>Place</th><th>DQ / Notes</th></tr></thead>"
                f"<tbody>{table_body}</tbody>"
                "</table>"
                "<div class='sig'>Lane Timer Signature: ___________________________</div>"
                "</div>"
            )

    elif version == "by_event":
        ev_chunks: list[str] = []
        for ev in MIAA_STANDARD_EVENTS:
            rows = event_seeded.get(ev.code, [])
            if not rows:
                continue
            by_heat: dict[int, list[dict]] = {}
            for r in rows:
                by_heat.setdefault(int(r["heat"]), []).append(r)

            heat_tables: list[str] = []
            for h_num in sorted(by_heat.keys()):
                h_map = {int(r["lane"]): r for r in by_heat[h_num]}
                lane_trs: list[str] = []
                for l_num in range(1, lane_count + 1):
                    ent = h_map.get(l_num)
                    if ent:
                        exh_badge = " [EXH]" if ent.get("is_exhibition") else ""
                        sw_txt = f"<strong>{html.escape(ent['team_code'])}</strong> · {html.escape(ent['athlete_name'])}{html.escape(exh_badge)}"
                        seed_txt = html.escape(ent["seed_time"])
                    else:
                        sw_txt = "<span style='color:#94a3b8'>— Open Lane —</span>"
                        seed_txt = ""
                    lane_trs.append(
                        "<tr>"
                        f"<td class='center'><strong>{l_num}</strong></td>"
                        f"<td>{sw_txt}</td>"
                        f"<td class='center'>{seed_txt}</td>"
                        "<td class='blank'></td>"
                        "<td class='blank'></td>"
                        "<td class='blank'></td>"
                        "</tr>"
                    )
                heat_tables.append(
                    f"<h3>Heat {h_num}</h3>"
                    "<table>"
                    "<thead><tr><th>Lane</th><th>Swimmer / Relay</th><th>Seed</th><th>Final Time</th><th>Place</th><th>DQ / SCR</th></tr></thead>"
                    f"<tbody>{''.join(lane_trs)}</tbody>"
                    "</table>"
                )
            ev_chunks.append(
                "<div class='ev-card'>"
                f"<h2>#{ev.event_number} · {html.escape(ev.name)}</h2>"
                f"{''.join(heat_tables)}"
                "</div>"
            )
        sections.append(
            "<div class='sheet-page'>"
            f"<h1>{html.escape(meet_name)} — Event-by-Event Timer Sheets</h1>"
            f"<div class='sub'>Date: {html.escape(meet_date.isoformat())}</div>"
            f"{''.join(ev_chunks) or '<p>No entries seeded yet.</p>'}"
            "</div>"
        )

    else:
        # Compact / Clip-Board Fill-In version
        compact_rows: list[str] = []
        for ev in MIAA_STANDARD_EVENTS:
            for row in event_seeded.get(ev.code, []):
                compact_rows.append(
                    "<tr>"
                    f"<td>#{ev.event_number}</td>"
                    f"<td class='center'>H{row['heat']} / L{row['lane']}</td>"
                    f"<td><strong>{html.escape(row['team_code'])}</strong> {html.escape(row['athlete_name'])}</td>"
                    f"<td class='center'>{html.escape(row['seed_time'])}</td>"
                    "<td class='write-box'>______ . ____</td>"
                    "<td class='center'>[ ] OK &nbsp; [ ] DQ &nbsp; [ ] SCR</td>"
                    "</tr>"
                )
        sections.append(
            "<div class='sheet-page'>"
            f"<h1>{html.escape(meet_name)} — Deck Clipboard Fill-In Timer Sheet</h1>"
            f"<div class='sub'>Date: {html.escape(meet_date.isoformat())} · Quick Write-In Version</div>"
            "<table>"
            "<thead><tr><th>Ev</th><th>H / L</th><th>Swimmer / Relay</th><th>Seed</th><th>Recorded Time</th><th>Status</th></tr></thead>"
            f"<tbody>{''.join(compact_rows) or '<tr><td colspan=\"6\">No entries seeded yet.</td></tr>'}</tbody>"
            "</table>"
            "</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(meet_name)} — Timer Sheets</title>
<style>
  @page {{ size: letter portrait; margin: 0.4in; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10px; color: #111; margin: 0; }}
  h1 {{ font-size: 15px; margin: 0 0 3px 0; }}
  h2 {{ font-size: 12.5px; margin: 8px 0 3px 0; }}
  h3 {{ font-size: 10.5px; margin: 6px 0 2px 0; color: #1e293b; }}
  .sub {{ font-size: 10px; color: #475569; margin-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; }}
  th, td {{ border: 1px solid #64748b; padding: 4px 5px; }}
  th {{ background: #e2e8f0; font-size: 9.5px; }}
  .center {{ text-align: center; }}
  .blank {{ width: 68px; background: #fff; }}
  .write-box {{ width: 92px; text-align: center; letter-spacing: 1px; color: #64748b; }}
  .sig {{ margin-top: 10px; font-size: 10px; }}
  .sheet-page {{ page-break-after: always; }}
  .sheet-page:last-child {{ page-break-after: auto; }}
  .ev-card {{ break-inside: avoid; margin-bottom: 12px; }}
</style>
</head>
<body>
  {''.join(sections)}
</body>
</html>"""
