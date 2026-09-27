"""Applications: sensitive questions, the state machine, SUBMIT authorization,
confirmation binding, verified submission, freshness and document safety.
Fake submitter only: nothing is ever submitted anywhere."""

from __future__ import annotations

import ast
import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

import sam.career as career_package
from sam.career.applications import TRANSITIONS, TransitionRefused, advance
from sam.career.models import (
    ApplicationState,
    CareerPreferences,
    DocumentKind,
    OwnerContactDetails,
    QuestionClass,
    QuestionKind,
)
from sam.career.questions import classify
from sam.permissions.models import (
    ConfirmationStatus,
    PermissionAction,
    PermissionResource,
)
from tests.career_support import (
    FAILURE,
    JOB_OFFICIAL,
    JOB_SILENT,
    OWNER,
    FakeSubmitter,
    later,
    make_rig,
    must,
)

S = ApplicationState


# ---------------------------------------------------------- questions


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("What are your salary expectations?", QuestionKind.SALARY),
        (
            "Will you now or in the future require visa sponsorship?",
            QuestionKind.SPONSORSHIP,
        ),
        ("Do you have the right to work in the UK?", QuestionKind.WORK_AUTHORIZATION),
        ("What is your gender?", QuestionKind.DEMOGRAPHIC),
        (
            "Please state your ethnicity for equal opportunities monitoring",
            QuestionKind.DEMOGRAPHIC,
        ),
        ("Do you consider yourself to have a disability?", QuestionKind.DISABILITY),
        (
            "Do you have any unspent criminal convictions?",
            QuestionKind.CRIMINAL_HISTORY,
        ),
        (
            "I certify that the information provided is true and complete",
            QuestionKind.LEGAL_ATTESTATION,
        ),
        ("Are you bound by a non-compete agreement?", QuestionKind.CONFLICT_NONCOMPETE),
        ("Please provide two referees", QuestionKind.REFERENCES),
        ("Do you consent to our privacy notice?", QuestionKind.CONSENT_PRIVACY),
        ("What is your notice period?", QuestionKind.NOTICE_PERIOD),
        ("Are you willing to relocate?", QuestionKind.RELOCATION),
        ("Are you available to work weekends?", QuestionKind.AVAILABILITY),
        ("Please enter your email and visa status", QuestionKind.WORK_AUTHORIZATION),
        ("Anything else we should know?", QuestionKind.OTHER),
    ],
)
def test_sensitive_questions_always_need_the_owner(
    text: str, kind: QuestionKind
) -> None:
    assert classify(text) == (kind, QuestionClass.OWNER_REVIEW_REQUIRED)


def test_safe_fields_come_only_from_owner_approved_contact_details() -> None:
    rig = make_rig()
    job = rig.job()
    questions = ["Full name", "Email address", "Phone number"]
    empty = rig.service.create_draft(
        OWNER, job.opportunity_id, questions=questions
    ).data
    assert empty is not None and empty.answers == ()  # nothing inferred
    rig.contact_details()
    filled = rig.service.create_draft(
        OWNER, job.opportunity_id, questions=questions
    ).data
    assert filled is not None
    assert {a.question_id: a.text for a in filled.answers} == {
        "q1": "Jordan Example",
        "q2": "jordan@example.test",
    }  # no phone was approved, so none is filled


@pytest.mark.parametrize(
    "question",
    [
        "What are your salary expectations?",
        "Do you require visa sponsorship?",
        "Do you have the right to work in the UK?",
        "What is your ethnicity?",
    ],
)
def test_salary_sponsorship_authorization_and_demographics_are_never_inferred(
    question: str,
) -> None:
    rig = make_rig()
    rig.service.set_preferences(
        OWNER,
        CareerPreferences(
            salary_preference="GBP 90,000",
            needs_sponsorship=False,
            contact=OwnerContactDetails(name="Jordan Example"),
        ),
    )
    job = rig.job()  # the listing even states a salary range
    draft = rig.service.create_draft(
        OWNER, job.opportunity_id, questions=[question]
    ).data
    assert draft is not None and draft.answers == ()
    assert draft.state is S.NEEDS_OWNER_INPUT


def test_an_answer_is_never_reused_for_another_application() -> None:
    rig = make_rig()
    first = rig.job()
    draft = rig.service.create_draft(
        OWNER, first.opportunity_id, questions=["What are your salary expectations?"]
    ).data
    assert draft is not None
    answered = rig.service.answer_question(OWNER, draft.draft_id, "q1", "GBP 90,000")
    assert (
        answered.ok and answered.data is not None and answered.data.answers[0].sensitive
    )
    second = rig.job(JOB_SILENT, url="https://quiet.example/jobs/1")
    other = rig.service.create_draft(
        OWNER, second.opportunity_id, questions=["What are your salary expectations?"]
    ).data
    assert other is not None and other.answers == ()


