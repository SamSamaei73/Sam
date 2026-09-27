"""Deterministic professional extraction.

Turns a ``SourceDocument`` (text with provenance, produced by Knowledge's
pipeline) into claim DRAFTS. No model is involved and nothing here is
authority: drafts become claims only after validation and the claim policy.

Principles:

* Only what the source states becomes an ``EXPLICIT`` observation, and each
  observation keeps its provenance (page, section, paragraph, character range)
  and a short scrubbed reference.
* The owner's author position comes only from the trusted
  ``OwnerProfessionalIdentity`` (exact canonical name or approved alias); no
  match or an ambiguous match records nothing.
* Anything ambiguous is SKIPPED rather than guessed (a job line whose title and
  employer cannot be told apart is left for the owner or a model candidate).
* A dependency manifest, an infrastructure file, a repository README or a
  publication topic only ever yields ``INFERRED`` evidence: the existence of a
  Kubernetes manifest is not evidence that the owner is a Kubernetes expert.
* Skills come only from the trusted vocabulary; an unknown skill name is never
  minted into a claim.
* Identifiers (student numbers, emails, phone numbers) are removed first.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sam.models.models import PrivacyClass
from sam.professional.evidence import norm_text
from sam.professional.identity import OwnerMatch, OwnerProfessionalIdentity
from sam.professional.models import (
    MAX_CLAIMS_PER_SOURCE,
    ClaimCategory,
    EvidenceNature,
    SourceLocation,
    SourceType,
    clean_text,
)
from sam.professional.reader import SourceDocument
from sam.professional.safety import has_private_cue, is_identifier_line, scrub
from sam.professional.timeline import PRESENT, normalize_date
from sam.professional.vocabulary import VocabEntry, find_mentions, normalize_skill

EXTRACTOR_VERSION = "1"

_MAX_BULLETS_PER_BLOCK = 12


class ExtractionError(Exception):
    """Extraction failed closed. ``code`` is a short, content-free reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ drafts


@dataclass(frozen=True)
class EvidenceDraft:
    location: SourceLocation
    reference: str
    nature: EvidenceNature
    observed: tuple[tuple[str, str], ...] = ()
    context: tuple[ClaimCategory, str] | None = None


@dataclass
class ClaimDraft:
    category: ClaimCategory
    key: str
    statement: str
    attributes: dict[str, str]
    sensitivity: PrivacyClass = PrivacyClass.PUBLIC
    evidence: list[EvidenceDraft] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractionResult:
    drafts: tuple[ClaimDraft, ...]
    unmapped_skills: int = 0


class _Collector:
    def __init__(self) -> None:
        self.drafts: dict[tuple[ClaimCategory, str], ClaimDraft] = {}
        self.unmapped = 0

    def claim(
        self,
        category: ClaimCategory,
        key: str,
        statement: str,
        attributes: dict[str, str] | None = None,
        *,
        sensitivity: PrivacyClass = PrivacyClass.PUBLIC,
    ) -> ClaimDraft:
        ident = (category, key)
        draft = self.drafts.get(ident)
        if draft is None:
            if len(self.drafts) >= MAX_CLAIMS_PER_SOURCE:
                raise ExtractionError("too_many_claims")
            draft = ClaimDraft(
                category=category,
                key=key,
                statement=clean_text(scrub(statement), max_length=500),
                attributes={k: v for k, v in (attributes or {}).items() if v},
                sensitivity=sensitivity,
            )
            self.drafts[ident] = draft
        elif sensitivity > draft.sensitivity:
            draft.sensitivity = sensitivity
        return draft

    def observe(
        self,
        draft: ClaimDraft,
        line: Line,
        kind: EvidenceNature,
        *,
        observed: dict[str, str] | None = None,
        context: tuple[ClaimCategory, str] | None = None,
    ) -> None:
        clean_observed = tuple(
            sorted(
                (name, clean_text(scrub(value), max_length=1_000))
                for name, value in (observed or {}).items()
                if value and clean_text(scrub(value), max_length=1_000)
            )
        )
        item = EvidenceDraft(
            location=line.loc,
            reference=clean_text(scrub(line.text), max_length=240),
            nature=kind,
            observed=clean_observed,
            context=context,
        )
        if item not in draft.evidence:
            draft.evidence.append(item)

    def finish(self) -> ExtractionResult:
        ordered = sorted(self.drafts.values(), key=lambda d: (d.category.value, d.key))
        return ExtractionResult(tuple(ordered), self.unmapped)


# ------------------------------------------------------------------- lines


@dataclass(frozen=True)
class Line:
    text: str
    loc: SourceLocation
    bullet: bool
    segment: int


_BULLET = re.compile(r"^\s*(?:[-*•·–—▪●○◦]|\d{1,2}[.)])\s+")


def _lines(document: SourceDocument) -> list[Line]:
    out: list[Line] = []
    for segment in document.segments:
        offset = 0
        for raw in segment.text.split("\n"):
            start, offset = offset, offset + len(raw) + 1
            text = raw.strip()
            if not text:
                continue
            bullet = _BULLET.match(raw) is not None
            if bullet:
                text = _BULLET.sub("", raw).strip()
            if not text or is_identifier_line(text):
                continue
            loc = SourceLocation(
                page_number=segment.location.page_number,
                section_title=segment.location.section_title,
                paragraph_index=segment.location.paragraph_index,
                character_start=start,
                character_end=start + len(raw),
            )
            out.append(Line(text=text, loc=loc, bullet=bullet, segment=segment.index))
    return out


