"""Unit tests for API error envelope shape — AC7.

Verify the standardised error JSON is returned for validation,
unauthenticated, forbidden, conflict, and not-found paths.
"""


from ting_ting.errors import error_response
from fastapi import status


class TestErrorResponse:
    def _payload(self, code, message, http_status, details=None):
        resp = error_response(code, message, http_status, details)
        return resp.status_code, resp.body

    def test_validation_envelope(self):
        code, body = self._payload("validation", "bad input", status.HTTP_422_UNPROCESSABLE_CONTENT)
        assert code == 422
        body_str = body.decode()
        assert '"error"' in body_str
        assert '"code"' in body_str
        assert "validation" in body_str

    def test_unauthenticated_envelope(self):
        code, body = self._payload(
            "unauthenticated", "no token", status.HTTP_401_UNAUTHORIZED
        )
        assert code == 401
        assert b"unauthenticated" in body

    def test_forbidden_envelope(self):
        code, body = self._payload("forbidden", "no access", status.HTTP_403_FORBIDDEN)
        assert code == 403
        assert b"forbidden" in body

    def test_conflict_envelope(self):
        code, body = self._payload("conflict", "duplicate", status.HTTP_409_CONFLICT)
        assert code == 409
        assert b"conflict" in body

    def test_not_found_envelope(self):
        code, body = self._payload("not_found", "missing", status.HTTP_404_NOT_FOUND)
        assert code == 404
        assert b"not_found" in body

    def test_details_optional(self):
        code, body = self._payload(
            "validation", "bad", status.HTTP_422_UNPROCESSABLE_CONTENT,
            details={"field": "extra info"},
        )
        assert b"extra info" in body