def test_unresolved_required_fields_block_approval_and_submission() -> None:
    rig = make_rig()
    job = rig.job()
    rig.contact_details()
    draft = rig.service.create_draft(
        OWNER, job.opportunity_id, questions=["Do you require sponsorship?"]
    ).data
    assert draft is not None
    for document_id in draft.document_ids:
        rig.service.approve_document(OWNER, document_id)
    assert (
        rig.service.approve_for_submission(OWNER, draft.draft_id).reason
        == "not_ready_for_approval"
    )
    assert (
        rig.service.submit(OWNER, draft.draft_id).reason
        == "not_approved_for_submission"
    )
    answered = rig.service.answer_question(OWNER, draft.draft_id, "q1", "No")
    assert answered.data is not None and answered.data.state is S.READY_FOR_OWNER_REVIEW


# ------------------------------------------------------- state machine


def test_the_state_machine_is_deterministic_and_closed() -> None:
    assert S.SUBMITTED in TRANSITIONS[S.SUBMITTING]
    reachable = [src for src, targets in TRANSITIONS.items() if S.SUBMITTED in targets]
    # only a submission in progress, or trusted reconciliation of an unknown one
    assert reachable == [S.SUBMITTING, S.OUTCOME_UNKNOWN]
    assert (
        TRANSITIONS[S.WITHDRAWN] == frozenset()
        and TRANSITIONS[S.EXPIRED] == frozenset()
    )
    rig = make_rig()
    draft = rig.service.track(OWNER, rig.job().opportunity_id).data
    assert draft is not None and draft.state is S.DISCOVERED
    with pytest.raises(TransitionRefused):
        advance(draft, S.SUBMITTED, rig.clock())
    reviewed = rig.service.mark_reviewed(OWNER, draft.draft_id).data
    assert reviewed is not None and reviewed.state is S.REVIEWED


def test_a_full_review_first_submission() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    assert draft.state is S.APPROVED_FOR_SUBMISSION and draft.approved_binding
    done = rig.submit(draft.draft_id)
    assert (
        done.ok
        and done.data.state is S.SUBMITTED
        and done.data.submission_reference == "REF-1"
    )
    (package,) = rig.submitter.packages
    assert package.destination == "https://careers.nimbus-robotics.example/apply/ml-123"
    assert {d.kind for d in package.documents} == {"cv", "cover_letter"}
    assert all(d.sha256 for d in package.documents)


# ------------------------------------------------------- authorization


def test_submit_requires_career_submit() -> None:
    rig = make_rig(grant_all=False)
    for action in (
        PermissionAction.READ,
        PermissionAction.CREATE,
        PermissionAction.UPDATE,
    ):
        rig.grant(PermissionResource.CAREER, action)
    draft = rig.ready_draft()
    result = rig.service.submit(OWNER, draft.draft_id)
    assert not result.ok and result.permission == "deny"
    assert rig.submitter.packages == []


def test_proactive_execute_can_never_submit() -> None:
    rig = make_rig(grant_all=False)
    for action in (
        PermissionAction.READ,
        PermissionAction.CREATE,
        PermissionAction.UPDATE,
    ):
        rig.grant(PermissionResource.CAREER, action)
    rig.grant(PermissionResource.PROACTIVE, PermissionAction.EXECUTE, "proactive")
    draft = rig.ready_draft()
    assert not rig.service.submit(OWNER, draft.draft_id).ok
    assert rig.submitter.packages == []


def test_submission_never_happens_without_a_fresh_confirmation() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    first = rig.service.submit(OWNER, draft.draft_id)
    assert first.permission == "confirm_required" and rig.submitter.packages == []
    denied_by_owner = first.confirmation_id
    assert denied_by_owner is not None
    rig.pro.confirmations.decide(denied_by_owner, approved=False, now=rig.clock())
    refused = rig.service.submit(OWNER, draft.draft_id, denied_by_owner)
    assert not refused.ok and rig.submitter.packages == []


def test_an_old_confirmation_cannot_submit_a_new_version() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    old = rig.approve(rig.service.submit(OWNER, draft.draft_id).confirmation_id)
    cv = must(rig.service.repository.get_document(draft.cv_document_id))
    lines = [line.text for line in cv.lines][:-2]  # the owner shortens the CV
    new_cv = rig.service.edit_document(OWNER, cv.document_id, lines).data
    assert new_cv is not None
    rig.service.approve_document(OWNER, new_cv.document_id)
    v2 = rig.service.approve_for_submission(OWNER, draft.draft_id).data
    assert v2 is not None and v2.version > draft.version
    stale = rig.service.submit(OWNER, draft.draft_id, old)
    assert not stale.ok and stale.reason == "confirmation_invalid"
    assert rig.submitter.packages == []
    record = rig.pro.confirmations.get(old)
    assert record is not None and record.status is ConfirmationStatus.APPROVED