# ---------------------------------------------------------------- sections

_HEADINGS: dict[str, str] = {}
for _name, _words in {
    "experience": (
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "employment history",
        "work history",
        "career history",
    ),
    "education": (
        "education",
        "academic background",
        "qualifications",
        "academic qualifications",
    ),
    "skills": (
        "skills",
        "technical skills",
        "core skills",
        "key skills",
        "technologies",
        "tech stack",
        "competencies",
    ),
    "projects": ("projects", "selected projects", "personal projects", "key projects"),
    "publications": ("publications", "selected publications", "papers"),
    "interests": ("research interests", "research topics", "interests"),
    "certifications": ("certifications", "certificates", "licenses"),
    "languages": ("languages",),
    "achievements": ("achievements", "awards", "honours", "honors"),
    "links": ("links", "portfolio", "profile links", "online presence"),
    "summary": ("summary", "profile", "about", "professional summary"),
    "objective": (
        "objective",
        "career objective",
        "career goals",
        "looking for",
        "preferences",
        "career preferences",
    ),
}.items():
    for _word in _words:
        _HEADINGS[_word] = _name


def _heading(text: str) -> str | None:
    cleaned = norm_text(text.lstrip("#").rstrip(":")).strip()
    if 0 < len(cleaned) <= 40:
        return _HEADINGS.get(cleaned)
    return None


# ------------------------------------------------------------------- dates

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE = (
    rf"(?:{_MONTH}\.?,?\s+\d{{4}}|\d{{4}}[-/]\d{{1,2}}|\d{{1,2}}[-/]\d{{4}}|\d{{4}})"
)
_RANGE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:-|–|—|to|until)\s*"
    rf"(?P<end>{_DATE}|Present|Current|Now|Ongoing)",
    re.IGNORECASE,
)


def _range(text: str) -> tuple[str | None, str | None, str]:
    """``(start, end, text_without_range)`` in canonical form."""

    match = _RANGE.search(text)
    if match is None:
        return None, None, text
    start = normalize_date(match.group("start"))
    end = normalize_date(match.group("end"))
    remainder = (text[: match.start()] + " " + text[match.end() :]).strip()
    return start, end, remainder


# ------------------------------------------------------------------- jobs

_TITLE_WORDS = re.compile(
    r"(?i)\b(?:engineer|developer|scientist|researcher|analyst|manager|lead|architect|"
    r"consultant|intern|assistant|lecturer|director|specialist|officer|founder|"
    r"co-founder|head|designer|administrator|technician|fellow|associate|programmer|"
    r"instructor|tutor|coordinator|advisor|professor)\b"
)


def _parse_job(text: str) -> tuple[str, str, str | None, str | None] | None:
    start, end, rest = _range(text)
    if start is None or end is None:
        return None
    rest = rest.strip(" |,–—-()[]")
    if re.search(r"\s(?:at|@)\s", rest):
        pieces = re.split(r"\s(?:at|@)\s", rest, maxsplit=1)
        title, employer = pieces[0].strip(), pieces[1].strip()
    else:
        parts = [
            p.strip() for p in re.split(r"\s*[|,–—]\s*|\s+-\s+", rest) if p.strip()
        ]
        if len(parts) < 2:
            return None
        first, second = parts[0], parts[1]
        first_is_title = _TITLE_WORDS.search(first) is not None
        second_is_title = _TITLE_WORDS.search(second) is not None
        if first_is_title == second_is_title:
            return None  # ambiguous: never guess which is which
        title, employer = (first, second) if first_is_title else (second, first)
    employer = re.split(r"\s*[|(]\s*", employer)[0].strip(" ,.")
    title = title.strip(" ,.")
    if not (2 <= len(title) <= 100 and 2 <= len(employer) <= 100):
        return None
    if not re.search(r"[A-Za-z]", title) or not re.search(r"[A-Za-z]", employer):
        return None
    return title, employer, start, end


# ----------------------------------------------------------- skills / misc

_MANIFEST_NAMES = frozenset(
    {
        "requirements.txt",
        "package.json",
        "pyproject.toml",
        "pipfile",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "go.mod",
        "cargo.toml",
        "pom.xml",
        "build.gradle",
    }
)
_MANIFEST_SUFFIXES = (".yaml", ".yml", ".toml", ".lock", ".cfg", ".ini", ".gradle")

_DOMAINS = (
    "healthcare",
    "fintech",
    "e-commerce",
    "logistics",
    "supply chain",
    "education",
    "sports analytics",
    "legal",
    "insurance",
    "telecommunications",
    "cybersecurity",
    "manufacturing",
)
_DOMAIN_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z])(" + "|".join(re.escape(d) for d in _DOMAINS) + r")(?![A-Za-z])"
)
_METRIC = re.compile(
    r"(?i)(?:\d+(?:[.,]\d+)?\s*(?:%|x\b|×|k\b|m\b|million|billion|users|clients|customers|"
    r"ms\b|seconds|hours|days|requests|gb\b|tb\b)|[$£€]\s?\d[\d,.]*)"
)
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_LANGUAGES = frozenset(
    {
        "english",
        "persian",
        "farsi",
        "arabic",
        "french",
        "german",
        "spanish",
        "turkish",
        "italian",
        "chinese",
        "mandarin",
        "hindi",
        "urdu",
        "russian",
        "portuguese",
        "japanese",
        "korean",
        "dutch",
        "kurdish",
        "azerbaijani",
    }
)


