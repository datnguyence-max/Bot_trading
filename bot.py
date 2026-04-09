"""
MT5 Forex Trading Bot - Main Engine
Kiến trúc 3 khối tách biệt hoàn toàn:

  Khối 1 — ScannerWorker  : chờ nến M5 → quét tín hiệu → vào lệnh → chuyển sang Khối 2
  Khối 2A — PositionPoller : gọi MT5 positions_get() 1 lần/50ms → cache toàn bộ lệnh
  Khối 2B — MonitorWorker  : đọc cache → phản ứng TP/SL/Timeout trong ~50ms

Lợi ích:
  - Scanner không bao giờ bị block bởi monitor
  - N lệnh đồng thời vẫn chỉ tốn 20 MT5 API calls/giây (thay vì N×20)
  - Phản ứng cắt lỗ/chốt lời trong 50ms dù có nhiều lệnh
"""

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone, timedelta
import time
import logging
import threading
from config import BotConfig
from strategy import Strategy

# ─── ANSI Color codes ─────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BLUE   = "\033[94m"
WHITE  = "\033[97m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

# Màu riêng cho từng symbol
SYMBOL_COLORS = {
    "XAUUSD": "\033[93m",   # Vàng
    "BTCUSD": "\033[95m",   # Tím
    "USDJPY": "\033[96m",   # Cyan
}


class ColorFormatter(logging.Formatter):
    """Tô màu log theo nội dung dòng log"""

    def format(self, record):
        msg = super().format(record)

        # Tô màu theo symbol
        for sym, color in SYMBOL_COLORS.items():
            if f"[{sym}]" in msg:
                if not any(k in msg for k in ["📈 BUY", "📉 SELL", "🎯 Chốt", "🛑 Cắt", "❌", "thất bại", "✅"]):
                    return f"{color}{msg}{RESET}"
                break

        if "📈 BUY" in msg or ("✅" in msg and "BUY" in msg):
            return f"{BOLD}{GREEN}{msg}{RESET}"
        if "📉 SELL" in msg or ("✅" in msg and "SELL" in msg):
            return f"{BOLD}{BLUE}{msg}{RESET}"
        if "❌ BỎ QUA" in msg or "Không có tín hiệu" in msg:
            return f"{WHITE}{msg}{RESET}"
        if "🎯 Chốt lời" in msg:
            return f"{BOLD}{GREEN}{msg}{RESET}"
        if "🛑 Cắt lỗ" in msg:
            return f"{BOLD}{RED}{msg}{RESET}"
        if "⏰ Hết giờ" in msg:
            return f"{YELLOW}{msg}{RESET}"
        if record.levelno >= logging.ERROR or "thất bại" in msg:
            return f"{RED}{msg}{RESET}"
        if record.levelno == logging.WARNING or "⚠️" in msg:
            return f"{YELLOW}{msg}{RESET}"
        if "✅ Kết nối" in msg or "🚀 Bot" in msg:
            return f"{CYAN}{msg}{RESET}"

        return msg


