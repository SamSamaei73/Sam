"""Contacts, outreach, sending, follow-ups, audit privacy, domain separation and
the Proactive boundary. Fakes only: no message is ever sent anywhere real."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import sam.career as career_package
from sam.career.models import (
    ContactRole,
    OutreachChannel,
    OutreachKind,
    OutreachState,
    SourceKind,
)
from sam.career.tracking import MIN_FOLLOW_UP_INTERVAL
from sam.permissions.models import PermissionAction, PermissionResource
from tests.career_support import OWNER, later, make_rig, must

QUOTE = (
    "Contact Dana Recruiter (Talent, Nimbus Robotics) at "
    "dana.recruiter@nimbus-robotics.example"
)


def outreach(
    rig: Any, channel: OutreachChannel = OutreachChannel.EMAIL, **kwargs: Any
) -> Any:
    job = rig.job()
    contact = rig.add_contact()
    result = rig.service.draft_outreach(
        OWNER,
        contact_id=contact.contact_id,
        kind=OutreachKind.RECRUITER,
        channel=channel,
        opportunity_id=job.opportunity_id,
        **kwargs,
    )
    assert result.ok and result.data is not None, result.reason
    return result.data


def send_fully(rig: Any, outreach_id: str) -> Any:
    first = rig.service.send_outreach(OWNER, outreach_id)
    assert first.permission == "confirm_required", first.reason
    career_conf = rig.approve(first.confirmation_id)
    second = rig.service.send_outreach(OWNER, outreach_id, career_conf)
    assert second.permission == "confirm_required", second.reason
    email_conf = rig.approve(second.confirmation_id)
    return rig.service.send_outreach(OWNER, outreach_id, career_conf, email_conf)


# ------------------------------------------------------------- contacts


def test_a_recruiter_email_is_never_guessed() -> None:
    rig = make_rig()
    contact = rig.add_contact(
        quote="Dana Recruiter leads talent acquisition at Nimbus Robotics.", email=None
    )
    assert contact.email is None  # not dana.recruiter@nimbus-robotics.example
    refused = rig.service.add_contact(
        OWNER,
        name="Dana Recruiter",
        role=ContactRole.RECRUITER,
        organization="Nimbus Robotics",
        source_kind=SourceKind.OFFICIAL_CAREER_PAGE,
        url="https://careers.nimbus-robotics.example/team",
        quote="Dana Recruiter leads talent acquisition at Nimbus Robotics.",
        email="dana.recruiter@nimbus-robotics.example",
    )
    assert not refused.ok and refused.reason == "email_not_in_evidence"


@pytest.mark.parametrize(
    ("name", "organization", "quote", "reason"),
    [
        ("Dana", "Nimbus Robotics", QUOTE, "full_name_required"),
        ("Dana Someone", "Nimbus Robotics", QUOTE, "name_not_in_evidence"),
        ("Dana Recruiter", "Other Corp", QUOTE, "organization_not_in_evidence"),
    ],
)
def test_identity_is_never_inferred_from_thin_evidence(
    name: str, organization: str, quote: str, reason: str
) -> None:
    rig = make_rig()
    result = rig.service.add_contact(
        OWNER,
        name=name,
        role=ContactRole.RECRUITER,
        organization=organization,
        source_kind=SourceKind.OFFICIAL_CAREER_PAGE,
        url="https://careers.nimbus-robotics.example/team",
        quote=quote,
    )
    assert not result.ok and result.reason == reason


def test_contact_provenance_is_preserved() -> None:
    rig = make_rig()
    contact = rig.add_contact()
    (evidence,) = contact.evidence
    assert evidence.url == "https://careers.nimbus-robotics.example/team"
    assert (
        evidence.quote == QUOTE
        and evidence.source_kind is SourceKind.OFFICIAL_CAREER_PAGE
    )
    assert contact.email == "dana.recruiter@nimbus-robotics.example"


# -------------------------------------------------------------- drafting


def test_drafting_an_email_or_linkedin_note_sends_nothing() -> None:
    rig = make_rig()
    email = outreach(rig)
    note = rig.service.draft_outreach(
        OWNER,
        contact_id=email.contact_id,
        kind=OutreachKind.RECRUITER,
        channel=OutreachChannel.LINKEDIN_CONNECTION_NOTE,
        opportunity_id=email.opportunity_id,
    ).data
    assert note is not None and len(note.body) <= 300
    assert email.state is OutreachState.READY_FOR_OWNER_REVIEW
    assert rig.email.sent == []


def test_linkedin_and_recruiter_messages_are_drafts_only() -> None:
    rig = make_rig()
    for channel in (
        OutreachChannel.LINKEDIN_CONNECTION_NOTE,
        OutreachChannel.LINKEDIN_MESSAGE,
        OutreachChannel.RECRUITER_MESSAGE,
    ):
        draft = outreach(rig, channel)
        rig.service.approve_outreach(OWNER, draft.outreach_id)
        result = rig.service.send_outreach(OWNER, draft.outreach_id, "x", "y")
        assert not result.ok and result.reason == "drafts_only_channel"
    assert rig.email.sent == []


def test_an_outreach_draft_with_an_unsupported_claim_cannot_be_approved() -> None:
    rig = make_rig()
    draft = outreach(rig, note="I have 20 years of Rust experience.")
    assert draft.unresolved and draft.state is OutreachState.DRAFT
    assert (
        rig.service.approve_outreach(OWNER, draft.outreach_id).reason
        == "unresolved_lines"
    )


# --------------------------------------------------------------- sending


def test_a_full_send_needs_career_send_and_the_email_tool_permission() -> None:
    rig = make_rig()
    draft = outreach(rig)
    assert rig.service.approve_outreach(OWNER, draft.outreach_id).ok
    done = send_fully(rig, draft.outreach_id)
    assert done.ok and done.data.state is OutreachState.SENT
    (message,) = rig.email.sent
    assert message.to == "dana.recruiter@nimbus-robotics.example"


def test_send_requires_career_send() -> None:
    rig = make_rig(grant_all=False)
    for action in (
        PermissionAction.READ,
        PermissionAction.CREATE,
        PermissionAction.UPDATE,
    ):
        rig.grant(PermissionResource.CAREER, action)
    draft = outreach(rig)
    rig.service.approve_outreach(OWNER, draft.outreach_id)
    result = rig.service.send_outreach(OWNER, draft.outreach_id)
    assert not result.ok and result.permission == "deny"
    assert rig.email.sent == []


def test_career_send_cannot_bypass_the_email_tool_permission() -> None:
    rig = make_rig(email_grant=False)
    draft = outreach(rig)
    rig.service.approve_outreach(OWNER, draft.outreach_id)
    first = rig.service.send_outreach(OWNER, draft.outreach_id)
    career_conf = rig.approve(first.confirmation_id)
    second = rig.service.send_outreach(OWNER, draft.outreach_id, career_conf)
    assert not second.ok and second.permission == "deny"
    assert rig.email.sent == []


def test_proactive_execute_can_never_send() -> None:
    rig = make_rig(grant_all=False)
    for action in (
        PermissionAction.READ,
        PermissionAction.CREATE,
        PermissionAction.UPDATE,
    ):
        rig.grant(PermissionResource.CAREER, action)
    rig.grant(PermissionResource.PROACTIVE, PermissionAction.EXECUTE, "proactive")
    draft = outreach(rig)
    rig.service.approve_outreach(OWNER, draft.outreach_id)
    assert not rig.service.send_outreach(OWNER, draft.outreach_id).ok
    assert rig.email.sent == []


def test_a_send_confirmation_is_bound_to_one_message() -> None:
    rig = make_rig()
    first_draft = outreach(rig)
    rig.service.approve_outreach(OWNER, first_draft.outreach_id)
    conf = rig.approve(
        rig.service.send_outreach(OWNER, first_draft.outreach_id).confirmation_id
    )
    other = rig.service.draft_outreach(
        OWNER,
        contact_id=first_draft.contact_id,
        kind=OutreachKind.HIRING_MANAGER,
        channel=OutreachChannel.EMAIL,
        opportunity_id=first_draft.opportunity_id,
        note="I admire your team's robotics work.",
    ).data
    assert other is not None
    rig.service.approve_outreach(OWNER, other.outreach_id)
    email_conf = rig.approve(
        rig.service.send_outreach(OWNER, other.outreach_id, conf).confirmation_id
    )
    result = rig.service.send_outreach(OWNER, other.outreach_id, conf, email_conf)
    assert not result.ok and result.reason == "confirmation_invalid"
    assert rig.email.sent == []


def test_without_an_email_tool_nothing_can_be_sent() -> None:
    rig = make_rig()
    rig.service._email = None
    draft = outreach(rig)
    rig.service.approve_outreach(OWNER, draft.outreach_id)
    assert (
        rig.service.send_outreach(OWNER, draft.outreach_id).reason
        == "email_unavailable"
    )


# ------------------------------------------------------------ follow-ups


def test_follow_up_interval_is_enforced_and_duplicates_suppressed() -> None:
    rig = make_rig()
    draft = outreach(rig)
    rig.service.approve_outreach(OWNER, draft.outreach_id)
    send_fully(rig, draft.outreach_id)
    (follow_up,) = rig.service.repository.list_follow_ups()
    too_early = rig.service.draft_follow_up(OWNER, follow_up.follow_up_id)
    assert not too_early.ok and too_early.reason == "follow_up_not_due"
    rig.clock.advance(MIN_FOLLOW_UP_INTERVAL)
    first = rig.service.draft_follow_up(OWNER, follow_up.follow_up_id)
    assert first.ok and first.data is not None
    second = rig.service.draft_follow_up(OWNER, follow_up.follow_up_id)
    assert second.data is not None and second.data.outreach_id == first.data.outreach_id
    follow_ups = [
        o
        for o in rig.service.repository.list_outreach()
        if o.kind is OutreachKind.FOLLOW_UP
    ]
    assert len(follow_ups) == 1
    assert rig.email.sent[1:] == []  # drafting a follow-up never sends


def test_follow_up_due_appears_in_the_review_queue() -> None:
    rig = make_rig()
    draft = outreach(rig)
    rig.service.approve_outreach(OWNER, draft.outreach_id)
    send_fully(rig, draft.outreach_id)
    later(rig, days=8)
    queue = must(rig.service.overview(OWNER).data).review_queue
    assert any(item.kind == "follow_up_due" for item in queue)


# --------------------------------------------------------------- privacy


def test_the_audit_holds_no_application_or_message_content() -> None:
    rig = make_rig()
    job = rig.job()
    draft = rig.service.create_draft(
        OWNER,
        job.opportunity_id,
        questions=["What are your salary expectations?", "Do you require sponsorship?"],
    ).data
    assert draft is not None
    rig.service.answer_question(OWNER, draft.draft_id, "q1", "GBP 91,234 minimum")
    rig.service.answer_question(OWNER, draft.draft_id, "q2", "Yes, Tier 2 visa")
    message = outreach(rig, note="I admire the robotics work at Nimbus.")
    rig.service.approve_outreach(OWNER, message.outreach_id)
    dumped = " ".join(repr(e) for e in rig.service.audit.events())
    dumped += " ".join(repr(e) for e in rig.pro.permission_audit.list_events())
    for private in (
        "91,234",
        "Tier 2",
        "Acme Analytics",
        "robotics work",
        "Dear",
        "Senior Software Engineer",
    ):
        assert private not in dumped


def test_career_never_imports_or_writes_memory_and_cannot_create_permissions() -> None:
    offenders = []
    for path in sorted(Path(career_package.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [node.module]
                    if isinstance(node, ast.ImportFrom) and node.module
                    else [a.name for a in getattr(node, "names", [])]
                )
                for name in names:
                    if name and name.split(".")[:2] in (
                        ["sam", "memory"],
                        ["sam", "professional"],
                        ["sam", "knowledge"],
                        ["sam", "proactive"],
                        ["sam", "mcp"],
                        ["sam", "computer"],
                    ):
                        offenders.append(f"{path.name}: {name}")
            if isinstance(node, ast.Attribute) and node.attr in (
                "create_grant",
                "revoke_grant",
                "decide",
            ):
                offenders.append(f"{path.name}: .{node.attr}")
    assert offenders == []


def test_running_career_writes_nothing_to_memory() -> None:
    from tests.desktop_support import Bridge

    b = Bridge()
    runtime = b.runtime
    result = runtime.career.import_listing(
        runtime.principal,
        make_rig(with_cv=False).listing(),
    )
    assert result.ok
    memory = b.post("/memory/search", {"text": "Nimbus"}).json()
    assert memory["items"] == [] and memory["working"] == []


# -------------------------------------------------------------- Proactive


def test_proactive_career_watches_only_read_and_notify() -> None:
    from tests.desktop_support import Bridge

    runtime = Bridge().runtime
    entries = {e.condition_id: e for e in runtime.proactive.policy.registry.entries()}
    career = {k: v for k, v in entries.items() if k.startswith("career_")}
    assert set(career) == {
        "career_review_queue",
        "career_deadlines",
        "career_follow_ups_due",
    }
    for entry in career.values():
        assert entry.permission.resource is PermissionResource.CAREER
        assert entry.permission.action is PermissionAction.READ
    # A proactive run may only ever request its own EXECUTE and READs, so no
    # CAREER SUBMIT or SEND can come from it.
    runner = runtime.proactive.runner
    from sam.permissions.models import PermissionScope

    for action in (
        PermissionAction.SUBMIT,
        PermissionAction.SEND,
        PermissionAction.DELETE,
        PermissionAction.CREATE,
    ):
        assert (
            runner.authorize(
                runtime.principal,
                PermissionResource.CAREER,
                action,
                PermissionScope.from_path("career"),
            )
            == "blocked"
        )
