"""Follow-up policy: deterministic, bounded, no spam loops.

* A follow-up is DUE only once ``MIN_FOLLOW_UP_INTERVAL`` (7 days) has passed
  since the last real contact (a verified submission or a sent message).
* At most ``MAX_FOLLOW_UPS`` (2) follow-ups per opportunity and contact.
* Drafting a follow-up records ``last_draft_at``; asking again inside the same
  cooldown window returns the SAME draft instead of creating another
  (deduplication), and no new draft is possible until the interval passes again.
* Sam only drafts. Sending still needs CAREER SEND and the e-mail tool.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from sam.career.models import FollowUp

MIN_FOLLOW_UP_INTERVAL = timedelta(days=7)
MAX_FOLLOW_UPS = 2


class FollowUpState(StrEnum):
    NOT_DUE = "not_due"
    FOLLOW_UP_DUE = "follow_up_due"
    DRAFTED = "drafted"
    EXHAUSTED = "exhausted"


def schedule(
    follow_up_id: str, opportunity_id: str, contact_id: str | None, at: datetime
) -> FollowUp:
    return FollowUp(
        follow_up_id=follow_up_id,
        opportunity_id=opportunity_id,
        contact_id=contact_id,
        last_contact_at=at,
        due_at=at + MIN_FOLLOW_UP_INTERVAL,
    )


def state(follow_up: FollowUp, now: datetime) -> FollowUpState:
    if follow_up.count >= MAX_FOLLOW_UPS:
        return FollowUpState.EXHAUSTED
    if (
        follow_up.last_draft_at is not None
        and now - follow_up.last_draft_at < MIN_FOLLOW_UP_INTERVAL
    ):
        return FollowUpState.DRAFTED  # cooldown: one draft per window
    return (
        FollowUpState.FOLLOW_UP_DUE
        if now >= follow_up.due_at
        else FollowUpState.NOT_DUE
    )


def record_draft(follow_up: FollowUp, now: datetime) -> FollowUp:
    return follow_up.model_copy(update={"last_draft_at": now})


def record_sent(follow_up: FollowUp, now: datetime) -> FollowUp:
    return follow_up.model_copy(
        update={
            "count": follow_up.count + 1,
            "last_contact_at": now,
            "due_at": now + MIN_FOLLOW_UP_INTERVAL,
        }
    )


__all__ = [
    "MAX_FOLLOW_UPS",
    "MIN_FOLLOW_UP_INTERVAL",
    "FollowUpState",
    "record_draft",
    "record_sent",
    "schedule",
    "state",
]
