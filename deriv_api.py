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
                target_ticks INTEGER
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
    except:
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
                     final_balance, error, parameters, volatility, growth_rate, target_ticks)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    trade_data.get('target_ticks')
                ))
                conn.commit()
    except:
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
                conn.commit()
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
        self.stake_per_trade = parameters.get('stake', 10.0)
        self.symbol = parameters.get('symbol', '1HZ25V')  # Changed default to lower volatility
        self.mode = parameters.get('mode', 'adaptive')  # 'adaptive' or 'fixed'
        self.fixed_growth_rate = parameters.get('growth_rate', 0.015)  # 1.5% default
        self.fixed_target_ticks = parameters.get('target_ticks', 5)
        
        # Risk management settings
        self.max_daily_trades = parameters.get('max_daily_trades', 10)
        self.max_consecutive_losses = parameters.get('max_consecutive_losses', 3)
        self.daily_loss_limit_pct = parameters.get('daily_loss_limit_pct', 0.05)  # 5% of balance
        
        # Determined dynamically
        self.growth_rate = None
        self.target_ticks = None
        self.volatility = None
        
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
        """Analyze recent tick volatility to determine market conditions"""
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
                    # Calculate percentage changes
                    changes = [(prices[i] - prices[i-1]) / prices[i-1] * 100 
                              for i in range(1, len(prices))]
                    
                    # Calculate standard deviation of changes
                    mean = sum(changes) / len(changes)
                    variance = sum((c - mean) ** 2 for c in changes) / len(changes)
                    std_dev = (variance ** 0.5)
                    
                    return std_dev
        except:
            pass
        return None
    
    async def select_optimal_growth_rate(self):
        """Choose growth rate based on current volatility"""
        volatility = await self.analyze_volatility()
        
        if volatility is None:
            return 0.015  # Default 1.5%
        
        self.volatility = volatility
        
        # Dynamic growth rate based on volatility
        # Lower volatility = higher growth rate (tighter bands)
        # Higher volatility = lower growth rate (wider bands)
        if volatility < 0.3:
            return 0.03  # 3% - very low volatility
        elif volatility < 0.6:
            return 0.02  # 2% - low volatility
        elif volatility < 1.0:
            return 0.015  # 1.5% - moderate
        elif volatility < 1.5:
            return 0.01  # 1% - moderate-high
        else:
            return 0.005  # 0.5% - high volatility
    
    def calculate_target_ticks(self, growth_rate):
        """Calculate optimal target ticks based on growth rate"""
        # Higher growth rates = shorter duration (fewer ticks)
        # Lower growth rates = longer duration (more ticks)
        if growth_rate >= 0.025:
            return 3
        elif growth_rate >= 0.018:
            return 5
        elif growth_rate >= 0.012:
            return 7
        elif growth_rate >= 0.008:
            return 10
        else:
            return 12
    
    async def check_trading_conditions(self):
        """Check if trading should proceed based on session limits"""
        today = datetime.now().date().isoformat()
        session = get_session_data(today)
        
        if not session:
            # Initialize session
            update_session_data(today, 0, 0, 0.0, 0)
            return True, "New session started"
        
        # Check if already stopped for the day
        if session['stopped']:
            return False, "Trading stopped for today due to limits"
        
        # Check max daily trades
        if session['trades_count'] >= self.max_daily_trades:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            return False, f"Max daily trades reached ({self.max_daily_trades})"
        
        # Check consecutive losses
        if session['consecutive_losses'] >= self.max_consecutive_losses:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            return False, f"Max consecutive losses reached ({self.max_consecutive_losses})"
        
        # Check daily loss limit
        daily_loss_limit = self.initial_balance * self.daily_loss_limit_pct
        if session['total_profit_loss'] <= -daily_loss_limit:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            return False, f"Daily loss limit reached ({daily_loss_limit:.2f})"
        
        return True, "Trading conditions OK"
    
    def update_session_after_trade(self, profit):
        """Update session data after trade completion"""
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
            new_consecutive_losses = 0  # Reset on win
        
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
            
            # Determine growth rate and target ticks
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
                                "ticks_completed": tick_count
                            }
                        
                        # Track profit and ticks
                        current_profit = float(contract.get("profit", 0))
                        max_profit = max(max_profit, current_profit)
                        
                        if contract.get("entry_spot"):
                            tick_count += 1

                        # Exit conditions
                        # 1. Reached target ticks
                        if tick_count >= self.target_ticks:
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                        
                        # 2. Profit target reached (2% return on stake)
                        profit_target = self.stake_per_trade * 0.02
                        if current_profit >= profit_target:
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            
                    elif "error" in data:
                        return {"profit": 0, "status": "error", "error": data['error']['message']}
                except:
                    return {"profit": 0, "status": "error", "error": "Timeout"}
        except:
            return {"profit": 0, "status": "error", "error": "Monitor failed"}
    
    async def execute_trade_async(self):
        try:
            trade_results[self.trade_id] = {'status': 'running'}
            save_trade(self.trade_id, {
                'timestamp': datetime.now().isoformat(),
                'app_id': self.app_id,
                'status': 'running',
                'parameters': {
                    'stake': self.stake_per_trade,
                    'symbol': self.symbol,
                    'mode': self.mode
                }
            })
            
            await self.connect()
            
            # Check trading conditions
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
                return result
            
            monitor_result = await self.monitor_contract(contract_id)
            balance = await self.get_balance()
            
            # Update session after trade
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
                "volatility": self.volatility,
                "growth_rate": self.growth_rate,
                "target_ticks": self.target_ticks,
                "parameters": {
                    'stake': self.stake_per_trade,
                    'symbol': self.symbol,
                    'mode': self.mode,
                    'growth_rate_used': self.growth_rate,
                    'target_ticks_used': self.target_ticks,
                    'volatility_detected': self.volatility
                }
            }
            save_trade(self.trade_id, result)
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
            return result
        finally:
            if self.ws:
                try:
                    await self.ws.close()
                except:
                    pass
            if self.trade_id in trade_results:
                try:
                    del trade_results[self.trade_id]
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
        
        # Build parameters dict
        parameters = {
            'stake': float(data.get('stake', 10.0)),
            'symbol': data.get('symbol', '1HZ25V'),
            'mode': data.get('mode', 'adaptive'),  # 'adaptive' or 'fixed'
            'growth_rate': float(data.get('growth_rate', 0.015)),  # Used in 'fixed' mode
            'target_ticks': int(data.get('target_ticks', 5)),  # Used in 'fixed' mode
            'max_daily_trades': int(data.get('max_daily_trades', 10)),
            'max_consecutive_losses': int(data.get('max_consecutive_losses', 3)),
            'daily_loss_limit_pct': float(data.get('daily_loss_limit_pct', 0.05))
        }
        
        new_trade_id = str(uuid.uuid4())
        initial_data = {
            "status": "pending", 
            "timestamp": datetime.now().isoformat(),
            "app_id": app_id,
            "parameters": parameters
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
            "error_details": result.get('error')
        })
    
    return jsonify(response), 200