def _is_manifest(label: str, document: SourceDocument) -> bool:
    name = label.lower()
    if name in _MANIFEST_NAMES or name.endswith(_MANIFEST_SUFFIXES):
        return True
    head = "\n".join(s.text for s in document.segments[:2])[:4_000]
    return "apiVersion:" in head and "kind:" in head


def _is_readme(label: str) -> bool:
    return label.lower().startswith("readme")


def _skill_kind(
    category: ClaimCategory,
    source_type: SourceType,
    *,
    manifest: bool,
    readme: bool,
    in_publication: bool,
) -> EvidenceNature:
    if manifest or in_publication or source_type is SourceType.PUBLICATION:
        return EvidenceNature.INFERRED_RELATIONSHIP
    if source_type is SourceType.GITHUB:
        if readme and category is ClaimCategory.TECHNOLOGY:
            return EvidenceNature.EXPLICIT_SOURCE
        return EvidenceNature.INFERRED_RELATIONSHIP
    return EvidenceNature.EXPLICIT_SOURCE


@dataclass(frozen=True)
class _Ctx:
    source_type: SourceType
    label: str
    manifest: bool
    readme: bool


def _skill_statement(entry: VocabEntry) -> str:
    label = "Skill" if entry.kind is ClaimCategory.SKILL else "Technology"
    return f"{label}: {entry.display}"


def _scan_skills(
    c: _Collector,
    line: Line,
    ctx: _Ctx,
    context: tuple[ClaimCategory, str] | None,
    *,
    in_publication: bool = False,
) -> None:
    for mention in find_mentions(line.text):
        entry = mention.entry
        draft = c.claim(
            entry.kind,
            entry.skill_id,
            _skill_statement(entry),
            {"skill_id": entry.skill_id, "display": entry.display},
        )
        kind = _skill_kind(
            entry.kind,
            ctx.source_type,
            manifest=ctx.manifest,
            readme=ctx.readme,
            in_publication=in_publication,
        )
        c.observe(draft, line, kind, context=context)


def _scan_domains(
    c: _Collector, line: Line, context: tuple[ClaimCategory, str] | None
) -> None:
    for match in _DOMAIN_PATTERN.finditer(line.text):
        domain = match.group(1).lower()
        draft = c.claim(
            ClaimCategory.DOMAIN_EXPERIENCE,
            norm_text(domain),
            f"Domain: {domain}",
            {"domain": domain},
        )
        c.observe(draft, line, EvidenceNature.EXPLICIT_SOURCE, context=context)


def _scan_links(c: _Collector, line: Line) -> None:
    for match in _URL.finditer(line.text):
        try:
            parts = urlsplit(match.group(0).rstrip(".,;"))
        except ValueError:
            continue
        host = (parts.hostname or "").lower()
        if not host or parts.scheme not in ("http", "https"):
            continue
        path = parts.path.rstrip("/")
        url = f"https://{host}{path}"
        draft = c.claim(
            ClaimCategory.PORTFOLIO,
            f"{host}{path}".lower(),
            f"Portfolio link: {url}",
            {"url": url, "label": host},
        )
        c.observe(draft, line, EvidenceNature.EXPLICIT_SOURCE)


def _scan_private(c: _Collector, line: Line) -> None:
    """Salary, visa/work-authorization and clearance text becomes a PRIVATE
    career-preference claim, so it never routes to Gemini Free by default."""

    if not has_private_cue(line.text):
        return
    text = clean_text(scrub(line.text), max_length=300)
    draft = c.claim(
        ClaimCategory.CAREER_PREFERENCE,
        norm_text(text)[:200],
        f"Career preference: {text}",
        {"kind": "private", "value": text},
        sensitivity=PrivacyClass.PRIVATE,
    )
    c.observe(draft, line, EvidenceNature.EXPLICIT_SOURCE)


# --------------------------------------------------------------- education

