"""Structured CV: parse the candidate's own text, tailor it to a job, render a one-page PDF.

Tailoring only reorders and labels what the CV already says. Every change is
recorded with its reason so the user can see exactly what was adapted.
"""

from __future__ import annotations

from io import BytesIO
import re

from pydantic import BaseModel, Field

from tau_job_application.parsing import _find_skills

# "City, Country" at the end of a line, optionally several joined by " / " (multi-campus schools).
LOCATION = re.compile(
    r"\s+((?:[A-ZÉÈÀ][\w'’.-]+, [A-ZÉÈÀ][\w'’ .-]+?)"
    r"(?: / [A-ZÉÈÀ][\w'’.-]+, [A-ZÉÈÀ][\w'’ .-]+?)*)\s*$")
MONTH = (r"(?:janv|févr|fevr|mars|avr|mai|juin|juil|août|aout|sept|oct|nov|déc|dec|"
         r"jan|feb|mar|apr|may|jun|jul|aug|sep)[a-zéû]*\.?")
DATES = re.compile(rf"\s+((?:{MONTH}\s+)?\d{{4}}\s*[–—-]\s*(?:(?:{MONTH}\s+)?\d{{4}}|présent|present|aujourd'hui|today)"
                   rf"|(?:{MONTH}\s+)?\d{{4}})\s*$", re.I)
BULLET = ("•", "-", "*", "▪", "◦")
FRENCH_HINTS = re.compile(r"\b(expériences?|formations?|compétences|langues|diplômes|profil)\b", re.I)


class ResumeEntry(BaseModel):
    organisation: str | None = None
    location: str | None = None
    role: str | None = None
    dates: str | None = None
    bullets: list[str] = Field(default_factory=list)


class ResumeSection(BaseModel):
    title: str
    text: str | None = None
    entries: list[ResumeEntry] = Field(default_factory=list)


class ResumeDocument(BaseModel):
    name: str
    contact: str | None = None
    target: str | None = None
    sections: list[ResumeSection] = Field(default_factory=list)
    language: str = "en"


class ResumeChange(BaseModel):
    kind: str
    where: str
    detail: str


def parse_resume(text: str) -> ResumeDocument:
    lines = [line.rstrip() for line in text.splitlines()]
    content = [line.strip() for line in lines if line.strip()]
    if not content:
        raise ValueError("The CV is empty")
    name = content[0]
    index = 1
    contact = None
    if index < len(content) and not _is_heading(content[index]) and ("@" in content[index] or "|" in content[index]):
        contact = content[index].replace("mailto:", "")
        index += 1
    sections: list[ResumeSection] = []
    current: ResumeSection | None = None
    entry: ResumeEntry | None = None
    for line in content[index:]:
        if _is_heading(line):
            current = ResumeSection(title=line.strip())
            sections.append(current)
            entry = None
            continue
        if current is None:
            current = ResumeSection(title="Profile")
            sections.append(current)
        if line.startswith(BULLET):
            if entry is None:
                entry = ResumeEntry()
                current.entries.append(entry)
            entry.bullets.append(line.lstrip("".join(BULLET) + " \t"))
            continue
        if entry and entry.bullets and _continues(entry.bullets[-1], line):
            entry.bullets[-1] = f"{entry.bullets[-1]} {line}"
            continue
        if location := LOCATION.search(line):
            entry = ResumeEntry(organisation=line[:location.start()].strip(" -–"), location=location[1])
            current.entries.append(entry)
            continue
        if entry and entry.organisation and entry.role is None and not entry.bullets:
            dates = DATES.search(line)
            entry.role = line[:dates.start()].strip() if dates else line
            entry.dates = dates[1] if dates else None
            continue
        if not current.entries:
            current.text = f"{current.text} {line}" if current.text else line
            continue
        entry = ResumeEntry(organisation=line)
        current.entries.append(entry)
    return ResumeDocument(name=name, contact=contact, sections=sections,
                          language="fr" if FRENCH_HINTS.search(text) else "en")


