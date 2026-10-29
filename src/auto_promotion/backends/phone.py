"""Phone method boundary. All external promotion operations remain disabled."""

from .common import BackendError


class PhoneBackend:
    def call(self, account, operation, payload):
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