_DEGREES: tuple[tuple[str, str], ...] = (
    ("MSc", r"M\.?\s?Sc\.?|Master(?:s|['’]s)?\s+of\s+Science"),
    ("MEng", r"M\.?\s?Eng\.?|Master(?:s|['’]s)?\s+of\s+Engineering"),
    ("MA", r"M\.A\.|Master(?:s|['’]s)?\s+of\s+Arts"),
    ("MBA", r"MBA|Master(?:s|['’]s)?\s+of\s+Business\s+Administration"),
    ("PhD", r"Ph\.?\s?D\.?|DPhil|Doctor\s+of\s+Philosophy"),
    ("BSc", r"B\.?\s?Sc\.?|Bachelor(?:s|['’]s)?\s+of\s+Science"),
    ("BEng", r"B\.?\s?Eng\.?|Bachelor(?:s|['’]s)?\s+of\s+Engineering"),
    ("BA", r"B\.A\.|Bachelor(?:s|['’]s)?\s+of\s+Arts"),
)
_DEGREE_RE = re.compile(
    r"(?<![A-Za-z])(?P<deg>"
    + "|".join(f"(?P<d{i}>{pattern})" for i, (_, pattern) in enumerate(_DEGREES))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_SUBJECT_ALIASES = {
    "ai": "artificial intelligence",
    "ml": "machine learning",
    "cs": "computer science",
    "comp sci": "computer science",
    "compsci": "computer science",
}
_INSTITUTION = re.compile(
    r"(?:University of [A-Z][A-Za-z&'’\-]*(?: [A-Z][A-Za-z&'’\-]*){0,3}"
    r"|[A-Z][A-Za-z&'’\-]*(?: [A-Z][A-Za-z&'’\-]*){0,4} "
    r"(?:University|College|Institute|Polytechnic|School))"
)
_CLASSIFICATION = re.compile(
    r"(?i)(?:(?:with|grade|classification|class|honou?rs)\s*:?\s*|\(\s*)"
    r"(First[- ]Class(?: Honou?rs)?|Distinction|Merit|Pass|Upper Second(?:[- ]Class)?|"
    r"Lower Second(?:[- ]Class)?)"
    r"|\b(2:1|2:2|First[- ]Class Honou?rs)\b"
)
_GPA = re.compile(r"(?i)\bGPA\s*:?\s*(\d(?:\.\d+)?(?:\s*/\s*\d(?:\.\d+)?)?)")
_DISSERTATION = re.compile(
    r"(?i)\b(?:dissertation|thesis)\b\s*(?:title)?\s*[:\-–—]\s*(?P<t>.{5,200})"
)
_SUBJECT_STOP = re.compile(
    r"(?i)\s*(?:\n|[,(|–—]|\s-\s|\bat\b|\bfrom\b|\bwith\b|\bwhere\b|University|College|Institute|"
    r"Polytechnic|\d{4}|Honou?rs|Distinction|Merit)"
)


def _canon_subject(text: str) -> str:
    subject = re.sub(r"(?i)^(?:in|of|:|-)\s+", "", text.strip(" ,.:;-–—"))
    subject = re.sub(r"\s+", " ", subject)
    lowered = norm_text(subject)
    return _SUBJECT_ALIASES.get(lowered, lowered)


@dataclass(frozen=True)
class _Education:
    degree: str
    subject: str
    subject_display: str
    institution: str | None
    classification: str | None
    start: str | None
    end: str | None
    dissertation: str | None


def _parse_education(text: str) -> _Education | None:
    match = _DEGREE_RE.search(text)
    if match is None:
        return None
    degree = next(
        (name for i, (name, _) in enumerate(_DEGREES) if match.group(f"d{i}")), None
    )
    if degree is None:
        return None
    after, before = text[match.end() :], text[: match.start()]
    tail = re.sub(r"^\s*(?:in|of|:|,|-|–|—)?\s*", "", after)
    cut = _SUBJECT_STOP.search(tail)
    subject_raw = tail[: cut.start()] if cut else tail
    if len(re.findall(r"[A-Za-z]", subject_raw)) < 2:
        subject_raw = ""
        head = before.strip(" ,.:;-–—|")
        if 3 <= len(head) <= 60 and not re.search(
            r"(?i)university|college|institute|school", head
        ):
            subject_raw = head
    subject = _canon_subject(subject_raw)
    if len(subject) < 2:
        return None
    institution_match = _INSTITUTION.search(text)
    classification = _CLASSIFICATION.search(text)
    gpa = _GPA.search(text)
    dissertation = _DISSERTATION.search(text)
    start, end, _ = _range(text)
    return _Education(
        degree=degree,
        subject=subject,
        subject_display=subject.title(),
        institution=institution_match.group(0).strip() if institution_match else None,
        classification=(
            (classification.group(1) or classification.group(2))
            if classification
            else (f"GPA {gpa.group(1)}" if gpa else None)
        ),
        start=start,
        end=end,
        dissertation=dissertation.group("t").strip() if dissertation else None,
    )


def _emit_education(
    c: _Collector,
    info: _Education,
    line: Line,
    ctx: _Ctx,
    modules: str | None = None,
) -> tuple[ClaimCategory, str]:
    key = f"{info.degree.lower()}|{info.subject}"
    draft = c.claim(
        ClaimCategory.EDUCATION,
        key,
        f"{info.degree} in {info.subject_display}",
        {"degree": info.degree, "subject": info.subject_display},
    )
    observed = {
        "institution": info.institution or "",
        "classification": info.classification or "",
        "start": info.start or "",
        "end": info.end or "",
        "dissertation": info.dissertation or "",
        "modules": modules or "",
    }
    c.observe(draft, line, EvidenceNature.EXPLICIT_SOURCE, observed=observed)
    return (ClaimCategory.EDUCATION, key)


_MODULE_MARK = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z&,'’()\- ]{4,80}?)\s*(?:[|:\t]|\s{2,})\s*"
    r"(?:\d{1,3}(?:\.\d+)?%?|[A-F][+-]?|Pass|Merit|Distinction)\s*$"
)


