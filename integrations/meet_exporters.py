from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
import html
import io

from core_logic.meet_engine import EVENT_BY_CODE, ScoredPerformance, format_time_ms


def export_meet_results_csv(scored_rows: list[ScoredPerformance]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "event_code",
            "place",
            "team_code",
            "athlete_name",
            "time",
            "diving_score",
            "status",
            "is_exhibition",
            "points",
        ],
    )
    writer.writeheader()
    for r in scored_rows:
        writer.writerow(
            {
                "event_code": r.event_code,
                "place": r.place or "",
                "team_code": r.team_code,
                "athlete_name": r.athlete_name,
                "time": format_time_ms(r.final_time_ms),
                "diving_score": str(r.diving_score) if r.diving_score is not None else "",
                "status": r.status,
                "is_exhibition": r.is_exhibition,
                "points": f"{r.points:.2f}",
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

    a0 = f"A01V3      02{meet_name[:30]:<30}{date_str:<8}{'MIAA SWIM PLATFORM':<20}"
    lines.append(a0.ljust(160))

    b1 = f"B11{meet_name[:30]:<30}{'':<22}{'':<20}{'MA':<2}{'00000':<10}{'1'}{'1'}{date_str}{date_str}"
    lines.append(b1.ljust(160))

    c1 = f"C11{host_team_code[:6]:<6}{meet_name[:30]:<30}"
    lines.append(c1.ljust(160))

    for r in scored_rows:
        if r.status != "OK":
            continue
        spec = EVENT_BY_CODE.get(r.event_code)
        if spec is None or spec.is_diving:
            continue

        time_str = format_time_ms(r.final_time_ms)
        place_str = f"{r.place:02d}" if r.place else "00"
        pts_str = f"{int(r.points * 10):04d}"

        if spec.is_relay:
            e0 = (
                f"E01{r.team_code[:6]:<6}{'A':<1}{spec.gender:<1}"
                f"{spec.event_number:04d}{spec.stroke_code:<1}"
                f"{time_str:>8}{'Y':<1}{place_str}{pts_str}"
            )
            lines.append(e0.ljust(160))
        else:
            d0 = (
                f"D01{r.team_code[:6]:<6}{r.athlete_name[:28]:<28}{'':<12}"
                f"{spec.gender:<1}{spec.event_number:04d}{spec.stroke_code:<1}"
                f"{time_str:>8}{'Y':<1}{place_str}{pts_str}"
            )
            lines.append(d0.ljust(160))

    z0 = f"Z01{len(lines) + 1:06d}"
    lines.append(z0.ljust(160))
    return "\r\n".join(lines) + "\r\n"


def render_printable_results_html(
    *,
    meet_name: str,
    meet_date: date,
    gender_label: str,
    team_totals: dict[str, Decimal],
    scored_rows: list[ScoredPerformance],
) -> str:
    sorted_teams = sorted(team_totals.items(), key=lambda kv: kv[1], reverse=True)
    score_header = "  |  ".join(f"{html.escape(code)}: {pts:.1f}" for code, pts in sorted_teams)

    row_html: list[str] = []
    for r in scored_rows:
        spec = EVENT_BY_CODE.get(r.event_code)
        ev_label = spec.name if spec else r.event_code
        mark = (
            f"{r.diving_score:.2f}"
            if (spec and spec.is_diving and r.diving_score is not None)
            else format_time_ms(r.final_time_ms)
        )
        if r.status != "OK":
            mark = r.status
        place_txt = "EXH" if r.is_exhibition else (str(r.place) if r.place else "-")
        row_html.append(
            f"<tr>"
            f"<td>{html.escape(ev_label)}</td>"
            f"<td>{html.escape(place_txt)}</td>"
            f"<td>{html.escape(r.team_code)}</td>"
            f"<td>{html.escape(r.athlete_name)}</td>"
            f"<td style='text-align:right'>{html.escape(mark)}</td>"
            f"<td style='text-align:right'>{r.points:.2f}</td>"
            f"</tr>"
        )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>
  @page {{ size: letter portrait; margin: 0.4in; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 9pt; color: #111; margin: 0; }}
  .hdr {{ border-bottom: 2px solid #0b2545; padding-bottom: 4px; margin-bottom: 6px; }}
  .title {{ font-size: 12pt; font-weight: 700; color: #0b2545; }}
  .sub {{ font-size: 9pt; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 6px; }}
  th, td {{ border-bottom: 1px solid #d9e2ec; padding: 2.5px 4px; text-align: left; }}
  th {{ background: #f0f4f8; font-weight: 700; }}
  .sig-block {{ margin-top: 34px; display: flex; justify-content: space-between; gap: 28px; }}
  .sig-line {{ flex: 1; border-top: 1px solid #222; padding-top: 4px; font-size: 8.5pt; }}
</style>
</head>
<body>
  <div class="hdr">
    <div class="title">{html.escape(meet_name)} — Official {html.escape(gender_label)} Results</div>
    <div class="sub">Date: {meet_date.isoformat()} &nbsp;|&nbsp; Team Scores: <strong>{score_header}</strong></div>
  </div>
  <table>
    <thead>
      <tr>
        <th>Event</th><th>Place</th><th>Team</th><th>Athlete / Relay</th>
        <th style="text-align:right">Time / Score</th><th style="text-align:right">Points</th>
      </tr>
    </thead>
    <tbody>
      {''.join(row_html)}
    </tbody>
  </table>
  <div class="sig-block">
    <div class="sig-line">Referee Signature &amp; Certification #</div>
    <div class="sig-line">Meet Director / Scorer Signature</div>
    <div class="sig-line">Date &amp; Time Signed</div>
  </div>
</body>
</html>"""