@app.route('/session', methods=['GET'])
def get_session_status():
    """Get current session trading status"""
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
    """Reset today's session (use with caution)"""
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
    
    completed = [t for t in filtered_trades if t.get('status') == 'completed' and (t.get('success') or t.get('success') == 1)]
    
    total_profit = sum(t.get('profit', 0) for t in completed)
    wins = [t for t in completed if t.get('profit', 0) > 0]
    losses = [t for t in completed if t.get('profit', 0) <= 0]
    
    # Calculate average volatility and growth rate
    avg_volatility = sum(t.get('volatility', 0) or 0 for t in completed) / len(completed) if completed else 0
    avg_growth_rate = sum(t.get('growth_rate', 0) or 0 for t in completed) / len(completed) if completed else 0
    
    trades_dict = {t['trade_id']: dict(t) for t in filtered_trades}
    
    return jsonify({
        "filter": filter_by,
        "date": today.isoformat(),
        "total_trades": len(filtered_trades),
        "completed_trades": len(completed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": f"{(len(wins)/len(completed)*100):.2f}%" if completed else "0%",
        "total_profit_loss": round(total_profit, 2),
        "avg_volatility": round(avg_volatility, 4),
        "avg_growth_rate": round(avg_growth_rate, 4),
        "trades": trades_dict
    }), 200


@app.route('/trades/summary', methods=['GET'])
def get_trades_summary():
    all_trades = get_all_trades()
    completed = [t for t in all_trades if t.get('status') == 'completed' and (t.get('success') or t.get('success') == 1)]
    
    if not completed:
        return jsonify({"message": "No completed trades yet", "total_trades": len(all_trades)}), 200
    
    total_profit = sum(t.get('profit', 0) for t in completed)
    wins = [t for t in completed if t.get('profit', 0) > 0]
    losses = [t for t in completed if t.get('profit', 0) <= 0]
    
    win_amounts = [t.get('profit', 0) for t in wins]
    loss_amounts = [abs(t.get('profit', 0)) for t in losses]
    
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
        "recent_trades": [
            {
                "trade_id": t['trade_id'],
                "timestamp": t.get('timestamp'),
                "profit": t.get('profit', 0),
                "result": "WIN" if t.get('profit', 0) > 0 else "LOSS",
                "contract_id": t.get('contract_id'),
                "volatility": t.get('volatility'),
                "growth_rate": t.get('growth_rate')
            }
            for t in sorted(completed, key=lambda x: x.get('timestamp', ''), reverse=True)[:10]
        ]
    }), 200


