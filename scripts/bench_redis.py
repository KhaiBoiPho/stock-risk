"""
Benchmark Redis memory usage with realistic data for ~400 symbols.
Simulates 3-4 days of operation (all key types populated).

Usage:
    python scripts/bench_redis.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import get_settings
from src.data.redis_store import RedisStore

SYMBOLS = [
    "AAA","ACB","AGG","ANV","APC","APH","ASM","BCM","BID","BMI",
    "BMP","BSI","BVH","BWE","CII","CMG","CNG","CTD","CTG","CTP",
    "DCM","DGC","DGW","DIG","DPM","DRC","DXG","DXS","EIB","EVF",
    "FPT","FRT","GAS","GEX","GMD","GVR","HAG","HAH","HDC","HDG",
    "HDB","HHV","HMC","HPG","HT1","HTN","HVN","IDI","IMP","ITA",
    "KBC","KDC","KDH","KHG","KOS","KSB","LCG","LDG","LGC","LHG",
    "LPB","MBB","MCH","MHC","MSB","MSN","MWG","NAB","NBB","NKG",
    "NLG","NNT","NTL","NVL","OCB","OIL","PAC","PDR","PHR","PLX",
    "PNJ","POW","PPC","PTB","PVC","PVD","PVP","PVT","QCG","REE",
    "SAB","SAF","SAM","SBT","SCR","SCS","SHB","SJS","SKG","SRC",
    "SSB","SSI","STB","STG","SVC","SZC","TCB","TCH","TCM","TDC",
    "TDM","TDW","TIP","TLD","TLG","TMS","TNH","TPB","TRC","TSC",
    "TTA","TV2","TVB","TVS","TYA","VCB","VCG","VCI","VCR","VGC",
    "VGI","VHC","VHM","VIB","VIC","VID","VIP","VIX","VJC","VKC",
    "VMD","VNM","VOS","VPB","VPG","VPI","VPK","VRE","VSC","VSH",
    "VTP","VTR","YEG","ACG","AGR","ALP","AME","APG","APL","APX",
    "ASG","BAB","BAF","BCC","BCE","BCG","BFC","BHN","BIC","BKG",
    "BLD","BLI","BMC","BMS","BPC","BSH","BTP","BVB","C4G","CAB",
    "CAV","CC4","CCL","CCM","CEO","CII","CLC","CLG","CLH","CLL",
    "CMC","CMF","CMV","CMX","CNG","CNT","COM","CPC","CQN","CSC",
    "CSM","CSV","CT3","CTI","CTN","CTS","CVN","CVT","DAH","DAT",
    "DBD","DBT","DC4","DCG","DCL","DCR","DCS","DDD","DDG","DDL",
    "DDM","DDN","DFC","DGC","DGW","DHC","DHG","DHM","DHP","DHT",
    "DID","DIG","DL1","DLD","DLG","DLT","DMC","DNL","DNP","DPC",
    "DPG","DPM","DPP","DPS","DPT","DQC","DR2","DRC","DRG","DSE",
    "DSN","DTA","DTB","DTC","DTD","DTE","DTG","DTI","DTK","DTL",
    "DTT","DXG","DXL","DXP","DXS","DXV","E1VFVN30","EBS","ECI",
    "EID","EIN","ELC","ELG","EMG","EPH","ETC","EVE","EVF","EVG",
    "EVS","FBC","FDC","FIT","FLC","FPT","FRC","FRT","FSO","FTM",
    "GCB","GDT","GEG","GEX","GHC","GLT","GMC","GMD","GMH","GPC",
    "GVR","HAD","HAG","HAH","HAN","HAP","HAR","HAS","HAT","HBC",
    "HBS","HCD","HCH","HCM","HDC","HDG","HDB","HEV","HFB","HGM",
    "HHC","HHG","HHV","HID","HIG","HII","HJS","HLA","HLB","HLC",
    "HLG","HLY","HMC","HMS","HNA","HNC","HNF","HNM","HOT","HPG",
    "HPH","HPS","HPT","HPX","HQC","HRS","HSG","HT1","HTB","HTG",
]

# Deduplicate and take ~400
SYMBOLS = sorted(set(SYMBOLS))[:400]


async def main() -> None:
    s = get_settings()
    store = await RedisStore.create()

    info_before = await store._r.info("memory")
    keys_before = await store._r.dbsize()

    print(f"=== TRƯỚC KHI POPULATE ===")
    print(f"Keys: {keys_before}")
    print(f"RAM:  {info_before['used_memory_human']}")

    # --- Simulate end-of-day state (VMA9 + last_ratio) ---
    for sym in SYMBOLS:
        await store.set_vma9(sym, 1_234_567.89)
        await store.set_last_ratio(sym, 2.34)

    # --- Simulate intraday state (vol_today + some alerted keys) ---
    for sym in SYMBOLS:
        await store.set_vol_today(sym, 987_654)

    # 20% symbols have warning, 5% have critical, 2% have explosion
    warned  = SYMBOLS[:int(len(SYMBOLS) * 0.20)]
    critted = SYMBOLS[:int(len(SYMBOLS) * 0.05)]
    exploded= SYMBOLS[:int(len(SYMBOLS) * 0.02)]

    for sym in warned:
        await store.mark_alerted(sym, "warning", 900)
    for sym in critted:
        await store.mark_alerted(sym, "critical", 900)
    for sym in exploded:
        await store.mark_alerted(sym, "explosion", 900)

    info_after = await store._r.info("memory")
    keys_after = await store._r.dbsize()

    used_bytes = info_after["used_memory"]
    used_kb    = used_bytes / 1024
    used_mb    = used_bytes / 1024 / 1024

    print(f"\n=== SAU KHI POPULATE ({len(SYMBOLS)} mã) ===")
    print(f"Keys      : {keys_after} (tăng {keys_after - keys_before})")
    print(f"RAM used  : {info_after['used_memory_human']}  ({used_kb:.1f} KB / {used_mb:.2f} MB)")
    print(f"RAM peak  : {info_after['used_memory_peak_human']}")
    print(f"Overhead  : {info_after['mem_allocator']}")

    # Per-key breakdown estimate
    added_keys = keys_after - keys_before
    if added_keys > 0:
        bytes_per_key = (used_bytes - info_before["used_memory"]) / added_keys
        print(f"\n--- Ước tính per-key ---")
        print(f"Trung bình / key : {bytes_per_key:.0f} bytes")
        print(f"vma9 (400 keys)  : ~{bytes_per_key * 400 / 1024:.1f} KB")
        print(f"vol_today        : ~{bytes_per_key * 400 / 1024:.1f} KB")
        print(f"last_ratio       : ~{bytes_per_key * 400 / 1024:.1f} KB")
        print(f"alerted (peak)   : ~{bytes_per_key * 400 * 3 / 1024:.1f} KB  (nếu 100% triggered)")

    print(f"\n=== KẾT LUẬN ===")
    print(f"Peak RAM tối đa (100% mã alert): ~{(used_bytes + bytes_per_key * 1200) / 1024 / 1024:.2f} MB")

    # Cleanup
    await store.reset_daily()
    for sym in SYMBOLS:
        await store._r.delete(f"vma9:{sym}")
        await store._r.delete(f"last_ratio:{sym}")

    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
