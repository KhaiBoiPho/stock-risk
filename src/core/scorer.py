from dataclasses import dataclass


@dataclass
class Score:
    total: int
    vol_pts: int        # 0-2
    direction_pts: int  # 0-1  giá xanh so với mở cửa
    breakout_pts: int   # 0-2  phá đỉnh 20 phiên
    rs_pts: int         # 0-1  mạnh hơn VN-Index

    @property
    def signal(self) -> str | None:
        if self.total >= 4:
            return "🎯 BUY SIGNAL"
        if self.total >= 2:
            return "👀 WATCH"
        return None

    def breakdown(self) -> str:
        parts = []
        if self.vol_pts:    parts.append(f"Vol+{self.vol_pts}")
        if self.direction_pts: parts.append("Dir+1")
        if self.breakout_pts:  parts.append(f"BO+{self.breakout_pts}")
        if self.rs_pts:     parts.append("RS+1")
        return " | ".join(parts) or "—"


def compute_score(
    ratio: float,
    price_change_pct: float | None,
    is_breakout20: bool,
    rs_vs_index: float | None,
) -> Score:
    vol_pts       = 2 if ratio >= 3.0 else (1 if ratio >= 2.0 else 0)
    direction_pts = 1 if (price_change_pct is not None and price_change_pct > 0) else 0
    breakout_pts  = 2 if is_breakout20 else 0
    rs_pts        = 1 if (rs_vs_index is not None and rs_vs_index > 1.5) else 0

    return Score(
        total=vol_pts + direction_pts + breakout_pts + rs_pts,
        vol_pts=vol_pts,
        direction_pts=direction_pts,
        breakout_pts=breakout_pts,
        rs_pts=rs_pts,
    )
