"""Opportunity sources: trust order, URL/domain safety and the raw-listing input.

Everything a source returns is UNTRUSTED data. A ``RawListing`` is only text
plus provenance: it can never carry an instruction, a path or a credential, and
nothing in it is ever executed. Sam does not scrape private authenticated
sessions or bypass platform controls: a source adapter reads only public pages
(or text the owner pastes), through Sam's own trusted integrations.

Domain rules are deterministic and conservative: HTTPS only, no userinfo
(``https://company.com@evil.io``), no internationalized / punycode hosts
(``xn--``), no IP literals. The "site" of a host is its registrable domain,
with a small, explicit list of two-label public suffixes.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

from sam.career.models import OpportunityType, SourceKind

JOB_PRIORITY: tuple[SourceKind, ...] = (
    SourceKind.OFFICIAL_CAREER_PAGE,
    SourceKind.OFFICIAL_ATS,
    SourceKind.LINKEDIN,
    SourceKind.INDEED,
    SourceKind.GLASSDOOR,
    SourceKind.OTHER,
)
PHD_PRIORITY: tuple[SourceKind, ...] = (
    SourceKind.UNIVERSITY_PAGE,
    SourceKind.FUNDING_PAGE,
    SourceKind.SUPERVISOR_PAGE,
    SourceKind.ACADEMIC_SOURCE,
    SourceKind.OTHER,
)
# Only these kinds may define where an application is submitted.
OFFICIAL_KINDS = frozenset(
    {
        SourceKind.OFFICIAL_CAREER_PAGE,
        SourceKind.OFFICIAL_ATS,
        SourceKind.UNIVERSITY_PAGE,
        SourceKind.FUNDING_PAGE,
    }
)
_TWO_LABEL_SUFFIXES = frozenset(
    {
        "co.uk",
        "ac.uk",
        "org.uk",
        "gov.uk",
        "nhs.uk",
        "ltd.uk",
        "plc.uk",
        "com.au",
        "edu.au",
        "org.au",
        "co.jp",
        "ac.jp",
        "co.nz",
        "ac.nz",
        "com.br",
        "co.in",
        "ac.in",
        "edu.cn",
        "com.cn",
        "co.za",
        "ac.za",
    }
)


class UnsafeURL(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def priority(kind: SourceKind, opportunity_type: OpportunityType) -> int:
    order = JOB_PRIORITY if opportunity_type is OpportunityType.JOB else PHD_PRIORITY
    return order.index(kind) if kind in order else len(order)


def safe_host(url: str) -> str:
    """The lower-cased host of an HTTPS URL, or ``UnsafeURL``."""

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise UnsafeURL("url_invalid") from None
    if parts.scheme != "https":
        raise UnsafeURL("url_not_https")
    if "@" in parts.netloc or parts.username or parts.password:
        raise UnsafeURL("url_userinfo")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        raise UnsafeURL("url_invalid")
    if not host.isascii() or any(label.startswith("xn--") for label in host.split(".")):
        raise UnsafeURL("url_idn_host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeURL("url_ip_host")
    if not all(label and label.replace("-", "").isalnum() for label in host.split(".")):
        raise UnsafeURL("url_invalid")
    return host


def registrable_domain(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_host(url: str, expected_url: str) -> bool:
    """Exact host equality of two safe HTTPS URLs (no lookalikes, no subdomain
    tricks such as ``company.com.evil.io``)."""

    try:
        return safe_host(url) == safe_host(expected_url)
    except UnsafeURL:
        return False


_UNSAFE_CHARS = frozenset("\\ \t\r\n\x00")


def canonical_destination(url: str) -> str:
    """The one canonical form of a submission destination, or ``UnsafeURL``.

    HTTPS only, ASCII host only (no IDN / punycode lookalikes), no userinfo,
    no IP literal, default port only, no backslash or whitespace tricks; the
    fragment is dropped. Two URLs name the same destination only if their
    canonical forms are equal."""

    raw = url.strip()
    if any(c in _UNSAFE_CHARS or ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise UnsafeURL("url_invalid")
    host = safe_host(raw)
    parts = urlsplit(raw)
    try:
        port = parts.port
    except ValueError:
        raise UnsafeURL("url_invalid") from None
    if port not in (None, 443):
        raise UnsafeURL("url_port")
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{path}{query}"


class DestinationGuard:
    """Trusted destination identity for one submission, from opportunity
    provenance (an official source's application URL), never from page or
    model text at submission time.

    A future HTTP/browser adapter must either disable redirects or ask
    ``allows`` for EVERY redirect hop before following it; a hop to any other
    host (a lookalike, a subdomain trick, the company's unrelated main site or
    an attacker) is refused. Sam also re-checks the final destination an
    adapter reports before believing a success."""

    def __init__(self, trusted_url: str) -> None:
        self.url = canonical_destination(trusted_url)
        self.host = safe_host(self.url)

    def allows(self, url: str | None) -> bool:
        if url is None:
            return False
        try:
            return safe_host(canonical_destination(url)) == self.host
        except UnsafeURL:
            return False


@dataclass(frozen=True)
class RawListing:
    """One untrusted listing as a source returned it."""

    kind: SourceKind
    opportunity_type: OpportunityType
    url: str
    text: str
    retrieved_at: datetime
    external_id: str | None = None
    application_url: str | None = None


@dataclass(frozen=True)
class SearchQuery:
    keywords: tuple[str, ...]
    opportunity_type: OpportunityType
    locations: tuple[str, ...] = ()


class OpportunitySource(Protocol):
    """A trusted, read-only discovery integration (public pages only)."""

    kind: SourceKind

    def search(self, query: SearchQuery) -> Sequence[RawListing]: ...


__all__ = [
    "JOB_PRIORITY",
    "DestinationGuard",
    "OFFICIAL_KINDS",
    "PHD_PRIORITY",
    "OpportunitySource",
    "RawListing",
    "SearchQuery",
    "UnsafeURL",
    "canonical_destination",
    "priority",
    "registrable_domain",
    "safe_host",
    "same_host",
]
