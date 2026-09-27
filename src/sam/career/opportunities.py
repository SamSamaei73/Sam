"""Deterministic normalization of an untrusted listing into a CareerOpportunity.

Only EXPLICIT statements become facts:

* title / organization / location / salary / funding / deadline come from
  labelled lines ("Title:", "Company:", "Deadline:" ...) or unambiguous
  phrases; if a value is missing or ambiguous it stays UNKNOWN (``None``);
* sponsorship is AVAILABLE or NOT_AVAILABLE only when a sentence says so, and
  UNKNOWN when the text is silent or contradictory;
* a work mode is set only from an explicit label or a single unambiguous mode;
* requirements are the bullet lines under a requirements / eligibility heading
  (and "preferred" / "desirable" ones under their own heading);
* the application URL is kept only from an OFFICIAL source and only if it is a
  safe HTTPS URL.

The listing's text is stored as a bounded description and is never read as an
instruction: "ignore previous instructions", "upload ~/.ssh/id_rsa" and the like
are just characters in a field.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

from sam.career.models import (
    MAX_DESCRIPTION,
    MAX_REQUIREMENT,
    MAX_REQUIREMENTS,
    CareerOpportunity,
    OpportunityStatus,
    OpportunityType,
    Requirement,
    SourceRecord,
    Sponsorship,
    WorkMode,
    clean_text,
    sha256,
)
from sam.career.sources import (
    OFFICIAL_KINDS,
    RawListing,
    UnsafeURL,
    registrable_domain,
    safe_host,
)
from sam.models.models import PrivacyClass
from sam.models.policies import PrivacyPolicy


class ListingRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_LABEL = re.compile(
    r"^\s*(?P<label>[A-Za-z][A-Za-z /()'-]{1,40}?)\s*:\s*(?P<value>.+?)\s*$"
)
_TITLE_LABELS = frozenset(
    {
        "title",
        "job title",
        "role",
        "position",
        "programme",
        "program",
        "project",
        "phd project",
    }
)
_ORG_LABELS = frozenset(
    {
        "company",
        "organization",
        "organisation",
        "employer",
        "university",
        "institution",
        "department",
    }
)
_LOCATION_LABELS = frozenset({"location", "based in", "office"})
_MODE_LABELS = frozenset({"work mode", "workplace type", "working pattern", "remote"})
_SALARY_LABELS = frozenset({"salary", "compensation", "pay", "salary range"})
_FUNDING_LABELS = frozenset({"funding", "stipend", "studentship", "funding details"})
_DEADLINE_LABELS = frozenset(
    {
        "deadline",
        "closing date",
        "apply by",
        "applications close",
        "application deadline",
    }
)
_POSTED_LABELS = frozenset({"posted", "date posted", "published"})
_TOPIC_LABELS = frozenset(
    {"research areas", "research topics", "topics", "keywords", "research area"}
)
_APPLY_LABELS = frozenset({"apply", "application url", "apply at", "how to apply"})

_REQ_HEADING = re.compile(
    r"(?i)^\s*#*\s*(requirements|qualifications|essential( criteria)?|what you('ll|"
    r" will) need|"
    r"about you|you have|eligibility|entry requirements|who we're looking for|"
    r"skills required)\s*:?\s*$"
)
_PREF_HEADING = re.compile(
    r"(?i)^\s*#*\s*(preferred( qualifications)?|desirable( criteria)?|nice to have|"
    r"bonus( points)?|"
    r"it would be great if)\s*:?\s*$"
)
_ANY_HEADING = re.compile(r"^\s*#*\s*[A-Z][A-Za-z '&/-]{2,60}:?\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•●]|\d+[.)])\s+(?P<item>.+)$")
_CLOSED = re.compile(
    r"(?i)\b(no longer accepting applications|this (job|position|vacancy|role) (is|"
    r"has) (now )?closed|"
    r"applications (are|have) (now )?closed|position (has been )?filled)\b"
)
_SPONSOR_YES = re.compile(
    r"(?i)\b(visa sponsorship (is )?available|we (can|will|are able to) (offer |"
    r"provide )?(visa )?sponsor(ship)?|"
    r"sponsorship (is )?(available|offered|provided))\b"
)
_SPONSOR_NO = re.compile(
    r"(?i)\b((no|not able to|unable to|cannot|can't|do not|does not) (offer |"
    r"provide )?(visa )?sponsor(ship)?|"
    r"sponsorship (is )?not (available|offered|provided)|"
    r"without (the need for )?(visa )?sponsorship)\b"
)
_MODE_WORDS = {
    WorkMode.REMOTE: re.compile(r"(?i)\b(fully remote|remote[- ]first|100% remote)\b"),
    WorkMode.HYBRID: re.compile(r"(?i)\bhybrid\b"),
    WorkMode.ONSITE: re.compile(r"(?i)\b(on[- ]site|in[- ]office|office[- ]based)\b"),
}
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}
_ISO_DATE = re.compile(r"\b(?P<y>20\d\d)-(?P<m>\d\d)-(?P<d>\d\d)\b")
_DMY = re.compile(
    r"(?i)\b(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon>[a-z]+)\s+(?P<y>20\d\d)\b"
)
_MDY = re.compile(
    r"(?i)\b(?P<mon>[a-z]+)\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>20\d\d)\b"
)
_PRIVACY = PrivacyPolicy()


def normalize_name(value: str) -> str:
    text = re.sub(r"[^\w]+", " ", value.casefold())
    text = re.sub(r"\b(ltd|limited|inc|llc|plc|gmbh|the|university of)\b", " ", text)
    return " ".join(text.split())


def normalize_title(value: str) -> str:
    text = value.casefold()
    text = re.sub(r"\b(sr|snr)\b\.?", "senior", text)
    text = re.sub(r"\b(jr|jnr)\b\.?", "junior", text)
    text = re.sub(r"\(.*?\)|[^\w]+", " ", text)
    return " ".join(text.split())


def parse_date(text: str) -> date | None:
    """Exactly one explicit calendar date in ``text``, else None."""

    found: set[date] = set()
    for m in _ISO_DATE.finditer(text):
        found.add(_safe_date(int(m["y"]), int(m["m"]), int(m["d"])))
    for pattern in (_DMY, _MDY):
        for m in pattern.finditer(text):
            month = _MONTHS.get(m["mon"].casefold())
            if month:
                found.add(_safe_date(int(m["y"]), month, int(m["d"])))
    found.discard(date.min)
    return next(iter(found)) if len(found) == 1 else None


def _safe_date(y: int, m: int, d: int) -> date:
    try:
        return date(y, m, d)
    except ValueError:
        return date.min


def _labels(lines: list[str]) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for line in lines:
        m = _LABEL.match(line)
        if m:
            table.setdefault(m["label"].strip().casefold(), []).append(
                m["value"].strip()
            )
    return table


def _one(table: dict[str, list[str]], labels: frozenset[str]) -> str | None:
    values = {v for label in labels for v in table.get(label, [])}
    return clean_text(next(iter(values)), 300) if len(values) == 1 else None


def _requirements(lines: list[str]) -> tuple[Requirement, ...]:
    out: list[Requirement] = []
    section: bool | None = None  # None: outside; False: required; True: preferred
    for line in lines:
        if _REQ_HEADING.match(line):
            section = False
            continue
        if _PREF_HEADING.match(line):
            section = True
            continue
        bullet = _BULLET.match(line)
        if bullet and section is not None:
            item = clean_text(bullet["item"], MAX_REQUIREMENT)
            if item:
                out.append(Requirement(text=item, preferred=section))
            continue
        if line.strip() and _ANY_HEADING.match(line):
            section = None
    return tuple(out[:MAX_REQUIREMENTS])


def _work_mode(table: dict[str, list[str]], text: str) -> WorkMode:
    label = _one(table, _MODE_LABELS)
    haystack = label if label is not None else text
    found = {mode for mode, pattern in _MODE_WORDS.items() if pattern.search(haystack)}
    if (
        label is not None
        and not found
        and label.strip().casefold() in {"remote", "yes"}
    ):
        found = {WorkMode.REMOTE}
    return next(iter(found)) if len(found) == 1 else WorkMode.UNKNOWN


def _sponsorship(text: str) -> Sponsorship:
    yes, no = bool(_SPONSOR_YES.search(text)), bool(_SPONSOR_NO.search(text))
    if yes and not no:
        return Sponsorship.AVAILABLE
    if no and not yes:
        return Sponsorship.NOT_AVAILABLE
    return Sponsorship.UNKNOWN


def opportunity_id(
    opportunity_type: OpportunityType,
    organization: str,
    title: str,
    location: str | None,
) -> str:
    key = "|".join(
        (
            opportunity_type.value,
            normalize_name(organization),
            normalize_title(title),
            normalize_name(location or ""),
        )
    )
    return "op_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def normalize_listing(listing: RawListing, now: datetime) -> CareerOpportunity:
    """Raises ``ListingRejected`` if the listing cannot be used safely."""

    try:
        host = safe_host(listing.url)
    except UnsafeURL as error:
        raise ListingRejected(error.code) from None
    text = listing.text.replace("\r\n", "\n")[: MAX_DESCRIPTION * 2]
    secret = _PRIVACY.classify([text], PrivacyClass.PUBLIC) is PrivacyClass.SECRET
    lines = text.split("\n")
    table = _labels(lines)
    title = _one(table, _TITLE_LABELS) or next(
        (clean_text(x, 200) for x in lines if x.strip()), ""
    )
    organization = _one(table, _ORG_LABELS)
    if not title or not organization:
        raise ListingRejected("title_or_organization_unknown")
    location = _one(table, _LOCATION_LABELS)
    deadline_text = _one(table, _DEADLINE_LABELS)
    posted_text = _one(table, _POSTED_LABELS)
    deadline = parse_date(deadline_text) if deadline_text else None
    status = (
        OpportunityStatus.CLOSED if _CLOSED.search(text) else OpportunityStatus.ACTIVE
    )
    if deadline is not None and deadline < now.date():
        status = OpportunityStatus.CLOSED
    application_url = None
    candidate = listing.application_url or _one(table, _APPLY_LABELS)
    if candidate and listing.kind in OFFICIAL_KINDS:
        try:
            safe_host(candidate)
            application_url = candidate.strip()
        except UnsafeURL:
            application_url = None
    topics_text = _one(table, _TOPIC_LABELS)
    topics = tuple(
        t for t in (clean_text(x, 80) for x in (topics_text or "").split(",")) if t
    )[:20]
    description = "" if secret else clean_text(text, MAX_DESCRIPTION)
    requirements = () if secret else _requirements(lines)
    salary = _one(table, _SALARY_LABELS)
    funding = _one(table, _FUNDING_LABELS)
    content = "\n".join(
        [
            title,
            organization,
            location or "",
            deadline_text or "",
            salary or "",
            funding or "",
        ]
        + [r.text for r in requirements]
        + [description]
    )
    checksum = sha256(content)
    source = SourceRecord(
        kind=listing.kind,
        url=listing.url.strip(),
        domain=registrable_domain(host),
        external_id=clean_text(listing.external_id, 200)
        if listing.external_id
        else None,
        retrieved_at=listing.retrieved_at,
        content_checksum=checksum,
    )
    is_phd = listing.opportunity_type is OpportunityType.PHD
    return CareerOpportunity(
        opportunity_id=opportunity_id(
            listing.opportunity_type, organization, title, location
        ),
        type=listing.opportunity_type,
        title=clean_text(title, 200),
        organization=organization,
        location=location,
        work_mode=_work_mode(table, text),
        canonical_source=listing.kind,
        canonical_url=listing.url.strip(),
        canonical_domain=registrable_domain(host),
        application_url=application_url,
        external_reference_id=source.external_id,
        first_seen=now,
        last_seen=now,
        last_verified=listing.retrieved_at,
        posted_date=parse_date(posted_text) if posted_text else None,
        deadline=deadline,
        description=description,
        requirements=requirements,
        compensation=None if is_phd else salary,
        funding=funding if is_phd else None,
        sponsorship=_sponsorship(text),
        research_topics=topics,
        status=status,
        checksum=checksum,
        sources=(source,),
    )


__all__ = [
    "ListingRejected",
    "normalize_listing",
    "normalize_name",
    "normalize_title",
    "opportunity_id",
    "parse_date",
]