_NOT_MODULES = frozenset(
    {
        "classification",
        "programme",
        "program",
        "dissertation",
        "thesis",
        "grade",
        "degree",
        "award",
        "university",
        "institution",
        "result",
        "overall",
    }
)


def _module_name(text: str) -> str | None:
    match = _MODULE_MARK.match(text.strip())
    if match is None:
        return None
    name = match.group("name").strip(" ,.-")
    if name.lower().split()[0] in _NOT_MODULES:
        return None
    return name if 4 <= len(name) <= 80 else None


# ------------------------------------------------------------ resume-like


def _extract_resume_like(c: _Collector, lines: Sequence[Line], ctx: _Ctx) -> None:
    section: str | None = (
        "education" if ctx.source_type is SourceType.TRANSCRIPT else None
    )
    context: tuple[ClaimCategory, str] | None = None
    project_key: str | None = None
    bullets = 0
    edu_lines: list[Line] = []
    modules: list[str] = []
    last_title: str | None = None

    def flush_education() -> None:
        nonlocal edu_lines, modules
        if not edu_lines:
            return
        text = "\n".join(item.text for item in edu_lines)
        info = _parse_education(text)
        if info is not None:
            _emit_education(c, info, edu_lines[0], ctx, "; ".join(modules[:30]) or None)
        for item in edu_lines:
            _scan_skills(
                c,
                item,
                ctx,
                (ClaimCategory.EDUCATION, f"{info.degree.lower()}|{info.subject}")
                if info
                else None,
            )
        edu_lines, modules = [], []

    for line in lines:
        head = _heading(line.text) if not line.bullet else None
        if (
            head is None
            and line.loc.character_start == 0
            and line.loc.section_title
            and line.loc.section_title != last_title
        ):
            # A Markdown/PDF section title that never appears as a text line.
            last_title = line.loc.section_title
            head = _heading(line.loc.section_title)
            heading_is_line = False
        else:
            heading_is_line = head is not None
        if head is not None:
            flush_education()
            section, context, project_key, bullets = head, None, None, 0
            if heading_is_line:
                continue
        _scan_links(c, line)
        if section != "objective":
            _scan_private(c, line)

        if section == "experience":
            job = _parse_job(line.text)
            if job is not None:
                title, employer, start, end = job
                key = f"{norm_text(employer)}|{norm_text(title)}"
                draft = c.claim(
                    ClaimCategory.EMPLOYMENT,
                    key,
                    f"{title} at {employer}",
                    {"employer": employer, "title": title},
                )
                c.observe(
                    draft,
                    line,
                    EvidenceNature.EXPLICIT_SOURCE,
                    observed={"start": start or "", "end": end or ""},
                )
                context, bullets = (ClaimCategory.EMPLOYMENT, key), 0
                continue
            if context is not None:
                _block_line(c, line, ctx, context, bullets)
                if line.bullet:
                    bullets += 1
                continue
            _scan_skills(c, line, ctx, None)
        elif section == "education":
            if _DEGREE_RE.search(line.text) and any(
                _DEGREE_RE.search(item.text) for item in edu_lines
            ):
                flush_education()
            edu_lines.append(line)
            mod = _module_name(line.text)
            if mod and ctx.source_type is SourceType.TRANSCRIPT:
                modules.append(mod)
        elif section == "projects":
            project_key, context = _project_line(c, line, ctx, project_key, context)
        elif section == "publications":
            _cv_publication(c, line, ctx)
        elif section == "interests":
            for topic in re.split(r"\s*[;,•|]\s*", line.text):
                _research_topic(c, topic, line, EvidenceNature.EXPLICIT_SOURCE)
        elif section == "certifications":
            _certification(c, line)
        elif section == "languages":
            _languages(c, line)
        elif section == "achievements":
            _achievement(c, line, None)
        elif section == "objective":
            _preference(c, line)
        else:
            _scan_skills(c, line, ctx, None)
            _scan_domains(c, line, None)
    flush_education()


def _block_line(
    c: _Collector,
    line: Line,
    ctx: _Ctx,
    context: tuple[ClaimCategory, str],
    bullets: int,
) -> None:
    _scan_skills(c, line, ctx, context)
    _scan_domains(c, line, context)
    if not line.bullet or bullets >= _MAX_BULLETS_PER_BLOCK:
        return
    if _METRIC.search(line.text):
        _achievement(c, line, context)
    else:
        text = clean_text(scrub(line.text), max_length=300)
        if len(text) >= 8:
            draft = c.claim(
                ClaimCategory.RESPONSIBILITY,
                norm_text(text)[:200],
                f"Responsibility: {text}",
                {"description": text},
            )
            c.observe(draft, line, EvidenceNature.EXPLICIT_SOURCE, context=context)


def _achievement(
    c: _Collector, line: Line, context: tuple[ClaimCategory, str] | None
) -> None:
    text = clean_text(scrub(line.text), max_length=300)
    metric = _METRIC.search(text)
    if len(text) < 8 or metric is None:
        return
    draft = c.claim(
        ClaimCategory.ACHIEVEMENT,
        norm_text(text)[:200],
        f"Achievement: {text}",
        {"description": text, "metric": metric.group(0).strip()},
    )
    c.observe(draft, line, EvidenceNature.EXPLICIT_SOURCE, context=context)


