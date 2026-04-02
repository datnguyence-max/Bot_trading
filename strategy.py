import pandas as pd
import logging

log = logging.getLogger(__name__)


class Strategy:
    def __init__(self, config):
        self.cfg = config

    def generate_signal(self, df: pd.DataFrame, symbol: str = "", tick_price: float = None) -> str | None:
        """
        Phân tích tín hiệu từ DataFrame nến M5.

        Logic:
          - Nến T-1 (candle_t)   : nến vừa đóng  → df.iloc[-2]
          - Nến T-2 (candle_prev) : nến trước đó  → df.iloc[-3]
          - Nến hiện tại          : đang hình thành → df.iloc[-1]

        Điều kiện vào lệnh:
          BUY  : body(T-1) > m×body(T-2)  VÀ  vol(T-1) > m×vol(T-2)
                 VÀ T-1 tăng (close > open)  VÀ T-2 cũng tăng
          SELL : tương tự nhưng cả 2 nến đều giảm

        Cửa sổ thời gian: chỉ vào lệnh trong entry_window_sec giây đầu
                          sau khi nến mới mở.
        """
        from datetime import datetime, timezone

        tag = f"[{symbol}]" if symbol else ""

        if len(df) < 3:
            log.warning(f"{tag} Không đủ dữ liệu nến (cần >= 3)")
            return None

        candle_t    = df.iloc[-2]   # T-1: nến vừa đóng
        candle_now  = df.iloc[-1]   # nến đang hình thành
        candle_prev = df.iloc[-3]   # T-2: nến trước T-1

        a      = candle_t["tick_volume"]            # volume T-1
        x      = abs(candle_t["close"] - candle_t["open"])    # body T-1
        a_prev = candle_prev["tick_volume"]         # volume T-2
        x_prev = abs(candle_prev["close"] - candle_prev["open"])  # body T-2
        m      = self.cfg.VOLUME_MULTIPLIER

        # ── Kiểm tra cửa sổ thời gian ────────────────────────────────────────
        candle_open_time = candle_now["time"]
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        seconds_since_open = (now_utc - candle_open_time).total_seconds()

        if seconds_since_open > self.cfg.ENTRY_WINDOW_SEC:
            log.info(f"{tag} Đã qua cửa sổ vào lệnh: {seconds_since_open:.0f}s > {self.cfg.ENTRY_WINDOW_SEC}s — bỏ qua")
            return None

        # ── Nến Doji: body T-1 = 0 → bỏ qua ─────────────────────────────────
        if x == 0:
            log.info(f"{tag} [Signal] T-1 Doji ❌ — bỏ qua")
            return None

        # ── Đánh giá từng điều kiện ───────────────────────────────────────────
        body_ok  = (x_prev == 0) or (x > m * x_prev)   # body T-1 đủ lớn
        vol_ok   = a > m * a_prev                        # volume T-1 đủ lớn
        trend_ok = candle_t["close"] > candle_t["open"]  # T-1 tăng
        trend_dn = candle_t["close"] < candle_t["open"]  # T-1 giảm

        prev_trend_up = candle_prev["close"] > candle_prev["open"]  # T-2 tăng
        prev_trend_dn = candle_prev["close"] < candle_prev["open"]  # T-2 giảm

        c_body  = "✅" if body_ok  else "❌"
        c_vol   = "✅" if vol_ok   else "❌"

        dir_t1 = "tăng 📈" if trend_ok else ("giảm 📉" if trend_dn    else "doji —")
        dir_t2 = "tăng 📈" if prev_trend_up else ("giảm 📉" if prev_trend_dn else "doji —")

        # ── Xác định tín hiệu ────────────────────────────────────────────────
        if body_ok and vol_ok and trend_ok and prev_trend_up:
            result = "BUY"
            icon   = "📈 BUY"
        elif body_ok and vol_ok and trend_dn and prev_trend_dn:
            result = "SELL"
            icon   = "📉 SELL"
        else:
            result = None
            icon   = "❌ BỎ QUA"

        log.info(
            f"{tag} [Signal] "
            f"Body {x:.5f} >= {m}×{x_prev:.5f} {c_body} | "
            f"Vol {a:.0f} >= {m}×{a_prev:.0f} {c_vol} | "
            f"T-1 {dir_t1} | T-2 {dir_t2}"
            f" → {icon}"
        )

        return result
