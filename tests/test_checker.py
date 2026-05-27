import pytest
from src.core.checker import calculate_ratio, determine_alert_level, _parse_ohlcv, _price_change_pct
from src.alerts.formatter import AlertLevel


# --- _parse_ohlcv ---

def test_parse_ohlcv_sums_volumes():
    data = {"t": [1,2,3], "o": [49.0,50.0,51.0], "v": [100,200,300], "c": [50.0,51.0,52.0]}
    vol, open_p, close = _parse_ohlcv(data)
    assert vol == 600
    assert open_p == 49.0
    assert close == 52.0


def test_parse_ohlcv_none_data():
    vol, open_p, close = _parse_ohlcv(None)
    assert vol == 0
    assert open_p is None
    assert close is None


def test_parse_ohlcv_empty_candles():
    vol, open_p, close = _parse_ohlcv({"t": [], "o": [], "v": [], "c": []})
    assert vol == 0
    assert open_p is None
    assert close is None


# --- _price_change_pct ---

def test_price_change_positive():
    assert abs(_price_change_pct(10.0, 11.0) - 10.0) < 0.001

def test_price_change_negative():
    assert abs(_price_change_pct(10.0, 9.0) - (-10.0)) < 0.001

def test_price_change_none_when_no_open():
    assert _price_change_pct(None, 10.0) is None

def test_price_change_none_when_zero_open():
    assert _price_change_pct(0.0, 10.0) is None


# --- calculate_ratio ---

def test_ratio_at_midday():
    ratio = calculate_ratio(500_000, 1_000_000, 127)
    expected = 500_000 / (1_000_000 * 127 / 255)
    assert abs(ratio - expected) < 1e-6


def test_ratio_zero_elapsed():
    assert calculate_ratio(500_000, 1_000_000, 0) is None


def test_ratio_zero_vma9():
    assert calculate_ratio(500_000, 0, 127) is None


# --- determine_alert_level ---

ATO_THRESH  = 3.0
WARN_THRESH = 2.0
CRIT_THRESH = 3.0


@pytest.mark.parametrize("ratio,session,level", [
    (3.5,  "ATO",       AlertLevel.EXPLOSION),
    (2.5,  "ATO",       None),
    (3.0,  "MORNING",   AlertLevel.CRITICAL),
    (2.5,  "MORNING",   AlertLevel.WARNING),
    (1.9,  "MORNING",   None),
    (3.0,  "AFTERNOON", AlertLevel.CRITICAL),
    (2.0,  "ATC",       AlertLevel.WARNING),
])
def test_determine_alert_level(ratio, session, level):
    result = determine_alert_level(ratio, session, ATO_THRESH, WARN_THRESH, CRIT_THRESH)
    assert result == level


def test_warning_to_critical_escalation():
    assert determine_alert_level(2.5, "MORNING", ATO_THRESH, WARN_THRESH, CRIT_THRESH) == AlertLevel.WARNING
    assert determine_alert_level(3.5, "MORNING", ATO_THRESH, WARN_THRESH, CRIT_THRESH) == AlertLevel.CRITICAL