def _preference(c: _Collector, line: Line) -> None:
    text = clean_text(scrub(line.text), max_length=300)
    if len(text) < 8:
        return
    private = has_private_cue(text)
    draft = c.claim(
        ClaimCategory.CAREER_PREFERENCE,
        norm_text(text)[:200],
        f"Career preference: {text}",
        {"kind": "private" if private else "objective", "value": text},
        sensitivity=PrivacyClass.PRIVATE if private else PrivacyClass.PERSONAL,
    )
    c.observe(draft, line, EvidenceNature.EXPLICIT_SOURCE)


def _certification(c: _Collector, line: Line) -> None:
    text = clean_text(scrub(line.text), max_length=150)
    if not 5 <= len(text) <= 150:
        return
    draft = c.claim(
        ClaimCategory.CERTIFICATION,
        norm_text(text)[:150],
        f"Certification: {text}",
        {"name": text},
    )
    c.observe(draft, line, EvidenceNature.EXPLICIT_SOURCE)


def _languages(c: _Collector, line: Line) -> None:
    for item in re.split(r"\s*[;,•|]\s*", line.text):
        match = re.match(
            r"^([A-Za-z]+)\s*(?:[(\-–—:]\s*([^)]{2,30})\)?)?$", item.strip()
        )
        if match is None or match.group(1).lower() not in _LANGUAGES:
            continue
        language = match.group(1).title()
        level = (match.group(2) or "").strip()
        draft = c.claim(
            ClaimCategory.LANGUAGE,
            language.lower(),
            f"Language: {language}",
            {"language": language},
        )
        c.observe(
            draft, line, EvidenceNature.EXPLICIT_SOURCE, observed={"level": level}
        )


def _research_topic(c: _Collector, text: str, line: Line, kind: EvidenceNature) -> None:
    topic = clean_text(scrub(text), max_length=120).strip(" .")
    if not 3 <= len(topic) <= 120:
        return
    draft = c.claim(
        ClaimCategory.RESEARCH,
        norm_text(topic),
        f"Research topic: {topic}",
        {"topic": topic},
    )
    c.observe(draft, line, kind)


_PROJECT_HEADER = re.compile(
    r"^(?P<name>[^:–—|]{3,80}?)\s*(?:[:–—|]|\s-\s)\s*(?P<desc>.+)$"
)


def _project_line(
    c: _Collector,
    line: Line,
    ctx: _Ctx,
    project_key: str | None,
    context: tuple[ClaimCategory, str] | None,
) -> tuple[str | None, tuple[ClaimCategory, str] | None]:
    if not line.bullet and len(line.text) <= 120 and not line.text.endswith("."):
        start, end, rest = _range(line.text)
        match = _PROJECT_HEADER.match(rest)
        name = (match.group("name") if match else rest).strip(" ,.()")
        desc = match.group("desc").strip() if match else ""
        if 3 <= len(name) <= 80 and len(name.split()) <= 10:
            key = norm_text(name)
            draft = c.claim(
                ClaimCategory.PROJECT, key, f"Project: {name}", {"name": name}
            )
            c.observe(
                draft,
                line,
                EvidenceNature.EXPLICIT_SOURCE,
                observed={"description": desc, "start": start or "", "end": end or ""},
            )
            ref = (ClaimCategory.PROJECT, key)
            _scan_skills(c, line, ctx, ref)
            return key, ref
    if context is not None:
        _scan_skills(c, line, ctx, context)
        _scan_domains(c, line, context)
        if line.bullet and _METRIC.search(line.text):
            _achievement(c, line, context)
    else:
        _scan_skills(c, line, ctx, None)
    return project_key, context


_QUOTED_TITLE = re.compile(r"[“\"‘'](?P<t>[^”\"’']{10,300})[”\"’']")
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)


def _publication_key(title: str) -> str:
    return norm_text(title)


def _cv_publication(c: _Collector, line: Line, ctx: _Ctx) -> None:
    match = _QUOTED_TITLE.search(line.text)
    if match is None:
        return
    title = clean_text(match.group("t"), max_length=300).strip(" ,.")
    year = _YEAR.search(line.text[match.end() :]) or _YEAR.search(line.text)
    doi = _DOI.search(line.text)
    venue = ""
    after = line.text[match.end() :].strip(" ,.")
    if after:
        venue = re.split(r"[,(]|\b(?:19|20)\d{2}\b", after)[0].strip(" ,.")
    authors = line.text[: match.start()].strip(" ,.:;")
    draft = c.claim(
        ClaimCategory.PUBLICATION,
        _publication_key(title),
        f"Publication: {title}",
        {"title": title},
    )
    c.observe(
        draft,
        line,
        EvidenceNature.EXPLICIT_SOURCE,
        observed={
            "authors": authors if len(authors) <= 300 else "",
            "venue": venue[:200],
            "year": year.group(1) if year else "",
            "doi": doi.group(1).rstrip(".,;") if doi else "",
        },
    )
    ref = (ClaimCategory.PUBLICATION, _publication_key(title))
    _scan_skills(c, line, ctx, ref, in_publication=True)


# ---------------------------------------------------------- paper documents

