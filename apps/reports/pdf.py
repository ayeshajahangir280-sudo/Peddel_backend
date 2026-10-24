"""PDF builders (ReportLab). Always generated from persisted records, never from client state."""
import io
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from apps.matches.models import MatchStatus
from apps.standings.models import TournamentStanding
from apps.tournaments.models import TournamentFixture, TournamentStatus
from apps.tournaments.services import final_rankings

INK = colors.HexColor("#0f172a")
ACCENT = colors.HexColor("#0d9488")
MUTED = colors.HexColor("#64748b")
LINE = colors.HexColor("#cbd5e1")
HEAD_BG = colors.HexColor("#0f172a")

_base = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=_base["Title"], fontSize=20, leading=24, textColor=INK, alignment=0, spaceAfter=2)
H2 = ParagraphStyle("H2", parent=_base["Heading2"], fontSize=13, leading=16, textColor=ACCENT, spaceBefore=14, spaceAfter=6)
BODY = ParagraphStyle("Body", parent=_base["BodyText"], fontSize=9.5, leading=13, textColor=INK)
SMALL = ParagraphStyle("Small", parent=BODY, fontSize=8, leading=10, textColor=MUTED)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=9, leading=11)
CELLB = ParagraphStyle("CellB", parent=CELL, fontName="Helvetica-Bold")
CENTER = ParagraphStyle("Center", parent=CELL, alignment=TA_CENTER)
BANNER = ParagraphStyle("Banner", parent=BODY, fontName="Helvetica-Bold", textColor=colors.HexColor("#92400e"),
                        backColor=colors.HexColor("#fef3c7"), borderPadding=6, leading=14)


def _p(text, style=CELL):
    return Paragraph(escape(str(text if text is not None else "")), style)


def _logo(field, width=28 * mm):
    """Logo from ANY storage backend (local disk or Cloudinary): read through the storage API, not a filesystem path."""
    if not field:
        return None
    try:
        with field.open("rb") as fh:
            data = fh.read(5 * 1024 * 1024)
        img = Image(io.BytesIO(data))
        ratio = img.imageHeight / float(img.imageWidth)
        img.drawWidth, img.drawHeight = width, width * ratio
        if img.drawHeight > 22 * mm:
            img.drawHeight, img.drawWidth = 22 * mm, 22 * mm / ratio
        return img
    except Exception:  # a missing/unsupported image must never break a report
        return None


def _table(data, col_widths, header=True, extra=None):
    t = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                  ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")])]
    t.setStyle(TableStyle(style + (extra or [])))
    return t


def _head(cells):
    return [Paragraph(f"<font color='white'><b>{c}</b></font>", CELL) for c in cells]


def _doc(buf, title):
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, f"{title}  |  Generated {timezone.now():%Y-%m-%d %H:%M} UTC  |  Administrative document")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(2)
        canvas.line(18 * mm, A4[1] - 12 * mm, A4[0] - 18 * mm, A4[1] - 12 * mm)
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=20 * mm,
                            bottomMargin=18 * mm, title=title, author="Padel Management System")
    return doc, footer


def _fmt_sets(sets):
    out = []
    for s in sets:
        g = s["games"]
        txt = f"{g[0]}-{g[1]}"
        if s.get("tiebreak"):
            lo = min(s["tiebreak"])
            txt += f" ({lo})" if not s.get("super") else f" [{s['tiebreak'][0]}-{s['tiebreak'][1]}]"
        out.append(txt)
    return "  ".join(out) if out else "-"


def _header_block(title, subtitle, logo_field=None):
    left = [Paragraph(title, H1), Paragraph(subtitle, SMALL)]
    logo = _logo(logo_field)
    if logo:
        t = Table([[left, logo]], colWidths=[130 * mm, 40 * mm])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
        return [t, Spacer(1, 6)]
    return left + [Spacer(1, 6)]


