"""
MT5 Forex Trading Bot - Main Engine
Kết nối MetaTrader 5, tự động giao dịch theo chiến lược
Hỗ trợ MULTI-SYMBOL — mỗi symbol chạy 1 thread riêng
"""

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone
import time
import logging
import threading
from config import BotConfig
from notifier import TelegramNotifier
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


class ColorFormatter(logging.Formatter):
    """Tô màu log theo nội dung dòng log"""

    def format(self, record):
        msg = super().format(record)

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


# ─────────────────────────────────────────────────────────────────────────────
# SymbolWorker — 1 thread cho 1 symbol
# ─────────────────────────────────────────────────────────────────────────────
class SymbolWorker:
    """
    Chạy độc lập trong 1 thread riêng.
    Quản lý toàn bộ vòng đời: chờ nến → quét tín hiệu → vào lệnh → theo dõi.
    """

    def __init__(self, symbol: str, bot: "MT5Bot"):
        self.symbol  = symbol
        self.bot     = bot
        self.cfg     = bot.cfg
        self.tag     = f"[{symbol}]"
        self._thread: threading.Thread | None = None

    # ── Timing ───────────────────────────────────────────────────────────────
    def wait_until_next_candle_scan(self):
        """Chờ đến giây :05 của mốc M5 tiếp theo theo giờ server MT5."""
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            log.warning(f"{self.tag} ⚠️ Không lấy được tick — chờ 5s rồi thử lại")
            time.sleep(5)
            return

        now_mt5 = datetime.fromtimestamp(tick.time, tz=timezone.utc).replace(tzinfo=None)
        seconds_in_cycle = (now_mt5.minute % 5) * 60 + now_mt5.second
        wait = 5 - seconds_in_cycle
        if wait <= 0:
            wait += 300

        next_scan = datetime.fromtimestamp(tick.time + wait, tz=timezone.utc).replace(tzinfo=None)
        log.info(f"{self.tag} ⏳ Chờ {wait:.0f}s — quét lúc {next_scan.strftime('%H:%M:%S')} (giờ MT5)")
        time.sleep(wait)

    # ── Monitor lệnh đang mở ─────────────────────────────────────────────────
    def monitor_position(self, ticket: int):
        """Kiểm tra mỗi 0.05s. Đóng khi TP / SL / Timeout."""
        log.info(f"{self.tag} 👁 Theo dõi ticket #{ticket}")
        account = mt5.account_info()
        leverage = account.leverage if account else 100

        while self.bot.running:
            try:
                positions = mt5.positions_get(ticket=ticket)
                if not positions:
                    log.info(f"{self.tag} 🔒 Ticket #{ticket} đã đóng")
                    return

                pos       = positions[0]
                sym_info  = mt5.symbol_info(pos.symbol)
                contract  = sym_info.trade_contract_size if sym_info else 100000
                margin_used = (pos.price_open * pos.volume * contract) / leverage
                profit      = pos.profit
                profit_pct  = (profit / margin_used * 100) if margin_used > 0 else 0
                open_time   = datetime.fromtimestamp(pos.time, tz=timezone.utc)
                hold_minutes = (datetime.now(timezone.utc) - open_time).total_seconds() / 60

                log.info(
                    f"{self.tag} 📊 #{ticket} | P/L: {profit:.2f} USD "
                    f"({profit_pct:+.1f}%) | Giữ: {hold_minutes:.1f} phút"
                )

                # Chốt lời
                if profit_pct >= self.cfg.TAKE_PROFIT_PCT:
                    log.info(f"{self.tag} 🎯 Chốt lời #{ticket} | Lãi {profit_pct:.1f}%")
                    self.bot.notifier.send(
                        f"🎯 *Chốt lời* `{self.symbol}`\n"
                        f"Ticket: `{ticket}`\n"
                        f"Lãi: `{profit:.2f} USD` (`{profit_pct:.1f}%`)\n"
                        f"Giữ: `{hold_minutes:.1f} phút`"
                    )
                    self.bot.close_position(ticket)
                    return

                # Cắt lỗ
                if profit_pct <= -self.cfg.STOP_LOSS_PCT:
                    log.info(f"{self.tag} 🛑 Cắt lỗ #{ticket} | Lỗ {profit_pct:.1f}%")
                    self.bot.notifier.send(
                        f"🛑 *Cắt lỗ* `{self.symbol}`\n"
                        f"Ticket: `{ticket}`\n"
                        f"Lỗ: `{profit:.2f} USD` (`{profit_pct:.1f}%`)\n"
                        f"Giữ: `{hold_minutes:.1f} phút`"
                    )
                    self.bot.close_position(ticket)
                    return

                # Hết giờ
                if hold_minutes >= self.cfg.MAX_HOLD_MINUTES:
                    log.info(f"{self.tag} ⏰ Hết giờ #{ticket} | {hold_minutes:.1f} phút | P/L: {profit:.2f}")
                    self.bot.notifier.send(
                        f"⏰ *Đóng hết giờ* `{self.symbol}`\n"
                        f"Ticket: `{ticket}`\n"
                        f"P/L: `{profit:.2f} USD` (`{profit_pct:.1f}%`)\n"
                        f"Giữ: `{hold_minutes:.1f} phút`"
                    )
                    self.bot.close_position(ticket)
                    return

                time.sleep(0.05)

            except Exception as e:
                log.error(f"{self.tag} ❌ Lỗi monitor: {e}", exc_info=True)
                time.sleep(0.05)

    # ── Vòng lặp của symbol này ───────────────────────────────────────────────
    def run_loop(self):
        log.info(f"{self.tag} 🚀 Thread khởi động | TF: {self.cfg.TIMEFRAME_STR}")

        while self.bot.running:
            try:
                # 1. Chờ đến giây :05 của nến M5 mới
                self.wait_until_next_candle_scan()
                if not self.bot.running:
                    break

                now = datetime.now()
                log.info(f"{self.tag} 🔍 Quét tín hiệu lúc {now.strftime('%H:%M:%S')}")

                # 2. Lấy dữ liệu nến
                df = self.bot.get_rates(self.symbol, self.cfg.TIMEFRAME)
                if df.empty:
                    log.warning(f"{self.tag} ⚠️ Không lấy được dữ liệu nến")
                    continue

                # 3. Kiểm tra số lệnh đang mở của symbol này
                open_count = self.bot.count_open_positions(self.symbol)
                if open_count >= self.cfg.MAX_POSITIONS:
                    log.info(f"{self.tag} ⏸️ Đang có {open_count} lệnh mở — bỏ qua")
                    continue

                # 4. Giá tick real-time
                tick = self.bot.get_tick(self.symbol)
                tick_price = tick.ask if tick else None

                # 5. Chạy chiến lược
                signal = self.bot.strategy.generate_signal(df, symbol=self.symbol, tick_price=tick_price)

                if signal:
                    account = self.bot.get_account_info()
                    log.info(
                        f"{self.tag} Balance: {account.get('balance', 0):.2f} | "
                        f"Equity: {account.get('equity', 0):.2f}"
                    )
                    trade = self.bot.place_order(
                        symbol=self.symbol,
                        order_type=signal,
                        lot=self.cfg.LOT_SIZE,
                        sl_pips=self.cfg.SL_PIPS,
                        tp_pips=self.cfg.TP_PIPS
                    )
                    if trade:
                        # Block thread này cho đến khi lệnh đóng
                        self.monitor_position(trade["ticket"])
                else:
                    account = self.bot.get_account_info()
                    log.info(
                        f"{self.tag} ⏭️ Không có tín hiệu | "
                        f"Balance: {account.get('balance', 0):.2f} | "
                        f"Equity: {account.get('equity', 0):.2f}"
                    )

            except Exception as e:
                log.error(f"{self.tag} ❌ Lỗi trong vòng lặp: {e}", exc_info=True)
                self.bot.notifier.send(f"⚠️ *Lỗi bot* `{self.symbol}`\n`{str(e)}`")
                time.sleep(30)

        log.info(f"{self.tag} 🛑 Thread dừng")

    def start(self):
        self._thread = threading.Thread(
            target=self.run_loop,
            name=f"worker-{self.symbol}",
            daemon=True
        )
        self._thread.start()

    def join(self):
        if self._thread:
            self._thread.join()


