"""Local document parsing with source spans retained for every extracted fact.

This module deliberately supports a small, editable schema first.  It also makes a
best-effort extraction from ordinary CV/job text so the UI is useful before an LLM
is configured.  Ambiguous text is never treated as confirmed competence.
"""

from __future__ import annotations

from pathlib import Path
import re
from zipfile import ZipFile
from xml.etree import ElementTree

from pypdf import PdfReader

from tau_job_application.models import CandidateProfile, EvidenceItem, JobPosting
from tau_job_application.requirement_memory import RequirementMemory
from tau_job_application.requirements import CAPABILITIES, _hits, extract_requirements

KNOWN_SKILLS = (
    "Python", "SQL", "FastAPI", "Docker", "Kubernetes", "AWS", "GCP", "Azure",
    "Git", "pytest", "PostgreSQL", "Linux", "Java", "JavaScript", "TypeScript",
    "React", "Node.js", "C++", "C", "Rust", "Go", "TensorFlow", "PyTorch",
    "Machine Learning", "Data Analysis", "Pandas", "NumPy", "Spark", "Airflow",
    "Terraform", "CI/CD", "System Design", "REST APIs", "GraphQL", "Figma",
    "CUDA", "OpenCL", "ROCm", "HIP", "FPGA", "Verilog", "VHDL", "SystemVerilog",
    "RTOS", "FreeRTOS", "Embedded Linux", "PCB design", "PCB layout", "KiCad",
    "Altium", "Microcontrollers", "DSP", "MATLAB", "Simulink", "GPU programming",
    # Finance, controlling and consulting.
    "Excel", "VBA", "Power Query", "Power BI", "SAP", "Microsoft Office", "Bloomberg",
    "FP&A", "Management control", "Corporate finance", "Financial analysis", "Financial reporting",
    "Budgeting", "Forecasting", "Accounting", "Consolidation", "Valuation", "M&A", "Due diligence",
    "Risk management", "Asset management", "Audit", "ACCA", "CIMA",
    "English", "French", "Spanish", "German",
)
# CV lines that list skills in the candidate's own words count as self-declared evidence.
SKILL_LIST_FIELDS = {"skills", "compétences", "competences", "langues", "languages",
                     "certifications", "outils", "tools", "logiciels"}


def load_document(path: Path) -> str:
    """Load local text, Markdown, PDF, or DOCX text. Scanned PDFs need OCR first."""
    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")
    suffix = path.suffix.casefold()
    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8")
    elif suffix == ".pdf":
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif suffix == ".docx":
        text = _read_docx(path)
    else:
        raise ValueError("Supported documents are .txt, .md, .pdf, and .docx")
    if not text.strip():
        raise ValueError("No selectable text found; a scanned PDF needs OCR")
    return text


def infer_profile_links(text: str) -> dict[str, str]:
    """Infer only recognizable public profile links; a URL is not proof of ownership."""
    urls = re.findall(r"https?://[^\s<>\]\)\"']+", text)
    result: dict[str, str] = {}
    for raw_url in urls:
        url = raw_url.rstrip(".,;:")
        host = re.sub(r"^www\.", "", (re.match(r"https?://([^/]+)", url, re.I) or [None, ""])[1].casefold())
        if host in {"github.com"} and "github" not in result:
            result["github"] = url
        elif host in {"linkedin.com"} and "linkedin" not in result:
            result["linkedin"] = url
        elif host in {"x.com", "twitter.com"} and "x" not in result:
            result["x"] = url
        elif host and host not in {"example.com", "example.org"} and "portfolio" not in result:
            result["portfolio"] = url
    return result


