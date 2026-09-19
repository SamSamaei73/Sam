"""Agent execution endpoint."""

import logging
from typing import cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from sam.agent.core import AgentCore
from sam.agent.models import AgentRequest, AgentResponse
from sam.api.models import ErrorResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])


def get_agent_core(request: Request) -> AgentCore:
    """Resolve the explicitly composed agent core for this application."""

    return cast(AgentCore, request.app.state.agent_core)


_agent_core_dependency = Depends(get_agent_core)


@router.post(
    "/agent",
    response_model=AgentResponse,
    responses={
        400: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
def execute_agent(
    payload: AgentRequest,
    agent_core: AgentCore = _agent_core_dependency,
) -> AgentResponse:
    """Execute one bounded request through the application agent core."""

    execution_id = uuid4().hex
    logger.info("Agent request received id=%s", execution_id)
    return agent_core.execute(payload, execution_id)