"""Unit tests for monitoring/dashboard.py's password gate.

Streamlit has no built-in auth and this dashboard sits on a public Railway
domain showing live portfolio value/positions, so check_password() must fail
closed when DASHBOARD_PASSWORD is unset, and only unlock once the entered
value matches. st.session_state is swapped for a plain dict (monkeypatch) so
tests don't need a real Streamlit ScriptRunContext.
"""

from __future__ import annotations

import pytest

from monitoring import dashboard


class TestPasswordMatches:
    def test_matching_password_returns_true(self):
        assert dashboard._password_matches("hunter2", "hunter2") is True

    def test_mismatched_password_returns_false(self):
        assert dashboard._password_matches("wrong", "hunter2") is False

    def test_empty_entered_never_matches_nonempty_expected(self):
        assert dashboard._password_matches("", "hunter2") is False


class TestCheckPassword:
    def test_refuses_when_dashboard_password_unset(self, monkeypatch):
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
        monkeypatch.setattr(dashboard.st, "session_state", {})
        monkeypatch.setattr(dashboard.st, "error", lambda *a, **k: None)

        def _stop():
            raise SystemExit("st.stop() called")

        monkeypatch.setattr(dashboard.st, "stop", _stop)

        with pytest.raises(SystemExit):
            dashboard.check_password()

    def test_returns_true_when_already_authenticated(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
        monkeypatch.setattr(dashboard.st, "session_state", {"password_correct": True})
        assert dashboard.check_password() is True

    def test_returns_false_and_prompts_when_not_yet_authenticated(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
        monkeypatch.setattr(dashboard.st, "session_state", {})
        monkeypatch.setattr(dashboard.st, "text_input", lambda *a, **k: None)
        assert dashboard.check_password() is False

    def test_returns_false_and_shows_error_after_wrong_attempt(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
        monkeypatch.setattr(dashboard.st, "session_state", {"password_correct": False})
        monkeypatch.setattr(dashboard.st, "text_input", lambda *a, **k: None)
        errors = []
        monkeypatch.setattr(dashboard.st, "error", lambda msg: errors.append(msg))
        assert dashboard.check_password() is False
        assert errors == ["Incorrect password"]

    def test_on_submit_callback_sets_session_state_on_correct_password(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
        session_state = {"password_input": "hunter2"}
        monkeypatch.setattr(dashboard.st, "session_state", session_state)

        captured = {}

        def _fake_text_input(*args, **kwargs):
            captured["on_change"] = kwargs["on_change"]

        monkeypatch.setattr(dashboard.st, "text_input", _fake_text_input)

        dashboard.check_password()
        captured["on_change"]()

        assert session_state["password_correct"] is True
        assert "password_input" not in session_state

    def test_on_submit_callback_rejects_wrong_password(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
        session_state = {"password_input": "nope"}
        monkeypatch.setattr(dashboard.st, "session_state", session_state)

        captured = {}

        def _fake_text_input(*args, **kwargs):
            captured["on_change"] = kwargs["on_change"]

        monkeypatch.setattr(dashboard.st, "text_input", _fake_text_input)

        dashboard.check_password()
        captured["on_change"]()

        assert session_state["password_correct"] is False
