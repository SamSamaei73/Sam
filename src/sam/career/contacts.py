"""Trusted contact evidence: recruiters, hiring managers, team leads, professors,
supervisors and research-group contacts.

A contact exists only with provenance: a source (kind, URL, retrieval time) and
the exact quote that names the person. Identity is never inferred from thin
evidence: the quote must contain the person's full name AND the organization.
An e-mail address is recorded only if it appears VERBATIM in the quoted
evidence; Sam never constructs one from a name and a domain
("firstname.lastname@company.com") or any other pattern.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from sam.career.models import (
    Contact,
    ContactEvidence,
    ContactRole,
    SourceKind,
    clean_text,
)
from sam.career.sources import UnsafeURL, safe_host

_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w-])"
)


class ContactRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w@.]+", " ", text.casefold()).split())


def build_contact(
    *,
    contact_id: str,
    name: str,
    role: ContactRole,
    organization: str,
    source_kind: SourceKind,
    url: str,
    quote: str,
    retrieved_at: datetime,
    email: str | None = None,
    opportunity_id: str | None = None,
    research_topics: Sequence[str] = (),
) -> Contact:
    name = clean_text(name, 200)
    organization = clean_text(organization, 200)
    quote = clean_text(quote, 500)
    if len(name.split()) < 2:
        raise ContactRejected("full_name_required")
    try:
        safe_host(url)
    except UnsafeURL as error:
        raise ContactRejected(error.code) from None
    haystack = _norm(quote)
    if _norm(name) not in haystack:
        raise ContactRejected("name_not_in_evidence")
    if _norm(organization) not in haystack:
        raise ContactRejected("organization_not_in_evidence")
    stated = {m.group(0).casefold() for m in _EMAIL.finditer(quote)}
    if email is not None:
        email = email.strip().casefold()
        if email not in stated:
            raise ContactRejected("email_not_in_evidence")
    return Contact(
        contact_id=contact_id,
        name=name,
        role=role,
        organization=organization,
        email=email,
        opportunity_id=opportunity_id,
        research_topics=tuple(
            t for t in (clean_text(x, 80) for x in research_topics) if t
        )[:20],
        evidence=(
            ContactEvidence(
                source_kind=source_kind,
                url=url.strip(),
                quote=quote,
                retrieved_at=retrieved_at,
            ),
        ),
    )


__all__ = ["ContactRejected", "build_contact"]