def build_match_pdf(match):
    """Raises ValueError if the match has no valid completed result."""
    state = match.state.data
    if match.status != MatchStatus.COMPLETED or not state.get("completed") or state.get("winner") not in (0, 1):
        raise ValueError("Match does not have a valid completed result")
    buf = io.BytesIO()
    doc, footer = _doc(buf, f"Match Result {match.reference}")
    teams = [match.team_a, match.team_b]
    players = [[p.player.full_name for p in match.participants.all() if p.team_index == i] for i in (0, 1)]
    winner = teams[state["winner"]]
    story = _header_block(
        "Match Result",
        f"{match.tournament.name if match.tournament else 'Friendly match'}  |  Reference {match.reference}",
        match.tournament.logo if match.tournament_id else None,
    )
    info = [
        ["Match", match.name],
        ["Date", f"{match.scheduled_at or match.started_at or match.completed_at:%A %d %B %Y}"],
        ["Completed", f"{match.completed_at:%Y-%m-%d %H:%M} UTC" if match.completed_at else "-"],
        ["Court", match.court.name if match.court else "-"],
        ["Umpire", match.umpire.display_name if match.umpire else "-"],
        ["Duration", f"{match.duration_seconds() // 3600}h {(match.duration_seconds() % 3600) // 60:02d}m" if match.started_at else "-"],
        ["Result status", "Official (verified)" if match.result_verified else "Completed - awaiting verification"],
    ]
    story.append(_table([[_p(a, CELLB), _p(b)] for a, b in info], [35 * mm, 135 * mm], header=False))
    story.append(Paragraph("Teams", H2))
    story.append(_table(
        [_head(["Team", "Players"])] + [[_p(teams[i].name, CELLB), _p(" & ".join(players[i]))] for i in (0, 1)],
        [60 * mm, 110 * mm],
    ))
    story.append(Paragraph("Final result", H2))
    sets = state.get("sets", [])
    rows = [_head(["Set", teams[0].name, teams[1].name, "Tie-break"])]
    for i, s in enumerate(sets, start=1):
        g = s["games"]
        label = "Super tie-break" if s.get("super") else f"Set {i}"
        tb = f"{s['tiebreak'][0]}-{s['tiebreak'][1]}" if s.get("tiebreak") else ""
        rows.append([_p(label), _p("1" if s.get("super") and g[0] > g[1] else g[0] if not s.get("super") else "0", CENTER),
                     _p("1" if s.get("super") and g[1] > g[0] else g[1] if not s.get("super") else "0", CENTER), _p(tb, CENTER)])
    story.append(_table(rows, [40 * mm, 50 * mm, 50 * mm, 30 * mm]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"<b>Winner: {escape(winner.name)}</b> ({escape(_fmt_sets(sets))})", BODY))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _fixture_team(f, slot):
    team = f.team_a if slot == 0 else f.team_b
    src = f.slot_a_source if slot == 0 else f.slot_b_source
    return team.name if team else src.get("label", "TBD")