def parse_candidate_text(text: str, *, links: dict[str, str] | None = None,
                         confirmed_skills: list[str] | None = None) -> CandidateProfile:
    """Create a candidate profile without claiming that keyword mentions prove skill.

    Skills count as confirmed when the CV lists them itself (a Skills/Compétences/Langues
    line) or when the user confirms them; anything else found in the text stays unconfirmed.
    """
    fields = _parse_fields(text)
    inferred_links = infer_profile_links(text)
    inferred_links.update({key: value for key, value in (links or {}).items() if value})
    name = fields.get("name") or _guess_name(text) or "Candidate (please confirm)"
    explicit_skills = _split_list(fields.get("skills", ""))
    listed = {skill: f"{key.capitalize()}: {fields[key]}" for key in SKILL_LIST_FIELDS - {"skills"} if fields.get(key)
              for skill in _find_skills(fields[key])}
    user_confirmed = {skill.casefold(): skill for skill in confirmed_skills or []}
    detected = _find_skills(text)
    skills = _dedupe(explicit_skills + list(listed) + ([] if explicit_skills else detected) + list(user_confirmed.values()))
    evidence = []
    for skill in skills:
        if skill in explicit_skills:
            quote, location, confirmed = f"Skills: {fields['skills']}", "Skills", True
        elif skill in listed:
            quote, location, confirmed = listed[skill], "Skills list", True
        elif skill.casefold() in user_confirmed:
            quote, location, confirmed = f"{skill}: confirmed by the candidate", "Candidate confirmation", True
        else:
            quote, location, confirmed = "Skills inferred from CV text; confirm each one.", "CV keyword extraction", False
        evidence.append(EvidenceItem(id=f"candidate-skill-{_slug(skill)}", source="candidate", quote=quote,
                                     location=location, confirmed=confirmed))
    evidence.append(EvidenceItem(
        id="candidate-cv-source",
        source="candidate",
        quote=_clip(text),
        location="Imported CV",
        confirmed=True,
    ))
    return CandidateProfile(
        name=name,
        headline=fields.get("headline") or _first_nonempty_line(text),
        skills=skills,
        experience_summary=fields.get("experience") or _experience_summary(text),
        links=inferred_links,
        raw_cv=text,
        evidence=evidence,
    )


def parse_job_text(
    text: str, *, title: str | None = None, company: str | None = None, url: str | None = None,
    memory: RequirementMemory | None = None,
) -> JobPosting:
    """Parse a job description, retaining requirement wording and confidence context."""
    fields = _parse_fields(text)
    requirements, evidence = extract_requirements(text, KNOWN_SKILLS, memory=memory, url=url)
    if not requirements:
        raise ValueError("No requirements could be inferred. Paste more role detail or add a sourced requirement in the review editor.")
    evidence.append(EvidenceItem(
        id="job-description-source", source="job", quote=_clip(text),
        location="Imported job description", confirmed=True, url=url,
    ))
    return JobPosting(
        title=title or fields.get("title") or _guess_job_title(text) or "Role (please confirm)",
        company=company or fields.get("company") or _guess_company(text) or "Company (please confirm)",
        location=fields.get("location"), url=url, description=text, requirements=requirements, evidence=evidence,
    )


def _read_docx(path: Path) -> str:
    try:
        with ZipFile(path) as archive:
            document = archive.read("word/document.xml")
        root = ElementTree.fromstring(document)
    except Exception as exc:  # zip/xml errors should be useful at the boundary
        raise ValueError(f"Could not read DOCX file: {exc}") from exc
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    return "\n".join("".join(node.text or "" for node in paragraph.iter(f"{namespace}t")) for paragraph in root.iter(f"{namespace}p"))


def _parse_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if value.strip():
            fields[key.strip().strip("•-*\t ").casefold()] = value.strip()
    return fields


def _split_list(value: str) -> list[str]:
    return [item.strip(" -•\t") for item in re.split(r"[,;|]", value) if item.strip(" -•\t")]


def _find_skills(text: str) -> list[str]:
    """Canonical skills named in the text, including known aliases such as French terms."""
    catalog = {**{skill: skill for skill in KNOWN_SKILLS}, **CAPABILITIES}
    return _dedupe([catalog[phrase] for phrase in catalog if _hits(phrase, text)])


def _guess_name(text: str) -> str | None:
    first = _first_nonempty_line(text)
    if first and 1 <= len(first.split()) <= 5 and len(first) < 70 and not any(char.isdigit() for char in first):
        return first.strip("# ")
    return None


def _guess_job_title(text: str) -> str | None:
    for line in text.splitlines()[:8]:
        cleaned = line.strip("# -*•\t")
        if re.search(r"\b(engineer|designer|analyst|manager|developer|intern|scientist|consultant)\b", cleaned, re.I):
            return cleaned[:120]
    return None


def _guess_company(text: str) -> str | None:
    match = re.search(r"(?:about|join)\s+([A-Z][\w&.-]+(?:\s+[A-Z][\w&.-]+){0,3})", text)
    return match.group(1) if match else None


def _first_nonempty_line(text: str) -> str | None:
    return next((line.strip("# \t") for line in text.splitlines() if line.strip()), None)


def _experience_summary(text: str) -> str | None:
    lines = [line.strip(" -•\t") for line in text.splitlines() if line.strip()]
    bullets = [line for line in lines if line.startswith(("Built", "Led", "Developed", "Created", "Worked", "Implemented"))]
    return " ".join(bullets[:3]) or None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _clip(text: str, maximum: int = 600) -> str:
    normalized = " ".join(text.split())
    return normalized[:maximum] + ("…" if len(normalized) > maximum else "")
