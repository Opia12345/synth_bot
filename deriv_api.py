import json
import websockets
from datetime import datetime, timedelta
from threading import Thread, Lock
import uuid
import asyncio
import os
import sqlite3
import sys
import gc
from contextlib import contextmanager
from collections import deque
import logging
from logging.handlers import RotatingFileHandler

# === LOGGING CONFIGURATION ===
LOG_DIR = os.environ.get('LOG_DIR', 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# File handler with rotation
file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'trading_bot.log'),
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(name)-15s | %(funcName)-20s | %(message)s'
))

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(message)s'
))

# Add handlers
root_logger = logging.getLogger()
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Create specialized loggers
logger = logging.getLogger('TradingBot')
trade_logger = logging.getLogger('TradeExecution')
db_logger = logging.getLogger('Database')
api_logger = logging.getLogger('API')

# Suppress werkzeug logging unless error
logging.getLogger('werkzeug').setLevel(logging.ERROR)

import warnings
warnings.filterwarnings('ignore')

from flask import Flask, request, jsonify

# Database setup
DB_PATH = os.environ.get('DB_PATH', 'trades.db')

def init_db():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                timestamp TEXT,
                app_id TEXT,
                status TEXT,
                success INTEGER,
                contract_id TEXT,
                profit REAL,
                final_balance REAL,
                initial_balance REAL,
                error TEXT,
                parameters TEXT,
                volatility REAL,
                growth_rate REAL,
                target_ticks INTEGER,
                exit_reason TEXT,
                max_profit_reached REAL,
                ticks_completed INTEGER,
                duration_seconds REAL,
                entry_spot REAL,
                exit_spot REAL,
                volatility_at_exit REAL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trading_sessions (
                session_date TEXT PRIMARY KEY,
                trades_count INTEGER DEFAULT 0,
                consecutive_losses INTEGER DEFAULT 0,
                total_profit_loss REAL DEFAULT 0,
                stopped INTEGER DEFAULT 0,
                last_updated TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT,
                component TEXT,
                message TEXT,
                details TEXT
            )
        ''')
        conn.commit()
        conn.close()
        db_logger.info("Database initialized successfully")
    except Exception as e:
        db_logger.error(f"Database initialization failed: {e}")

@contextmanager
def get_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        db_logger.error(f"Database error: {e}")
        if conn:
            conn.rollback()
    except Exception as e:
        db_logger.error(f"Unexpected database error: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

def log_system_event(level, component, message, details=None):
    """Log system events to database"""
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO system_logs (timestamp, level, component, message, details)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    datetime.now().isoformat(),
                    level,
                    component,
                    message,
                    json.dumps(details) if details else None
                ))
    except Exception as e:
        db_logger.error(f"Failed to log system event: {e}")

def save_trade(trade_id, trade_data):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO trades 
                    (trade_id, timestamp, app_id, status, success, contract_id, profit, 
                     final_balance, initial_balance, error, parameters, volatility, growth_rate, 
                     target_ticks, exit_reason, max_profit_reached, ticks_completed,
                     duration_seconds, entry_spot, exit_spot, volatility_at_exit)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    trade_id,
                    trade_data.get('timestamp'),
                    trade_data.get('app_id'),
                    trade_data.get('status'),
                    1 if trade_data.get('success') else 0,
                    trade_data.get('contract_id'),
                    trade_data.get('profit'),
                    trade_data.get('final_balance'),
                    trade_data.get('initial_balance'),
                    trade_data.get('error'),
                    json.dumps(trade_data.get('parameters', {})),
                    trade_data.get('volatility'),
                    trade_data.get('growth_rate'),
                    trade_data.get('target_ticks'),
                    trade_data.get('exit_reason'),
                    trade_data.get('max_profit_reached'),
                    trade_data.get('ticks_completed'),
                    trade_data.get('duration_seconds'),
                    trade_data.get('entry_spot'),
                    trade_data.get('exit_spot'),
                    trade_data.get('volatility_at_exit')
                ))
                db_logger.debug(f"Trade {trade_id} saved successfully")
    except Exception as e:
        db_logger.error(f"Failed to save trade {trade_id}: {e}")

def get_trade(trade_id):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM trades WHERE trade_id = ?', (trade_id,))
                row = cursor.fetchone()
                if row:
                    return dict(row)
    except Exception as e:
        db_logger.error(f"Failed to get trade {trade_id}: {e}")
    return None

def get_all_trades():
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM trades ORDER BY timestamp DESC')
                return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        db_logger.error(f"Failed to get all trades: {e}")
    return []

def get_session_data(session_date):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM trading_sessions WHERE session_date = ?', (session_date,))
                row = cursor.fetchone()
                if row:
                    return dict(row)
    except Exception as e:
        db_logger.error(f"Failed to get session data: {e}")
    return None

def update_session_data(session_date, trades_count, consecutive_losses, total_profit_loss, stopped):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO trading_sessions 
                    (session_date, trades_count, consecutive_losses, total_profit_loss, stopped, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (session_date, trades_count, consecutive_losses, total_profit_loss, stopped, 
                      datetime.now().isoformat()))
                db_logger.debug(f"Session data updated: {trades_count} trades, {consecutive_losses} losses")
    except Exception as e:
        db_logger.error(f"Failed to update session data: {e}")

try:
    init_db()
except Exception as e:
    logger.error(f"Database initialization error: {e}")

trade_results = {}
MAX_CONCURRENT_TRADES = 2
active_trades_lock = Lock()
active_trade_count = 0

def can_start_trade():
    global active_trade_count
    try:
        with active_trades_lock:
            if active_trade_count >= MAX_CONCURRENT_TRADES:
                logger.warning(f"Max concurrent trades reached: {active_trade_count}/{MAX_CONCURRENT_TRADES}")
                return False
            active_trade_count += 1
            logger.info(f"Trade slot acquired. Active: {active_trade_count}/{MAX_CONCURRENT_TRADES}")
            return True
    except Exception as e:
        logger.error(f"Error in can_start_trade: {e}")
        return False

def trade_completed():
    global active_trade_count
    try:
        with active_trades_lock:
            active_trade_count = max(0, active_trade_count - 1)
            logger.info(f"Trade slot released. Active: {active_trade_count}/{MAX_CONCURRENT_TRADES}")
    except Exception as e:
        logger.error(f"Error in trade_completed: {e}")

class DerivAccumulatorBot:
    def __init__(self, api_token, app_id, trade_id, parameters):
        self.api_token = api_token
        self.app_id = app_id
        self.trade_id = trade_id
        self.stake_per_trade = parameters.get('stake', 5.0)
        self.symbol = parameters.get('symbol', '1HZ10V')
        self.mode = parameters.get('mode', 'adaptive')
        self.fixed_growth_rate = parameters.get('growth_rate', 0.02)
        self.fixed_target_ticks = parameters.get('target_ticks', 4)
        
        self.max_daily_trades = parameters.get('max_daily_trades', 15)
        self.max_consecutive_losses = parameters.get('max_consecutive_losses', 2)
        self.daily_loss_limit_pct = parameters.get('daily_loss_limit_pct', 0.03)
        
        self.profit_target_pct = parameters.get('profit_target_pct', 0.25)
        self.stop_loss_pct = parameters.get('stop_loss_pct', 0.5)
        self.trailing_stop_pct = parameters.get('trailing_stop_pct', 0.3)
        
        # Enhanced volatility monitoring
        self.volatility_check_interval = parameters.get('volatility_check_interval', 2)
        self.volatility_exit_threshold = parameters.get('volatility_exit_threshold', 1.5)  # More sensitive
        
        self.growth_rate = None
        self.target_ticks = None
        self.volatility = None
        self.initial_volatility = None
        
        self.ws_urls = [
            f"wss://ws.derivws.com/websockets/v3?app_id={app_id}",
            f"wss://wscluster1.deriv.com/websockets/v3?app_id={app_id}",
            f"wss://wscluster2.deriv.com/websockets/v3?app_id={app_id}",
        ]
        self.ws_url = self.ws_urls[0]
        self.ws = None
        self.request_id = 0
        self.account_balance = 0.0
        self.initial_balance = 0.0
        self.symbol_available = False
        self.contract_type = "ACCU"
        
        self.price_history = deque(maxlen=30)
        self.trade_start_time = None
        self.entry_spot = None
        self.exit_spot = None
        
        trade_logger.info(f"Bot initialized - Trade ID: {trade_id}, Symbol: {self.symbol}, Mode: {self.mode}")
        
    def get_next_request_id(self):
        self.request_id += 1
        return self.request_id
        
    async def connect(self, retry_count=0, max_retries=3):
        for ws_url in self.ws_urls:
            self.ws_url = ws_url
            try:
                trade_logger.info(f"Attempting connection to {ws_url}")
                self.ws = await asyncio.wait_for(
                    websockets.connect(self.ws_url, ping_interval=None, close_timeout=5),
                    timeout=15.0
                )
                auth_success = await self.authorize()
                if not auth_success:
                    trade_logger.warning(f"Authorization failed on {ws_url}")
                    try:
                        await self.ws.close()
                    except:
                        pass
                    self.ws = None
                    raise Exception("Authorization failed")
                trade_logger.info(f"Successfully connected and authorized on {ws_url}")
                return True
            except Exception as e:
                trade_logger.error(f"Connection failed to {ws_url}: {e}")
                if self.ws:
                    try:
                        await self.ws.close()
                    except:
                        pass
                    self.ws = None
                continue
        
        if retry_count < max_retries:
            wait_time = 10 * (2 ** retry_count)
            trade_logger.info(f"Retry {retry_count + 1}/{max_retries} in {wait_time}s")
            await asyncio.sleep(wait_time)
            return await self.connect(retry_count + 1, max_retries)
        else:
            trade_logger.error("Failed to connect after all retries")
            raise Exception("Failed to connect after retries")
    
    async def authorize(self):
        if not self.api_token:
            trade_logger.error("No API token provided")
            return False
        
        try:
            auth_request = {
                "authorize": self.api_token,
                "req_id": self.get_next_request_id()
            }
            await self.ws.send(json.dumps(auth_request))
            
            response = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            data = json.loads(response)
            
            if "error" in data:
                trade_logger.error(f"Authorization error: {data['error']}")
                return False
            
            if "authorize" in data:
                self.account_balance = float(data['authorize']['balance'])
                self.initial_balance = self.account_balance
                trade_logger.info(f"Authorized successfully. Balance: ${self.account_balance:.2f}")
                return True
        except Exception as e:
            trade_logger.error(f"Authorization exception: {e}")
        return False
    
    async def get_balance(self):
        try:
            balance_request = {"balance": 1, "subscribe": 0, "req_id": self.get_next_request_id()}
            response_data = await self.send_request(balance_request)
            
            if response_data and "balance" in response_data:
                self.account_balance = float(response_data["balance"]["balance"])
                trade_logger.debug(f"Balance updated: ${self.account_balance:.2f}")
                return self.account_balance
        except Exception as e:
            trade_logger.error(f"Failed to get balance: {e}")
        return self.account_balance
    
    async def analyze_volatility(self, periods=30):
        try:
            ticks_request = {
                "ticks_history": self.symbol,
                "count": periods,
                "end": "latest",
                "style": "ticks",
                "req_id": self.get_next_request_id()
            }
            response = await self.send_request(ticks_request)
            
            if response and "history" in response:
                prices = [float(p) for p in response["history"]["prices"]]
                if len(prices) >= 2:
                    changes = [(prices[i] - prices[i-1]) / prices[i-1] * 100 
                              for i in range(1, len(prices))]
                    
                    mean = sum(changes) / len(changes)
                    variance = sum((c - mean) ** 2 for c in changes) / len(changes)
                    std_dev = (variance ** 0.5)
                    
                    trade_logger.info(f"Historical volatility analyzed: {std_dev:.4f}")
                    return std_dev
        except Exception as e:
            trade_logger.error(f"Volatility analysis failed: {e}")
        return None
    
    def calculate_realtime_volatility(self):
        if len(self.price_history) < 3:
            return None
        
        prices = list(self.price_history)
        changes = [(prices[i] - prices[i-1]) / prices[i-1] * 100 
                  for i in range(1, len(prices))]
        
        mean = sum(changes) / len(changes)
        variance = sum((c - mean) ** 2 for c in changes) / len(changes)
        return (variance ** 0.5)
    
    async def select_optimal_growth_rate(self):
        """Industry-standard adaptive growth rate selection"""
        volatility = await self.analyze_volatility()
        
        if volatility is None:
            trade_logger.warning("Using default growth rate (no volatility data)")
            return 0.02
        
        self.volatility = volatility
        self.initial_volatility = volatility
        
        # FIXED: Adjusted volatility thresholds to more realistic ranges for tick data
        # Typical tick volatility is much smaller than percentage-based volatility
        if volatility < 0.05:
            rate = 0.05  # Very low volatility
            tier = "Very Low"
        elif volatility < 0.10:
            rate = 0.04
            tier = "Low"
        elif volatility < 0.15:
            rate = 0.03
            tier = "Moderate-Low"
        elif volatility < 0.20:
            rate = 0.025
            tier = "Moderate"
        elif volatility < 0.30:
            rate = 0.02
            tier = "Moderate-High"
        elif volatility < 0.50:
            rate = 0.015
            tier = "High"
        else:
            rate = 0.01  # Very high volatility
            tier = "Very High"
        
        trade_logger.info(f"VOLATILITY ANALYSIS - Tier: {tier} | Value: {volatility:.4f} | Selected growth rate: {rate*100:.2f}%")
        return rate

    def calculate_target_ticks(self, growth_rate):
        """Dynamically calculate target ticks based on growth rate"""
        if growth_rate >= 0.045:
            ticks = 3
        elif growth_rate >= 0.035:
            ticks = 4
        elif growth_rate >= 0.025:
            ticks = 5
        elif growth_rate >= 0.018:
            ticks = 6
        elif growth_rate >= 0.012:
            ticks = 7
        else:
            ticks = 9
        
        trade_logger.info(f"Target ticks calculated: {ticks} for growth rate: {growth_rate*100:.2f}%")
        return ticks
    
    async def check_trading_conditions(self):
        today = datetime.now().date().isoformat()
        session = get_session_data(today)
        
        if not session:
            update_session_data(today, 0, 0, 0.0, 0)
            trade_logger.info("New trading session started")
            return True, "New session started"
        
        if session['stopped']:
            trade_logger.warning("Trading stopped for today")
            return False, "Trading stopped for today"
        
        if session['trades_count'] >= self.max_daily_trades:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            trade_logger.warning(f"Max daily trades reached: {self.max_daily_trades}")
            return False, f"Max daily trades reached ({self.max_daily_trades})"
        
        if session['consecutive_losses'] >= self.max_consecutive_losses:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            trade_logger.warning(f"Max consecutive losses reached: {self.max_consecutive_losses}")
            return False, f"Max consecutive losses reached ({self.max_consecutive_losses})"
        
        daily_loss_limit = self.initial_balance * self.daily_loss_limit_pct
        if session['total_profit_loss'] <= -daily_loss_limit:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            trade_logger.warning(f"Daily loss limit reached: ${daily_loss_limit:.2f}")
            return False, f"Daily loss limit reached ({daily_loss_limit:.2f})"
        
        trade_logger.info("Trading conditions check passed")
        return True, "Trading conditions OK"
    
    def update_session_after_trade(self, profit):
        today = datetime.now().date().isoformat()
        session = get_session_data(today) or {
            'trades_count': 0, 
            'consecutive_losses': 0, 
            'total_profit_loss': 0.0,
            'stopped': 0
        }
        
        new_trades_count = session['trades_count'] + 1
        new_total_pl = session['total_profit_loss'] + profit
        
        if profit <= 0:
            new_consecutive_losses = session['consecutive_losses'] + 1
        else:
            new_consecutive_losses = 0
        
        update_session_data(today, new_trades_count, new_consecutive_losses, 
                          new_total_pl, session['stopped'])
        
        trade_logger.info(f"Session updated: Trades={new_trades_count}, Losses={new_consecutive_losses}, P/L=${new_total_pl:.2f}")
    
    async def validate_symbol(self):
        try:
            spec_request = {
                "contracts_for": self.symbol,
                "req_id": self.get_next_request_id()
            }
            response = await self.send_request(spec_request)
            
            if not response or "error" in response:
                trade_logger.error(f"Symbol validation failed for {self.symbol}")
                return False
            
            if "contracts_for" in response:
                contracts = response["contracts_for"].get("available", [])
                has_accu = any(c.get("contract_type") == self.contract_type for c in contracts) 
                if has_accu:
                    self.symbol_available = True
                    trade_logger.info(f"Symbol {self.symbol} validated successfully")
                    return True
        except Exception as e:
            trade_logger.error(f"Symbol validation exception: {e}")
        return False
    
    async def send_request(self, request):
        req_id = self.get_next_request_id()
        request["req_id"] = req_id
        
        try:
            await self.ws.send(json.dumps(request))
            
            while True:
                response_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                data = json.loads(response_text)
                if data.get("req_id") == req_id:
                    return data
                if "subscription" in data:
                    continue
        except Exception as e:
            trade_logger.error(f"Request failed: {e}")
        return None

    async def place_accumulator_trade(self):
        try:
            balance = await self.get_balance()
            if balance < self.stake_per_trade:
                trade_logger.error(f"Insufficient balance: ${balance:.2f} < ${self.stake_per_trade:.2f}")
                return None, "Insufficient balance"
            
            if not self.symbol_available:
                if not await self.validate_symbol():
                    return None, "Symbol validation failed"
            
            if self.mode == 'adaptive':
                self.growth_rate = await self.select_optimal_growth_rate()
                self.target_ticks = self.calculate_target_ticks(self.growth_rate)
            else:
                self.growth_rate = self.fixed_growth_rate
                self.target_ticks = self.fixed_target_ticks
            
            trade_logger.info(f"Placing trade: Growth={self.growth_rate*100:.2f}%, Target ticks={self.target_ticks}")
            
            proposal_request = {
                "proposal": 1,
                "amount": self.stake_per_trade,
                "basis": "stake",
                "contract_type": self.contract_type,
                "currency": "USD",
                "symbol": self.symbol,
                "growth_rate": self.growth_rate
            }
            
            proposal_response = await self.send_request(proposal_request)
            if not proposal_response or "error" in proposal_response:
                trade_logger.error("Proposal failed")
                return None, "Proposal failed"
            
            proposal_id = proposal_response["proposal"]["id"]
            ask_price = proposal_response["proposal"]["ask_price"]
            
            buy_request = {"buy": proposal_id, "price": ask_price}
            buy_response = await self.send_request(buy_request)
            
            if not buy_response or "error" in buy_response:
                trade_logger.error("Buy failed")
                return None, "Buy failed"
            
            contract_id = buy_response["buy"]["contract_id"]
            trade_logger.info(f"Trade placed successfully - Contract ID: {contract_id}")
            return contract_id, None
        except Exception as e:
            trade_logger.error(f"Place trade exception: {e}")
            return None, str(e)
    
    async def monitor_contract(self, contract_id):
        try:
            self.trade_start_time = datetime.now()
            req_id = self.get_next_request_id()
            proposal_request = {
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "subscribe": 1,
                "req_id": req_id
            }
            await self.ws.send(json.dumps(proposal_request))
            
            tick_count = 0
            max_profit = 0
            last_volatility_check = 0
            current_volatility = self.initial_volatility
            exit_reason = "unknown"
            
            profit_target = self.stake_per_trade * self.profit_target_pct
            stop_loss = -self.stake_per_trade * self.stop_loss_pct
            
            trade_logger.info(f"Monitoring started - Contract: {contract_id}")
            
            while True:
                try:
                    response = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                    data = json.loads(response)
                    
                    if "proposal_open_contract" in data:
                        contract = data["proposal_open_contract"]
                        
                        # Capture entry and exit spots
                        if contract.get("entry_spot") and not self.entry_spot:
                            self.entry_spot = float(contract["entry_spot"])
                            trade_logger.info(f"Entry spot: {self.entry_spot}")
                        
                        if contract.get("exit_tick") and not self.exit_spot:
                            self.exit_spot = float(contract["exit_tick"])
                        
                        if contract.get("is_sold") or contract.get("status") == "sold":
                            profit = float(contract.get("profit", 0))
                            duration = (datetime.now() - self.trade_start_time).total_seconds()
                            
                            trade_logger.info(f"Trade closed - Profit: ${profit:.2f}, Duration: {duration:.1f}s, Reason: {exit_reason}")
                            
                            try:
                                forget_request = {
                                    "forget": data.get("subscription", {}).get("id"),
                                    "req_id": self.get_next_request_id()
                                }
                                await self.ws.send(json.dumps(forget_request))
                            except:
                                pass
                            
                            return {
                                "profit": profit,
                                "status": "win" if profit > 0 else "loss",
                                "contract_id": contract_id,
                                "ticks_completed": tick_count,
                                "exit_reason": exit_reason,
                                "max_profit_reached": max_profit,
                                "final_volatility": current_volatility,
                                "duration_seconds": duration,
                                "entry_spot": self.entry_spot,
                                "exit_spot": self.exit_spot
                            }
                        
                        current_profit = float(contract.get("profit", 0))
                        max_profit = max(max_profit, current_profit)
                        
                        if contract.get("current_spot"):
                            self.price_history.append(float(contract["current_spot"]))
                        
                        if contract.get("entry_spot"):
                            tick_count += 1
                            trade_logger.debug(f"Tick {tick_count}: Profit=${current_profit:.2f}, Max=${max_profit:.2f}")
                        
                        # Enhanced volatility monitoring - check every tick
                        if tick_count - last_volatility_check >= self.volatility_check_interval:
                            current_volatility = self.calculate_realtime_volatility()
                            last_volatility_check = tick_count
                            
                            if current_volatility and self.initial_volatility:
                                volatility_ratio = current_volatility / self.initial_volatility
                                
                                # More sensitive volatility exit
                                if volatility_ratio > self.volatility_exit_threshold:
                                    exit_reason = f"volatility_spike_{volatility_ratio:.2f}x"
                                    trade_logger.warning(f"Volatility spike detected: {volatility_ratio:.2f}x - Closing trade")
                                    sell_request = {
                                        "sell": contract_id,
                                        "price": 0.0,
                                        "req_id": self.get_next_request_id()
                                    }
                                    await self.send_request(sell_request)
                                    continue
                        
                        # Profit target
                        if current_profit >= profit_target:
                            exit_reason = "profit_target"
                            trade_logger.info(f"Profit target reached: ${current_profit:.2f}")
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                        
                        # Stop loss
                        if current_profit <= stop_loss:
                            exit_reason = "stop_loss"
                            trade_logger.warning(f"Stop loss triggered: ${current_profit:.2f}")
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                        
                        # Trailing stop
                        if max_profit > 0:
                            trailing_threshold = max_profit * (1 - self.trailing_stop_pct)
                            if current_profit < trailing_threshold:
                                exit_reason = "trailing_stop"
                                trade_logger.info(f"Trailing stop triggered: ${current_profit:.2f} < ${trailing_threshold:.2f}")
                                sell_request = {
                                    "sell": contract_id,
                                    "price": 0.0,
                                    "req_id": self.get_next_request_id()
                                }
                                await self.send_request(sell_request)
                                continue
                        
                        # Target ticks reached
                        if tick_count >= self.target_ticks:
                            exit_reason = "target_ticks"
                            trade_logger.info(f"Target ticks reached: {tick_count}/{self.target_ticks}")
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                            
                    elif "error" in data:
                        trade_logger.error(f"Contract error: {data['error']['message']}")
                        return {
                            "profit": 0, 
                            "status": "error", 
                            "error": data['error']['message'],
                            "exit_reason": "error",
                            "ticks_completed": tick_count,
                            "max_profit_reached": max_profit,
                            "duration_seconds": (datetime.now() - self.trade_start_time).total_seconds()
                        }
                except asyncio.TimeoutError:
                    trade_logger.error("Contract monitoring timeout")
                    return {
                        "profit": 0, 
                        "status": "error", 
                        "error": "Timeout",
                        "exit_reason": "timeout",
                        "ticks_completed": tick_count,
                        "max_profit_reached": max_profit,
                        "duration_seconds": (datetime.now() - self.trade_start_time).total_seconds()
                    }
        except Exception as e:
            trade_logger.error(f"Monitor contract exception: {e}")
            return {
                "profit": 0, 
                "status": "error", 
                "error": str(e),
                "exit_reason": "monitor_failed",
                "ticks_completed": 0,
                "max_profit_reached": 0,
                "duration_seconds": 0
            }
    
    async def execute_trade_async(self):
        try:
            trade_results[self.trade_id] = {'status': 'running'}
            save_trade(self.trade_id, {
                'timestamp': datetime.now().isoformat(),
                'app_id': self.app_id,
                'status': 'running',
                'success': 0,
                'initial_balance': self.initial_balance,
                'parameters': {
                    'stake': self.stake_per_trade,
                    'symbol': self.symbol,
                    'mode': self.mode
                }
            })
            
            log_system_event('INFO', 'TradeExecution', f'Trade {self.trade_id} started', {
                'symbol': self.symbol,
                'stake': self.stake_per_trade,
                'mode': self.mode
            })
            
            await self.connect()
            
            can_trade, reason = await self.check_trading_conditions()
            if not can_trade:
                result = {
                    "success": False,
                    "error": reason,
                    "trade_id": self.trade_id,
                    "timestamp": datetime.now().isoformat(),
                    "status": "completed"
                }
                save_trade(self.trade_id, result)
                trade_results[self.trade_id] = result
                log_system_event('WARNING', 'TradeExecution', f'Trade {self.trade_id} rejected', {'reason': reason})
                return result
            
            if not await self.validate_symbol():
                result = {
                    "success": False,
                    "error": "Symbol validation failed",
                    "trade_id": self.trade_id,
                    "timestamp": datetime.now().isoformat(),
                    "status": "completed"
                }
                save_trade(self.trade_id, result)
                trade_results[self.trade_id] = result
                log_system_event('ERROR', 'TradeExecution', f'Symbol validation failed for {self.trade_id}')
                return result
            
            contract_id, error = await self.place_accumulator_trade()
            if error:
                result = {
                    "success": False,
                    "error": error,
                    "trade_id": self.trade_id,
                    "timestamp": datetime.now().isoformat(),
                    "status": "completed"
                }
                save_trade(self.trade_id, result)
                trade_results[self.trade_id] = result
                log_system_event('ERROR', 'TradeExecution', f'Trade placement failed for {self.trade_id}', {'error': error})
                return result
            
            monitor_result = await self.monitor_contract(contract_id)
            balance = await self.get_balance()
            
            self.update_session_after_trade(monitor_result.get("profit", 0))
            
            result = {
                "success": True,
                "trade_id": self.trade_id,
                "contract_id": contract_id,
                "profit": monitor_result.get("profit", 0),
                "status": "completed",
                "final_balance": balance,
                "initial_balance": self.initial_balance,
                "timestamp": datetime.now().isoformat(),
                "app_id": self.app_id,
                "volatility": self.initial_volatility,
                "growth_rate": self.growth_rate,
                "target_ticks": self.target_ticks,
                "exit_reason": monitor_result.get("exit_reason"),
                "max_profit_reached": monitor_result.get("max_profit_reached"),
                "ticks_completed": monitor_result.get("ticks_completed"),
                "duration_seconds": monitor_result.get("duration_seconds"),
                "entry_spot": monitor_result.get("entry_spot"),
                "exit_spot": monitor_result.get("exit_spot"),
                "volatility_at_exit": monitor_result.get("final_volatility"),
                "parameters": {
                    'stake': self.stake_per_trade,
                    'symbol': self.symbol,
                    'mode': self.mode,
                    'growth_rate_used': self.growth_rate,
                    'target_ticks_used': self.target_ticks,
                    'initial_volatility': self.initial_volatility,
                    'final_volatility': monitor_result.get("final_volatility")
                }
            }
            save_trade(self.trade_id, result)
            trade_results[self.trade_id] = result
            
            log_system_event('INFO', 'TradeExecution', f'Trade {self.trade_id} completed', {
                'profit': monitor_result.get("profit", 0),
                'exit_reason': monitor_result.get("exit_reason"),
                'duration': monitor_result.get("duration_seconds")
            })
            
            return result
        except Exception as e:
            trade_logger.error(f"Execute trade exception for {self.trade_id}: {e}")
            result = {
                "success": False,
                "error": str(e),
                "trade_id": self.trade_id,
                "timestamp": datetime.now().isoformat(),
                "status": "completed"
            }
            save_trade(self.trade_id, result)
            trade_results[self.trade_id] = result
            log_system_event('ERROR', 'TradeExecution', f'Trade {self.trade_id} failed', {'error': str(e)})
            return result
        finally:
            if self.ws:
                try:
                    await self.ws.close()
                except:
                    pass
            gc.collect()


def run_async_trade_in_thread(api_token, app_id, parameters, trade_id):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        bot = DerivAccumulatorBot(api_token, app_id, trade_id, parameters)
        loop.run_until_complete(bot.execute_trade_async())
        loop.close()
    except Exception as e:
        logger.error(f"Thread execution error for {trade_id}: {e}")
    finally:
        trade_completed()
        gc.collect()


app = Flask(__name__)
app.logger.disabled = True

@app.route('/trade/<app_id>/<api_token>', methods=['POST'])
def execute_trade(app_id, api_token):
    try:
        if not can_start_trade():
            api_logger.warning("Trade rejected - max concurrent trades reached")
            return jsonify({
                "success": False, 
                "error": "Too many concurrent trades",
                "max_concurrent": MAX_CONCURRENT_TRADES
            }), 429
        
        data = request.get_json(silent=True) or {}
        
        parameters = {
            'stake': float(data.get('stake', 5.0)),
            'symbol': data.get('symbol', '1HZ10V'),
            'mode': data.get('mode', 'adaptive'),
            'growth_rate': float(data.get('growth_rate', 0.02)),
            'target_ticks': int(data.get('target_ticks', 4)),
            'max_daily_trades': int(data.get('max_daily_trades', 15)),
            'max_consecutive_losses': int(data.get('max_consecutive_losses', 3)),
            'daily_loss_limit_pct': float(data.get('daily_loss_limit_pct', 0.03)),
            'profit_target_pct': float(data.get('profit_target_pct', 0.25)),
            'stop_loss_pct': float(data.get('stop_loss_pct', 0.5)),
            'trailing_stop_pct': float(data.get('trailing_stop_pct', 0.3)),
            'volatility_check_interval': int(data.get('volatility_check_interval', 2)),
            'volatility_exit_threshold': float(data.get('volatility_exit_threshold', 1.5))
        }
        
        new_trade_id = str(uuid.uuid4())
        initial_data = {
            "status": "pending", 
            "timestamp": datetime.now().isoformat(),
            "app_id": app_id,
            "parameters": parameters,
            "success": 0
        }
        save_trade(new_trade_id, initial_data)
        trade_results[new_trade_id] = initial_data
        
        api_logger.info(f"Trade initiated - ID: {new_trade_id}, Symbol: {parameters['symbol']}")

        thread = Thread(
            target=run_async_trade_in_thread,
            args=(api_token, app_id, parameters, new_trade_id)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({"status": "initiated", "trade_id": new_trade_id}), 202
    except Exception as e:
        api_logger.error(f"Trade execution endpoint error: {e}")
        trade_completed()
        return jsonify({"success": False, "error": "Internal Server Error"}), 500


@app.route('/trade/<trade_id>', methods=['GET'])
def get_trade_result(trade_id):
    result = trade_results.get(trade_id) or get_trade(trade_id)
    
    if not result:
        return jsonify({"success": False, "error": "Trade ID not found"}), 404
    
    response = {
        "trade_id": trade_id,
        "status": result.get('status', 'unknown'),
        "timestamp": result.get('timestamp')
    }
    
    if result.get('success') is not None or result.get('success') == 1:
        profit = result.get('profit', 0)
        response.update({
            "success": bool(result.get('success')),
            "profit_loss": profit,
            "result": "PROFIT" if profit > 0 else "LOSS",
            "amount": abs(profit),
            "contract_id": result.get('contract_id'),
            "final_balance": result.get('final_balance'),
            "initial_balance": result.get('initial_balance'),
            "volatility": result.get('volatility'),
            "volatility_at_exit": result.get('volatility_at_exit'),
            "growth_rate": result.get('growth_rate'),
            "target_ticks": result.get('target_ticks'),
            "exit_reason": result.get('exit_reason'),
            "max_profit_reached": result.get('max_profit_reached'),
            "ticks_completed": result.get('ticks_completed'),
            "duration_seconds": result.get('duration_seconds'),
            "entry_spot": result.get('entry_spot'),
            "exit_spot": result.get('exit_spot'),
            "error_details": result.get('error')
        })
    
    return jsonify(response), 200


@app.route('/session', methods=['GET'])
def get_session_status():
    today = datetime.now().date().isoformat()
    session = get_session_data(today)
    
    if not session:
        return jsonify({
            "session_date": today,
            "trades_count": 0,
            "consecutive_losses": 0,
            "total_profit_loss": 0.0,
            "stopped": False,
            "message": "No trades today"
        }), 200
    
    return jsonify({
        "session_date": today,
        "trades_count": session['trades_count'],
        "consecutive_losses": session['consecutive_losses'],
        "total_profit_loss": round(session['total_profit_loss'], 2),
        "stopped": bool(session['stopped']),
        "can_trade": not bool(session['stopped']),
        "last_updated": session.get('last_updated')
    }), 200


@app.route('/session/reset', methods=['POST'])
def reset_session():
    today = datetime.now().date().isoformat()
    update_session_data(today, 0, 0, 0.0, 0)
    api_logger.info("Trading session reset")
    log_system_event('INFO', 'SessionManagement', 'Session reset performed', {'date': today})
    return jsonify({"message": "Session reset successfully", "date": today}), 200


@app.route('/trades', methods=['GET'])
def get_all_trades_endpoint():
    all_trades = get_all_trades()
    filter_by = request.args.get('filter', 'today')
    
    from datetime import date
    today = date.today()
    
    if filter_by == 'today':
        filtered_trades = [t for t in all_trades if t.get('timestamp', '').startswith(today.isoformat())]
    elif filter_by == 'all':
        filtered_trades = all_trades
    else:
        filtered_trades = [t for t in all_trades if t.get('timestamp', '').startswith(today.isoformat())]
    
    # Show ALL trades (including running and pending)
    all_status_trades = filtered_trades
    completed = [t for t in filtered_trades if t.get('status') == 'completed']
    running = [t for t in filtered_trades if t.get('status') == 'running']
    pending = [t for t in filtered_trades if t.get('status') == 'pending']
    
    # Calculate wins and losses from completed trades
    wins = [t for t in completed if t.get('profit', 0) > 0]
    losses = [t for t in completed if t.get('profit', 0) <= 0]
    total_profit = sum(t.get('profit', 0) for t in completed)
    
    # Calculate averages
    avg_volatility = sum(t.get('volatility', 0) or 0 for t in completed) / len(completed) if completed else 0
    avg_growth_rate = sum(t.get('growth_rate', 0) or 0 for t in completed) / len(completed) if completed else 0
    avg_duration = sum(t.get('duration_seconds', 0) or 0 for t in completed) / len(completed) if completed else 0
    
    # Exit reasons analysis
    exit_reasons = {}
    for t in completed:
        reason = t.get('exit_reason', 'unknown')
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    
    # Convert trades to dictionary format
    trades_dict = {}
    for t in all_status_trades:
        trade_dict = dict(t)
        trades_dict[trade_dict['trade_id']] = trade_dict
    
    # Calculate win rate
    if len(completed) > 0:
        win_rate = f"{(len(wins)/len(completed)*100):.2f}%"
    else:
        win_rate = "0%"
    
    # Risk metrics
    if losses:
        avg_loss = sum(abs(t.get('profit', 0)) for t in losses) / len(losses)
        max_loss = max(abs(t.get('profit', 0)) for t in losses)
    else:
        avg_loss = 0
        max_loss = 0
    
    if wins:
        avg_win = sum(t.get('profit', 0) for t in wins) / len(wins)
        max_win = max(t.get('profit', 0) for t in wins)
    else:
        avg_win = 0
        max_win = 0

    return jsonify({
        "filter": filter_by,
        "date": today.isoformat(),
        "total_trades": len(all_status_trades),
        "completed_trades": len(completed),
        "running_trades": len(running),
        "pending_trades": len(pending),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_profit_loss": round(total_profit, 2),
        "avg_volatility": round(avg_volatility, 4),
        "avg_growth_rate": round(avg_growth_rate, 4),
        "avg_duration_seconds": round(avg_duration, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_win": round(max_win, 2),
        "max_loss": round(max_loss, 2),
        "profit_factor": round(avg_win / avg_loss, 2) if avg_loss > 0 else 0,
        "exit_reasons": exit_reasons,
        "trades": trades_dict
    }), 200

@app.route('/trades/summary', methods=['GET'])
def get_trades_summary():
    all_trades = get_all_trades()
    completed = [t for t in all_trades if t.get('status') == 'completed']
    
    if not completed:
        return jsonify({"message": "No completed trades yet", "total_trades": len(all_trades)}), 200
    
    total_profit = sum(t.get('profit', 0) for t in completed)
    wins = [t for t in completed if t.get('profit', 0) > 0]
    losses = [t for t in completed if t.get('profit', 0) <= 0]
    
    win_amounts = [t.get('profit', 0) for t in wins]
    loss_amounts = [abs(t.get('profit', 0)) for t in losses]
    
    exit_reasons = {}
    for t in completed:
        reason = t.get('exit_reason', 'unknown')
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    
    # Additional metrics
    durations = [t.get('duration_seconds', 0) for t in completed if t.get('duration_seconds')]
    avg_duration = sum(durations) / len(durations) if durations else 0
    
    return jsonify({
        "summary": {
            "total_trades": len(completed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": f"{(len(wins)/len(completed)*100):.2f}%",
            "total_profit_loss": round(total_profit, 2),
            "avg_trade_duration": round(avg_duration, 2)
        },
        "profit_stats": {
            "average_win": round(sum(win_amounts)/len(win_amounts), 2) if win_amounts else 0,
            "average_loss": round(sum(loss_amounts)/len(loss_amounts), 2) if loss_amounts else 0,
            "largest_win": round(max(win_amounts), 2) if win_amounts else 0,
            "largest_loss": round(max(loss_amounts), 2) if loss_amounts else 0,
            "profit_factor": round((sum(win_amounts)/sum(loss_amounts)), 2) if loss_amounts and sum(loss_amounts) > 0 else 0
        },
        "exit_reasons": exit_reasons,
        "recent_trades": [
            {
                "trade_id": t['trade_id'],
                "timestamp": t.get('timestamp'),
                "profit": t.get('profit', 0),
                "result": "WIN" if t.get('profit', 0) > 0 else "LOSS",
                "contract_id": t.get('contract_id'),
                "volatility": t.get('volatility'),
                "growth_rate": t.get('growth_rate'),
                "exit_reason": t.get('exit_reason'),
                "ticks_completed": t.get('ticks_completed'),
                "duration_seconds": t.get('duration_seconds')
            }
            for t in sorted(completed, key=lambda x: x.get('timestamp', ''), reverse=True)[:10]
        ]
    }), 200

@app.route('/trades/export', methods=['GET'])
def export_trades():
    all_trades = get_all_trades()
    filter_by = request.args.get('filter', 'today')
    
    from datetime import date
    today = date.today()
    
    if filter_by == 'today':
        trades_to_export = [t for t in all_trades if t.get('timestamp', '').startswith(today.isoformat())]
    else:
        trades_to_export = all_trades
    
    if not trades_to_export:
        return jsonify({"message": "No trades to export"}), 200
    
    csv_lines = ["Trade_ID,Timestamp,Status,Contract_ID,Profit_Loss,Result,Initial_Balance,Final_Balance,Volatility,Volatility_Exit,Growth_Rate,Target_Ticks,Exit_Reason,Ticks_Completed,Max_Profit,Duration_Seconds,Entry_Spot,Exit_Spot"]
    
    for trade in sorted(trades_to_export, key=lambda x: x.get('timestamp', '')):
        profit = trade.get('profit', 0) or 0
        csv_lines.append(
            f"{trade['trade_id']},"
            f"{trade.get('timestamp', 'N/A')},"
            f"{trade.get('status', 'N/A')},"
            f"{trade.get('contract_id', 'N/A')},"
            f"{profit:.2f},"
            f"{'WIN' if profit > 0 else 'LOSS' if trade.get('status') == 'completed' else 'PENDING'},"
            f"{trade.get('initial_balance', 'N/A')},"
            f"{trade.get('final_balance', 'N/A')},"
            f"{trade.get('volatility', 'N/A')},"
            f"{trade.get('volatility_at_exit', 'N/A')},"
            f"{trade.get('growth_rate', 'N/A')},"
            f"{trade.get('target_ticks', 'N/A')},"
            f"{trade.get('exit_reason', 'N/A')},"
            f"{trade.get('ticks_completed', 'N/A')},"
            f"{trade.get('max_profit_reached', 'N/A')},"
            f"{trade.get('duration_seconds', 'N/A')},"
            f"{trade.get('entry_spot', 'N/A')},"
            f"{trade.get('exit_spot', 'N/A')}"
        )
    
    return "\n".join(csv_lines), 200, {
        'Content-Type': 'text/csv',
        'Content-Disposition': f'attachment; filename=trades_{filter_by}_{today.isoformat()}.csv'
    }

@app.route('/logs', methods=['GET'])
def get_logs():
    """Retrieve system logs"""
    limit = request.args.get('limit', 100, type=int)
    level = request.args.get('level', None)
    
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                if level:
                    cursor.execute('SELECT * FROM system_logs WHERE level = ? ORDER BY timestamp DESC LIMIT ?', (level.upper(), limit))
                else:
                    cursor.execute('SELECT * FROM system_logs ORDER BY timestamp DESC LIMIT ?', (limit,))
                
                logs = [dict(row) for row in cursor.fetchall()]
                return jsonify({"logs": logs, "count": len(logs)}), 200
    except Exception as e:
        api_logger.error(f"Failed to retrieve logs: {e}")
        return jsonify({"error": "Failed to retrieve logs"}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "Deriv Accumulator Trading Bot - Industry Standard v3.0",
        "timestamp": datetime.now().isoformat(),
        "active_trades": active_trade_count,
        "max_concurrent": MAX_CONCURRENT_TRADES,
        "features": [
            "Enhanced volatility monitoring (1.5x threshold)",
            "Real-time adaptive exit strategies",
            "Comprehensive logging system",
            "Trailing stop loss",
            "Dynamic growth rate selection (1-5%)",
            "Industry-standard risk management",
            "Complete trade analytics"
        ],
        "logging_enabled": True,
        "log_location": LOG_DIR
    }), 200


@app.route('/config/optimal', methods=['GET'])
def get_optimal_config():
    return jsonify({
        "recommended_setup": {
            "description": "Industry-standard setup with enhanced volatility monitoring",
            "parameters": {
                "stake": 5.0,
                "symbol": "1HZ10V",
                "mode": "adaptive",
                "growth_rate": 0.02,
                "target_ticks": 4,
                "max_daily_trades": 15,
                "max_consecutive_losses": 3,
                "daily_loss_limit_pct": 0.03,
                "profit_target_pct": 0.25,
                "stop_loss_pct": 0.5,
                "trailing_stop_pct": 0.3,
                "volatility_check_interval": 2,
                "volatility_exit_threshold": 1.5
            },
            "notes": [
                "Volatility threshold reduced to 1.5x for quicker exits",
                "Check interval reduced to 2 ticks for faster response",
                "Adaptive mode automatically selects 1-5% growth rate",
                "Growth rate varies based on market volatility"
            ]
        },
        "conservative_setup": {
            "description": "Lower risk with smaller stakes and tighter controls",
            "parameters": {
                "stake": 3.0,
                "symbol": "1HZ10V",
                "mode": "adaptive",
                "max_daily_trades": 10,
                "max_consecutive_losses": 2,
                "daily_loss_limit_pct": 0.02,
                "profit_target_pct": 0.20,
                "stop_loss_pct": 0.4,
                "trailing_stop_pct": 0.25,
                "volatility_exit_threshold": 1.3
            }
        },
        "aggressive_setup": {
            "description": "Higher risk with potential for larger gains",
            "parameters": {
                "stake": 10.0,
                "symbol": "1HZ25V",
                "mode": "adaptive",
                "max_daily_trades": 20,
                "max_consecutive_losses": 4,
                "daily_loss_limit_pct": 0.05,
                "profit_target_pct": 0.35,
                "stop_loss_pct": 0.6,
                "trailing_stop_pct": 0.35,
                "volatility_exit_threshold": 1.8
            }
        },
        "growth_rate_info": {
            "adaptive_mode": "Automatically selects from 1% to 5% based on volatility",
            "volatility_ranges": {
                "<0.15": "5.0% growth rate",
                "0.15-0.30": "4.0% growth rate",
                "0.30-0.50": "3.0% growth rate",
                "0.50-0.70": "2.5% growth rate",
                "0.70-1.00": "2.0% growth rate",
                "1.00-1.50": "1.5% growth rate",
                ">1.50": "1.0% growth rate"
            },
            "recommendation": "Use adaptive mode for best results. System automatically adjusts growth rate based on real-time market conditions."
        },
        "usage": "POST /trade/<app_id>/<api_token> with JSON body containing any of these parameters"
    }), 200


@app.route('/dashboard', methods=['GET'])
def dashboard():
    html = """<!DOCTYPE html>
<html>
<head>
    <title>Trading Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; min-height: 100vh; padding: 20px; }
        .container { max-width: 1600px; margin: 0 auto; }
        .header { background: white; padding: 30px; border-radius: 12px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
        .header h1 { color: #2d3748; font-size: 32px; margin-bottom: 10px; }
        .header p { color: #718096; font-size: 16px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .stat-card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); transition: all 0.3s; }
        .stat-card:hover { transform: translateY(-5px); box-shadow: 0 10px 20px rgba(0,0,0,0.2); }
        .stat-label { font-size: 12px; color: #718096; margin-bottom: 8px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; }
        .stat-value { font-size: 28px; font-weight: bold; color: #2d3748; }
        .stat-value.positive { color: #48bb78; }
        .stat-value.negative { color: #f56565; }
        .stat-value.neutral { color: #4299e1; }
        .session-card { background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 20px; }
        .session-status { display: inline-block; padding: 8px 20px; border-radius: 20px; font-weight: 600; font-size: 14px; }
        .session-active { background: #48bb78; color: white; }
        .session-stopped { background: #f56565; color: white; }
        .controls { background: white; padding: 20px; border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
        .btn { padding: 12px 24px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; transition: all 0.2s; }
        .btn-primary { background: #667eea; color: white; }
        .btn-primary:hover { background: #5a67d8; transform: translateY(-2px); box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4); }
        .btn-success { background: #48bb78; color: white; }
        .btn-success:hover { background: #38a169; transform: translateY(-2px); box-shadow: 0 4px 12px rgba(72, 187, 120, 0.4); }
        .btn-info { background: #4299e1; color: white; }
        .btn-info:hover { background: #3182ce; transform: translateY(-2px); }
        .select { padding: 12px; border-radius: 8px; border: 2px solid #e2e8f0; font-size: 14px; cursor: pointer; background: white; }
        .trades-table { background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); overflow-x: auto; }
        .trades-table h2 { color: #2d3748; margin-bottom: 20px; font-size: 24px; }
        table { width: 100%; border-collapse: collapse; min-width: 1200px; }
        th { background: #4a5568; color: white; padding: 14px; text-align: left; font-weight: 600; font-size: 12px; text-transform: uppercase; position: sticky; top: 0; }
        td { padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 13px; }
        tr:hover { background: #f7fafc; }
        .win { color: #48bb78; font-weight: 700; }
        .loss { color: #f56565; font-weight: 700; }
        .running { color: #4299e1; font-weight: 700; }
        .badge { display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 11px; font-weight: 600; }
        .badge-success { background: #c6f6d5; color: #22543d; }
        .badge-danger { background: #fed7d7; color: #742a2a; }
        .badge-info { background: #bee3f8; color: #2c5282; }
        .badge-warning { background: #feebc8; color: #7c2d12; }
        .exit-reasons { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 15px; margin-bottom: 20px; }
        .reason-badge { padding: 8px 16px; background: #edf2f7; border-radius: 8px; font-size: 12px; font-weight: 600; }
        .no-data { text-align: center; padding: 40px; color: #718096; font-size: 16px; }
        .metric-highlight { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 3px 8px; border-radius: 4px; font-size: 13px; font-weight: 700; }
        .status-running { animation: pulse 2s infinite; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 Trading Dashboard</h1>
            <p>Real-time monitoring with adaptive volatility detection, smart exit strategies & comprehensive analytics</p>
            <p id="currentDate" style="margin-top: 12px; font-size: 15px; color: #4a5568;"></p>
        </div>
        
        <div class="session-card" id="sessionCard">
            <h3 style="margin-bottom: 15px; color: #2d3748;">📊 Today's Session Status</h3>
            <div id="sessionStatus">Loading...</div>
        </div>
        
        <div class="controls">
            <button class="btn btn-primary" onclick="loadData()">🔄 Refresh Data</button>
            <button class="btn btn-success" onclick="exportCSV()">📥 Export CSV</button>
            <button class="btn btn-info" onclick="showOptimalConfig()">⚙️ View Config</button>
            <button class="btn btn-info" onclick="showLogs()">📋 View Logs</button>
            <select id="filterSelect" onchange="changeFilter()" class="select">
                <option value="today">Today's Trades</option>
                <option value="all">All Trades</option>
            </select>
            <div style="margin-left: auto; font-size: 13px; color: #4a5568;">
                <strong>Auto-refresh:</strong> <span id="countdown">10</span>s
            </div>
        </div>
        
        <div class="stats-grid" id="stats">
            <div class="stat-card"><div class="no-data">Loading stats...</div></div>
        </div>
        
        <div class="trades-table">
            <h2>📊 Complete Trade History</h2>
            <div id="exitReasons" class="exit-reasons"></div>
            <div style="max-height: 600px; overflow-y: auto;">
                <table id="tradesTable">
                    <thead>
                        <tr>
                            <th>Status</th>
                            <th>Timestamp</th>
                            <th>Trade ID</th>
                            <th>Contract</th>
                            <th>Initial Vol</th>
                            <th>Exit Vol</th>
                            <th>Growth %</th>
                            <th>Ticks</th>
                            <th>Duration</th>
                            <th>Exit Reason</th>
                            <th>Max P/L</th>
                            <th>Final P/L</th>
                            <th>Result</th>
                        </tr>
                    </thead>
                    <tbody id="tradesBody">
                        <tr><td colspan="13" class="no-data">Loading trades...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    <script>
        let currentFilter = 'today';
        let countdownTimer = 10;
        
        function changeFilter() {
            currentFilter = document.getElementById('filterSelect').value;
            loadData();
        }
        
        async function loadSession() {
            try {
                const response = await fetch('/session');
                const data = await response.json();
                
                const statusClass = data.stopped ? 'session-stopped' : 'session-active';
                const statusText = data.stopped ? '🔴 STOPPED' : '🟢 ACTIVE';
                
                const sessionHtml = `
                    <p style="margin-bottom: 15px;"><span class="session-status ${statusClass}">${statusText}</span></p>
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
                        <div><strong style="color: #718096;">Trades Today:</strong> <span style="font-size: 24px; font-weight: bold; color: #2d3748;">${data.trades_count}</span></div>
                        <div><strong style="color: #718096;">Consecutive Losses:</strong> <span style="font-size: 24px; font-weight: bold; color: ${data.consecutive_losses >= 2 ? '#f56565' : '#2d3748'};">${data.consecutive_losses}</span></div>
                        <div><strong style="color: #718096;">Total P/L:</strong> <span style="font-size: 24px; font-weight: bold;" class="${data.total_profit_loss >= 0 ? 'win' : 'loss'}">${data.total_profit_loss.toFixed(2)}</span></div>
                    </div>
                `;
                document.getElementById('sessionStatus').innerHTML = sessionHtml;
            } catch (error) {
                console.error('Error loading session:', error);
                document.getElementById('sessionStatus').innerHTML = '<div class="no-data">Error loading session data</div>';
            }
        }
        
        async function loadData() {
            try {
                const response = await fetch(`/trades?filter=${currentFilter}`);
                if (!response.ok) {
                    throw new Error('Failed to fetch trades');
                }
                const data = await response.json();
                
                const dateStr = new Date(data.date).toLocaleDateString('en-US', { 
                    weekday: 'long', 
                    year: 'numeric', 
                    month: 'long', 
                    day: 'numeric' 
                });
                document.getElementById('currentDate').textContent = 
                    currentFilter === 'today' ? `📅 ${dateStr}` : '📅 All Time';
                
                const statsHtml = `
                    <div class="stat-card">
                        <div class="stat-label">Total Trades</div>
                        <div class="stat-value">${data.total_trades || 0}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Completed</div>
                        <div class="stat-value neutral">${data.completed_trades || 0}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Wins</div>
                        <div class="stat-value positive">${data.wins || 0}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Losses</div>
                        <div class="stat-value negative">${data.losses || 0}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Win Rate</div>
                        <div class="stat-value">${data.win_rate || '0%'}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Total P/L</div>
                        <div class="stat-value ${(data.total_profit_loss || 0) >= 0 ? 'positive' : 'negative'}">
                            ${(data.total_profit_loss || 0).toFixed(2)}
                        </div>
                    </div>
                `;
                document.getElementById('stats').innerHTML = statsHtml;
                
                // Display exit reasons
                if (data.exit_reasons && Object.keys(data.exit_reasons).length > 0) {
                    const reasonsHtml = '<strong style="display: block; margin-bottom: 10px; color: #2d3748;">📈 Exit Reasons Distribution:</strong>' + 
                        Object.entries(data.exit_reasons)
                            .sort((a, b) => b[1] - a[1])
                            .map(([reason, count]) => 
                                `<div class="reason-badge">${reason.replace(/_/g, ' ')}: <span class="metric-highlight">${count}</span></div>`
                            ).join('');
                    document.getElementById('exitReasons').innerHTML = reasonsHtml;
                } else {
                    document.getElementById('exitReasons').innerHTML = '';
                }
                
                if (!data.trades || Object.keys(data.trades).length === 0) {
                    document.getElementById('tradesBody').innerHTML = 
                        '<tr><td colspan="13" class="no-data">No trades yet. Start trading to see results here.</td></tr>';
                    return;
                }
                
                // Show ALL trades including running and pending
                const trades = Object.entries(data.trades)
                    .sort((a, b) => new Date(b[1].timestamp) - new Date(a[1].timestamp));
                
                const tradesHtml = trades.map(([id, trade]) => {
                    const profit = trade.profit || 0;
                    const status = trade.status || 'unknown';
                    
                    let statusBadge = '';
                    let resultClass = '';
                    let resultText = '';
                    let badgeClass = '';
                    
                    if (status === 'running') {
                        statusBadge = '<span class="badge badge-info status-running">RUNNING</span>';
                        resultClass = 'running';
                        resultText = 'RUNNING';
                        badgeClass = 'badge-info';
                    } else if (status === 'pending') {
                        statusBadge = '<span class="badge badge-warning">PENDING</span>';
                        resultClass = 'neutral';
                        resultText = 'PENDING';
                        badgeClass = 'badge-warning';
                    } else {
                        statusBadge = '<span class="badge badge-success">COMPLETED</span>';
                        resultClass = profit > 0 ? 'win' : 'loss';
                        resultText = profit > 0 ? 'WIN' : 'LOSS';
                        badgeClass = profit > 0 ? 'badge-success' : 'badge-danger';
                    }
                    
                    const duration = trade.duration_seconds 
                        ? `${trade.duration_seconds.toFixed(1)}s` 
                        : 'N/A';
                    
                    return `
                        <tr>
                            <td>${statusBadge}</td>
                            <td style="font-size: 11px;">${new Date(trade.timestamp).toLocaleString()}</td>
                            <td style="font-family: monospace; font-size: 11px;">${id.substring(0, 8)}...</td>
                            <td style="font-family: monospace; font-size: 11px;">${(trade.contract_id || 'N/A').substring(0, 10)}...</td>
                            <td><span class="metric-highlight">${(trade.volatility || 0).toFixed(3)}</span></td>
                            <td>${trade.volatility_at_exit ? `<span class="metric-highlight">${trade.volatility_at_exit.toFixed(3)}</span>` : 'N/A'}</td>
                            <td><strong>${((trade.growth_rate || 0) * 100).toFixed(2)}%</strong></td>
                            <td>${trade.ticks_completed || '0'}/${trade.target_ticks || 'N/A'}</td>
                            <td>${duration}</td>
                            <td><span class="badge badge-info">${(trade.exit_reason || 'N/A').replace(/_/g, ' ')}</span></td>
                            <td class="positive">${(trade.max_profit_reached || 0).toFixed(2)}</td>
                            <td class="${resultClass}"><strong>${status === 'completed' ? profit.toFixed(2) : 'N/A'}</strong></td>
                            <td><span class="badge ${badgeClass}">${resultText}</span></td>
                        </tr>
                    `;
                }).join('');
                
                document.getElementById('tradesBody').innerHTML = tradesHtml;
            } catch (error) {
                console.error('Error loading data:', error);
            }
        }
        
        function exportCSV() {
            window.location.href = `/trades/export?filter=${currentFilter}`;
        }
        
        async function showOptimalConfig() {
            try {
                const response = await fetch('/config/optimal');
                const data = await response.json();
                const config = JSON.stringify(data.recommended_setup.parameters, null, 2);
                const notes = data.recommended_setup.notes.join('\\n• ');
                alert(`📋 Optimal Configuration\\n\\n${config}\\n\\n📝 Notes:\\n• ${notes}\\n\\nSee /config/optimal endpoint for more setups`);
            } catch (error) {
                console.error('Error fetching config:', error);
                alert('Failed to fetch configuration');
            }
        }
        
        async function showLogs() {
            try {
                const response = await fetch('/logs?limit=50');
                const data = await response.json();
                if (data.logs && data.logs.length > 0) {
                    const logText = data.logs.map(log => 
                        `[${log.timestamp}] ${log.level} - ${log.component}: ${log.message}`
                    ).join('\\n');
                    alert(`📋 Recent System Logs (${data.count}):\\n\\n${logText}`);
                } else {
                    alert('No logs available');
                }
            } catch (error) {
                console.error('Error fetching logs:', error);
                alert('Failed to fetch logs');
            }
        }
        
        // Auto-refresh countdown
        setInterval(() => {
            countdownTimer--;
            document.getElementById('countdown').textContent = countdownTimer;
            if (countdownTimer <= 0) {
                loadSession();
                loadData();
                countdownTimer = 10;
            }
        }, 1000);
        
        loadSession();
        loadData();
    </script>
</body>
</html>"""
    return html, 200

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Starting Deriv Accumulator Trading Bot v3.0")
    logger.info("Industry Standard Edition with Enhanced Logging")
    logger.info("=" * 60)
    
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Server starting on port {port}")
    logger.info(f"Logs directory: {LOG_DIR}")
    logger.info(f"Database path: {DB_PATH}")
    logger.info(f"Max concurrent trades: {MAX_CONCURRENT_TRADES}")
    
    app.run(debug=False, host='0.0.0.0', port=port, use_reloader=False, threaded=True)