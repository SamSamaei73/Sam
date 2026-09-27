# ruff: noqa: E501  (synthetic document fixtures: line width here is content)
"""Shared fixtures for the Professional Intelligence tests.

Every document here is SYNTHETIC ("Jordan Example" and friends): no real CV,
transcript or private professional material is used anywhere in the tests, and
none is ever sent to a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sam.models.models import PrivacyClass
from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
)
from sam.permissions.store import InMemoryPermissionStore
from sam.professional.audit import InMemoryProfessionalAuditSink
from sam.professional.candidates import CandidateExtractor
from sam.professional.identity import OwnerProfessionalIdentity
from sam.professional.models import SourceType
from sam.professional.repository import InMemoryProfessionalRepository
from sam.professional.service import ProfessionalService
from sam.professional.views import IngestSummary, OpResult

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
OWNER = Principal(kind=PrincipalKind.USER, id="owner")
STRANGER = Principal(kind=PrincipalKind.USER, id="stranger")
OWNER_NAME = "Jordan Example"
OWNER_IDENTITY = OwnerProfessionalIdentity(OWNER_NAME)

CV_TEXT = """Jordan Example
jordan@example.test   +44 7700 900123

SUMMARY
Machine learning engineer.

EXPERIENCE
Senior Software Engineer, Acme Analytics, Mar 2019 - Jun 2022
- Built production RAG pipelines with FastAPI and PyTorch serving 12k users
- Led a team of 4 engineers
- Deployed on AWS using Docker
Data Scientist at Globex Health | Jul 2022 - Present
- Developed NLP models for healthcare triage, improving recall by 18%

EDUCATION
MSc Artificial Intelligence, Example University, 2021 - 2022, with Distinction
Student ID: A1234567

SKILLS
Python, TypeScript, React, FastAPI, PyTorch, Docker, AWS, Kubernetes

PROJECTS
Smart Widget - an agentic AI assistant built with Python and LangChain
- Reduced manual triage time by 40%

PUBLICATIONS
J. Example, "Semantic Embeddings for Health Misinformation Detection", Journal of Example AI, 2023.

LANGUAGES
English (fluent), Persian (native)