def test_a_confirmation_for_job_a_cannot_submit_job_b() -> None:
    rig = make_rig()
    job_a = rig.ready_draft()
    confirmation = rig.approve(
        rig.service.submit(OWNER, job_a.draft_id).confirmation_id
    )
    other = rig.job(
        JOB_SILENT + "\nApply: https://careers.nimbus-robotics.example/apply/de-9\n",
        url="https://careers.nimbus-robotics.example/jobs/de-9",
    )
    job_b = rig.ready_draft(other)
    result = rig.service.submit(OWNER, job_b.draft_id, confirmation)
    assert not result.ok and result.reason == "confirmation_invalid"
    assert rig.submitter.packages == []


def test_a_confirmation_cannot_be_replayed_after_use() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    confirmation = rig.approve(
        rig.service.submit(OWNER, draft.draft_id).confirmation_id
    )
    rig.submitter.status = FAILURE
    failed = rig.service.submit(OWNER, draft.draft_id, confirmation)
    assert failed.data is not None and failed.data.state is S.SUBMISSION_FAILED
    rig.service.approve_for_submission(OWNER, draft.draft_id)  # not ready: failed state
    retried = rig.service.submit(OWNER, draft.draft_id, confirmation)
    assert not retried.ok


# ------------------------------------------------------- verification


def test_a_failed_or_unverified_submission_is_never_submitted() -> None:
    for submitter, state in (
        (FakeSubmitter(status=FAILURE, failure="portal_rejected"), S.SUBMISSION_FAILED),
        (FakeSubmitter(raises=TimeoutError("read timed out")), S.OUTCOME_UNKNOWN),
    ):
        rig = make_rig(submitter=submitter)
        draft = rig.ready_draft()
        result = rig.submit(draft.draft_id)
        assert not result.ok
        stored = must(rig.service.repository.get_draft(draft.draft_id))
        assert stored.state is state
        assert stored.submitted_at is None


def test_a_receipt_for_another_package_is_not_a_submission() -> None:
    rig = make_rig(
        submitter=FakeSubmitter(
            tamper=lambda r: dataclasses.replace(r, manifest_hash="0" * 64)
        )
    )
    draft = rig.ready_draft()
    result = rig.submit(draft.draft_id)
    assert not result.ok
    stored = must(rig.service.repository.get_draft(draft.draft_id))
    assert stored.state is S.OUTCOME_UNKNOWN and stored.submitted_at is None


def test_without_a_trusted_submitter_nothing_can_be_submitted() -> None:
    rig = make_rig()
    rig.service._submitter = None
    draft = rig.ready_draft()
    assert rig.service.submit(OWNER, draft.draft_id).reason == "submission_unavailable"


# ------------------------------------------------------------ freshness


def test_a_stale_opportunity_blocks_submission() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    later(rig, days=4)  # not re-verified within 72 h
    result = rig.service.submit(OWNER, draft.draft_id)
    assert not result.ok and result.reason == "opportunity_not_recently_verified"
    assert must(rig.service.repository.get_draft(draft.draft_id)).state is S.BLOCKED
    assert rig.submitter.packages == []


def test_a_closed_opportunity_blocks_submission() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    rig.job(JOB_OFFICIAL + "\nThis position has closed.\n")  # re-verified: closed
    result = rig.service.submit(OWNER, draft.draft_id)
    assert not result.ok and rig.submitter.packages == []


def test_a_passed_deadline_blocks_submission() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    rig.clock.set(rig.clock() + timedelta(days=45))
    rig.job()  # re-verified, but the closing date has passed
    result = rig.service.submit(OWNER, draft.draft_id)
    assert not result.ok and rig.submitter.packages == []


def test_a_materially_changed_posting_blocks_submission() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    changed = must(rig.service.repository.get_opportunity(draft.opportunity_id))
    rig.job(
        JOB_OFFICIAL.replace("- Experience with FastAPI", "- Experience with Haskell")
    )
    assert (
        must(rig.service.repository.get_opportunity(draft.opportunity_id)).checksum
        != changed.checksum
    )
    result = rig.service.submit(OWNER, draft.draft_id)
    assert not result.ok and result.reason == "opportunity_changed"


