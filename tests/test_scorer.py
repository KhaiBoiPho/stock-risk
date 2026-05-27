import pytest
from src.core.scorer import Score, compute_score


@pytest.mark.parametrize("ratio,pts", [(3.5, 2), (2.5, 1), (1.5, 0)])
def test_vol_pts(ratio, pts):
    s = compute_score(ratio, None, False, None)
    assert s.vol_pts == pts


@pytest.mark.parametrize("chg,pts", [(2.0, 1), (0.1, 1), (0.0, 0), (-1.0, 0)])
def test_direction_pts(chg, pts):
    s = compute_score(2.5, chg, False, None)
    assert s.direction_pts == pts


def test_breakout_pts():
    assert compute_score(2.5, 1.0, True,  None).breakout_pts == 2
    assert compute_score(2.5, 1.0, False, None).breakout_pts == 0


@pytest.mark.parametrize("rs,pts", [(2.0, 1), (1.5, 0), (1.6, 1), (-1.0, 0)])
def test_rs_pts(rs, pts):
    s = compute_score(2.5, 1.0, False, rs)
    assert s.rs_pts == pts


def test_buy_signal_max_score():
    s = compute_score(3.5, 2.0, True, 2.0)
    assert s.total == 6
    assert s.signal == "🎯 BUY SIGNAL"


def test_watch_signal():
    s = compute_score(2.5, 1.0, False, None)
    assert s.total == 2
    assert s.signal == "👀 WATCH"


def test_no_signal_low_score():
    s = compute_score(2.5, -1.0, False, None)
    assert s.total == 1
    assert s.signal is None


def test_breakdown_string():
    s = compute_score(3.5, 2.0, True, 2.0)
    assert "Vol+2" in s.breakdown()
    assert "Dir+1" in s.breakdown()
    assert "BO+2" in s.breakdown()
    assert "RS+1" in s.breakdown()


def test_vib_scenario():
    # VIB 27/05: ratio=3.73x, giá tăng, không rõ breakout, RS tốt
    s = compute_score(ratio=3.73, price_change_pct=2.1, is_breakout20=False, rs_vs_index=1.8)
    assert s.total == 4   # vol(2) + dir(1) + rs(1)
    assert s.signal == "🎯 BUY SIGNAL"


def test_tpb_scenario():
    # TPB 27/05: escalate từ warning lên critical
    s = compute_score(ratio=3.06, price_change_pct=1.5, is_breakout20=True, rs_vs_index=2.0)
    assert s.total == 6
    assert s.signal == "🎯 BUY SIGNAL"
