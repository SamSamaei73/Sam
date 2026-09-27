"""Immutable manifests of exactly what an external action would send.

A SUBMIT or SEND confirmation is bound (as the PermissionEngine ``target``
``manifest:<hash>``) to the hash of one of these manifests, so any meaningful
change to the external payload (an answer, a document, the destination, the
recipient, the subject or body) produces a new hash and invalidates every
earlier confirmation.

Canonicalization is deterministic: every string is Unicode NFC-normalized,
collections are sorted by their identity key, mappings are serialized with
sorted keys and no insignificant whitespace, and irrelevant metadata
(timestamps, review flags, storage order) is never part of a manifest.

A manifest may contain PRIVATE content (answers are hashed, but the recipient
address is not). It is never logged or audited: only its hash is.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

SUBMIT_SCHEMA = "sam.career.submit/1"
SEND_SCHEMA = "sam.career.send/1"


def _normalize(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {_normalize(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    raise TypeError("unsupported manifest value")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def text_hash(text: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", text).encode()).hexdigest()


@dataclass(frozen=True)
class ManifestDocument:
    document_id: str
    kind: str
    version: int
    sha256: str


@dataclass(frozen=True)
class ManifestAnswer:
    question_id: str
    question_sha256: str
    answer_sha256: str


@dataclass(frozen=True)
class SubmissionManifest:
    """Everything a submission would send, by identity and hash."""

    opportunity_id: str
    draft_id: str
    draft_version: int
    destination_host: str
    destination_url: str
    documents: tuple[ManifestDocument, ...]
    answers: tuple[ManifestAnswer, ...]
    form_version: str | None = None

    def canonical(self) -> dict[str, object]:
        return {
            "schema": SUBMIT_SCHEMA,
            "action": "submit",
            "opportunity_id": self.opportunity_id,
            "draft_id": self.draft_id,
            "draft_version": self.draft_version,
            "destination": {
                "host": self.destination_host,
                "url": self.destination_url,
                "form_version": self.form_version,
            },
            "documents": [
                {
                    "document_id": d.document_id,
                    "kind": d.kind,
                    "version": d.version,
                    "sha256": d.sha256,
                }
                for d in sorted(self.documents, key=lambda d: d.document_id)
            ],
            "answers": [
                {
                    "question_id": a.question_id,
                    "question_sha256": a.question_sha256,
                    "answer_sha256": a.answer_sha256,
                }
                for a in sorted(self.answers, key=lambda a: a.question_id)
            ],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical())).hexdigest()


@dataclass(frozen=True)
class SendManifest:
    """Everything an outreach send would transmit, by identity and hash."""

    outreach_id: str
    outreach_version: int
    opportunity_id: str | None
    contact_id: str
    channel: str
    recipient: str
    subject: str
    body: str
    attachments: tuple[ManifestDocument, ...] = ()

    def canonical(self) -> dict[str, object]:
        return {
            "schema": SEND_SCHEMA,
            "action": "send",
            "channel": self.channel,
            "outreach_id": self.outreach_id,
            "outreach_version": self.outreach_version,
            "opportunity_id": self.opportunity_id,
            "contact_id": self.contact_id,
            "recipient": unicodedata.normalize("NFC", self.recipient)
            .strip()
            .casefold(),
            "subject_sha256": text_hash(self.subject),
            "body_sha256": text_hash(self.body),
            "attachments": [
                {
                    "document_id": d.document_id,
                    "kind": d.kind,
                    "version": d.version,
                    "sha256": d.sha256,
                }
                for d in sorted(self.attachments, key=lambda d: d.document_id)
            ],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical())).hexdigest()


def answers_for(
    questions: Mapping[str, str], answers: Sequence[tuple[str, str]]
) -> tuple[ManifestAnswer, ...]:
    """``questions`` maps question id to question text; ``answers`` is the
    (question id, answer text) pairs that would be submitted."""

    return tuple(
        ManifestAnswer(qid, text_hash(questions.get(qid, "")), text_hash(text))
        for qid, text in answers
    )


__all__ = [
    "SEND_SCHEMA",
    "SUBMIT_SCHEMA",
    "ManifestAnswer",
    "ManifestDocument",
    "SendManifest",
    "SubmissionManifest",
    "answers_for",
    "canonical_json",
    "text_hash",
]