def tailor_resume_document(document: ResumeDocument, job_title: str, company: str,
                           relevant_skills: list[str]) -> tuple[ResumeDocument, list[ResumeChange]]:
    """Reorder each entry's bullets so the ones evidencing the job's skills come first."""
    tailored = document.model_copy(deep=True)
    changes: list[ResumeChange] = []
    label = "Candidature" if document.language == "fr" else "Application"
    tailored.target = f"{label} : {job_title} – {company}" if document.language == "fr" else f"{label}: {job_title}, {company}"
    changes.append(ResumeChange(kind="Added", where="Header", detail=f"Target line \"{tailored.target}\" so a recruiter sees which role this CV is for."))
    wanted = {skill.casefold() for skill in relevant_skills}
    for section in tailored.sections:
        for entry in section.entries:
            if len(entry.bullets) < 2:
                continue
            scored = [(len(wanted & {s.casefold() for s in _find_skills(bullet)}), position, bullet)
                      for position, bullet in enumerate(entry.bullets)]
            ordered = sorted(scored, key=lambda item: (-item[0], item[1]))
            if [item[1] for item in ordered] == list(range(len(scored))):
                continue
            entry.bullets = [item[2] for item in ordered]
            where = entry.organisation or section.title
            for new_position, (score, old_position, bullet) in enumerate(ordered):
                if score and new_position < old_position:
                    skills = sorted({s for s in _find_skills(bullet) if s.casefold() in wanted})
                    changes.append(ResumeChange(
                        kind="Moved up", where=where,
                        detail=f"\"{_clip(bullet)}\" from bullet {old_position + 1} to {new_position + 1}: it evidences {', '.join(skills)}, which the job asks for."))
    if len(changes) == 1:
        changes.append(ResumeChange(kind="Kept", where="Experience", detail="Bullet order already led with the job's skills; nothing reordered."))
    changes.append(ResumeChange(kind="Kept", where="All sections",
                                detail="No wording, employer, date, metric or skill was added or invented."))
    return tailored, changes


def render_resume_pdf(document: ResumeDocument) -> bytes:
    """One A4 page: shrink type and spacing step by step until the CV fits."""
    from pypdf import PdfReader

    for size in (10, 9.5, 9, 8.5, 8):
        pdf = _build_pdf(document, size)
        if len(PdfReader(BytesIO(pdf)).pages) == 1:
            return pdf
    return pdf


def pdf_preview_png(pdf: bytes, page: int = 0, scale: float = 1.6) -> bytes:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    image = document[page].render(scale=scale).to_pil()
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def render_resume_markdown(document: ResumeDocument) -> str:
    lines = [f"# {document.name}"]
    if document.contact:
        lines.append(document.contact)
    if document.target:
        lines.append(f"**{document.target}**")
    for section in document.sections:
        lines += ["", f"## {section.title}"]
        if section.text:
            lines.append(section.text)
        for entry in section.entries:
            header = " | ".join(part for part in (entry.organisation, entry.location) if part)
            if header:
                lines.append(f"**{header}**")
            role = " | ".join(part for part in (entry.role, entry.dates) if part)
            if role:
                lines.append(f"*{role}*")
            lines += [f"- {bullet}" for bullet in entry.bullets]
    return "\n".join(lines)


def parse_resume_markdown(markdown: str) -> ResumeDocument:
    """Read back the layout `render_resume_markdown` writes, so the user's edits reach the PDF."""
    lines = [line.strip() for line in markdown.strip().splitlines()]
    if not lines or not lines[0].startswith("# "):
        raise ValueError("The CV draft does not start with '# Name'")
    document = ResumeDocument(name=lines[0][2:].strip())
    section: ResumeSection | None = None
    entry: ResumeEntry | None = None
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith("## "):
            section = ResumeSection(title=line[3:].strip())
            document.sections.append(section)
            entry = None
        elif section is None:
            if line.startswith("**") and line.endswith("**"):
                document.target = line.strip("*").strip()
            else:
                document.contact = f"{document.contact} {line}" if document.contact else line
        elif line.startswith(("- ", "• ")):
            if entry is None:
                entry = ResumeEntry()
                section.entries.append(entry)
            entry.bullets.append(line[2:].strip())
        elif line.startswith("**") and line.endswith("**"):
            organisation, _, location = line.strip("*").partition(" | ")
            entry = ResumeEntry(organisation=organisation.strip(), location=location.strip() or None)
            section.entries.append(entry)
        elif line.startswith("*") and line.endswith("*") and entry is not None and entry.role is None and not entry.bullets:
            role, _, dates = line.strip("*").partition(" | ")
            entry.role, entry.dates = role.strip(), dates.strip() or None
        elif not section.entries:
            section.text = f"{section.text} {line}" if section.text else line
        else:
            entry = ResumeEntry(organisation=line)
            section.entries.append(entry)
    if not document.sections:
        raise ValueError("The CV draft has no '## Section' headings")
    document.language = "fr" if FRENCH_HINTS.search(markdown) else "en"
    return document


