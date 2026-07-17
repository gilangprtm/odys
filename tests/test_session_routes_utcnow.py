"""Regression: session routes must not call datetime.now(timezone.utc).replace(tzinfo=None) (#1116)."""

import inspect

import routes.session_routes as sr


def test_session_routes_module_does_not_reference_utcnow():
    source = inspect.getsource(sr)
    assert "datetime.now(timezone.utc).replace(tzinfo=None)" not in source
    assert "_dt.utcnow()" not in source