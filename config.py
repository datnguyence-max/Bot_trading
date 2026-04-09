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

        for section in ["MT5", "TRADING", "STRATEGY", "SYSTEM"]:
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

        # ── Trading ───────────────────────────────────────────────────────────
        try:
            if "symbols" in cfg["TRADING"]:
                raw = cfg["TRADING"]["symbols"]
            elif "symbol" in cfg["TRADING"]:
                raw = cfg["TRADING"]["symbol"]
                print("⚠️  Dùng key 'symbol' cũ — khuyến nghị đổi thành 'symbols'")
            else:
                raise KeyError("'symbols'")

            self.SYMBOLS = [s.strip() for s in raw.split(",") if s.strip()]
            if not self.SYMBOLS:
                raise ValueError("Danh sách symbols trống")

            self.SYMBOL        = self.SYMBOLS[0]
            self.SL_PIPS       = float(cfg["TRADING"]["sl_pips"])
            self.MAX_POSITIONS = int(cfg["TRADING"]["max_positions"])

            # Lot mặc định fallback
            default_lot = float(cfg["TRADING"].get("lot_size", "0.05"))

            # Đọc lot riêng theo từng symbol: lot_size_XAUUSD, lot_size_BTCUSD, ...
            self.LOT_SIZE_MAP: dict[str, float] = {}
            for sym in self.SYMBOLS:
                key = f"lot_size_{sym}"
                if key in cfg["TRADING"]:
                    self.LOT_SIZE_MAP[sym] = float(cfg["TRADING"][key])
                else:
                    self.LOT_SIZE_MAP[sym] = default_lot
                    print(f"⚠️  [{sym}] Không có lot_size_{sym} → dùng mặc định {default_lot}")

            # Tương thích ngược — giữ LOT_SIZE cho code cũ nếu có
            self.LOT_SIZE = default_lot

        except KeyError as e:
            raise KeyError(f"❌ Thiếu key {e} trong section [TRADING]")
        except ValueError as e:
            raise ValueError(f"❌ Sai kiểu dữ liệu trong [TRADING]: {e}")

        self.SLIPPAGE     = 10
        self.MAGIC_NUMBER = 20240101

        # ── Strategy ──────────────────────────────────────────────────────────
        try:
            self.ENTRY_WINDOW_SEC    = int(cfg["STRATEGY"]["entry_window_sec"])
            self.VOLUME_MULTIPLIER   = float(cfg["STRATEGY"]["volume_multiplier"])
            self.BODY_MULTIPLIER     = float(cfg["STRATEGY"]["body_multiplier"])
            self.BODY_MULTIPLIER_MAX = float(cfg["STRATEGY"].get("body_multiplier_max", "0"))
            self.MIN_BODY_T2 = {
                key.replace("min_body_t2_", "").upper(): float(value)
                for key, value in cfg["STRATEGY"].items()
                if key.startswith("min_body_t2_")
            }
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
        self.SESSION_START = 0
        self.SESSION_END   = 0

        # ── Tóm tắt ───────────────────────────────────────────────────────────
        print(f"✅ Đọc config thành công!")
        print(f"   Login   : {self.MT5_LOGIN} | Server: {self.MT5_SERVER}")
        print(f"   Symbols : {', '.join(self.SYMBOLS)}")
        for sym in self.SYMBOLS:
            lot = self.LOT_SIZE_MAP[sym]
            mb  = self.MIN_BODY_T2.get(sym, 0)
            print(f"   [{sym}] Lot: {lot} | MinBody(T-2): {mb}")
        print(f"   Vol(T-1) > {self.VOLUME_MULTIPLIER}×Vol(T-2)")
        print(f"   {self.BODY_MULTIPLIER}×Body(T-2) < Body(T-1) < {self.BODY_MULTIPLIER_MAX}×Body(T-2)")
        print(f"   Cửa sổ vào lệnh: {self.ENTRY_WINDOW_SEC}s")

    def get_lot(self, symbol: str) -> float:
        """Lấy lot size cho symbol cụ thể."""
        return self.LOT_SIZE_MAP.get(symbol, self.LOT_SIZE)