# ─────────────────────────────────────────────────────────────────────────────
# MT5Bot — Engine chính, dùng chung cho mọi symbol
# ─────────────────────────────────────────────────────────────────────────────
class MT5Bot:
    def __init__(self, config: BotConfig):
        self.cfg      = config
        self.notifier = TelegramNotifier(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID)
        self.strategy = Strategy(config)
        self.running  = False
        self._workers: list[SymbolWorker] = []
        # Lock để tránh race condition khi nhiều thread gửi lệnh MT5 cùng lúc
        self._order_lock = threading.Lock()

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

        # Đảm bảo mọi symbol có trong Market Watch
        for sym in self.cfg.SYMBOLS:
            if not mt5.symbol_select(sym, True):
                log.error(f"Không tìm thấy symbol: {sym}")
                return False
            log.info(f"  ✅ Symbol OK: {sym}")

        self.notifier.send(
            f"🤖 *Bot khởi động*\n"
            f"Login: `{info.login}`\n"
            f"Balance: `{info.balance:.2f} {info.currency}`\n"
            f"Symbols: `{', '.join(self.cfg.SYMBOLS)}`\n"
            f"Server: `{info.server}`"
        )
        return True

    def disconnect(self):
        mt5.shutdown()
        log.info("🔌 Đã ngắt kết nối MT5")

    # ── Lấy giá & nến ─────────────────────────────────────────────────────────
    def get_rates(self, symbol: str, timeframe: int, count: int = 200) -> pd.DataFrame:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df

    def get_tick(self, symbol: str):
        return mt5.symbol_info_tick(symbol)

    # ── Đặt lệnh (thread-safe) ────────────────────────────────────────────────
    def place_order(self, symbol: str, order_type: str, lot: float,
                    sl_pips: float = None, tp_pips: float = None,
                    comment: str = "MT5Bot") -> dict | None:

        with self._order_lock:  # Chỉ 1 thread gửi lệnh tại một thời điểm
            tick = self.get_tick(symbol)
            if tick is None:
                log.error(f"[{symbol}] Không lấy được tick")
                return None

            sym_info = mt5.symbol_info(symbol)
            if sym_info is None:
                log.error(f"[{symbol}] Không tìm thấy symbol")
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
                self.notifier.send(f"❌ *Lệnh thất bại*\n`{order_type} {symbol}`\nLỗi: `{err}`")
                return None

            trade_info = {
                "ticket": result.order,
                "symbol": symbol,
                "type":   order_type.upper(),
                "lot":    lot,
                "price":  price,
                "sl":     sl,
                "tp":     tp,
                "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "comment": comment
            }

            log.info(f"[{symbol}] ✅ {order_type} | Lot: {lot} | Price: {price} | SL: {sl} | TP: {tp} | Ticket: {result.order}")
            self.notifier.send(
                f"✅ *Lệnh thành công*\n"
                f"{'🟢 BUY' if order_type=='BUY' else '🔴 SELL'} `{symbol}`\n"
                f"Lot: `{lot}` | Price: `{price}`\n"
                f"SL: `{sl}` | TP: `{tp}`\n"
                f"Ticket: `{result.order}`"
            )
            return trade_info

    # ── Đóng lệnh ─────────────────────────────────────────────────────────────
    def close_position(self, ticket: int) -> bool:
        with self._order_lock:
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                log.warning(f"Không tìm thấy vị thế ticket {ticket}")
                return False

            pos  = positions[0]
            tick = self.get_tick(pos.symbol)

            close_type  = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

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
                log.info(f"[{pos.symbol}] ✅ Đóng ticket {ticket} | P/L: {pos.profit:.2f}")
                self.notifier.send(f"🔒 *Đóng lệnh* `{pos.symbol}`\nTicket: `{ticket}`\nP/L: `{pos.profit:.2f}`")
                return True
            else:
                log.error(f"[{pos.symbol}] ❌ Đóng lệnh thất bại: {result.comment if result else 'None'}")
                return False

    # ── Thông tin hỗ trợ ──────────────────────────────────────────────────────
    def count_open_positions(self, symbol: str = None) -> int:
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return len(positions) if positions else 0

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

    # ── Chạy tất cả symbol ────────────────────────────────────────────────────
    def run(self):
        if not self.connect():
            return

        self.running = True
        symbols_str  = ", ".join(self.cfg.SYMBOLS)
        log.info(f"🚀 Bot khởi động | Symbols: {symbols_str} | TF: {self.cfg.TIMEFRAME_STR}")

        # Tạo và start 1 thread cho mỗi symbol
        self._workers = [SymbolWorker(sym, self) for sym in self.cfg.SYMBOLS]
        for w in self._workers:
            w.start()

        try:
            # Main thread chỉ giữ process sống, Ctrl+C để dừng
            while self.running:
                time.sleep(1)

        except KeyboardInterrupt:
            log.info("⛔ Bot dừng theo yêu cầu")
            self.notifier.send(f"⛔ *Bot đã dừng*\nSymbols: `{symbols_str}`")
        finally:
            self.running = False
            for w in self._workers:
                w.join(timeout=5)
            self.disconnect()


# ── Chạy bot ──────────────────────────────────────────────────────────────────
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