def _build_pdf(document: ResumeDocument, size: float) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    gap = size * 0.25
    styles = {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=size + 6, leading=size + 9, alignment=TA_CENTER),
        "contact": ParagraphStyle("contact", fontName="Helvetica", fontSize=size - 0.5, leading=size + 2, alignment=TA_CENTER),
        "target": ParagraphStyle("target", fontName="Helvetica-Bold", fontSize=size, leading=size + 3, alignment=TA_CENTER,
                                 textColor=colors.HexColor("#1f5041"), spaceBefore=gap),
        "section": ParagraphStyle("section", fontName="Helvetica-Bold", fontSize=size + 0.5, leading=size + 3, spaceBefore=gap * 3),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=size, leading=size * 1.22),
        "bold": ParagraphStyle("bold", fontName="Helvetica-Bold", fontSize=size, leading=size * 1.22),
        "italic": ParagraphStyle("italic", fontName="Helvetica-Oblique", fontSize=size, leading=size * 1.22),
        "right": ParagraphStyle("right", fontName="Helvetica", fontSize=size, leading=size * 1.22, alignment=2),
        "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=size, leading=size * 1.22, leftIndent=10, bulletIndent=2),
    }
    width = A4[0] - 3 * cm - 12  # the frame keeps 6pt of padding on each side
    story = [Paragraph(_safe(document.name), styles["name"])]
    if document.contact:
        story.append(Paragraph(_safe(document.contact), styles["contact"]))
    if document.target:
        story.append(Paragraph(_safe(document.target), styles["target"]))
    for section in document.sections:
        story += [Paragraph(_safe(section.title.upper()), styles["section"]),
                  HRFlowable(width="100%", thickness=0.7, color=colors.black, spaceBefore=1, spaceAfter=gap)]
        if section.text:
            story.append(Paragraph(_safe(section.text), styles["body"]))
        for entry in section.entries:
            if entry.organisation or entry.location:
                story.append(_row(entry.organisation or "", entry.location or "", styles["bold"], styles["right"], width, Table, TableStyle, upper=True))
            if entry.role or entry.dates:
                story.append(_row(entry.role or "", entry.dates or "", styles["italic"], styles["right"], width, Table, TableStyle))
            story += [Paragraph(_safe(bullet), styles["bullet"], bulletText="•") for bullet in entry.bullets]
            story.append(Spacer(1, gap))
    output = BytesIO()
    SimpleDocTemplate(output, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.2 * cm,
                      bottomMargin=1.2 * cm, title=f"CV {document.name}", author=document.name).build(story)
    return output.getvalue()


def _row(left: str, right: str, left_style, right_style, width, table_class, style_class, upper: bool = False):
    from reportlab.platypus import Paragraph

    text = left.upper() if upper else left
    table = table_class([[Paragraph(_safe(text), left_style), Paragraph(_safe(right), right_style)]],
                        colWidths=[width * 0.7, width * 0.3])
    table.setStyle(style_class([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return table


def _is_heading(line: str) -> bool:
    letters = [char for char in line if char.isalpha()]
    return (bool(letters) and all(char.isupper() for char in letters) and len(line.split()) <= 5
            and "," not in line and not any(char.isdigit() for char in line) and not line.startswith(BULLET))


def _continues(previous: str, line: str) -> bool:
    if line[0].islower():
        return True
    return previous.rstrip().endswith((",", "/", "(", "-", "–")) and not LOCATION.search(line) and not DATES.search(line)


def _safe(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.encode("cp1252", "replace").decode("cp1252")


def _clip(text: str, limit: int = 70) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