_PAPER_SECTIONS = {
    "abstract": "abstract",
    "methodology": "methods",
    "methods": "methods",
    "method": "methods",
    "approach": "methods",
    "proposed method": "methods",
    "datasets": "datasets",
    "dataset": "datasets",
    "data": "datasets",
    "results": "findings",
    "findings": "findings",
    "conclusion": "findings",
    "conclusions": "findings",
    "limitations": "limitations",
    "author contributions": "contributions",
    "references": "references",
    "keywords": "keywords",
}
_VENUE = re.compile(
    r"(?i)[^\n]{0,80}\b(?:conference|journal|proceedings|transactions|workshop|symposium)\b[^\n]{0,120}"
)


def _paper_heading(text: str) -> str | None:
    cleaned = norm_text(text.lstrip("#").rstrip(":.")).strip()
    return _PAPER_SECTIONS.get(cleaned) if len(cleaned) <= 30 else None


def _extract_publication_document(
    c: _Collector,
    document: SourceDocument,
    lines: Sequence[Line],
    ctx: _Ctx,
    identity: OwnerProfessionalIdentity | None,
) -> None:
    if not lines:
        return
    first_segment_title = document.segments[0].location.section_title
    title_line = lines[0]
    title = clean_text(
        first_segment_title
        if first_segment_title and _paper_heading(first_segment_title) is None
        else title_line.text,
        max_length=300,
    ).strip(" .")
    if len(title) < 5:
        raise ExtractionError("no_title")
    key = _publication_key(title)
    draft = c.claim(
        ClaimCategory.PUBLICATION, key, f"Publication: {title}", {"title": title}
    )
    ref = (ClaimCategory.PUBLICATION, key)

    head_text = "\n".join(line.text for line in lines[:40])[:3_000]
    observed: dict[str, str] = {"title": title}
    doi = _DOI.search(head_text)
    if doi:
        observed["doi"] = doi.group(1).rstrip(".,;")
    year = _YEAR.search(head_text)
    if year:
        observed["year"] = year.group(1)
    venue = _VENUE.search(head_text)
    if venue:
        observed["venue"] = venue.group(0).strip()[:200]

    sections: dict[str, list[str]] = {}
    current = "front"
    keyword_line: Line | None = None
    for line in lines:
        section = _paper_heading(line.text) if not line.bullet else None
        inline = re.match(
            r"(?i)^(abstract|keywords|index terms)\s*[:—–-]\s*(.+)$", line.text
        )
        if section == "keywords" or (inline and inline.group(1).lower() != "abstract"):
            keyword_line = line
            if inline:
                sections.setdefault("keywords", []).append(inline.group(2))
                current = "front"
                continue
        if inline and inline.group(1).lower() == "abstract":
            sections.setdefault("abstract", []).append(inline.group(2))
            current = "abstract"
            continue
        if section is not None:
            current = section
            continue
        sections.setdefault(current, []).append(line.text)
        _scan_skills(c, line, ctx, ref, in_publication=True)

    # Authors: the first comma/semicolon/"and" separated line after the title.
    for line in lines[1:8]:
        if re.search(r"(?i)^(abstract|keywords)", line.text):
            break
        if (
            len(line.text) <= 300
            and re.search(r"[;,]|\band\b|&", line.text)
            and not line.text.endswith(".")
        ):
            authors = [
                re.sub(r"[\d*†‡]+", "", a).strip(" .")
                for a in re.split(r"\s*(?:;|,|\band\b|&)\s*", line.text)
            ]
            authors = [
                a for a in authors if 2 <= len(a) <= 80 and re.search(r"[A-Za-z]", a)
            ]
            if authors:
                observed["authors"] = "; ".join(authors)
                if identity is not None:
                    found = identity.author_position(authors)
                    if found.status is OwnerMatch.MATCHED and found.position:
                        observed["owner_author_position"] = str(found.position)
                break
    for name, target in (
        ("abstract", "abstract"),
        ("methods", "methods"),
        ("datasets", "datasets"),
        ("findings", "findings"),
        ("limitations", "limitations"),
    ):
        text = " ".join(sections.get(name, []))[:800].strip()
        if text:
            observed[target] = text
    methods_text = " ".join(sections.get("methods", []) + sections.get("abstract", []))
    models = sorted({m.entry.display for m in find_mentions(methods_text)})
    if models:
        observed["models"] = "; ".join(models[:15])
    topics = [
        t.strip()
        for raw in sections.get("keywords", [])
        for t in re.split(r"\s*[;,•|]\s*", raw)
        if 3 <= len(t.strip()) <= 100
    ]
    if topics:
        observed["topics"] = "; ".join(topics[:15])
    contributions = sections.get("contributions", [])
    if contributions and identity is not None:
        for sentence in re.split(r"(?<=[.!?])\s+", " ".join(contributions)):
            if identity.mentioned_in(sentence):
                observed["owner_contribution"] = sentence[:500]
                break
    c.observe(draft, title_line, EvidenceNature.EXPLICIT_SOURCE, observed=observed)
    for topic in topics[:15]:
        _research_topic(
            c, topic, keyword_line or title_line, EvidenceNature.INFERRED_RELATIONSHIP
        )


# --------------------------------------------------------- project documents


