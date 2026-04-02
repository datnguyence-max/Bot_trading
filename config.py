"""
Đọc cấu hình từ file config.ini bên ngoài
"""

import configparser
import os
import sys
import MetaTrader5 as mt5


def get_config_path():
    """Tìm file config.ini cạnh file exe hoặc script"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "config.ini")


class BotConfig:
    def __init__(self):
        path = get_config_path()
        print(f"📂 Đang tìm config tại: {path}")

        if not os.path.exists(path):
            raise FileNotFoundError(
                f"\n❌ Không tìm thấy file config.ini tại:\n   {path}\n"
                f"   Vui lòng đặt file config.ini cùng thư mục với bot.py"
            )

        cfg = configparser.ConfigParser()
        cfg.read(path, encoding="utf-8")

        errors = []

        # ── Kiểm tra các section bắt buộc ────────────────────────────────────
        for section in ["MT5", "TELEGRAM", "TRADING", "STRATEGY", "SYSTEM"]:
            if section not in cfg:
                errors.append(f"Thiếu section [{section}] trong config.ini")

        if errors:
            raise ValueError("\n❌ Lỗi config.ini:\n" + "\n".join(f"   - {e}" for e in errors))

        # ── MT5 ───────────────────────────────────────────────────────────────
        try:
            self.MT5_LOGIN    = int(cfg["MT5"]["login"])
            self.MT5_PASSWORD = cfg["MT5"]["password"]
            self.MT5_SERVER   = cfg["MT5"]["server"]
        except KeyError as e:
            raise KeyError(f"❌ Thiếu key {e} trong section [MT5]")
        except ValueError:
            raise ValueError("❌ [MT5] login phải là số nguyên")

        # ── Telegram ──────────────────────────────────────────────────────────
        try:
            self.TELEGRAM_TOKEN   = cfg["TELEGRAM"]["token"]
            self.TELEGRAM_CHAT_ID = cfg["TELEGRAM"]["chat_id"]
        except KeyError as e:
            raise KeyError(f"❌ Thiếu key {e} trong section [TELEGRAM]")

        # ── Trading ───────────────────────────────────────────────────────────
        try:
            # Hỗ trợ cả "symbols" (mới) lẫn "symbol" (cũ — tương thích ngược)
            if "symbols" in cfg["TRADING"]:
                raw = cfg["TRADING"]["symbols"]
            elif "symbol" in cfg["TRADING"]:
                raw = cfg["TRADING"]["symbol"]
                print("⚠️  Dùng key 'symbol' cũ — khuyến nghị đổi thành 'symbols' trong config.ini")
            else:
                raise KeyError("'symbols'")

            self.SYMBOLS = [s.strip() for s in raw.split(",") if s.strip()]
            if not self.SYMBOLS:
                raise ValueError("Danh sách symbols trống")

            self.SYMBOL        = self.SYMBOLS[0]
            self.LOT_SIZE      = float(cfg["TRADING"]["lot_size"])
            self.SL_PIPS       = float(cfg["TRADING"]["sl_pips"])
            self.MAX_POSITIONS = int(cfg["TRADING"]["max_positions"])
        except KeyError as e:
            raise KeyError(f"❌ Thiếu key {e} trong section [TRADING]")
        except ValueError as e:
            raise ValueError(f"❌ Sai kiểu dữ liệu trong [TRADING]: {e}")

        self.SLIPPAGE     = 10
        self.MAGIC_NUMBER = 20240101

        # ── Strategy ──────────────────────────────────────────────────────────
        try:
            self.ENTRY_WINDOW_SEC  = int(cfg["STRATEGY"]["entry_window_sec"])
            self.TAKE_PROFIT_PCT   = float(cfg["STRATEGY"]["take_profit_pct"])
            self.STOP_LOSS_PCT     = float(cfg["STRATEGY"]["stop_loss_pct"])
            self.MAX_HOLD_MINUTES  = float(cfg["STRATEGY"]["max_hold_minutes"])
            self.VOLUME_MULTIPLIER = float(cfg["STRATEGY"]["volume_multiplier"])
            self.PRICE_MULTIPLIER  = float(cfg["STRATEGY"]["price_multiplier"])
        except KeyError as e:
            raise KeyError(f"❌ Thiếu key {e} trong section [STRATEGY]")
        except ValueError as e:
            raise ValueError(f"❌ Sai kiểu dữ liệu trong [STRATEGY]: {e}")

        # ── System ────────────────────────────────────────────────────────────
        try:
            self.LOOP_INTERVAL  = int(cfg["SYSTEM"]["loop_interval"])
            self.EXCEL_LOG_PATH = cfg["SYSTEM"]["excel_log"]
        except KeyError as e:
            raise KeyError(f"❌ Thiếu key {e} trong section [SYSTEM]")

        # ── Timeframe ─────────────────────────────────────────────────────────
        self.TIMEFRAME     = mt5.TIMEFRAME_M5
        self.TIMEFRAME_STR = "M5"
        self.TP_PIPS       = 0

        # ── Tóm tắt ───────────────────────────────────────────────────────────
        print(f"✅ Đọc config thành công!")
        print(f"   Login   : {self.MT5_LOGIN} | Server: {self.MT5_SERVER}")
        print(f"   Symbols : {', '.join(self.SYMBOLS)} | Lot: {self.LOT_SIZE}")
        print(f"   TP: {self.TAKE_PROFIT_PCT}% | SL: {self.STOP_LOSS_PCT}% | Hold: {self.MAX_HOLD_MINUTES}m")
        print(f"   Vol×{self.VOLUME_MULTIPLIER} | Cửa sổ: {self.ENTRY_WINDOW_SEC}s")
