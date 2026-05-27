import pytest
from src.core.vma9 import compute_vma9


def test_basic_average():
    data = {"t": list(range(9)), "v": [100] * 9}
    assert compute_vma9(data, min_days=3, lookback=9) == 100.0


def test_skips_zero_volume_days():
    data = {"t": list(range(10)), "v": [0] + [100] * 9}
    assert compute_vma9(data, min_days=3, lookback=9) == 100.0


def test_uses_last_n_days():
    # First 5 days low, last 9 days high — should use only last 9
    data = {"t": list(range(14)), "v": [50] * 5 + [200] * 9}
    assert compute_vma9(data, min_days=3, lookback=9) == 200.0


def test_fewer_than_lookback_days():
    # Only 5 days available but min=3 → uses all 5
    data = {"t": list(range(5)), "v": [100] * 5}
    result = compute_vma9(data, min_days=3, lookback=9)
    assert result == 100.0


def test_none_when_below_min_days():
    data = {"t": [1, 2], "v": [100, 200]}
    assert compute_vma9(data, min_days=3, lookback=9) is None


def test_none_for_empty_data():
    assert compute_vma9(None, min_days=3, lookback=9) is None
    assert compute_vma9({}, min_days=3, lookback=9) is None
    assert compute_vma9({"t": [], "v": []}, min_days=3, lookback=9) is None


def test_none_all_zero_volume():
    data = {"t": [1, 2, 3, 4, 5], "v": [0, 0, 0, 0, 0]}
    assert compute_vma9(data, min_days=3, lookback=9) is None