@app.route('/trades/export', methods=['GET'])
def export_trades():
    all_trades = get_all_trades()
    completed = [t for t in all_trades if t.get('status') == 'completed' and (t.get('success') or t.get('success') == 1)]
    
    if not completed:
        return jsonify({"message": "No completed trades to export"}), 200
    
    csv_lines = ["Trade_ID,Timestamp,Contract_ID,Profit_Loss,Result,Final_Balance,Volatility,Growth_Rate,Target_Ticks"]
    
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
            f"{trade.get('target_ticks', 'N/A')}"
        )
    
    return "\n".join(csv_lines), 200, {
        'Content-Type': 'text/csv',
        'Content-Disposition': 'attachment; filename=trades.csv'
    }


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "Deriv Accumulator Trading API",
        "timestamp": datetime.now().isoformat(),
        "active_trades": active_trade_count,
        "max_concurrent": MAX_CONCURRENT_TRADES
    }), 200


@app.route('/restart', methods=['POST', 'GET'])
def restart_service():
    """Manual restart endpoint - clears logs and memory"""
    try:
        render_api_key = os.environ.get('RENDER_API_KEY')
        service_id = os.environ.get('RENDER_SERVICE_ID')
        
        if not render_api_key or not service_id:
            return jsonify({
                "success": False,
                "error": "Render API credentials not configured",
                "has_api_key": bool(render_api_key),
                "has_service_id": bool(service_id)
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
        
        payload = {
            "clearCache": "clear"
        }
        
        try:
            response = req.post(url, headers=headers, json=payload, timeout=15)
            
            try:
                response_json = response.json()
            except:
                response_json = {"raw_text": response.text}
            
            return jsonify({
                "success": response.status_code in [200, 201],
                "status_code": response.status_code,
                "message": "Server restart initiated (new deploy)" if response.status_code in [200, 201] else "Request sent",
                "response": response_json,
                "url": url
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


@app.route('/restart/test', methods=['GET'])
def test_restart_config():
    """Test endpoint to check if restart credentials are configured"""
    render_api_key = os.environ.get('RENDER_API_KEY')
    service_id = os.environ.get('RENDER_SERVICE_ID')
    
    return jsonify({
        "has_api_key": bool(render_api_key),
        "api_key_preview": render_api_key[:8] + "..." if render_api_key else None,
        "has_service_id": bool(service_id),
        "service_id": service_id if service_id else None
    }), 200


@app.route('/dashboard', methods=['GET'])
def dashboard():
    html = """<!DOCTYPE html>
<html>
<head>
    <title>Trading Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { background: #2c3e50; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .stat-card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .stat-label { font-size: 14px; color: #666; margin-bottom: 5px; }
        .stat-value { font-size: 28px; font-weight: bold; color: #2c3e50; }
        .stat-value.positive { color: #27ae60; }
        .stat-value.negative { color: #e74c3c; }
        .trades-table { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th { background: #34495e; color: white; padding: 12px; text-align: left; }
        td { padding: 10px; border-bottom: 1px solid #ddd; }
        tr:hover { background: #f8f9fa; }
        .win { color: #27ae60; font-weight: bold; }
        .loss { color: #e74c3c; font-weight: bold; }
        .refresh-btn { background: #3498db; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; margin-bottom: 20px; }
        .refresh-btn:hover { background: #2980b9; }
        .export-btn { background: #27ae60; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; margin-left: 10px; }
        .export-btn:hover { background: #229954; }
        .session-card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
        .session-status { display: inline-block; padding: 5px 15px; border-radius: 20px; font-weight: bold; }
        .session-active { background: #27ae60; color: white; }
        .session-stopped { background: #e74c3c; color: white; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Deriv Trading Dashboard (Enhanced)</h1>
            <p>Real-time trading performance monitoring with adaptive strategy</p>
            <p id="currentDate" style="margin-top: 10px; font-size: 14px;"></p>
        </div>
        
        <div class="session-card" id="sessionCard">
            <h3>Today's Session Status</h3>
            <div id="sessionStatus"></div>
        </div>
        
        <div style="margin-bottom: 20px;">
            <button class="refresh-btn" onclick="loadData()">🔄 Refresh Data</button>
            <button class="export-btn" onclick="exportCSV()">📥 Export CSV</button>
            <select id="filterSelect" onchange="changeFilter()" style="padding: 10px; border-radius: 5px; margin-left: 10px; cursor: pointer;">
                <option value="today">Today's Trades</option>
                <option value="all">All Trades</option>
            </select>
        </div>
        
        <div class="stats" id="stats"></div>
        <div class="trades-table">
            <h2>Recent Trades</h2>
            <table id="tradesTable">
                <thead>
                    <tr>
                        <th>Timestamp</th>
                        <th>Trade ID</th>
                        <th>Contract ID</th>
                        <th>Volatility</th>
                        <th>Growth Rate</th>
                        <th>Ticks</th>
                        <th>Profit/Loss</th>
                        <th>Result</th>
                    </tr>
                </thead>
                <tbody id="tradesBody"></tbody>
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
                    <p><span class="session-status ${statusClass}">${statusText}</span></p>
                    <p><strong>Trades Today:</strong> ${data.trades_count}</p>
                    <p><strong>Consecutive Losses:</strong> ${data.consecutive_losses}</p>
                    <p><strong>Total P/L:</strong> <span class="${data.total_profit_loss >= 0 ? 'win' : 'loss'}">${data.total_profit_loss.toFixed(2)}</span></p>
                `;
                document.getElementById('sessionStatus').innerHTML = sessionHtml;
            } catch (error) {
                console.error('Error loading session:', error);
            }
        }
        
        async function loadData() {
            try {
                const response = await fetch(`/trades?filter=${currentFilter}`);
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
                        <div class="stat-value">${data.total_trades}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Completed</div>
                        <div class="stat-value">${data.completed_trades}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Wins</div>
                        <div class="stat-value positive">${data.wins}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Losses</div>
                        <div class="stat-value negative">${data.losses}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Win Rate</div>
                        <div class="stat-value">${data.win_rate}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Total P/L</div>
                        <div class="stat-value ${data.total_profit_loss >= 0 ? 'positive' : 'negative'}">
                            ${data.total_profit_loss.toFixed(2)}
                        </div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Avg Volatility</div>
                        <div class="stat-value">${(data.avg_volatility || 0).toFixed(3)}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Avg Growth Rate</div>
                        <div class="stat-value">${((data.avg_growth_rate || 0) * 100).toFixed(2)}%</div>
                    </div>
                `;
                document.getElementById('stats').innerHTML = statsHtml;
                
                const trades = Object.entries(data.trades)
                    .filter(([_, t]) => t.status === 'completed' && (t.success || t.success === 1))
                    .sort((a, b) => new Date(b[1].timestamp) - new Date(a[1].timestamp));
                
                const tradesHtml = trades.map(([id, trade]) => {
                    const profit = trade.profit || 0;
                    const resultClass = profit > 0 ? 'win' : 'loss';
                    const resultText = profit > 0 ? 'WIN' : 'LOSS';
                    return `
                        <tr>
                            <td>${new Date(trade.timestamp).toLocaleString()}</td>
                            <td>${id.substring(0, 8)}...</td>
                            <td>${trade.contract_id || 'N/A'}</td>
                            <td>${(trade.volatility || 0).toFixed(3)}</td>
                            <td>${((trade.growth_rate || 0) * 100).toFixed(2)}%</td>
                            <td>${trade.target_ticks || 'N/A'}</td>
                            <td class="${resultClass}">${profit.toFixed(2)}</td>
                            <td class="${resultClass}">${resultText}</td>
                        </tr>
                    `;
                }).join('');
                
                document.getElementById('tradesBody').innerHTML = tradesHtml || '<tr><td colspan="8">No completed trades yet</td></tr>';
            } catch (error) {
                console.error('Error loading data:', error);
            }
        }
        
        function exportCSV() {
            window.location.href = '/trades/export';
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