# ─── Logging setup ────────────────────────────────────────────────────────────
handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(
    level=logging.INFO,
    handlers=[
        handler,
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# KHỐI 2A — PositionPoller
# Thread singleton: gọi positions_get() 1 lần duy nhất mỗi 50ms
# Cache kết quả → tất cả MonitorWorker đọc từ đây, không đụng MT5 trực tiếp
# ══════════════════════════════════════════════════════════════════════════════
class PositionPoller:
    POLL_INTERVAL = 0.05  # 50ms

    def __init__(self, bot: "MT5Bot"):
        self.bot     = bot
        self._cache: dict[int, object] = {}  # ticket → position object
        self._lock   = threading.Lock()
        self._thread: threading.Thread | None = None

    def get(self, ticket: int):
        """MonitorWorker gọi để lấy position — không đụng MT5."""
        with self._lock:
            return self._cache.get(ticket)

    def _poll_loop(self):
        log.info("🔄 PositionPoller khởi động — poll mỗi 50ms")
        while self.bot.running:
            try:
                with self.bot._mt5_lock:
                    all_pos = mt5.positions_get()
                new_cache = {}
                if all_pos:
                    for p in all_pos:
                        new_cache[p.ticket] = p
                with self._lock:
                    self._cache = new_cache
            except Exception as e:
                log.error(f"PositionPoller lỗi: {e}")
            time.sleep(self.POLL_INTERVAL)

    def start(self):
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="position-poller",
            daemon=True
        )
        self._thread.start()


# ══════════════════════════════════════════════════════════════════════════════
# KHỐI 2B — MonitorWorker
# Mỗi lệnh 1 thread. Đọc từ PositionPoller cache → phản ứng trong ~50ms
# Không gọi MT5 trực tiếp → không gây tải dù có nhiều lệnh đồng thời
# ══════════════════════════════════════════════════════════════════════════════
class MonitorWorker:
    CHECK_INTERVAL = 0.05  # 50ms

    def __init__(self, ticket: int, symbol: str, direction: str,
                 open_t1: float, close_t1: float, candle_t1_time, bot: "MT5Bot"):
        """
        ticket        : MT5 ticket của lệnh
        symbol        : tên symbol
        direction     : "BUY" hoặc "SELL"
        open_t1       : open của nến T-1 (nến tín hiệu)
        close_t1      : close của nến T-1 (nến tín hiệu)
        candle_t1_time: thời gian mở của nến HIỆN TẠI (candle_now) → dùng tính timeout +300s
        """
        self.ticket        = ticket
        self.symbol        = symbol
        self.direction     = direction
        self.open_t1       = open_t1
        self.close_t1      = close_t1
        # SL = giữa thân nến T-1
        if direction == "BUY":
            self.sl_price = open_t1 + (close_t1 - open_t1) * 0.25
        else:
            self.sl_price = open_t1 - (open_t1 - close_t1) * 0.25
        self.candle_now_time = candle_t1_time  # time nến T (đang chạy lúc vào lệnh)
        self.timeout_at    = candle_t1_time + timedelta(seconds=300)  # hết nến T → thoát
        self.bot           = bot
        self.cfg           = bot.cfg
        self.tag           = f"[{symbol}][#{ticket}]"

    def run(self):
        log.info(
            f"{self.tag} 👁 Monitor | {self.direction} | "
            f"SL(mid T-1)={self.sl_price:.5f}"
        )

        while self.bot.running:
            try:
                # Đọc vị thế từ cache — không gọi MT5
                pos = self.bot.poller.get(self.ticket)
                if pos is None:
                    log.info(f"{self.tag} 🔒 Lệnh đã đóng")
                    return

                profit       = pos.profit
                open_time    = datetime.fromtimestamp(pos.time, tz=timezone.utc)
                hold_minutes = (datetime.now(timezone.utc) - open_time).total_seconds() / 60

                # ── Lấy giá tick hiện tại ─────────────────────────────────────
                tick = self.bot.get_tick(self.symbol)
                if tick is None:
                    time.sleep(self.CHECK_INTERVAL)
                    continue

                bid = tick.bid
                ask = tick.ask

                # ── Log mỗi 10s thay vì mỗi 50ms để tránh spam ─────────────
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                if not hasattr(self, "_last_log") or (now_utc - self._last_log).total_seconds() >= 10:
                    log.info(
                        f"{self.tag} 📊 {self.direction} | "
                        f"bid={bid} ask={ask} | "
                        f"SL(mid)={self.sl_price:.5f} | "
                        f"P/L: {profit:+.2f} USD | Giữ: {hold_minutes:.1f}m"
                    )
                    self._last_log = now_utc

                # ── Timeout: hết nến T (nến T+1 mở) → đóng lệnh ────────────
                if now_utc >= self.timeout_at:
                    log.info(f"{self.tag} ⏰ Hết nến T → đóng lệnh | P/L: {profit:+.2f} USD")
                    actual_profit = self.bot.close_position(self.ticket)
                    if actual_profit is not None and actual_profit > 0:
                        self.bot.set_win_cooldown(self.symbol)
                    return

                # ── BUY: bid dưới giữa thân T-1 → cắt lỗ ───────────────────
                if self.direction == "BUY" and bid < self.sl_price:
                    log.info(
                        f"{self.tag} 🛑 BUY cắt lỗ | bid {bid} < SL(mid) {self.sl_price:.5f} | "
                        f"P/L: {profit:+.2f} USD"
                    )
                    self.bot.close_position(self.ticket)
                    # thua — không set cooldown
                    return

                # ── SELL: ask trên giữa thân T-1 → cắt lỗ ──────────────────
                if self.direction == "SELL" and ask > self.sl_price:
                    log.info(
                        f"{self.tag} 🛑 SELL cắt lỗ | ask {ask} > SL(mid) {self.sl_price:.5f} | "
                        f"P/L: {profit:+.2f} USD"
                    )
                    self.bot.close_position(self.ticket)
                    # thua — không set cooldown
                    return

                time.sleep(self.CHECK_INTERVAL)

            except Exception as e:
                log.error(f"{self.tag} ❌ Lỗi monitor: {e}", exc_info=True)
                time.sleep(self.CHECK_INTERVAL)

    def start(self):
        t = threading.Thread(
            target=self.run,
            name=f"monitor-{self.symbol}-{self.ticket}",
            daemon=True
        )
        t.start()

    @classmethod
    def launch(cls, ticket: int, symbol: str, direction: str,
               open_t1: float, close_t1: float, candle_t1_time, bot: "MT5Bot"):
        """Factory: tạo và start MonitorWorker với đầy đủ context tín hiệu."""
        worker = cls(ticket, symbol, direction, open_t1, close_t1, candle_t1_time, bot)
        worker.start()
        return worker


# ══════════════════════════════════════════════════════════════════════════════
# KHỐI 1 — ScannerWorker
# Mỗi symbol 1 thread. Chỉ làm 1 việc: chờ nến → quét → vào lệnh → chuyển Monitor
# Không bao giờ block, không quan tâm đến lệnh đã vào
# ══════════════════════════════════════════════════════════════════════════════
class ScannerWorker:
    def __init__(self, symbol: str, bot: "MT5Bot"):
        self.symbol  = symbol
        self.bot     = bot
        self.cfg     = bot.cfg
        self.tag     = f"[{symbol}]"
        self._thread: threading.Thread | None = None

    def _wait_next_candle(self):
        """Chờ đến giây :05 của mốc M5 tiếp theo theo giờ server MT5."""
        with self.bot._mt5_lock:
            tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            log.warning(f"{self.tag} ⚠️ Không lấy được tick — chờ 5s")
            time.sleep(5)
            return

        # Dùng thời gian thực UTC thay vì tick.time (tránh tick cũ khi market đóng)
        from datetime import timezone as _tz
        now_mt5 = datetime.now(_tz.utc).replace(tzinfo=None)
        seconds_in_cycle = (now_mt5.minute % 5) * 60 + now_mt5.second
        wait = 5 - seconds_in_cycle
        if wait < 0:
            wait += 300
        if wait == 0:
            wait = 300

        next_scan = now_mt5 + __import__('datetime').timedelta(seconds=wait)
        log.info(f"{self.tag} ⏳ Chờ {wait:.0f}s — quét lúc {next_scan.strftime('%H:%M:%S')} (giờ MT5)")
        time.sleep(wait)

    def run_loop(self):
        log.info(f"{self.tag} 🔎 Scanner khởi động | TF: {self.cfg.TIMEFRAME_STR}")

        while self.bot.running:
            try:
                # 1. Chờ đúng thời điểm
                self._wait_next_candle()
                if not self.bot.running:
                    break

                log.info(f"{self.tag} 🔍 Quét tín hiệu lúc {datetime.now().strftime('%H:%M:%S')}")

                # 2. Lấy dữ liệu nến
                df = self.bot.get_rates(self.symbol, self.cfg.TIMEFRAME)
                if df.empty:
                    log.warning(f"{self.tag} ⚠️ Không lấy được dữ liệu nến")
                    continue

                # 3. Không giới hạn số lệnh theo symbol

                # 4. Phân tích tín hiệu
                tick       = self.bot.get_tick(self.symbol)
                tick_price = tick.ask if tick else None
                signal     = self.bot.strategy.generate_signal(df, symbol=self.symbol, tick_price=tick_price)

                if not signal:
                    acc = self.bot.get_account_info()
                    log.info(
                        f"{self.tag} ⏭️ Không có tín hiệu | "
                        f"Balance: {acc.get('balance', 0):.2f} | "
                        f"Equity: {acc.get('equity', 0):.2f}"
                    )
                    continue

                # 4b. Kiểm tra cooldown sau lệnh thắng
                if self.bot.is_in_cooldown(self.symbol):
                    continue

                # 5. Vào lệnh
                acc = self.bot.get_account_info()
                log.info(
                    f"{self.tag} Balance: {acc.get('balance', 0):.2f} | "
                    f"Equity: {acc.get('equity', 0):.2f}"
                )
                trade = self.bot.place_order(
                    symbol=self.symbol,
                    order_type=signal,
                    lot=self.cfg.get_lot(self.symbol),
                    sl_pips=self.cfg.SL_PIPS,
                    tp_pips=self.cfg.TP_PIPS
                )

                # 6. Chuyển sang Khối 2 — Scanner tiếp tục ngay lập tức
                if trade:
                    candle_t   = df.iloc[-2]   # T-1: nến tín hiệu vừa đóng
                    candle_now = df.iloc[-1]   # nến đang hình thành lúc vào lệnh
                    MonitorWorker.launch(
                        ticket         = trade["ticket"],
                        symbol         = self.symbol,
                        direction      = signal,
                        open_t1        = float(candle_t["open"]),
                        close_t1       = float(candle_t["close"]),
                        candle_t1_time = candle_now["time"],  # nến hiện tại → timeout = +300s từ bây giờ
                        bot            = self.bot,
                    )
                    sl_mid = float(candle_t["open"]) + (float(candle_t["close"]) - float(candle_t["open"])) * 0.5
                    log.info(
                        f"{self.tag} ✉️ Chuyển ticket #{trade['ticket']} → Monitor | "
                        f"Open(T-1)={candle_t['open']} Close(T-1)={candle_t['close']} "
                        f"SL(mid)={sl_mid:.5f}"
                    )

            except Exception as e:
                log.error(f"{self.tag} ❌ Lỗi scanner: {e}", exc_info=True)
                time.sleep(30)

        log.info(f"{self.tag} 🛑 Scanner dừng")

    def start(self):
        self._thread = threading.Thread(
            target=self.run_loop,
            name=f"scanner-{self.symbol}",
            daemon=True
        )
        self._thread.start()

    def join(self):
        if self._thread:
            self._thread.join()


# ══════════════════════════════════════════════════════════════════════════════
# MT5Bot — Engine chính
# Kết nối MT5, cung cấp các hàm đặt/đóng lệnh dùng chung cho các khối
# ══════════════════════════════════════════════════════════════════════════════
class MT5Bot:
    def __init__(self, config: BotConfig):
        self.cfg      = config
        self.strategy = Strategy(config)
        self.running  = False
        self._scanners: list[ScannerWorker] = []
        self._order_lock = threading.Lock()  # Thread-safe khi gửi/đóng lệnh
        self._mt5_lock   = threading.Lock()  # Thread-safe cho tất cả MT5 read calls
        self.poller      = PositionPoller(self)  # Cache positions mỗi 50ms
        # ── Cooldown per-symbol sau lệnh thắng ───────────────────────────────
        self._cooldown_lock = threading.Lock()
        self._win_cooldown: dict[str, datetime] = {}  # symbol → cooldown_until

    # ── Cooldown helpers ──────────────────────────────────────────────────────
    WIN_COOLDOWN_SEC = 20 * 60  # 20 phút

    def set_win_cooldown(self, symbol: str):
        """Gọi sau lệnh thắng — block scanner symbol này thêm 20 phút."""
        until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=self.WIN_COOLDOWN_SEC)
        with self._cooldown_lock:
            self._win_cooldown[symbol] = until
        log.info(f"[{symbol}] 🕐 Cooldown 20' sau thắng — scan lại lúc {until.strftime('%H:%M:%S')} UTC")

    def is_in_cooldown(self, symbol: str) -> bool:
        """Trả về True nếu symbol đang trong cooldown."""
        with self._cooldown_lock:
            until = self._win_cooldown.get(symbol)
        if until is None:
            return False
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if now < until:
            remaining = (until - now).total_seconds()
            log.info(f"[{symbol}] ⏸️ Cooldown còn {remaining:.0f}s — bỏ qua tín hiệu")
            return True
        return False

    # ── Kết nối MT5 ───────────────────────────────────────────────────────────
    def connect(self) -> bool:
        if not mt5.initialize(
            login=self.cfg.MT5_LOGIN,
            password=self.cfg.MT5_PASSWORD,
            server=self.cfg.MT5_SERVER
        ):
            log.error(f"Kết nối MT5 thất bại: {mt5.last_error()}")
            return False

        info = mt5.account_info()
        if info is None:
            log.error("Không lấy được thông tin tài khoản")
            return False

        log.info(f"✅ Kết nối thành công | Login: {info.login} | Balance: {info.balance:.2f} {info.currency}")

        for sym in self.cfg.SYMBOLS:
            if not mt5.symbol_select(sym, True):
                log.error(f"Không tìm thấy symbol: {sym}")
                return False
            log.info(f"  ✅ Symbol OK: {sym}")

        return True

    def disconnect(self):
        mt5.shutdown()
        log.info("🔌 Đã ngắt kết nối MT5")

    # ── Lấy dữ liệu ───────────────────────────────────────────────────────────
    def get_rates(self, symbol: str, timeframe: int, count: int = 200) -> pd.DataFrame:
        with self._mt5_lock:
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df

    def get_tick(self, symbol: str):
        with self._mt5_lock:
            return mt5.symbol_info_tick(symbol)

    def get_account_info(self) -> dict:
        info = mt5.account_info()
        if info is None:
            return {}
        return {
            "balance":     info.balance,
            "equity":      info.equity,
            "margin":      info.margin,
            "free_margin": info.margin_free,
            "profit":      info.profit,
            "currency":    info.currency,
            "leverage":    info.leverage
        }

    def count_open_positions(self, symbol: str = None) -> int:
        with self._mt5_lock:
            positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return len(positions) if positions else 0

    # ── Đặt lệnh (thread-safe) ────────────────────────────────────────────────
    def place_order(self, symbol: str, order_type: str, lot: float,
                    sl_pips: float = None, tp_pips: float = None,
                    comment: str = "MT5Bot") -> dict | None:

        with self._order_lock:
            tick     = mt5.symbol_info_tick(symbol)
            sym_info = mt5.symbol_info(symbol)
            if tick is None or sym_info is None:
                log.error(f"[{symbol}] Không lấy được tick/symbol info")
                return None

            point  = sym_info.point
            digits = sym_info.digits

            if order_type.upper() == "BUY":
                price    = tick.ask
                mt5_type = mt5.ORDER_TYPE_BUY
                sl = round(price - sl_pips * point, digits) if sl_pips else 0.0
                tp = round(price + tp_pips * point, digits) if tp_pips else 0.0
            elif order_type.upper() == "SELL":
                price    = tick.bid
                mt5_type = mt5.ORDER_TYPE_SELL
                sl = round(price + sl_pips * point, digits) if sl_pips else 0.0
                tp = round(price - tp_pips * point, digits) if tp_pips else 0.0
            else:
                log.error(f"[{symbol}] Loại lệnh không hợp lệ: {order_type}")
                return None

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       lot,
                "type":         mt5_type,
                "price":        price,
                "sl":           sl,
                "tp":           tp,
                "deviation":    self.cfg.SLIPPAGE,
                "magic":        self.cfg.MAGIC_NUMBER,
                "comment":      comment,
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                err = mt5.last_error() if result is None else result.comment
                log.error(f"[{symbol}] ❌ Lệnh thất bại [{order_type}]: {err}")
                return None

            log.info(f"[{symbol}] ✅ {order_type} | Lot: {lot} | Price: {price} | SL: {sl} | TP: {tp} | Ticket: {result.order}")
            return {
                "ticket":  result.order,
                "symbol":  symbol,
                "type":    order_type.upper(),
                "lot":     lot,
                "price":   price,
                "sl":      sl,
                "tp":      tp,
                "time":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "comment": comment
            }

    # ── Đóng lệnh (thread-safe) ───────────────────────────────────────────────
    def close_position(self, ticket: int) -> "float | None":
        """
        Đóng lệnh theo ticket.
        Trả về profit thực tế (float) nếu thành công, None nếu thất bại.
        """
        with self._order_lock:
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                log.warning(f"Không tìm thấy vị thế ticket #{ticket}")
                return None

            pos           = positions[0]
            actual_profit = pos.profit  # profit tại thời điểm đóng thực tế
            tick          = mt5.symbol_info_tick(pos.symbol)
            close_type    = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            close_price   = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       pos.symbol,
                "volume":       pos.volume,
                "type":         close_type,
                "position":     ticket,
                "price":        close_price,
                "deviation":    self.cfg.SLIPPAGE,
                "magic":        self.cfg.MAGIC_NUMBER,
                "comment":      "bot_close",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"[{pos.symbol}] ✅ Đóng ticket #{ticket} | P/L: {actual_profit:.2f}")
                return actual_profit
            else:
                log.error(f"[{pos.symbol}] ❌ Đóng lệnh thất bại: {result.comment if result else 'None'}")
                return None

    # ── Khởi động tất cả khối ─────────────────────────────────────────────────
    def run(self):
        if not self.connect():
            return

        self.running  = True
        symbols_str   = ", ".join(self.cfg.SYMBOLS)

        log.info(f"🚀 Bot khởi động | Symbols: {symbols_str} | TF: {self.cfg.TIMEFRAME_STR}")
        log.info("━" * 60)
        log.info("  Khối 1  [Scanner×N]    : chờ nến → tín hiệu → vào lệnh")
        log.info("  Khối 2A [PositionPoller]: poll MT5 mỗi 50ms → cache")
        log.info("  Khối 2B [Monitor×M]    : đọc cache → TP/SL/Timeout ~50ms")
        log.info("━" * 60)

        # Khởi động PositionPoller trước (Khối 2A)
        self.poller.start()

        # Khởi động Scanner cho từng symbol (Khối 1)
        self._scanners = [ScannerWorker(sym, self) for sym in self.cfg.SYMBOLS]
        for s in self._scanners:
            s.start()

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("⛔ Bot dừng theo yêu cầu")
        finally:
            self.running = False
            for s in self._scanners:
                s.join(timeout=5)
            self.disconnect()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        from config import BotConfig
        config = BotConfig()
        bot    = MT5Bot(config)
        bot.run()
    except FileNotFoundError as e:
        print(f"\n{e}")
        input("\nNhấn Enter để thoát...")
    except (KeyError, ValueError) as e:
        print(f"\n{e}")
        input("\nNhấn Enter để thoát...")
    except Exception as e:
        import traceback
        print(f"\n❌ Lỗi không xác định:\n{traceback.format_exc()}")
        input("\nNhấn Enter để thoát...")
