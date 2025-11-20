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

# === ABSOLUTE ZERO OUTPUT ===
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')

import logging
logging.disable(logging.CRITICAL)
os.environ['PYTHONUNBUFFERED'] = '0'

import warnings
warnings.filterwarnings('ignore')

from flask import Flask, request, jsonify

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
log.disabled = True

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
                error TEXT,
                parameters TEXT,
                volatility REAL,
                growth_rate REAL,
                target_ticks INTEGER,
                exit_reason TEXT,
                max_profit_reached REAL,
                ticks_completed INTEGER
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trading_sessions (
                session_date TEXT PRIMARY KEY,
                trades_count INTEGER DEFAULT 0,
                consecutive_losses INTEGER DEFAULT 0,
                total_profit_loss REAL DEFAULT 0,
                stopped INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()
    except:
        pass

@contextmanager
def get_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        yield conn
        conn.commit()  # CRITICAL: Always commit after successful operations
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
    except Exception as e:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

def save_trade(trade_id, trade_data):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO trades 
                    (trade_id, timestamp, app_id, status, success, contract_id, profit, 
                     final_balance, error, parameters, volatility, growth_rate, target_ticks,
                     exit_reason, max_profit_reached, ticks_completed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    trade_id,
                    trade_data.get('timestamp'),
                    trade_data.get('app_id'),
                    trade_data.get('status'),
                    1 if trade_data.get('success') else 0,
                    trade_data.get('contract_id'),
                    trade_data.get('profit'),
                    trade_data.get('final_balance'),
                    trade_data.get('error'),
                    json.dumps(trade_data.get('parameters', {})),
                    trade_data.get('volatility'),
                    trade_data.get('growth_rate'),
                    trade_data.get('target_ticks'),
                    trade_data.get('exit_reason'),
                    trade_data.get('max_profit_reached'),
                    trade_data.get('ticks_completed')
                ))
    except Exception as e:
        pass

def get_trade(trade_id):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM trades WHERE trade_id = ?', (trade_id,))
                row = cursor.fetchone()
                if row:
                    return dict(row)
    except:
        pass
    return None

def get_all_trades():
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM trades ORDER BY timestamp DESC')
                return [dict(row) for row in cursor.fetchall()]
    except:
        pass
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
    except:
        pass
    return None

