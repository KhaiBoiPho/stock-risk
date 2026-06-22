# Volume Spike Bot — Tài liệu hệ thống

## Mục lục
1. [Tổng quan](#1-tổng-quan)
2. [Kiến trúc](#2-kiến-trúc)
3. [Thuật toán chi tiết](#3-thuật-toán-chi-tiết)
4. [Lịch chạy (Scheduler)](#4-lịch-chạy-scheduler)
5. [Redis — các key quan trọng](#5-redis--các-key-quan-trọng)
6. [Cấu hình (.env)](#6-cấu-hình-env)
7. [Luồng dữ liệu từ đầu đến cuối](#7-luồng-dữ-liệu-từ-đầu-đến-cuối)
8. [Điều kiện để alert được gửi](#8-điều-kiện-để-alert-được-gửi)
9. [Các lý do phổ biến khiến alert không gửi](#9-các-lý-do-phổ-biến-khiến-alert-không-gửi)
10. [Cách debug một mã cụ thể (ví dụ: TPB)](#10-cách-debug-một-mã-cụ-thể)
11. [Cấu trúc file](#11-cấu-trúc-file)

---

## 1. Tổng quan

Bot quét **toàn bộ cổ phiếu HoSE + HNX + UPCoM** mỗi 15 phút trong giờ giao dịch. Mỗi lần quét, bot so sánh **khối lượng thực tế trong phiên** với **khối lượng kỳ vọng theo tốc độ bình thường** (dựa trên VMA9). Nếu khối lượng vượt ngưỡng → gửi cảnh báo Telegram.

**Nguồn dữ liệu:**
- Giá / khối lượng OHLCV: DNSE API (`services.entrade.com.vn`)
- Danh sách mã HoSE: CafeF → fallback VPS → fallback file cache
- Danh sách mã HNX / UPCoM: VPS → fallback file cache

---

## 2. Kiến trúc

```
main.py
├── Startup: load symbols (HoSE/HNX/UPCoM), kết nối Redis, bootstrap VMA9
├── Scheduler (APScheduler)
│   ├── 09:00 daily  → reset_daily()       [xoá vol_today:*, alerted:*, alerted_today]
│   ├── mỗi 15 phút  → check_volume()      [lõi chính]
│   ├── 14:46 daily  → eod_summary()       [tổng kết cuối phiên → rổ]
│   └── 14:50 daily  → update_vma9()       [cập nhật VMA9 sau phiên]
└── Signal handler (SIGINT/SIGTERM)

src/
├── core/
│   ├── checker.py      — logic kiểm tra ratio, quyết định alert level
│   ├── eod_summary.py  — tổng kết cuối phiên: scan toàn bộ mã → lọc spike + thanh khoản → rổ
│   ├── session.py      — xác định phiên giao dịch, tính elapsed_minutes
│   └── vma9.py         — tính và lưu VMA9 vào Redis
├── data/
│   ├── dnse_client.py  — fetch OHLCV và danh sách mã từ API
│   └── redis_store.py  — đọc/ghi Redis (VMA9, vol_today, alerted, eod_basket)
├── alerts/
│   ├── formatter.py — format tin nhắn Telegram (HTML)
│   └── telegram.py  — gửi tin nhắn qua Telegram Bot API
├── jobs/
│   ├── check.py       — wrapper gọi run_check()
│   ├── eod_summary.py — wrapper gọi run_eod_summary()
│   ├── vma9_update.py — wrapper gọi update_vma9_all()
│   └── reset.py       — xoá Redis keys theo ngày, gửi thông báo phiên mới
└── config/
    ├── settings.py  — đọc .env qua pydantic-settings
    └── constants.py — SESSIONS, TOTAL_TRADING_MINUTES, URLs
```

---

## 3. Thuật toán chi tiết

### 3.1 VMA9 (Volume Moving Average 9 phiên)

**Khi nào tính:** Startup + 14:50 mỗi ngày.

```
from_date = today - 25 ngày
to_date   = hôm qua (KHÔNG tính hôm nay)

VMA9(symbol) = trung bình cộng khối lượng 9 phiên gần nhất
             (bỏ qua ngày có volume = 0)

Yêu cầu tối thiểu: ít nhất 3 ngày có dữ liệu hợp lệ (vma9_min_days=3)
→ Nếu không đủ: VMA9 = None → symbol bị bỏ qua hoàn toàn
```

**Redis key:** `vma9:{symbol}` — **không có TTL**, tồn tại vĩnh viễn cho đến khi Redis restart.

### 3.2 Elapsed Minutes (số phút giao dịch đã trôi qua)

Tổng phiên = **255 phút** (không tính nghỉ trưa).

| Thời gian | Phút tích lũy |
|-----------|--------------|
| 09:00     | 0            |
| 09:15     | 15           |
| 11:30     | 150          |
| 11:30–13:00 | đóng băng tại 150 (nghỉ trưa) |
| 13:00     | 150          |
| 14:30     | 240          |
| 14:45     | 255          |

### 3.3 Liquidity Filter (lọc thanh khoản)

Trước khi tính ratio, bot kiểm tra:

```
PASS nếu: VMA9 >= 1,000,000 cp/phiên
       HOẶC VMA9 × giá >= 10,000,000 (đơn vị: nghìn đồng × cp = tỷ đồng thực tế)

→ 10,000,000 nghìn đồng × cp ≈ 10 tỷ đồng/phiên

⚠️ DNSE API trả giá theo đơn vị nghìn đồng (ví dụ: FPT = 74.8 = 74,800đ)
```

### 3.4 Tính Ratio

```
expected_vol = VMA9 × elapsed_minutes / 255

ratio = vol_today / expected_vol
```

`vol_today` = tổng khối lượng tất cả candle 15 phút từ 09:00 đến thời điểm kiểm tra.

### 3.5 Phân loại Alert Level

| Phiên  | Ngưỡng         | Level      |
|--------|----------------|------------|
| ATO    | ratio >= 3.0x  | EXPLOSION  |
| MORNING / AFTERNOON | ratio >= 3.0x | CRITICAL |
| MORNING / AFTERNOON | ratio >= 2.0x | WARNING  |
| ATC    | ratio >= 3.0x  | EXPLOSION  |

> **Lưu ý:** ATO (09:00–09:15) **thực tế không bao giờ chạy** vì cron chỉ fire lúc :00/:15/:30/:45 và check lúc 09:00 có elapsed=0 (bị bỏ qua), check lúc 09:15 đã vào phiên MORNING. Đây là giới hạn thiết kế hiện tại.

### 3.6 Tổng kết cuối phiên (EOD Summary)

**Khi nào chạy:** 14:46 mỗi ngày (ngay sau ATC kết thúc, trước VMA9 update).

**Luồng xử lý:**

```
1. Fetch OHLCV cuối ngày cho TOÀN BỘ mã (1,500+ symbols)
2. Lấy VMA9 từ Redis
3. Với mỗi mã:
   a. Tính ratio = vol_today / VMA9
   b. ratio < 2.0x → SKIP (không có spike)
   c. Check EOD filter:
      vol_today >= 1,000,000 cp → PASS
      HOẶC vol_today × giá >= 10,000,000 (= 10 tỷ VND) → PASS
   d. Cả spike + filter pass → VÀO RỔ
4. Gửi Telegram bảng tổng kết + lưu rổ vào Redis
```

**Khác biệt với intraday alert:**
- **Intraday**: dùng VMA9 liquidity filter (lọc theo trung bình 9 phiên) → có thể bỏ sót mã
  thanh khoản thấp bình thường nhưng nổ bất thường
- **EOD**: dùng actual vol/value cuối phiên → bắt được mã nổ volume bất thường
  (ví dụ TTA: VMA9 chỉ 427K cp nhưng ngày nổ 3.8M cp = 42.7 tỷ)

**Redis keys:**
- `alerted_today` (SET) — mã đã trigger alert trong ngày, cleared daily
- `eod_basket:{YYYY-MM-DD}` (SET) — rổ mã đạt ĐK, persistent

### 3.7 Alert Deduplication

```
Redis key: alerted:{symbol}:{level}   TTL = 600 giây (10 phút)

Logic: SET NX (atomic) — chỉ coroutine đầu tiên SET được mới gửi alert.
Với interval 15 phút và TTL 10 phút:
  → mỗi check cycle đủ điều kiện sẽ gửi lại (TTL đã hết trước check tiếp theo)
  → tức là có thể alert mỗi 15 phút nếu điều kiện vẫn thoả
```

---

## 4. Lịch chạy (Scheduler)

| Cron | Job | Ghi chú |
|------|-----|---------|
| `09:00` | `reset_daily` | Xoá `vol_today:*`, `alerted:*`, `alerted_today`, gửi thông báo phiên mới |
| `*/15` trong giờ `9,10,11,13,14` | `check_volume` | Giờ 9: :00,:15,:30,:45 — giờ 12 không có |
| `14:46` | `eod_summary` | Scan toàn bộ mã → lọc spike + thanh khoản → gửi tổng kết + lưu rổ |
| `14:50` | `update_vma9` | Tính lại VMA9 dựa trên dữ liệu đến hôm qua |

**Các lần check trong ngày:**
```
09:00* 09:15 09:30 09:45
10:00  10:15 10:30 10:45
11:00  11:15 11:30**
13:00  13:15 13:30 13:45
14:00  14:15 14:30 14:45

* 09:00: elapsed=0, bỏ qua ngay
** 11:30: session=None (không nằm trong MORNING hay AFTERNOON), bỏ qua
```

---

## 5. Redis — các key quan trọng

| Key pattern | Kiểu | TTL | Ý nghĩa |
|-------------|------|-----|---------|
| `vma9:{symbol}` | String (float) | Không có | VMA9 của mã, đơn vị: cp/phiên |
| `vol_today:{symbol}` | String (int) | Xoá lúc 09:00 | Khối lượng tích lũy hôm nay |
| `alerted:{symbol}:{level}` | String ("1") | 600 giây | Dedup alert — có key = đã alert gần đây |
| `last_ratio:{symbol}` | String (float) | Không có | Ratio lần kiểm tra gần nhất (để theo dõi) |
| `alerted_today` | Set | Xoá lúc 09:00 | Tập mã đã trigger alert trong ngày |
| `eod_basket:{YYYY-MM-DD}` | Set | Không có | Rổ mã đạt ĐK cuối phiên (~46 KB/năm) |

**Kiểm tra Redis thủ công:**
```bash
# Kết nối vào container Redis
docker compose exec redis redis-cli

# Kiểm tra VMA9 của TPB
GET vma9:TPB

# Kiểm tra trạng thái alert của TPB
KEYS alerted:TPB:*

# Kiểm tra khối lượng hôm nay
GET vol_today:TPB

# Kiểm tra ratio lần kiểm tra gần nhất
GET last_ratio:TPB

# Đếm tổng số mã có VMA9
KEYS vma9:* | wc -l  (hoặc: DBSIZE)
```

---

## 6. Cấu hình (.env)

| Biến | Mặc định | Ý nghĩa |
|------|---------|---------|
| `TELEGRAM_BOT_TOKEN` | — | Token bot Telegram |
| `TELEGRAM_CHAT_ID` | — | ID channel/group nhận alert |
| `ATO_THRESHOLD` | 3.0 | Ratio để alert phiên ATO/ATC |
| `WARNING_THRESHOLD` | 2.0 | Ratio để gửi ⚠️ WARNING |
| `CRITICAL_THRESHOLD` | 3.0 | Ratio để gửi 🚨 CRITICAL |
| `VMA9_MIN_DAYS` | 3 | Số ngày tối thiểu để tính VMA9 (bỏ qua nếu thiếu) |
| `VMA9_LOOKBACK_DAYS` | 9 | Số phiên tính trung bình VMA9 |
| `VMA9_HISTORY_FETCH_DAYS` | 25 | Khoảng thời gian fetch dữ liệu VMA9 |
| `MIN_VMA9_VOLUME` | 1,000,000 | Lọc thanh khoản intraday: VMA9 tối thiểu (cp/phiên) |
| `MIN_VMA9_VALUE` | 10,000,000 | Lọc thanh khoản intraday: giá trị tối thiểu (nghìn đồng×cp) |
| `EOD_MIN_VOLUME` | 1,000,000 | EOD summary: vol thực tế tối thiểu (cp) |
| `EOD_MIN_VALUE` | 10,000,000 | EOD summary: giá trị thực tế tối thiểu (nghìn đồng×cp = 10 tỷ) |
| `DNSE_CONCURRENCY` | 20 | Số request song song đến DNSE |
| `DNSE_TIMEOUT` | 10.0 | Timeout mỗi request DNSE (giây) |
| `LOG_LEVEL` | INFO | DEBUG để xem chi tiết skip |

---

## 7. Luồng dữ liệu từ đầu đến cuối

```
[Startup]
  fetch_hose_symbols() + fetch_hnx_symbols() + fetch_upcom_symbols()
       ↓
  build exchange_map: {symbol → "HOSE"/"HNX"/"UPCOM"}
       ↓
  update_vma9_all()  →  Redis: vma9:{symbol} = <float>

[Mỗi 15 phút]
  check_volume(symbols, store, exchange_map)
       ↓
  run_check()
    ├── get_session(now) → None: exit sớm (ngoài giờ)
    ├── elapsed_minutes(now) → 0: exit sớm
    ├── fetch_all_ohlcv(symbols, 09:00→now, resolution="15")
    │     → {symbol: {t:[...], v:[...], c:[...]}}
    ├── store.get_all_vma9(symbols)
    │     → {symbol: float | None}
    └── asyncio.gather(_check_one × N)
          ↓
    _check_one(symbol):
      1. vma9 = None hoặc 0 → RETURN (silent)
      2. vol_today = sum(v) từ OHLCV; last_close = c[-1]
      3. passes_liquidity_filter? → False: RETURN (DEBUG log)
      4. ratio = vol_today / (vma9 × elapsed / 255)
      5. determine_alert_level(ratio, session)
         → None: RETURN
      6. store.claim_alert(symbol, level, 600s)
         → False (key tồn tại): RETURN (đã alert gần đây)
      7. format_alert() + send_message() → Telegram
      8. store.add_alerted_today(symbol) → ghi nhận vào set

[14:46 — Tổng kết cuối phiên]
  eod_summary(symbols, store, exchange_map)
       ↓
  run_eod_summary()
    ├── fetch_all_ohlcv(ALL symbols, 09:00→now, resolution="15")
    ├── store.get_all_vma9(ALL symbols)
    └── với mỗi symbol:
          1. vma9 = None → SKIP
          2. vol_today, last_close từ OHLCV
          3. ratio = vol_today / vma9 < 2.0x → SKIP
          4. passes_eod_filter(vol_today, price, 1M cp, 10 tỷ) → False: SKIP
          5. → VÀO RỔ
    ├── store.set_eod_basket(symbols, date) → Redis
    └── format_eod_summary() + send_message() → Telegram
```

---

## 8. Điều kiện để alert được gửi

Tất cả các bước sau phải PASS:

```
✅ 1. Đang trong giờ giao dịch (session != None)
✅ 2. elapsed_minutes > 0 (không phải đúng 09:00)
✅ 3. VMA9 tồn tại trong Redis và > 0
✅ 4. Dữ liệu OHLCV từ DNSE trả về (không None, không rỗng)
✅ 5. Vượt liquidity filter (VMA9 >= 1M cp HOẶC giá trị >= 10 tỷ)
✅ 6. ratio đủ ngưỡng theo phiên (WARNING: ≥2x, CRITICAL: ≥3x)
✅ 7. Redis claim_alert thành công (SET NX — chưa alert level này trong 10 phút)
```

---

## 9. Các lý do phổ biến khiến alert không gửi

### 9.1 VMA9 = None trong Redis ⭐ (nguyên nhân phổ biến nhất)

**Triệu chứng:** Bot chạy bình thường, log "VMA9 updated: 450/500 symbols" (số lệch).

**Nguyên nhân:**
- DNSE API timeout/lỗi khi fetch dữ liệu ngày của mã đó
- Mã mới niêm yết / tạm ngừng giao dịch → không đủ `vma9_min_days=3` ngày
- Redis bị restart mà không có persistence → mất hết VMA9

**Cách xác nhận:**
```bash
docker compose exec redis redis-cli GET vma9:TPB
# Nếu trả về (nil) → đây là nguyên nhân
```

**Fix:** Chạy lại bootstrap VMA9 (xem mục 10), hoặc restart service.

### 9.2 Liquidity filter loại ra

**Triệu chứng:** Log DEBUG "Skip {symbol}: low liquidity" (chỉ thấy khi LOG_LEVEL=DEBUG).

**Cách xác nhận:** Tính thủ công: VMA9 × giá × 1000 (VNĐ) có đủ 10 tỷ không?

### 9.3 Ratio không đủ tại thời điểm check

**Triệu chứng:** Volume cuối ngày cao nhưng intraday tại các mốc 15 phút chưa đủ.

**Ví dụ:** Volume bùng nổ lúc 14:20, nhưng check lúc 14:15 chưa đủ, check lúc 14:30 mới đủ. Nếu check 14:30 thì được, 14:15 thì không. Cần xem volume từng 15 phút.

### 9.4 Alert đã gửi nhưng không thấy

- Bot đã gửi ở check cycle trước (ví dụ 11:15) và user không để ý
- Kiểm tra: `GET last_ratio:TPB` để xem ratio lần cuối check

### 9.5 Cron schedule không cover hết giờ

Check lúc **11:30** bị bỏ qua (session=None). Check lúc **14:45** là lần cuối (ATC).
Nếu volume spike xảy ra trong khoảng 11:15–11:30 hoặc sau 14:45 thì không có alert.

### 9.6 Service bị down

Kiểm tra: `docker compose ps` và `docker compose logs stock-risk --tail=100`.

---

## 10. Cách debug một mã cụ thể

### Bước 1: Kiểm tra Redis

```bash
docker compose exec redis redis-cli

# VMA9 có không?
GET vma9:TPB          # Nếu nil → VMA9 missing, bot bỏ qua TPB

# Ratio lần check gần nhất
GET last_ratio:TPB    # Nếu nil → TPB chưa từng vượt liquidity filter

# Alert dedup key
KEYS alerted:TPB:*    # Có key → gần đây đã alert
TTL alerted:TPB:warning
```

### Bước 2: Xem log container

```bash
docker compose logs stock-risk --since="2026-06-06T09:00:00" | grep -i "TPB\|error\|failed"

# Nếu TPB không xuất hiện trong log INFO → bị filter ở liquidity hoặc VMA9=None
# Tìm dòng "VMA9 updated: X/Y" — nếu X < Y thì có mã bị bỏ qua
```

### Bước 3: Bật DEBUG log tạm thời

```bash
# Trong .env đổi LOG_LEVEL=DEBUG rồi restart
docker compose restart stock-risk
docker compose logs -f stock-risk | grep TPB
```
Với DEBUG sẽ thấy: "Skip TPB: low liquidity" hoặc ngầm hiểu từ không có log về TPB.

### Bước 4: Test thủ công

```bash
docker compose exec stock-risk python -c "
import asyncio
from src.data.redis_store import RedisStore
from src.data.dnse_client import fetch_all_ohlcv
from datetime import datetime, time
from zoneinfo import ZoneInfo

async def check():
    store = await RedisStore.create()
    vma9 = await store.get_all_vma9(['TPB'])
    print('VMA9 TPB:', vma9)
    # Fetch OHLCV hôm nay
    tz = ZoneInfo('Asia/Ho_Chi_Minh')
    now = datetime.now(tz)
    start = int(datetime.combine(now.date(), time(9,0), tzinfo=tz).timestamp())
    end = int(now.timestamp())
    raw = await fetch_all_ohlcv(['TPB'], start, end, '15')
    data = raw.get('TPB')
    if data and data.get('t'):
        vol = sum(v for v in data.get('v',[]) if v)
        price = data['c'][-1] if data.get('c') else None
        print(f'Vol today: {vol:,}  Price: {price}')
        if vma9['TPB']:
            ratio = vol / vma9['TPB']
            print(f'Ratio (full day basis): {ratio:.2f}x')
    await store.close()

asyncio.run(check())
"
```

### Bước 5: Bootstrap lại VMA9 cho một mã

```bash
docker compose exec stock-risk python -c "
import asyncio
from src.core.vma9 import update_vma9_all
from src.data.redis_store import RedisStore

async def run():
    store = await RedisStore.create()
    await update_vma9_all(['TPB'], store)
    vma9 = await store.get_all_vma9(['TPB'])
    print('VMA9 sau bootstrap:', vma9)
    await store.close()

asyncio.run(run())
"
```

---

## 11. Cấu trúc file

```
stock-risk/
├── main.py                  — entry point, khởi động scheduler
├── .env                     — cấu hình (không commit)
├── docker-compose.yml       — services: stock-risk + redis
├── Dockerfile
├── requirements.txt
├── data/
│   ├── symbols_hose.txt     — cache danh sách mã HoSE
│   ├── symbols_hnx.txt      — cache danh sách mã HNX
│   └── symbols_upcom.txt    — cache danh sách mã UPCoM
├── src/
│   ├── core/
│   │   ├── checker.py       — ⭐ LOGIC CHÍNH: ratio, filter, alert level
│   │   ├── eod_summary.py   — tổng kết cuối phiên: scan all → spike + filter → rổ
│   │   ├── session.py       — phiên giao dịch + elapsed_minutes
│   │   └── vma9.py          — tính VMA9 từ daily OHLCV
│   ├── data/
│   │   ├── dnse_client.py   — fetch OHLCV + danh sách mã
│   │   └── redis_store.py   — CRUD Redis keys
│   ├── alerts/
│   │   ├── formatter.py     — format HTML message cho Telegram
│   │   └── telegram.py      — gửi message
│   ├── jobs/
│   │   ├── check.py         — wrapper check_volume
│   │   ├── eod_summary.py   — wrapper eod_summary
│   │   ├── vma9_update.py   — wrapper update_vma9
│   │   └── reset.py         — reset daily + thông báo phiên mới
│   └── config/
│       ├── settings.py      — đọc .env (pydantic-settings)
│       └── constants.py     — SESSIONS, TOTAL_TRADING_MINUTES
├── scripts/
│   ├── replay_day.py        — replay cả ngày, in alert từng mốc 15p
│   └── test_eod_summary.py  — test EOD filter với data DNSE thực
└── tests/
    ├── test_checker.py
    ├── test_session.py
    └── test_vma9.py
```

---

## Script Replay — kiểm tra lại cả ngày

Script `scripts/replay_day.py` chạy lại toàn bộ một ngày giao dịch và in ra mã nào đủ điều kiện alert tại từng mốc 15 phút. **Không gửi Telegram, không cần Redis, chạy độc lập.**

```bash
# Replay hôm nay (mặc định xem chi tiết TPB)
python scripts/replay_day.py

# Replay ngày cụ thể
python scripts/replay_day.py 2026-06-06

# Replay ngày cụ thể + xem chi tiết nhiều mã
python scripts/replay_day.py 2026-06-06 TPB,VPB,MBB,ACB
```

**Output gồm 3 phần:**
1. **Từng mốc 15 phút**: danh sách mã alert (sort theo ratio giảm dần)
2. **Tóm tắt cuối ngày**: bảng tổng hợp mã + số lần + max ratio + lần đầu alert
3. **Chi tiết mã watch**: nếu mã không alert → in ratio tại mỗi mốc để thấy lý do (VMA9 None? Liquidity fail? Ratio chưa đủ?)

**Khi nào nên dùng:**
- Khách báo mã X có tín hiệu nhưng bot không gửi → chạy replay để xem bot tính gì tại từng mốc
- Kiểm tra ngưỡng threshold có hợp lý không trước khi thay đổi
- Sau khi sửa logic alert, so sánh kết quả trước/sau

---

## Script Test EOD Summary

Script `scripts/test_eod_summary.py` test filter tổng kết cuối phiên với data DNSE thực. **Không cần Redis hay Telegram.**

```bash
PYTHONPATH=. python scripts/test_eod_summary.py
```

**Output:**
- Scan toàn bộ 1,500+ mã cho 3 ngày (Thu, Fri, Mon)
- In danh sách mã đạt ĐK vào rổ (spike >= 2x + vol >= 1M cp or value >= 10 tỷ)
- Highlight target symbols (LDG, AGG, TTA)
- Ước tính Redis memory

**Khi nào nên dùng:**
- Kiểm tra EOD filter có bắt đúng mã khách hàng yêu cầu
- Điều chỉnh ngưỡng EOD_MIN_VOLUME / EOD_MIN_VALUE
- Verify trước khi deploy

---

## Quy trình sửa logic alert

Khi cần thay đổi thuật toán (ngưỡng, cách tính, thêm điều kiện):

| Muốn sửa | File cần vào |
|----------|-------------|
| Ngưỡng ratio (2x/3x) | `.env` hoặc `src/config/settings.py` |
| Cách tính ratio / expected | `src/core/checker.py` → `calculate_ratio()` |
| Điều kiện alert level | `src/core/checker.py` → `determine_alert_level()` |
| Lọc thanh khoản | `src/core/checker.py` → `passes_liquidity_filter()` |
| Cách tính elapsed (phiên) | `src/core/session.py` → `elapsed_minutes()` |
| Cách tính VMA9 | `src/core/vma9.py` → `compute_vma9()` |
| Format tin nhắn Telegram | `src/alerts/formatter.py` |
| Lịch chạy cron | `main.py` → `scheduler.add_job()` |
| TTL dedup alert | `src/config/constants.py` → `ALERT_TTL_SECONDS` |
| Ngưỡng EOD filter | `.env` → `EOD_MIN_VOLUME`, `EOD_MIN_VALUE` |
| Logic tổng kết cuối phiên | `src/core/eod_summary.py` → `run_eod_summary()` |
| Format tổng kết Telegram | `src/alerts/formatter.py` → `format_eod_summary()` |
