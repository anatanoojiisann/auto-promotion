"""Unconfigured phone methods deny writes; explicit services use AP's original queue."""

from .common import BackendError


class PhoneBackend:
    def __init__(self, *, service=None, execution_job=None):
        self.service, self.execution_job = service, execution_job

    def call(self, account, operation, payload):
        if self.service is not None:
            return self.service.backend(account, operation, payload, self.execution_job)
        if operation in {"health", "preflight"}:
            return {
                "healthy": False,
                "service_ready": False,
                "allowed": False,
                "identity_verified": False,
                "permissions": [],
                "reason": "PhoneFleetPreparationOnly",
                "confirmed_not_submitted": True,
            }
        if operation == "verify":
            return {
                "status": "unknown",
                "verified": False,
                "reason": "PhoneFleetRequiresBusinessEvidence",
            }
        raise BackendError("PhoneFleetExternalSubmissionDisabled")