def update_session_data(session_date, trades_count, consecutive_losses, total_profit_loss, stopped):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO trading_sessions 
                    (session_date, trades_count, consecutive_losses, total_profit_loss, stopped)
                    VALUES (?, ?, ?, ?, ?)
                ''', (session_date, trades_count, consecutive_losses, total_profit_loss, stopped))
    except:
        pass

try:
    init_db()
except:
    pass

trade_results = {}
MAX_CONCURRENT_TRADES = 2
active_trades_lock = Lock()
active_trade_count = 0

def can_start_trade():
    global active_trade_count
    try:
        with active_trades_lock:
            if active_trade_count >= MAX_CONCURRENT_TRADES:
                return False
            active_trade_count += 1
            return True
    except:
        return False

def trade_completed():
    global active_trade_count
    try:
        with active_trades_lock:
            active_trade_count = max(0, active_trade_count - 1)
    except:
        pass

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
        self.max_consecutive_losses = parameters.get('max_consecutive_losses', 3)
        self.daily_loss_limit_pct = parameters.get('daily_loss_limit_pct', 0.03)
        
        self.profit_target_pct = parameters.get('profit_target_pct', 0.25)
        self.stop_loss_pct = parameters.get('stop_loss_pct', 0.5)
        self.trailing_stop_pct = parameters.get('trailing_stop_pct', 0.3)
        
        self.volatility_check_interval = parameters.get('volatility_check_interval', 3)
        self.volatility_exit_threshold = parameters.get('volatility_exit_threshold', 2.0)
        
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
        
        self.price_history = deque(maxlen=20)
        
    def get_next_request_id(self):
        self.request_id += 1
        return self.request_id
        
    async def connect(self, retry_count=0, max_retries=3):
        for ws_url in self.ws_urls:
            self.ws_url = ws_url
            try:
                self.ws = await asyncio.wait_for(
                    websockets.connect(self.ws_url, ping_interval=None, close_timeout=5),
                    timeout=15.0
                )
                auth_success = await self.authorize()
                if not auth_success:
                    try:
                        await self.ws.close()
                    except:
                        pass
                    self.ws = None
                    raise Exception("Authorization failed")
                return True
            except:
                if self.ws:
                    try:
                        await self.ws.close()
                    except:
                        pass
                    self.ws = None
                continue
        
        if retry_count < max_retries:
            await asyncio.sleep(10 * (2 ** retry_count))
            return await self.connect(retry_count + 1, max_retries)
        else:
            raise Exception("Failed to connect after retries")
    
    async def authorize(self):
        if not self.api_token:
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
                return False
            
            if "authorize" in data:
                self.account_balance = float(data['authorize']['balance'])
                self.initial_balance = self.account_balance
                return True
        except:
            pass
        return False
    
    async def get_balance(self):
        try:
            balance_request = {"balance": 1, "subscribe": 0, "req_id": self.get_next_request_id()}
            response_data = await self.send_request(balance_request)
            
            if response_data and "balance" in response_data:
                self.account_balance = float(response_data["balance"]["balance"])
                return self.account_balance
        except:
            pass
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
                    
                    return std_dev
        except:
            pass
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
        volatility = await self.analyze_volatility()
        
        if volatility is None:
            return 0.02
        
        self.volatility = volatility
        self.initial_volatility = volatility
        
        if volatility < 0.25:
            return 0.04
        elif volatility < 0.5:
            return 0.03
        elif volatility < 0.8:
            return 0.02
        elif volatility < 1.2:
            return 0.015
        else:
            return 0.01
    
    def calculate_target_ticks(self, growth_rate):
        if growth_rate >= 0.035:
            return 3
        elif growth_rate >= 0.025:
            return 4
        elif growth_rate >= 0.018:
            return 5
        elif growth_rate >= 0.012:
            return 6
        else:
            return 8
    
    async def check_trading_conditions(self):
        today = datetime.now().date().isoformat()
        session = get_session_data(today)
        
        if not session:
            update_session_data(today, 0, 0, 0.0, 0)
            return True, "New session started"
        
        if session['stopped']:
            return False, "Trading stopped for today"
        
        if session['trades_count'] >= self.max_daily_trades:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            return False, f"Max daily trades reached ({self.max_daily_trades})"
        
        if session['consecutive_losses'] >= self.max_consecutive_losses:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            return False, f"Max consecutive losses reached ({self.max_consecutive_losses})"
        
        daily_loss_limit = self.initial_balance * self.daily_loss_limit_pct
        if session['total_profit_loss'] <= -daily_loss_limit:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            return False, f"Daily loss limit reached ({daily_loss_limit:.2f})"
        
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
    
    async def validate_symbol(self):
        try:
            spec_request = {
                "contracts_for": self.symbol,
                "req_id": self.get_next_request_id()
            }
            response = await self.send_request(spec_request)
            
            if not response or "error" in response:
                return False
            
            if "contracts_for" in response:
                contracts = response["contracts_for"].get("available", [])
                has_accu = any(c.get("contract_type") == self.contract_type for c in contracts) 
                if has_accu:
                    self.symbol_available = True
                    return True
        except:
            pass
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
        except:
            pass
        return None

    async def place_accumulator_trade(self):
        try:
            balance = await self.get_balance()
            if balance < self.stake_per_trade:
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
                return None, "Proposal failed"
            
            proposal_id = proposal_response["proposal"]["id"]
            ask_price = proposal_response["proposal"]["ask_price"]
            
            buy_request = {"buy": proposal_id, "price": ask_price}
            buy_response = await self.send_request(buy_request)
            
            if not buy_response or "error" in buy_response:
                return None, "Buy failed"
            
            return buy_response["buy"]["contract_id"], None
        except Exception as e:
            return None, str(e)
    
    async def monitor_contract(self, contract_id):
        try:
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
            
            while True:
                try:
                    response = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                    data = json.loads(response)
                    
                    if "proposal_open_contract" in data:
                        contract = data["proposal_open_contract"]
                        
                        if contract.get("is_sold") or contract.get("status") == "sold":
                            profit = float(contract.get("profit", 0))
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
                                "final_volatility": current_volatility
                            }
                        
                        current_profit = float(contract.get("profit", 0))
                        max_profit = max(max_profit, current_profit)
                        
                        if contract.get("current_spot"):
                            self.price_history.append(float(contract["current_spot"]))
                        
                        if contract.get("entry_spot"):
                            tick_count += 1
                        
                        if tick_count - last_volatility_check >= self.volatility_check_interval:
                            current_volatility = self.calculate_realtime_volatility()
                            last_volatility_check = tick_count
                            
                            if current_volatility and self.initial_volatility:
                                volatility_ratio = current_volatility / self.initial_volatility
                                if volatility_ratio > self.volatility_exit_threshold:
                                    exit_reason = f"volatility_spike_{volatility_ratio:.2f}x"
                                    sell_request = {
                                        "sell": contract_id,
                                        "price": 0.0,
                                        "req_id": self.get_next_request_id()
                                    }
                                    await self.send_request(sell_request)
                                    continue
                        
                        if current_profit >= profit_target:
                            exit_reason = "profit_target"
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                        
                        if current_profit <= stop_loss:
                            exit_reason = "stop_loss"
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                        
                        if max_profit > 0:
                            trailing_threshold = max_profit * (1 - self.trailing_stop_pct)
                            if current_profit < trailing_threshold:
                                exit_reason = "trailing_stop"
                                sell_request = {
                                    "sell": contract_id,
                                    "price": 0.0,
                                    "req_id": self.get_next_request_id()
                                }
                                await self.send_request(sell_request)
                                continue
                        
                        if tick_count >= self.target_ticks:
                            exit_reason = "target_ticks"
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                            
                    elif "error" in data:
                        return {
                            "profit": 0, 
                            "status": "error", 
                            "error": data['error']['message'],
                            "exit_reason": "error",
                            "ticks_completed": tick_count,
                            "max_profit_reached": max_profit
                        }
                except:
                    return {
                        "profit": 0, 
                        "status": "error", 
                        "error": "Timeout",
                        "exit_reason": "timeout",
                        "ticks_completed": tick_count,
                        "max_profit_reached": max_profit
                    }
        except:
            return {
                "profit": 0, 
                "status": "error", 
                "error": "Monitor failed",
                "exit_reason": "monitor_failed",
                "ticks_completed": 0,
                "max_profit_reached": 0
            }
    
    async def execute_trade_async(self):
        try:
            trade_results[self.trade_id] = {'status': 'running'}
            save_trade(self.trade_id, {
                'timestamp': datetime.now().isoformat(),
                'app_id': self.app_id,
                'status': 'running',
                'success': 0,
                'parameters': {
                    'stake': self.stake_per_trade,
                    'symbol': self.symbol,
                    'mode': self.mode
                }
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
                "timestamp": datetime.now().isoformat(),
                "app_id": self.app_id,
                "volatility": self.initial_volatility,
                "growth_rate": self.growth_rate,
                "target_ticks": self.target_ticks,
                "exit_reason": monitor_result.get("exit_reason"),
                "max_profit_reached": monitor_result.get("max_profit_reached"),
                "ticks_completed": monitor_result.get("ticks_completed"),
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
            return result
        except Exception as e:
            result = {
                "success": False,
                "error": str(e),
                "trade_id": self.trade_id,
                "timestamp": datetime.now().isoformat(),
                "status": "completed"
            }
            save_trade(self.trade_id, result)
            trade_results[self.trade_id] = result
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
    except:
        pass
    finally:
        trade_completed()
        gc.collect()


app = Flask(__name__)
app.logger.disabled = True

@app.route('/trade/<app_id>/<api_token>', methods=['POST'])
def execute_trade(app_id, api_token):
    try:
        if not can_start_trade():
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
            'volatility_check_interval': int(data.get('volatility_check_interval', 3)),
            'volatility_exit_threshold': float(data.get('volatility_exit_threshold', 2.0))
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

        thread = Thread(
            target=run_async_trade_in_thread,
            args=(api_token, app_id, parameters, new_trade_id)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({"status": "initiated", "trade_id": new_trade_id}), 202
    except:
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
            "volatility": result.get('volatility'),
            "growth_rate": result.get('growth_rate'),
            "target_ticks": result.get('target_ticks'),
            "exit_reason": result.get('exit_reason'),
            "max_profit_reached": result.get('max_profit_reached'),
            "ticks_completed": result.get('ticks_completed'),
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
        "can_trade": not bool(session['stopped'])
    }), 200


@app.route('/session/reset', methods=['POST'])
def reset_session():
    today = datetime.now().date().isoformat()
    update_session_data(today, 0, 0, 0.0, 0)
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
    
    # Filter only completed trades for statistics
    completed = [t for t in filtered_trades if t.get('status') == 'completed']
    
    # Calculate wins and losses from completed trades
    wins = [t for t in completed if t.get('profit', 0) > 0]
    losses = [t for t in completed if t.get('profit', 0) <= 0]
    total_profit = sum(t.get('profit', 0) for t in completed)
    
    # Calculate averages
    avg_volatility = sum(t.get('volatility', 0) or 0 for t in completed) / len(completed) if completed else 0
    avg_growth_rate = sum(t.get('growth_rate', 0) or 0 for t in completed) / len(completed) if completed else 0
    
    # Exit reasons analysis
    exit_reasons = {}
    for t in completed:
        reason = t.get('exit_reason', 'unknown')
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    
    # Convert trades to dictionary format
    trades_dict = {}
    for t in filtered_trades:
        trade_dict = dict(t)
        trades_dict[trade_dict['trade_id']] = trade_dict
    
    # Calculate win rate
    if len(completed) > 0:
        win_rate = f"{(len(wins)/len(completed)*100):.2f}%"
    else:
        win_rate = "0%"

    return jsonify({
        "filter": filter_by,
        "date": today.isoformat(),
        "total_trades": len(filtered_trades),
        "completed_trades": len(completed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_profit_loss": round(total_profit, 2),
        "avg_volatility": round(avg_volatility, 4),
        "avg_growth_rate": round(avg_growth_rate, 4),
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
    
    return jsonify({
        "summary": {
            "total_trades": len(completed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": f"{(len(wins)/len(completed)*100):.2f}%",
            "total_profit_loss": round(total_profit, 2)
        },
        "profit_stats": {
            "average_win": round(sum(win_amounts)/len(win_amounts), 2) if win_amounts else 0,
            "average_loss": round(sum(loss_amounts)/len(loss_amounts), 2) if loss_amounts else 0,
            "largest_win": round(max(win_amounts), 2) if win_amounts else 0,
            "largest_loss": round(max(loss_amounts), 2) if loss_amounts else 0
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
                "ticks_completed": t.get('ticks_completed')
            }
            for t in sorted(completed, key=lambda x: x.get('timestamp', ''), reverse=True)[:10]
        ]
    }), 200

@app.route('/trades/export', methods=['GET'])
def export_trades():
    all_trades = get_all_trades()
    completed = [t for t in all_trades if t.get('status') == 'completed']
    
    if not completed:
        return jsonify({"message": "No completed trades to export"}), 200
    
    csv_lines = ["Trade_ID,Timestamp,Contract_ID,Profit_Loss,Result,Final_Balance,Volatility,Growth_Rate,Target_Ticks,Exit_Reason,Ticks_Completed,Max_Profit"]
    
    for trade in sorted(completed, key=lambda x: x.get('timestamp', '')):
        profit = trade.get('profit', 0)
        csv_lines.append(
            f"{trade['trade_id']},"
            f"{trade.get('timestamp', 'N/A')},"
            f"{trade.get('contract_id', 'N/A')},"
            f"{profit:.2f},"
            f"{'WIN' if profit > 0 else 'LOSS'},"
            f"{trade.get('final_balance', 'N/A')},"
            f"{trade.get('volatility', 'N/A')},"
            f"{trade.get('growth_rate', 'N/A')},"
            f"{trade.get('target_ticks', 'N/A')},"
            f"{trade.get('exit_reason', 'N/A')},"
            f"{trade.get('ticks_completed', 'N/A')},"
            f"{trade.get('max_profit_reached', 'N/A')}"
        )
    
    return "\n".join(csv_lines), 200, {
        'Content-Type': 'text/csv',
        'Content-Disposition': 'attachment; filename=trades.csv'
    }

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "Deriv Accumulator Trading API v2",
        "timestamp": datetime.now().isoformat(),
        "active_trades": active_trade_count,
        "max_concurrent": MAX_CONCURRENT_TRADES,
        "features": [
            "Real-time volatility monitoring",
            "Adaptive exit strategies",
            "Trailing stop loss",
            "Dynamic growth rate selection"
        ]
    }), 200


@app.route('/config/optimal', methods=['GET'])
def get_optimal_config():
    return jsonify({
        "recommended_setup": {
            "description": "Optimized for consistent profitability with risk management",
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
                "volatility_check_interval": 3,
                "volatility_exit_threshold": 2.0
            }
        },
        "conservative_setup": {
            "description": "Lower risk with smaller stakes",
            "parameters": {
                "stake": 3.0,
                "symbol": "1HZ10V",
                "mode": "adaptive",
                "max_daily_trades": 10,
                "max_consecutive_losses": 2,
                "daily_loss_limit_pct": 0.02,
                "profit_target_pct": 0.20,
                "stop_loss_pct": 0.4,
                "trailing_stop_pct": 0.25
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
                "trailing_stop_pct": 0.35
            }
        },
        "usage": "POST /trade/<app_id>/<api_token> with JSON body containing any of these parameters"
    }), 200


@app.route('/restart', methods=['POST', 'GET'])
def restart_service():
    try:
        render_api_key = os.environ.get('RENDER_API_KEY')
        service_id = os.environ.get('RENDER_SERVICE_ID')
        
        if not render_api_key or not service_id:
            return jsonify({
                "success": False,
                "error": "Render API credentials not configured"
            }), 500
        
        try:
            import requests as req
        except ImportError:
            return jsonify({
                "success": False,
                "error": "requests library not installed"
            }), 500
        
        url = f"https://api.render.com/v1/services/{service_id}/deploys"
        headers = {
            "Authorization": f"Bearer {render_api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        payload = {"clearCache": "clear"}
        
        try:
            response = req.post(url, headers=headers, json=payload, timeout=15)
            
            try:
                response_json = response.json()
            except:
                response_json = {"raw_text": response.text}
            
            return jsonify({
                "success": response.status_code in [200, 201],
                "status_code": response.status_code,
                "message": "Server restart initiated" if response.status_code in [200, 201] else "Request sent",
                "response": response_json
            }), 200
            
        except Exception as e:
            return jsonify({
                "success": False,
                "error": "Request failed",
                "details": str(e)
            }), 500
            
    except Exception as e:
        import traceback
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/dashboard', methods=['GET'])
def dashboard():
    html = """<!DOCTYPE html>
<html>
<head>
    <title>Trading Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; min-height: 100vh; padding: 20px; background: #f7fafc; }
        .container { max-width: 1400px; margin: 0 auto; }
        .header { background: white; padding: 30px; border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .header h1 { color: #2d3748; font-size: 32px; margin-bottom: 10px; }
        .header p { color: #718096; font-size: 16px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .stat-card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); transition: transform 0.2s; }
        .stat-card:hover { transform: translateY(-2px); box-shadow: 0 6px 12px rgba(0,0,0,0.15); }
        .stat-label { font-size: 13px; color: #718096; margin-bottom: 8px; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; }
        .stat-value { font-size: 32px; font-weight: bold; color: #2d3748; }
        .stat-value.positive { color: #48bb78; }
        .stat-value.negative { color: #f56565; }
        .session-card { background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 20px; }
        .session-status { display: inline-block; padding: 8px 20px; border-radius: 20px; font-weight: 600; font-size: 14px; }
        .session-active { background: #48bb78; color: white; }
        .session-stopped { background: #f56565; color: white; }
        .controls { background: white; padding: 20px; border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .btn { padding: 12px 24px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; transition: all 0.2s; margin-right: 10px; margin-bottom: 10px; }
        .btn-primary { background: #667eea; color: white; }
        .btn-primary:hover { background: #5a67d8; transform: translateY(-1px); }
        .btn-success { background: #48bb78; color: white; }
        .btn-success:hover { background: #38a169; transform: translateY(-1px); }
        .select { padding: 12px; border-radius: 8px; border: 2px solid #e2e8f0; font-size: 14px; cursor: pointer; }
        .trades-table { background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); overflow-x: auto; }
        .trades-table h2 { color: #2d3748; margin-bottom: 20px; font-size: 24px; }
        table { width: 100%; border-collapse: collapse; min-width: 800px; }
        th { background: #4a5568; color: white; padding: 14px; text-align: left; font-weight: 600; font-size: 13px; text-transform: uppercase; }
        td { padding: 12px 14px; border-bottom: 1px solid #e2e8f0; font-size: 14px; }
        tr:hover { background: #f7fafc; }
        .win { color: #48bb78; font-weight: 600; }
        .loss { color: #f56565; font-weight: 600; }
        .badge { display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; }
        .badge-success { background: #c6f6d5; color: #22543d; }
        .badge-danger { background: #fed7d7; color: #742a2a; }
        .badge-info { background: #bee3f8; color: #2c5282; }
        .exit-reasons { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 15px; margin-bottom: 20px; }
        .reason-badge { padding: 8px 16px; background: #edf2f7; border-radius: 8px; font-size: 13px; }
        .no-data { text-align: center; padding: 40px; color: #718096; font-size: 16px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 Enhanced Trading Dashboard</h1>
            <p>Real-time monitoring with adaptive volatility detection & smart exit strategies</p>
            <p id="currentDate" style="margin-top: 12px; font-size: 15px; color: #4a5568;"></p>
        </div>
        
        <div class="session-card" id="sessionCard">
            <h3 style="margin-bottom: 15px; color: #2d3748;">Today's Session Status</h3>
            <div id="sessionStatus">Loading...</div>
        </div>
        
        <div class="controls">
            <button class="btn btn-primary" onclick="loadData()">🔄 Refresh Data</button>
            <button class="btn btn-success" onclick="exportCSV()">📥 Export CSV</button>
            <button class="btn btn-primary" onclick="showOptimalConfig()">⚙️ Optimal Config</button>
            <select id="filterSelect" onchange="changeFilter()" class="select">
                <option value="today">Today's Trades</option>
                <option value="all">All Trades</option>
            </select>
        </div>
        
        <div class="stats-grid" id="stats">
            <div class="stat-card"><div class="no-data">Loading stats...</div></div>
        </div>
        
        <div class="trades-table">
            <h2>📊 Recent Trades</h2>
            <div id="exitReasons" class="exit-reasons"></div>
            <table id="tradesTable">
                <thead>
                    <tr>
                        <th>Timestamp</th>
                        <th>Trade ID</th>
                        <th>Volatility</th>
                        <th>Growth %</th>
                        <th>Ticks</th>
                        <th>Exit Reason</th>
                        <th>Max Profit</th>
                        <th>Final P/L</th>
                        <th>Result</th>
                    </tr>
                </thead>
                <tbody id="tradesBody">
                    <tr><td colspan="9" class="no-data">Loading trades...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
    <script>
        let currentFilter = 'today';
        
        function changeFilter() {
            currentFilter = document.getElementById('filterSelect').value;
            loadData();
        }
        
        async function loadSession() {
            try {
                const response = await fetch('/session');
                const data = await response.json();
                
                const statusClass = data.stopped ? 'session-stopped' : 'session-active';
                const statusText = data.stopped ? 'STOPPED' : 'ACTIVE';
                
                const sessionHtml = `
                    <p style="margin-bottom: 15px;"><span class="session-status ${statusClass}">${statusText}</span></p>
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
                        <div><strong style="color: #718096;">Trades Today:</strong> <span style="font-size: 24px; font-weight: bold; color: #2d3748;">${data.trades_count}</span></div>
                        <div><strong style="color: #718096;">Consecutive Losses:</strong> <span style="font-size: 24px; font-weight: bold; color: #2d3748;">${data.consecutive_losses}</span></div>
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
                        <div class="stat-value">${data.completed_trades || 0}</div>
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
                    <div class="stat-card">
                        <div class="stat-label">Avg Volatility</div>
                        <div class="stat-value">${(data.avg_volatility || 0).toFixed(3)}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Avg Growth</div>
                        <div class="stat-value">${((data.avg_growth_rate || 0) * 100).toFixed(2)}%</div>
                    </div>
                `;
                document.getElementById('stats').innerHTML = statsHtml;
                
                // Display exit reasons
                if (data.exit_reasons && Object.keys(data.exit_reasons).length > 0) {
                    const reasonsHtml = '<strong style="display: block; margin-bottom: 10px; color: #2d3748;">Exit Reasons Distribution:</strong>' + 
                        Object.entries(data.exit_reasons).map(([reason, count]) => 
                            `<div class="reason-badge">${reason.replace(/_/g, ' ')}: ${count}</div>`
                        ).join('');
                    document.getElementById('exitReasons').innerHTML = reasonsHtml;
                } else {
                    document.getElementById('exitReasons').innerHTML = '';
                }
                
                if (!data.trades || Object.keys(data.trades).length === 0) {
                    document.getElementById('tradesBody').innerHTML = 
                        '<tr><td colspan="9" class="no-data">No trades yet. Start trading to see results here.</td></tr>';
                    return;
                }
                
                const trades = Object.entries(data.trades)
                    .filter(([_, t]) => t.status === 'completed')
                    .sort((a, b) => new Date(b[1].timestamp) - new Date(a[1].timestamp));
                
                if (trades.length === 0) {
                    document.getElementById('tradesBody').innerHTML = 
                        '<tr><td colspan="9" class="no-data">No completed trades yet</td></tr>';
                    return;
                }
                
                const tradesHtml = trades.map(([id, trade]) => {
                    const profit = trade.profit || 0;
                    const resultClass = profit > 0 ? 'win' : 'loss';
                    const resultText = profit > 0 ? 'WIN' : 'LOSS';
                    const badgeClass = profit > 0 ? 'badge-success' : 'badge-danger';
                    return `
                        <tr>
                            <td>${new Date(trade.timestamp).toLocaleString()}</td>
                            <td style="font-family: monospace;">${id.substring(0, 8)}...</td>
                            <td>${(trade.volatility || 0).toFixed(3)}</td>
                            <td>${((trade.growth_rate || 0) * 100).toFixed(2)}%</td>
                            <td>${trade.ticks_completed || 'N/A'}/${trade.target_ticks || 'N/A'}</td>
                            <td><span class="badge badge-info">${(trade.exit_reason || 'unknown').replace(/_/g, ' ')}</span></td>
                            <td class="positive">${(trade.max_profit_reached || 0).toFixed(2)}</td>
                            <td class="${resultClass}">${profit.toFixed(2)}</td>
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
            window.location.href = '/trades/export';
        }
        
        async function showOptimalConfig() {
            try {
                const response = await fetch('/config/optimal');
                const data = await response.json();
                alert('Optimal Configuration:\\n\\n' + JSON.stringify(data.recommended_setup.parameters, null, 2) + '\\n\\nSee /config/optimal endpoint for more setups');
            } catch (error) {
                console.error('Error fetching config:', error);
            }
        }
        
        loadSession();
        loadData();
        setInterval(() => {
            loadSession();
            loadData();
        }, 10000);
    </script>
</body>
</html>"""
    return html, 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port, use_reloader=False, threaded=True)