LINKS
https://github.com/jordan-example/smart-widget
"""

LINKEDIN_CSV = (
    "Company Name,Title,Description,Location,Started On,Finished On\n"
    'Acme Analytics,Senior Software Engineer,"Built RAG systems with FastAPI",'
    "London,Apr 2019,Jun 2022\n"
    'Globex Health,Data Scientist,"NLP for triage",Remote,Jul 2022,\n'
)

LINKEDIN_AGREEING_CSV = (
    "Company Name,Title,Description,Location,Started On,Finished On\n"
    'Acme Analytics,Senior Software Engineer,"Built RAG systems",London,Mar 2019,Jun 2022\n'
)

PAPER_TEXT = """Semantic Embeddings for Health Misinformation Detection
Jordan Example, Alex Sample and Riley Placeholder
Journal of Example AI 2023
Abstract: We propose a Graph Convolutional Network over semantic embeddings for health misinformation.
Keywords: graph neural networks; health misinformation; semantic embeddings
Methodology
We build a graph of claims and apply a graph convolutional network with semantic embeddings.
Datasets
We evaluate on two public datasets of health claims.
Results
The model improves F1 over the baselines.
Limitations
Evaluation is limited to English text.
Author contributions
Riley Placeholder implemented the model. Alex Sample curated the datasets.
DOI 10.1234/example.2023.001
"""

TRANSCRIPT_TEXT = """Example University
Academic Transcript
Student ID: 12345678
Account Number: 99887766
Programme: MSc Artificial Intelligence
Classification: Distinction
2021 - 2022
Dissertation: Graph Learning for Health Claims
Student number 12345678 enrolled in Deep Learning with Python
Modules
Machine Learning        78
Natural Language Processing        81
Deep Learning        74
"""

README_TEXT = """# Smart Widget
An agentic AI assistant that triages support tickets for small teams.
Built with Python and LangChain, deployed with Docker.
"""

REQUIREMENTS_TXT = "fastapi==0.110.0\nkubernetes==29.0.0\ntorch==2.2.0\n"

K8S_YAML = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
    "spec:\n  containers:\n  - name: web\n    image: web:latest\n"
)


@dataclass
class Rig:
    service: ProfessionalService
    repo: InMemoryProfessionalRepository
    engine: PermissionEngine
    grants: InMemoryPermissionStore
    confirmations: InMemoryConfirmationProvider
    audit: InMemoryProfessionalAuditSink
    permission_audit: InMemoryAuditSink

    def ingest(
        self,
        text: str | bytes,
        *,
        name: str = "cv.txt",
        source_type: SourceType | str = SourceType.MASTER_CV,
        privacy: PrivacyClass = PrivacyClass.PERSONAL,
        resource_type: str = "txt",
        principal: Principal = OWNER,
        **kwargs: Any,
    ) -> OpResult[IngestSummary]:
        content = text if isinstance(text, bytes) else text.encode("utf-8")
        kind = source_type.value if isinstance(source_type, SourceType) else source_type
        return self.service.ingest_source(
            principal,
            name=name,
            source_type=kind,
            privacy_class=privacy,
            resource_type=resource_type,
            content=content,
            **kwargs,
        )

    def approve(self, confirmation_id: str) -> None:
        self.confirmations.decide(confirmation_id, approved=True, now=NOW)

    def source_id(self, name: str, source_type: SourceType) -> str:
        from sam.professional.models import new_source_id

        return new_source_id(source_type, name)


def _grant(
    store: InMemoryPermissionStore,
    principal: Principal,
    seq: int,
    action: PermissionAction,
    scope: PermissionScope,
) -> None:
    store.create_grant(
        PermissionGrant(
            grant_id=f"p-{seq}",
            principal=principal,
            resource=PermissionResource.PROFESSIONAL,
            action=action,
            scope=scope,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def make_rig(
    *,
    grants: bool = True,
    owner_identity: OwnerProfessionalIdentity | None = OWNER_IDENTITY,
    candidate_extractor: CandidateExtractor | None = None,
    now: datetime = NOW,
    clock_advance: timedelta | None = None,
) -> Rig:
    clock_state = {"now": now}

    def clock() -> datetime:
        return clock_state["now"] + (clock_advance or timedelta())

    store = InMemoryPermissionStore()
    confirmations = InMemoryConfirmationProvider()
    perm_audit = InMemoryAuditSink()
    engine = PermissionEngine(
        store=store,
        confirmation_provider=confirmations,
        audit_sink=perm_audit,
        clock=clock,
    )
    if grants:
        # Mirrors the desktop bootstrap: exactly these four, nothing wider.
        _grant(
            store,
            OWNER,
            1,
            PermissionAction.READ,
            PermissionScope.identifier("profile:read"),
        )
        _grant(
            store,
            OWNER,
            2,
            PermissionAction.WRITE,
            PermissionScope.identifier("profile:ingest"),
        )
        _grant(
            store,
            OWNER,
            3,
            PermissionAction.UPDATE,
            PermissionScope.identifier("profile:review"),
        )
        _grant(
            store,
            OWNER,
            4,
            PermissionAction.DELETE,
            PermissionScope.from_path("profile"),
        )
    repo = InMemoryProfessionalRepository()
    audit = InMemoryProfessionalAuditSink()
    service = ProfessionalService(
        repository=repo,
        permission_engine=engine,
        audit_sink=audit,
        candidate_extractor=candidate_extractor,
        owner_identity=owner_identity,
        clock=clock,
    )
    return Rig(service, repo, engine, store, confirmations, audit, perm_audit)


def ingest_cv(rig: Rig, **kwargs: Any) -> OpResult[IngestSummary]:
    result = rig.ingest(CV_TEXT, **kwargs)
    assert result.ok, result.reason
    return result