def build_tournament_pdf(tournament, sections=("info", "teams", "standings", "fixtures", "knockout", "champion")):
    buf = io.BytesIO()
    final = tournament.status == TournamentStatus.COMPLETED and tournament.champion_id is not None
    kind = "Final Results" if final else "Progress Report"
    doc, footer = _doc(buf, f"{tournament.name} - {kind}")
    story = _header_block(
        tournament.name, f"Tournament {kind.lower()}  |  Status: {tournament.get_status_display()}", tournament.logo)
    if not final:
        story += [Paragraph("PROGRESS REPORT - tournament not finished. Standings and brackets are provisional and "
                            "must not be treated as final results.", BANNER), Spacer(1, 6)]

    if "info" in sections:
        info = [
            ["Dates", f"{tournament.start_date or '-'}  to  {tournament.end_date or '-'}"],
            ["Venue", tournament.venue or "-"], ["Organizer", tournament.organizer or "-"],
            ["Format", tournament.get_format_display()],
            ["Ranking criteria", " > ".join(c.replace("_", " ") for c in tournament.ranking_criteria)],
        ]
        story.append(_table([[_p(a, CELLB), _p(b)] for a, b in info], [35 * mm, 135 * mm], header=False))

    if "teams" in sections:
        story.append(Paragraph("Participating teams", H2))
        regs = tournament.registrations.filter(withdrawn=False).select_related("team", "group").prefetch_related("team__memberships__player")
        rows = [_head(["#", "Team", "Players", "Group"])]
        for i, r in enumerate(regs, start=1):
            rows.append([_p(i), _p(r.team.name, CELLB), _p(" & ".join(m.player.full_name for m in r.team.memberships.all())),
                         _p(r.group.name if r.group else "-")])
        story.append(_table(rows, [10 * mm, 55 * mm, 80 * mm, 25 * mm]))

    fixtures = list(TournamentFixture.objects.filter(tournament=tournament)
                    .select_related("group", "team_a", "team_b", "match__state", "match__court")
                    .order_by("stage", "round_number", "position"))

    if "standings" in sections:
        story.append(Paragraph("Group standings" + ("" if final else " (provisional)"), H2))
        by_group = {}
        for s in TournamentStanding.objects.filter(tournament=tournament).select_related("team", "group").order_by("group__order", "rank"):
            by_group.setdefault(s.group.name, []).append(s)
        if not by_group:
            story.append(Paragraph("No group stage standings available.", BODY))
        for gname, rows_ in by_group.items():
            data = [_head(["#", "Team", "P", "W", "L", "Pts", "Sets", "SD", "Games", "GD"])]
            for s in rows_:
                data.append([_p(s.rank), _p(s.team.name, CELLB), _p(s.played), _p(s.wins), _p(s.losses), _p(s.points),
                             _p(f"{s.sets_for}-{s.sets_against}"), _p(f"{s.set_difference:+d}"),
                             _p(f"{s.games_for}-{s.games_against}"), _p(f"{s.game_difference:+d}")])
            story.append(KeepTogether([Paragraph(gname, BODY), Spacer(1, 3), _table(
                data, [8 * mm, 52 * mm, 10 * mm, 10 * mm, 10 * mm, 12 * mm, 20 * mm, 12 * mm, 24 * mm, 12 * mm],
                extra=[("BACKGROUND", (0, i + 1), (-1, i + 1), colors.HexColor("#ccfbf1")) for i, s in enumerate(rows_) if s.qualified])]))
            story.append(Spacer(1, 8))

    if "fixtures" in sections:
        story.append(Paragraph("Fixtures and results", H2))
        rows = [_head(["Round", "Match", "Court", "Status", "Result"])]
        for f in fixtures:
            if f.is_bye:
                continue
            m = f.match
            res = "-"
            if m and m.status == MatchStatus.COMPLETED:
                res = f"{_fmt_sets(m.state.data.get('sets', []))}  (W: {m.winner.name if m.winner else '-'})"
            rows.append([_p(f.round_name), _p(f"{_fixture_team(f, 0)} vs {_fixture_team(f, 1)}"),
                         _p(m.court.name if m and m.court else "-"), _p(m.get_status_display() if m else "-"), _p(res)])
        story.append(_table(rows, [30 * mm, 56 * mm, 20 * mm, 22 * mm, 42 * mm]))

    if "knockout" in sections:
        ko = [f for f in fixtures if f.stage == "knockout" and not f.is_bye]
        if ko:
            story.append(Paragraph("Knockout bracket", H2))
            rounds = {}
            for f in ko:
                rounds.setdefault((f.round_number, f.round_name), []).append(f)
            for (_, name), fx in sorted(rounds.items()):
                rows = [_head([name, "Result"])]
                for f in fx:
                    m = f.match
                    res = "Pending"
                    if m and m.status == MatchStatus.COMPLETED:
                        res = f"{_fmt_sets(m.state.data.get('sets', []))}  (W: {m.winner.name})"
                    elif m and m.status in (MatchStatus.LIVE, MatchStatus.PAUSED):
                        res = "In progress"
                    rows.append([_p(f"{_fixture_team(f, 0)} vs {_fixture_team(f, 1)}"), _p(res)])
                story.append(KeepTogether([_table(rows, [100 * mm, 70 * mm]), Spacer(1, 8)]))

    if "champion" in sections:
        story.append(Paragraph("Champion and final ranking", H2))
        if final:
            ranks = final_rankings(tournament)
            story.append(Paragraph(f"<b>Champion: {escape(tournament.champion.name)}</b>", BODY))
            if ranks:
                story.append(Spacer(1, 4))
                story.append(_table([_head(["Place", "Team"])] + [[_p(p), _p(t.name)] for p, t in ranks], [25 * mm, 145 * mm]))
        else:
            story.append(Paragraph("The champion has not been decided yet.", BODY))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
