# -*- coding: utf-8 -*-
from __future__ import annotations

from super_planner.schemas.workflow import ConfirmationRequest


class WorkflowBusinessInterruption(Exception):
    """Raised when the workflow needs business input before it can continue."""

    def __init__(self, confirmation_request: ConfirmationRequest):
        super().__init__(confirmation_request.reason)
        self.confirmation_request = confirmation_request


class WorkflowSystemError(Exception):
    """Raised for system failures that should be handled by IT."""