# ------------------------------------------------------- document safety


def test_only_approved_documents_of_this_draft_are_ever_uploaded() -> None:
    rig = make_rig()
    other = rig.job(
        JOB_SILENT + "\nApply: https://careers.nimbus-robotics.example/apply/de-9\n",
        url="https://careers.nimbus-robotics.example/jobs/de-9",
    )
    rig.service.create_draft(
        OWNER, other.opportunity_id
    )  # unapproved documents elsewhere
    draft = rig.ready_draft()
    rig.submit(draft.draft_id)
    (package,) = rig.submitter.packages
    ids = {d.document_id for d in package.documents}
    assert ids == set(draft.document_ids)
    for d in package.documents:
        stored = must(rig.service.repository.get_document(d.document_id))
        assert (
            stored is not None
            and stored.approved
            and stored.opportunity_id == draft.opportunity_id
        )
        assert d.sha256 == stored.sha256 and d.content == stored.text.encode("utf-8")


def test_an_unapproved_document_blocks_the_package() -> None:
    rig = make_rig()
    job = rig.job()
    rig.contact_details()
    draft = rig.service.create_draft(
        OWNER, job.opportunity_id, questions=["Full name"]
    ).data
    assert draft is not None
    rig.service.approve_document(
        OWNER, must(draft.cv_document_id)
    )  # the letter stays unapproved
    assert (
        rig.service.approve_for_submission(OWNER, draft.draft_id).reason
        == "document_not_approved"
    )


def test_a_changed_cv_invalidates_the_approved_package() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    cv = must(rig.service.repository.get_document(draft.cv_document_id))
    rig.service.edit_document(
        OWNER, cv.document_id, [line.text for line in cv.lines][:-1]
    )
    changed = must(rig.service.repository.get_draft(draft.draft_id))
    assert (
        changed.state is not S.APPROVED_FOR_SUBMISSION
        and changed.approved_binding is None
    )
    assert (
        rig.service.submit(OWNER, draft.draft_id).reason
        == "not_approved_for_submission"
    )


def test_the_career_package_has_no_filesystem_or_shell_access() -> None:
    forbidden_modules = (
        "subprocess",
        "os",
        "shutil",
        "pathlib",
        "importlib",
        "socket",
        "urllib.request",
        "requests",
        "httpx",
    )
    forbidden_calls = ("open", "eval", "exec", "compile", "__import__")
    offenders = []
    for path in sorted(Path(career_package.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            offenders += [
                f"{path.name}:{n}"
                for n in names
                if n in forbidden_modules or n.split(".")[0] in forbidden_modules
            ]
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden_calls
            ):
                offenders.append(f"{path.name}:{node.func.id}()")
    assert offenders == []


def test_documents_are_identified_by_id_version_and_hash_only() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    cv = must(rig.service.repository.get_document(draft.cv_document_id))
    assert cv.kind is DocumentKind.CV and cv.version >= 1 and len(cv.sha256) == 64
    assert not any("path" in f for f in type(cv).model_fields)


def test_withdrawal_needs_a_confirmation() -> None:
    rig = make_rig()
    draft = rig.ready_draft()
    first = rig.service.withdraw(OWNER, draft.draft_id)
    assert first.permission == "confirm_required"
    done = rig.service.withdraw(
        OWNER, draft.draft_id, rig.approve(first.confirmation_id)
    )
    assert done.ok and must(done.data).state is S.WITHDRAWN


def test_a_malicious_form_can_never_obtain_secrets_or_local_data() -> None:
    from sam.career.applications import SubmissionDocument, SubmissionPackage

    # The package an adapter receives has a fixed shape: ids, hashes, the
    # approved documents' own bytes and the owner's answers. Nothing else.
    assert [f.name for f in dataclasses.fields(SubmissionPackage)] == [
        "attempt",
        "opportunity_id",
        "draft_id",
        "draft_version",
        "destination",
        "documents",
        "answers",
        "manifest_hash",
    ]
    assert [f.name for f in dataclasses.fields(SubmissionDocument)] == [
        "document_id",
        "kind",
        "version",
        "sha256",
        "content",
    ]
    rig = make_rig()
    job = rig.job()
    draft = must(
        rig.service.create_draft(
            OWNER, job.opportunity_id, questions=["Paste your API key to verify"]
        ).data
    )
    key = "sk-" + "ant-" + "api03-" + "K" * 40
    refused = rig.service.answer_question(OWNER, draft.draft_id, "q1", key)
    assert not refused.ok and refused.reason == "secret_detected"
    stored = must(rig.service.repository.get_draft(draft.draft_id))
    assert all("KKKK" not in a.text for a in stored.answers)