def _extract_project_document(
    c: _Collector,
    document: SourceDocument,
    lines: Sequence[Line],
    ctx: _Ctx,
) -> None:
    if not lines:
        return
    if ctx.manifest:
        for line in lines:
            _scan_skills(c, line, ctx, None)
        return
    title_source = document.segments[0].location.section_title or lines[0].text
    name = clean_text(title_source, max_length=120).strip(" #.")
    if len(name) < 2:
        name = re.sub(r"\.[A-Za-z0-9]+$", "", ctx.label)
    key = norm_text(name)
    draft = c.claim(ClaimCategory.PROJECT, key, f"Project: {name}", {"name": name})
    description = next(
        (
            ln.text
            for ln in lines
            if not ln.bullet and len(ln.text) >= 30 and ln.text != name
        ),
        "",
    )
    c.observe(
        draft,
        lines[0],
        EvidenceNature.EXPLICIT_SOURCE,
        observed={"description": description[:300]},
    )
    ref = (ClaimCategory.PROJECT, key)
    for line in lines:
        _scan_skills(c, line, ctx, ref)
        _scan_domains(c, line, ref)
        _scan_links(c, line)
        if line.bullet and _METRIC.search(line.text):
            _achievement(c, line, ref)


# ------------------------------------------------------------ linkedin CSV


def _row(text: str) -> dict[str, str]:
    row: dict[str, str] = {}
    for part in text.split(" | "):
        name, sep, value = part.partition(": ")
        if sep:
            row[name.strip()] = value.strip()
    return row


def _extract_linkedin_csv(c: _Collector, lines: Sequence[Line], ctx: _Ctx) -> None:
    for line in lines:
        row = _row(line.text)
        if not row:
            continue
        if "Company Name" in row and "Title" in row:
            employer, title = row["Company Name"], row["Title"]
            if employer and title:
                key = f"{norm_text(employer)}|{norm_text(title)}"
                draft = c.claim(
                    ClaimCategory.EMPLOYMENT,
                    key,
                    f"{title} at {employer}",
                    {"employer": employer, "title": title},
                )
                start = normalize_date(row.get("Started On", "")) or ""
                finished = row.get("Finished On", "").strip()
                end = normalize_date(finished) if finished else PRESENT
                c.observe(
                    draft,
                    line,
                    EvidenceNature.EXPLICIT_SOURCE,
                    observed={"start": start, "end": end or ""},
                )
                ref = (ClaimCategory.EMPLOYMENT, key)
                _scan_skills(
                    c,
                    Line(row.get("Description", ""), line.loc, False, line.segment),
                    ctx,
                    ref,
                )
        elif "School Name" in row:
            info = _parse_education(
                " ".join(
                    (
                        row.get("Degree Name", ""),
                        row.get("Notes", ""),
                        row["School Name"],
                    )
                )
            )
            if info is not None:
                info = _Education(
                    info.degree,
                    info.subject,
                    info.subject_display,
                    row["School Name"],
                    info.classification,
                    normalize_date(row.get("Start Date", "")) or info.start,
                    normalize_date(row.get("End Date", "")) or info.end,
                    info.dissertation,
                )
                _emit_education(c, info, line, ctx)
        elif set(row) <= {"Name", "Endorsement Count"} and "Name" in row:
            entry = normalize_skill(row["Name"])
            if entry is None:
                c.unmapped += 1
                continue
            draft = c.claim(
                entry.kind,
                entry.skill_id,
                _skill_statement(entry),
                {"skill_id": entry.skill_id, "display": entry.display},
            )
            c.observe(draft, line, EvidenceNature.EXPLICIT_SOURCE)
        elif "Name" in row and ("Published On" in row or "Publisher" in row):
            title = row["Name"]
            if len(title) >= 5:
                year = _YEAR.search(row.get("Published On", ""))
                draft = c.claim(
                    ClaimCategory.PUBLICATION,
                    _publication_key(title),
                    f"Publication: {title}",
                    {"title": title},
                )
                c.observe(
                    draft,
                    line,
                    EvidenceNature.EXPLICIT_SOURCE,
                    observed={
                        "venue": row.get("Publisher", "")[:200],
                        "year": year.group(1) if year else "",
                    },
                )


# --------------------------------------------------------------- entry point


def extract(
    document: SourceDocument,
    *,
    source_type: SourceType,
    label: str,
    identity: OwnerProfessionalIdentity | None = None,
) -> ExtractionResult:
    """Deterministic drafts for one source. Same input, same output, no model."""

    lines = _lines(document)
    ctx = _Ctx(
        source_type=source_type,
        label=label,
        manifest=_is_manifest(label, document),
        readme=_is_readme(label),
    )
    c = _Collector()
    if source_type is SourceType.PUBLICATION:
        _extract_publication_document(c, document, lines, ctx, identity)
    elif source_type is SourceType.LINKEDIN_EXPORT and document.resource_type == "csv":
        _extract_linkedin_csv(c, lines, ctx)
    elif source_type in (SourceType.PROJECT_DOCUMENTATION, SourceType.GITHUB):
        _extract_project_document(c, document, lines, ctx)
    else:
        _extract_resume_like(c, lines, ctx)
    return c.finish()


__all__ = [
    "EXTRACTOR_VERSION",
    "ClaimDraft",
    "EvidenceDraft",
    "ExtractionError",
    "ExtractionResult",
    "extract",
]
