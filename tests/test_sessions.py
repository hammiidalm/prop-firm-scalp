"""Tests for the session filter."""

from datetime import UTC, datetime

from app.utils.sessions import Session, SessionFilter


class TestSessionFilter:
    def setup_method(self):
        self.sf = SessionFilter(
            london_open_utc=7,
            london_close_utc=11,
            ny_open_utc=12,
            ny_close_utc=16,
        )

    def test_london_session(self):
        ts = datetime(2024, 6, 10, 8, 30, tzinfo=UTC)
        assert self.sf.classify(ts) is Session.LONDON
        assert self.sf.is_active(ts)

    def test_ny_session(self):
        ts = datetime(2024, 6, 10, 13, 0, tzinfo=UTC)
        assert self.sf.classify(ts) is Session.NEW_YORK
        assert self.sf.is_active(ts)

    def test_off_session(self):
        ts = datetime(2024, 6, 10, 3, 0, tzinfo=UTC)
        assert self.sf.classify(ts) is Session.OFF
        assert not self.sf.is_active(ts)

    def test_after_ny_close_is_off(self):
        ts = datetime(2024, 6, 10, 17, 0, tzinfo=UTC)
        assert self.sf.classify(ts) is Session.OFF

    def test_overlap_when_configured(self):
        # Configure overlap (London 7-16, NY 12-16 -> overlap 12-16)
        sf = SessionFilter(
            london_open_utc=7,
            london_close_utc=16,
            ny_open_utc=12,
            ny_close_utc=16,
        )
        ts = datetime(2024, 6, 10, 13, 0, tzinfo=UTC)
        assert sf.classify(ts) is Session.OVERLAP
