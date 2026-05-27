from datetime import time
import pytest

from src.core.session import elapsed_minutes, get_session, is_trading


@pytest.mark.parametrize("t,expected", [
    (time(8, 0),   0),    # before market
    (time(9, 0),   0),    # ATO start
    (time(9, 7),   7),    # mid-ATO
    (time(9, 15),  15),   # MORNING start
    (time(10, 15), 75),   # mid-MORNING  (15 + 60)
    (time(11, 29), 149),  # end-MORNING
    (time(12, 0),  150),  # lunch break
    (time(13, 0),  150),  # AFTERNOON start
    (time(13, 45), 195),  # mid-AFTERNOON (150 + 45)
    (time(14, 30), 240),  # ATC start
    (time(14, 45), 255),  # ATC end
    (time(15, 0),  255),  # after market
])
def test_elapsed_minutes(t, expected):
    assert elapsed_minutes(t) == expected


@pytest.mark.parametrize("t,session_name", [
    (time(9,  0),  "ATO"),
    (time(9, 14),  "ATO"),
    (time(9, 15),  "MORNING"),
    (time(11, 29), "MORNING"),
    (time(13, 0),  "AFTERNOON"),
    (time(14, 29), "AFTERNOON"),
    (time(14, 30), "ATC"),
    (time(14, 45), "ATC"),
])
def test_get_session(t, session_name):
    assert get_session(t).name == session_name


@pytest.mark.parametrize("t", [time(8, 59), time(11, 30), time(12, 0), time(14, 46)])
def test_no_session_outside_trading(t):
    assert get_session(t) is None


@pytest.mark.parametrize("t,trading", [
    (time(9, 0),  True),
    (time(11, 30), False),   # lunch
    (time(12, 0),  False),
    (time(13, 0),  True),
    (time(14, 45), True),
    (time(14, 46), False),
])
def test_is_trading(t, trading):
    assert is_trading(t) == trading
