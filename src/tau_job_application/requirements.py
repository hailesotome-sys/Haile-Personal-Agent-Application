"""Contextual, offline job requirement extraction with exact source quotations.

Rules propose capabilities, not applicant competence. Unknown terms after experience
cues remain reviewable phrases. Reviewed aliases extend vocabulary, not importance.
"""
from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

from tau_job_application.models import EvidenceItem, JobRequirement
from tau_job_application.requirement_memory import RequirementMemory

# Phrase -> capability, not an implication of proficiency in a specific vendor/tool.
CAPABILITIES = {
    "building APIs": "API development", "build APIs": "API development",
    "design APIs": "API development", "designing APIs": "API development",
    "RESTful APIs": "REST APIs", "relational databases": "Relational databases",
    "distributed systems": "Distributed systems", "data pipelines": "Data pipelines",
    "automated tests": "Automated testing", "unit testing": "Unit testing",
    "communicating with stakeholders": "Stakeholder communication",
    "stakeholder communication": "Stakeholder communication",
    "explain technical decisions": "Technical communication",
    "financial modeling": "Financial modeling", "financial modelling": "Financial modeling",
    "user research": "User research", "embedded systems": "Embedded systems",
    "continuous integration": "CI/CD", "version control": "Version control",
    "Amazon Web Services": "AWS", "Google Cloud Platform": "GCP",
    # Finance and business vocabulary, including the French terms used in CVs and postings.
    "contrôle de gestion": "Management control", "contrôleur de gestion": "Management control",
    "controle de gestion": "Management control", "management controller": "Management control",
    "management control": "Management control", "controlling": "Management control",
    "financial planning and analysis": "FP&A", "corporate finance": "Corporate finance",
    "finance d'entreprise": "Corporate finance", "finances d'entreprise": "Corporate finance",
    "financial analysis": "Financial analysis", "analyse financière": "Financial analysis",
    "financial analyst": "Financial analysis", "analyste financier": "Financial analysis",
    "budget reporting": "Financial reporting", "financial reporting": "Financial reporting",
    "reporting financier": "Financial reporting", "reportings financiers": "Financial reporting",
    "budgetary": "Budgeting", "budget process": "Budgeting", "suivi budgétaire": "Budgeting",
    "forecasts": "Forecasting", "forecast": "Forecasting",
    "mergers and acquisitions": "M&A", "fusions-acquisitions": "M&A", "post-merger integration": "M&A",
    "due diligences": "Due diligence", "risk management": "Risk management",
    "asset management": "Asset management", "portfolio management": "Asset management",
    "data analysis": "Data Analysis", "analyse de données": "Data Analysis",
    "Microsoft Office": "Microsoft Office", "Pack Office": "Microsoft Office", "MS Office": "Microsoft Office",
    "Power BI": "Power BI", "Power Query": "Power Query",
    "clôture comptable": "Accounting", "comptabilité": "Accounting", "accounting close": "Accounting",
    "anglais": "English", "français": "French", "espagnol": "Spanish",
}
# Avoid ordinary words ("go", "excel at", "sap") being read as tools or languages.
CASE_SENSITIVE = {"Go", "C", "R", "Excel", "SAP", "VBA", "ACCA", "CIMA"}
PREFERRED = re.compile(r"\b(preferred|desirable|nice[- ]to[- ]have|bonus|ideally|an? advantage|a plus|would be helpful)\b", re.I)
CUE = re.compile(
    r"\b(?:experienced?\s+(?:with|in)|experience\s+(?:working\s+)?(?:with|in|using)|"
    r"comfortable\s+(?:with|using)|proficien(?:t|cy)\s+(?:with|in)|"
    r"familiar(?:ity)?\s+with|knowledge\s+of|projects?\s+(?:with|using|involving)|"
    r"background\s+in|worked\s+(?:with|on)|ability\s+to|able\s+to)\s+", re.I)
ACTION = re.compile(r"\b(?:you (?:will|must|should)|you'll|must have|we (?:require|expect)|"
                    r"design(?:ing)?|build(?:ing)?|develop(?:ing)?|maintain(?:ing)?|"
                    r"implement(?:ing)?|communicat(?:e|ing)|comfortable)\b", re.I)
NEGATED = re.compile(r"\b(?:not (?:required|necessary|essential)|no .{0,70}required|"
                     r"(?:do not|don't) (?:need|require)|we (?:will )?(?:teach|train)|training (?:is )?provided)\b", re.I)
HEADINGS = {
    "requirements": "required", "required skills": "required", "qualifications": "required",
    "what you bring": "required", "what we're looking for": "required", "about you": "required",
    "must have": "required", "essential skills": "required", "your skills": "required",
    "responsibilities": "required", "what you will do": "required", "what you'll do": "required",
    "nice to have": "preferred", "nice-to-have": "preferred", "preferred skills": "preferred",
    "preferred qualifications": "preferred", "bonus": "preferred", "desirable": "preferred",
    "about us": "ignore", "about the company": "ignore", "benefits": "ignore",
    "what we offer": "ignore", "our benefits": "ignore",
}


def _hits(phrase: str, text: str):
    # Avoid ordinary "go" and single-letter prose being interpreted as languages.
    flags = 0 if phrase in CASE_SENSITIVE else re.I
    return list(re.finditer(rf"(?<![\w+#]){re.escape(phrase)}(?![\w+#])", text, flags))


def _clauses(line: str):
    # Keep Node.js/C++ intact; split contrast/optional qualifications locally.
    pattern = (r"(?<=[.!?;])\s+|\s+(?:but|whereas|while)\s+|"
               r",?\s+and\s+(?=[^.;]{0,100}\b(?:is (?:a plus|preferred|not required)|not required)\b)")
    yield from (part.strip() for part in re.split(pattern, line, flags=re.I) if part.strip())


