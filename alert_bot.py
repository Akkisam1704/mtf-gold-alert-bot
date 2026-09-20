"""
MTF (1H -> 15M) Trend Alignment + Liquidity Sweep Detector — GitHub Actions bot
================================================================================
Runs on a schedule (see .github/workflows/mtf_alert.yml), fetches fresh
TopstepX bars, runs the detector, and sends a Telegram message ONLY when
the signal actually changes (so you get one alert per event, not a repeat
message every run while nothing has changed).

Secrets required (set these in your GitHub repo:
Settings -> Secrets and variables -> Actions -> New repository secret):
    TOPSTEPX_USERNAME
    TOPSTEPX_API_KEY
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

See README.md for how to create the Telegram bot and get these values.
"""

import os
import sys
import json
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional, Literal

BASE_URL = "https://api.topstepx.com"
MAX_BARS_PER_REQUEST = 20000
STATE_FILE = "state.json"


def die(msg):
    print(f"\n❌ {msg}")
    sys.exit(1)


# ============================================================================
# PART 1 — DATA FETCH
# ============================================================================

def authenticate(session, username, api_key):
    print("→ Authenticating...")
    resp = session.post(
        f"{BASE_URL}/api/Auth/loginKey",
        headers={"accept": "text/plain", "Content-Type": "application/json"},
        json={"userName": username, "apiKey": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        die(f"Auth failed: {data}")
    token = data.get("token")
    if not token:
        die(f"No token in response: {data}")
    print("✅ Authenticated successfully.")
    return {
        "accept": "text/plain",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def find_mgc_contract(session, headers):
    print("→ Searching for MGC (Micro Gold) contract...")
    resp = session.post(
        f"{BASE_URL}/api/Contract/search",
        headers=headers,
        json={"searchText": "MGC", "live": False},
        timeout=15,
    )
    if not resp.ok:
        die(f"Contract search failed: {resp.status_code} {resp.text}")
    contracts = resp.json()
    results = contracts.get("contracts") or contracts.get("data") or []
    if not results:
        die(f"No MGC contracts found: {contracts}")
    match = next(
        (c for c in results if str(c.get("name", "")).upper().startswith("MGC")
         or str(c.get("symbol", "")).upper().startswith("MGC")
         or c.get("activeContract")),
        results[0],
    )
    contract_id = match.get("id") or match.get("contractId")
    print(f"✅ Using contract: {match.get('description', contract_id)} → {contract_id}")
    return contract_id


def fetch_bars(session, headers, contract_id, unit, unit_number, label, lookback_days=90):
    print(f"→ Fetching {label}...")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    resp = session.post(
        f"{BASE_URL}/api/History/retrieveBars",
        headers=headers,
        json={
            "contractId": contract_id, "live": False,
            "startTime": start.isoformat(), "endTime": end.isoformat(),
            "unit": unit, "unitNumber": unit_number,
            "limit": MAX_BARS_PER_REQUEST, "includePartialBar": False,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"⚠️  {label} fetch failed ({resp.status_code}): {resp.text}")
        return None
    data = resp.json()
    bars = data.get("bars", [])
    if not bars:
        print(f"⚠️  {label}: no bars returned.")
        return None
    df = pd.DataFrame(bars)
    df = df.rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    print(f"✅ {label}: {len(df)} bars, back to {df['timestamp'].iloc[0]}")
    return df


def fetch_1h_and_15m():
    username = os.environ.get("TOPSTEPX_USERNAME")
    api_key = os.environ.get("TOPSTEPX_API_KEY")
    if not username or not api_key:
        die("Missing TOPSTEPX_USERNAME / TOPSTEPX_API_KEY environment variables.")

    session = requests.Session()
    headers = authenticate(session, username, api_key)
    contract_id = find_mgc_contract(session, headers)

    df_1h = fetch_bars(session, headers, contract_id, unit=3, unit_number=1, label="1hr", lookback_days=120)
    time.sleep(1)
    df_15m = fetch_bars(session, headers, contract_id, unit=2, unit_number=15, label="15min", lookback_days=60)
    return df_1h, df_15m


# ============================================================================
# PART 2 — DETECTOR (swing detection, structure/bias, liquidity sweep)
# ============================================================================

Bias = Literal["bullish", "bearish", "ranging"]


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df.sort_values("timestamp").reset_index(drop=True)
    elif isinstance(df.index, pd.DatetimeIndex):
        df = df.sort_index().reset_index()
        df = df.rename(columns={df.columns[0]: "timestamp"})
    else:
        raise ValueError("DataFrame needs a 'timestamp' column or a DatetimeIndex.")
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {missing}")
    return df.reset_index(drop=True)


def detect_swings(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    swing_high = np.full(n, np.nan)
    swing_low = np.full(n, np.nan)
    confirmed_at = np.full(n, -1, dtype=int)
    for i in range(left, n - right):
        wh = highs[i - left: i + right + 1]
        wl = lows[i - left: i + right + 1]
        if highs[i] == wh.max():
            swing_high[i] = highs[i]
            confirmed_at[i] = i + right
        if lows[i] == wl.min():
            swing_low[i] = lows[i]
            confirmed_at[i] = i + right if confirmed_at[i] == -1 else confirmed_at[i]
    out = df.copy()
    out["swing_high"] = swing_high
    out["swing_low"] = swing_low
    out["swing_confirmed_at"] = confirmed_at
    return out


def alternating_swings(df: pd.DataFrame) -> list:
    raw = []
    for i, row in df.iterrows():
        if not np.isnan(row["swing_high"]):
            raw.append({"idx": i, "confirmed_at": int(row["swing_confirmed_at"]),
                        "time": row["timestamp"], "price": row["swing_high"], "type": "high"})
        if not np.isnan(row["swing_low"]):
            raw.append({"idx": i, "confirmed_at": int(row["swing_confirmed_at"]),
                        "time": row["timestamp"], "price": row["swing_low"], "type": "low"})
    raw.sort(key=lambda s: (s["idx"], 0 if s["type"] == "high" else 1))
    cleaned = []
    for s in raw:
        if cleaned and cleaned[-1]["type"] == s["type"]:
            if s["type"] == "high" and s["price"] >= cleaned[-1]["price"]:
                cleaned[-1] = s
            elif s["type"] == "low" and s["price"] <= cleaned[-1]["price"]:
                cleaned[-1] = s
        else:
            cleaned.append(s)
    return cleaned


@dataclass
class BiasPoint:
    idx: int
    confirmed_at: int
    time: object
    bias: Bias
    label: str
    swing_type: str
    price: float


def compute_bias_timeline(swings: list) -> list:
    timeline = []
    last_high = last_low = None
    last_high_label = last_low_label = None
    bias: Bias = "ranging"
    for s in swings:
        if s["type"] == "high":
            label = "H" if last_high is None else ("HH" if s["price"] > last_high else "LH")
            last_high, last_high_label = s["price"], label
        else:
            label = "L" if last_low is None else ("HL" if s["price"] > last_low else "LL")
            last_low, last_low_label = s["price"], label
        if last_high_label == "HH" and last_low_label == "HL":
            bias = "bullish"
        elif last_high_label == "LH" and last_low_label == "LL":
            bias = "bearish"
        elif last_high_label in ("HH", "LH") and last_low_label in ("HL", "LL"):
            bias = "ranging"
        timeline.append(BiasPoint(idx=s["idx"], confirmed_at=s["confirmed_at"], time=s["time"],
                                   bias=bias, label=label, swing_type=s["type"], price=s["price"]))
    return timeline


def current_bias(timeline: list) -> Bias:
    return timeline[-1].bias if timeline else "ranging"


def classify_liquidity_level(swings, target_idx, price, swing_type, ext_lookback=40, int_lookback=10) -> str:
    same_type = [s for s in swings if s["type"] == swing_type and s["idx"] < target_idx]
    ext_pool = [s for s in same_type if s["idx"] >= target_idx - ext_lookback]
    int_pool = [s for s in same_type if s["idx"] >= target_idx - int_lookback]
    if not ext_pool:
        return "neither"
    if swing_type == "high":
        is_ext_extreme = price >= max(s["price"] for s in ext_pool)
        is_int_extreme = price >= max((s["price"] for s in int_pool), default=-np.inf)
    else:
        is_ext_extreme = price <= min(s["price"] for s in ext_pool)
        is_int_extreme = price <= min((s["price"] for s in int_pool), default=np.inf)
    if is_ext_extreme:
        return "external"
    if is_int_extreme:
        return "internal"
    return "neither"


@dataclass
class PreAlignmentAnalysis:
    direction: Bias
    pattern: Optional[str] = None
    leg1_price: Optional[float] = None
    leg1_time: Optional[object] = None
    leg2_price: Optional[float] = None
    leg2_time: Optional[object] = None
    second_leg_position: Optional[str] = None
    leg2_liquidity_type: Optional[str] = None
    notes: list = field(default_factory=list)


def analyze_pre_alignment_leg(swings, timeline, align_confirmed_at, direction,
                               ext_lookback=40, int_lookback=10) -> PreAlignmentAnalysis:
    result = PreAlignmentAnalysis(direction=direction)
    want_type = "low" if direction == "bullish" else "high"
    pre_window = [s for s in swings if s["type"] == want_type and s["confirmed_at"] < align_confirmed_at]
    if len(pre_window) < 2:
        result.notes.append(f"Not enough {want_type} swings before alignment to form a two-leg pattern.")
        return result
    recent = pre_window[-4:] if len(pre_window) >= 4 else pre_window
    leg2 = min(recent, key=lambda s: s["price"]) if want_type == "low" else max(recent, key=lambda s: s["price"])
    leg2_pos = pre_window.index(leg2)
    if leg2_pos == 0:
        leg2 = pre_window[-1]
        leg1 = pre_window[-2]
    else:
        leg1 = pre_window[leg2_pos - 1]
    result.pattern = "W" if direction == "bullish" else "M"
    result.leg1_price, result.leg1_time = leg1["price"], leg1["time"]
    result.leg2_price, result.leg2_time = leg2["price"], leg2["time"]
    if direction == "bullish":
        if leg2["price"] < leg1["price"]:
            result.second_leg_position = "outside_first_leg"
            result.notes.append("Second leg swept BELOW the first leg's low before reversing up.")
        else:
            result.second_leg_position = "inside_first_leg"
            result.notes.append("Second leg held ABOVE the first leg's low (no sweep, higher low).")
    else:
        if leg2["price"] > leg1["price"]:
            result.second_leg_position = "outside_first_leg"
            result.notes.append("Second leg swept ABOVE the first leg's high before reversing down.")
        else:
            result.second_leg_position = "inside_first_leg"
            result.notes.append("Second leg held BELOW the first leg's high (no sweep, lower high).")
    result.leg2_liquidity_type = classify_liquidity_level(
        swings, leg2["idx"], leg2["price"], want_type, ext_lookback=ext_lookback, int_lookback=int_lookback)
    if result.leg2_liquidity_type == "external":
        result.notes.append("Level = EXTERNAL liquidity (significant older swing) -> stronger signal.")
    elif result.leg2_liquidity_type == "internal":
        result.notes.append("Level = INTERNAL liquidity (minor recent swing) -> more inducement-risk.")
    else:
        result.notes.append("Level doesn't clearly register as internal or external liquidity.")
    return result


def _tf_bias(df, left, right):
    df = _normalize_df(df)
    df = detect_swings(df, left=left, right=right)
    swings = alternating_swings(df)
    timeline = compute_bias_timeline(swings)
    bias = current_bias(timeline)
    return df, swings, timeline, bias


def run_mtf_detector(df_1h, df_15m, swing_left_1h=2, swing_right_1h=2,
                      swing_left_15m=2, swing_right_15m=2,
                      ext_lookback_15m=40, int_lookback_15m=10) -> dict:
    df1, swings1, timeline1, bias1 = _tf_bias(df_1h, swing_left_1h, swing_right_1h)
    df15, swings15, timeline15, bias15 = _tf_bias(df_15m, swing_left_15m, swing_right_15m)
    aligned = (bias1 == bias15) and bias1 != "ranging"

    pre_alignment = None
    if bias1 in ("bullish", "bearish"):
        target_bias = bias1
        align_confirmed_at = None
        if aligned:
            run_start = None
            for bp in reversed(timeline15):
                if bp.bias == target_bias:
                    run_start = bp
                else:
                    break
            if run_start is not None:
                align_confirmed_at = run_start.confirmed_at
        else:
            matching_positions = [i for i, bp in enumerate(timeline15) if bp.bias == target_bias]
            if matching_positions:
                last_match_pos = max(matching_positions)
                run_start_pos = last_match_pos
                while run_start_pos > 0 and timeline15[run_start_pos - 1].bias == target_bias:
                    run_start_pos -= 1
                align_confirmed_at = timeline15[run_start_pos].confirmed_at
        if align_confirmed_at is not None:
            pre_alignment = analyze_pre_alignment_leg(
                swings15, timeline15, align_confirmed_at, target_bias,
                ext_lookback=ext_lookback_15m, int_lookback=int_lookback_15m)

    return {
        "bias_1h": bias1, "bias_15m": bias15, "aligned": aligned,
        "ride_directional_move": aligned, "pre_alignment": pre_alignment,
    }


# ============================================================================
# PART 3 — TELEGRAM ALERT
# ============================================================================

def send_telegram_message(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        die("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID environment variables.")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    if not resp.ok:
        print(f"⚠️  Telegram send failed ({resp.status_code}): {resp.text}")
    else:
        print("✅ Telegram alert sent.")


def format_alert(report: dict) -> str:
    b1, b15 = report["bias_1h"].upper(), report["bias_15m"].upper()
    lines = [f"<b>MGC MTF Signal</b>", f"1H: {b1}  |  15M: {b15}"]
    if report["aligned"]:
        lines.append("✅ ALIGNED — 15M moving with 1H trend. Directional continuation setup.")
    else:
        lines.append("⏳ NOT ALIGNED — 15M is a pullback/counter-move vs 1H. Wait.")
    pa = report["pre_alignment"]
    if pa and pa.pattern:
        lines.append(f"\nPre-alignment {pa.pattern}: leg1={round(pa.leg1_price, 2)}  leg2={round(pa.leg2_price, 2)}")
        lines.append(f"Second leg: {pa.second_leg_position} | Liquidity: {pa.leg2_liquidity_type}")
    return "\n".join(lines)


# ============================================================================
# PART 4 — STATE (only alert when something actually changes)
# ============================================================================

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def state_changed(old: dict, new: dict) -> bool:
    if not old:
        return True  # first run ever -> always announce
    keys = ["bias_1h", "bias_15m", "aligned"]
    return any(old.get(k) != new.get(k) for k in keys)


# ============================================================================
# MAIN
# ============================================================================

def main():
    df_1h, df_15m = fetch_1h_and_15m()
    if df_1h is None or df_15m is None:
        die("Could not fetch required data.")

    report = run_mtf_detector(df_1h, df_15m)
    print(f"\n1H: {report['bias_1h']}  |  15M: {report['bias_15m']}  |  aligned: {report['aligned']}")

    new_state = {
        "bias_1h": report["bias_1h"],
        "bias_15m": report["bias_15m"],
        "aligned": report["aligned"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    old_state = load_state()

    if state_changed(old_state, new_state):
        print("→ State changed since last run — sending Telegram alert.")
        send_telegram_message(format_alert(report))
    else:
        print("→ No change since last run — no alert sent.")

    save_state(new_state)


if __name__ == "__main__":
    main()