def _phrase_candidates(clause: str) -> list[str]:
    results = []
    for match in CUE.finditer(clause):
        tail = clause[match.end():]
        tail = re.split(r"\b(?:is|are|would|to support|to help|for our|such as|including)\b", tail, maxsplit=1, flags=re.I)[0]
        for phrase in re.split(r",|\band\b|\bor\b", tail, flags=re.I):
            phrase = phrase.strip(" .;:-•\t")
            phrase = re.sub(r"\s+(?:is )?(?:required|preferred|essential)$", "", phrase, flags=re.I)
            if 2 <= len(phrase) <= 100 and len(phrase.split()) <= 8:
                results.append(phrase)
    return results


def extract_requirements(text: str, vocabulary: tuple[str, ...], *, memory: RequirementMemory | None = None,
                         url: str | None = None) -> tuple[list[JobRequirement], list[EvidenceItem]]:
    if memory and (reviewed := memory.reviewed(text)) is not None:
        reqs, evidence = reviewed
        return reqs, [item.model_copy(update={"url": url}) for item in evidence]
    learned = memory.aliases() if memory else {}
    catalog = {**{skill: skill for skill in vocabulary}, **CAPABILITIES, **learned}
    found: dict[str, tuple[JobRequirement, EvidenceItem]] = {}
    section = "uncertain"
    position = 0
    for raw_line in text.splitlines(keepends=True):
        line_start = position
        position += len(raw_line)
        line = raw_line.strip().strip("#•* ")
        if not line:
            continue
        heading = line.rstrip(":").casefold()
        if heading in HEADINGS:
            section = HEADINGS[heading]
            continue
        explicit = re.match(r"^(required skills|preferred skills)\s*[:=]\s*(.+)", line, re.I)
        if re.match(r"^(name|title|company|location)\s*:", line, re.I):
            continue
        for clause in ([line] if explicit else _clauses(line)):
            if NEGATED.search(clause):
                continue
            if section == "ignore" and not explicit and not re.search(r"\b(?:you|your)\b", clause, re.I):
                continue
            importance = "preferred" if PREFERRED.search(clause) else section
            if importance in {"uncertain", "ignore"}:
                importance = "required" if CUE.search(clause) or ACTION.search(clause) or re.search(r"\brequired\b", clause, re.I) else "uncertain"
            if explicit:
                importance = "preferred" if explicit[1].lower().startswith("preferred") else "required"
                candidates = [(s.strip(), "explicit", 1.0) for s in re.split(r"[,;|]|\band\b", explicit[2], flags=re.I) if s.strip()]
            else:
                candidates = []
                occupied: list[tuple[int, int]] = []
                # Reviewed mappings override builtin matches over the same source span.
                ordered = sorted(catalog, key=lambda p: (p in learned, len(p)), reverse=True)
                for phrase in ordered:
                    matches = _hits(phrase, clause)
                    matches = [m for m in matches if not any(m.start() < end and m.end() > start for start, end in occupied)]
                    if not matches:
                        continue
                    occupied.extend((m.start(), m.end()) for m in matches)
                    candidates.append((catalog[phrase], "learned" if phrase in learned else "context", 0.9 if importance != "uncertain" else 0.5))
                for phrase in _phrase_candidates(clause):
                    if any(_hits(known, phrase) for known in catalog):
                        continue
                    candidates.append((phrase, "phrase", 0.6))
            for skill, method, confidence in candidates:
                key = " ".join(skill.casefold().split())
                # Full hashes avoid collisions between skills such as C and C++.
                evidence_id = "job-skill-" + hashlib.sha256(key.encode()).hexdigest()[:16]
                offset = text.find(clause, line_start)
                item = EvidenceItem(id=evidence_id, source="job", quote=clause,
                                    location=f"Characters {offset}:{offset + len(clause)}", url=url,
                                    confirmed=method == "explicit")
                req = JobRequirement(skill=skill, required=importance != "preferred", evidence_id=evidence_id,
                                     importance=importance, confidence=confidence, extraction_method=method,
                                     needs_review=method != "explicit")
                previous = found.get(key)
                rank = {"uncertain": 0, "preferred": 1, "required": 2}
                if previous is None or rank[importance] > rank[previous[0].importance]:
                    found[key] = (req, item)
            if explicit:
                break
    return [pair[0] for pair in found.values()], [pair[1] for pair in found.values()]


class ReviewedRequirement(BaseModel):
    skill: str = Field(min_length=1, max_length=100)
    importance: Literal["required", "preferred", "uncertain"]
    quote: str = Field(min_length=1)


def save_requirement_review(text: str, rows: list[dict], memory: RequirementMemory) -> None:
    """Validate edited requirements against source text before committing atomically."""
    requirements, evidence = [], []
    seen = set()
    for row in rows:
        if not row.get("keep", True):
            continue
        item = ReviewedRequirement.model_validate(row)
        skill = item.skill.strip()
        if not skill or item.quote not in text:
            raise ValueError("Every skill needs a name and an exact quote copied from this job")
        if skill.casefold() in seen:
            raise ValueError("Merge duplicate skill names before saving")
        seen.add(skill.casefold())
        evidence_id = f"job-reviewed-{len(requirements)}"
        offset = text.index(item.quote)
        requirements.append(JobRequirement(skill=skill, required=item.importance != "preferred", importance=item.importance,
            evidence_id=evidence_id, confidence=1.0 if item.importance != "uncertain" else 0.5,
            needs_review=item.importance == "uncertain", extraction_method="reviewed"))
        evidence.append(EvidenceItem(id=evidence_id, source="job", quote=item.quote,
            location=f"Characters {offset}:{offset + len(item.quote)}", confirmed=True))
    memory.save_review(text, requirements, evidence)
