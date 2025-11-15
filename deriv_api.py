import json
import websockets
from datetime import datetime
from threading import Thread
import uuid
import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager

from flask import Flask, request, jsonify

# --- CRITICAL: Suppress ALL output for cron jobs ---
logging.basicConfig(level=logging.CRITICAL, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger('websockets').setLevel(logging.CRITICAL)
logging.getLogger('websockets.client').setLevel(logging.CRITICAL)
logging.getLogger('websockets.protocol').setLevel(logging.CRITICAL)
logging.getLogger('werkzeug').setLevel(logging.CRITICAL)

# Database setup for persistent storage
DB_PATH = os.environ.get('DB_PATH', 'trades.db')

def init_db():
    """Initialize SQLite database for persistent trade storage"""
    conn = sqlite3.connect(DB_PATH)
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
            parameters TEXT
        )
    ''')
    conn.commit()
    conn.close()

@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def save_trade(trade_id, trade_data):
    """Save trade to database"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO trades 
            (trade_id, timestamp, app_id, status, success, contract_id, profit, final_balance, error, parameters)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            json.dumps(trade_data.get('parameters', {}))
        ))
        conn.commit()

def get_trade(trade_id):
    """Get single trade from database"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trades WHERE trade_id = ?', (trade_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
    return None

def get_all_trades():
    """Get all trades from database"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trades ORDER BY timestamp DESC')
        return [dict(row) for row in cursor.fetchall()]

# Initialize database on startup
init_db()

# In-memory cache for active trades
trade_results = {}

class DerivAccumulatorBot:
    def __init__(self, api_token, app_id, trade_id, parameters):
        """Initialize the Deriv Accumulator Bot"""
        self.api_token = api_token
        self.app_id = app_id
        self.trade_id = trade_id
        
        self.stake_per_trade = parameters.get('stake', 10.0)
        self.target_ticks = parameters.get('target_ticks', 1)
        self.accumulator_range = parameters.get('growth_rate', 0.03)
        self.symbol = parameters.get('symbol', '1HZ10V')
        
        self.ws_urls = [
            f"wss://ws.derivws.com/websockets/v3?app_id={app_id}",
            f"wss://wscluster1.deriv.com/websockets/v3?app_id={app_id}",
            f"wss://wscluster2.deriv.com/websockets/v3?app_id={app_id}",
        ]
        self.ws_url = self.ws_urls[0]
        self.ws = None
        self.request_id = 0
        self.account_balance = 0.0
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
                    websockets.connect(self.ws_url),
                    timeout=15.0
                )
                auth_success = await self.authorize()
                if not auth_success:
                    await self.ws.close()
                    self.ws = None
                    raise Exception("Authorization failed")
                return True
            except asyncio.TimeoutError:
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                continue
            except Exception:
                if self.ws:
                    await self.ws.close()
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
        
        auth_request = {
            "authorize": self.api_token,
            "req_id": self.get_next_request_id()
        }
        await self.ws.send(json.dumps(auth_request))
        
        try:
            response = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            data = json.loads(response)
        except:
            return False
        
        if "error" in data:
            return False
        
        if "authorize" in data:
            self.account_balance = float(data['authorize']['balance'])
            return True
        return False
    
    async def get_balance(self):
        balance_request = {"balance": 1, "subscribe": 0, "req_id": self.get_next_request_id()}
        response_data = await self.send_request(balance_request)
        
        if response_data and "balance" in response_data:
            self.account_balance = float(response_data["balance"]["balance"])
            return self.account_balance
        return self.account_balance
    
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
        except:
            return None
        
        try:
            while True:
                response_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                data = json.loads(response_text)
                if data.get("req_id") == req_id:
                    return data
                if "subscription" in data:
                    continue
        except:
            return None

    async def place_accumulator_trade(self):
        balance = await self.get_balance()
        if balance < self.stake_per_trade:
            return None, "Insufficient balance"
        
        if not self.symbol_available:
            if not await self.validate_symbol():
                return None, "Symbol validation failed"
        
        proposal_request = {
            "proposal": 1,
            "amount": self.stake_per_trade,
            "basis": "stake",
            "contract_type": self.contract_type,
            "currency": "USD",
            "symbol": self.symbol,
            "growth_rate": self.accumulator_range
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
    
    async def monitor_contract(self, contract_id):
        req_id = self.get_next_request_id()
        proposal_request = {
            "proposal_open_contract": 1,
            "contract_id": contract_id,
            "subscribe": 1,
            "req_id": req_id
        }
        await self.ws.send(json.dumps(proposal_request))
        tick_count = 0
        
        while True:
            try:
                response = await asyncio.wait_for(self.ws.recv(), timeout=20.0) 
                data = json.loads(response)
                
                if "proposal_open_contract" in data:
                    contract = data["proposal_open_contract"]
                    
                    if contract.get("is_sold") or contract.get("status") == "sold":
                        profit = float(contract.get("profit", 0))
                        forget_request = {
                            "forget": data.get("subscription", {}).get("id"),
                            "req_id": self.get_next_request_id()
                        }
                        await self.ws.send(json.dumps(forget_request))
                        return {
                            "profit": profit,
                            "status": "win" if profit > 0 else "loss",
                            "contract_id": contract_id
                        }
                    
                    if contract.get("entry_spot"):
                        tick_count += 1

                    if tick_count >= self.target_ticks:
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
    
    async def execute_trade_async(self):
        """Execute trade and save to database"""
        trade_results[self.trade_id] = {'status': 'running'}
        save_trade(self.trade_id, {
            'timestamp': datetime.now().isoformat(),
            'app_id': self.app_id,
            'status': 'running',
            'parameters': {
                'stake': self.stake_per_trade,
                'target_ticks': self.target_ticks,
                'growth_rate': self.accumulator_range,
                'symbol': self.symbol
            }
        })
        
        try:
            await self.connect()
            
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
            
            result = {
                "success": True,
                "trade_id": self.trade_id,
                "contract_id": contract_id,
                "profit": monitor_result.get("profit", 0),
                "status": "completed",
                "final_balance": balance,
                "timestamp": datetime.now().isoformat(),
                "app_id": self.app_id,
                "parameters": {
                    'stake': self.stake_per_trade,
                    'target_ticks': self.target_ticks,
                    'growth_rate': self.accumulator_range,
                    'symbol': self.symbol
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
                await self.ws.close()
            if self.trade_id in trade_results:
                del trade_results[self.trade_id]


def run_async_trade_in_thread(api_token, app_id, stake, target_ticks, growth_rate, symbol, trade_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    parameters = {
        'stake': stake,
        'target_ticks': target_ticks,
        'growth_rate': growth_rate,
        'symbol': symbol
    }
    
    bot = DerivAccumulatorBot(api_token, app_id, trade_id, parameters)
    loop.run_until_complete(bot.execute_trade_async())
    loop.close()


app = Flask(__name__)

@app.route('/trade/<app_id>/<api_token>', methods=['POST'])
def execute_trade(app_id, api_token):
    try:
        data = request.get_json(silent=True) or {}
        stake = float(data.get('stake', 10.0))
        target_ticks = int(data.get('target_ticks', 1))
        growth_rate = float(data.get('growth_rate', 0.03))
        symbol = data.get('symbol', '1HZ10V')
        
        new_trade_id = str(uuid.uuid4())
        initial_data = {
            "status": "pending", 
            "timestamp": datetime.now().isoformat(),
            "app_id": app_id,
            "parameters": {'stake': stake, 'target_ticks': target_ticks, 'growth_rate': growth_rate, 'symbol': symbol}
        }
        save_trade(new_trade_id, initial_data)
        trade_results[new_trade_id] = initial_data

        thread = Thread(
            target=run_async_trade_in_thread,
            args=(api_token, app_id, stake, target_ticks, growth_rate, symbol, new_trade_id)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({"status": "initiated", "trade_id": new_trade_id}), 202
    except:
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
            "error_details": result.get('error')
        })
    
    return jsonify(response), 200


@app.route('/trades', methods=['GET'])
def get_all_trades_endpoint():
    all_trades = get_all_trades()
    
    # Get filter parameter (default to 'today')
    filter_by = request.args.get('filter', 'today')
    
    # Filter trades based on date
    from datetime import date, timedelta
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
                "contract_id": t.get('contract_id')
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
    
    csv_lines = ["Trade_ID,Timestamp,Contract_ID,Profit_Loss,Result,Final_Balance"]
    
    for trade in sorted(completed, key=lambda x: x.get('timestamp', '')):
        profit = trade.get('profit', 0)
        csv_lines.append(
            f"{trade['trade_id']},"
            f"{trade.get('timestamp', 'N/A')},"
            f"{trade.get('contract_id', 'N/A')},"
            f"{profit:.2f},"
            f"{'WIN' if profit > 0 else 'LOSS'},"
            f"{trade.get('final_balance', 'N/A')}"
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
        "timestamp": datetime.now().isoformat()
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
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Deriv Trading Dashboard</h1>
            <p>Real-time trading performance monitoring</p>
            <p id="currentDate" style="margin-top: 10px; font-size: 14px;"></p>
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
                        <th>Profit/Loss</th>
                        <th>Result</th>
                        <th>Final Balance</th>
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
        
        async function loadData() {
            try {
                const response = await fetch(`/trades?filter=${currentFilter}`);
                const data = await response.json();
                
                // Update date display
                const dateStr = new Date(data.date).toLocaleDateString('en-US', { 
                    weekday: 'long', 
                    year: 'numeric', 
                    month: 'long', 
                    day: 'numeric' 
                });
                document.getElementById('currentDate').textContent = 
                    currentFilter === 'today' ? `📅 ${dateStr}` : '📅 All Time';
                
                // Update stats
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
                `;
                document.getElementById('stats').innerHTML = statsHtml;
                
                // Update trades table
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
                            <td class="${resultClass}">${profit.toFixed(2)}</td>
                            <td class="${resultClass}">${resultText}</td>
                            <td>${(trade.final_balance || 0).toFixed(2)}</td>
                        </tr>
                    `;
                }).join('');
                
                document.getElementById('tradesBody').innerHTML = tradesHtml || '<tr><td colspan="6">No completed trades yet</td></tr>';
            } catch (error) {
                console.error('Error loading data:', error);
            }
        }
        
        function exportCSV() {
            window.location.href = '/trades/export';
        }
        
        // Load data on page load
        loadData();
        
        // Auto-refresh every 10 seconds
        setInterval(loadData, 10000);
    </script>
</body>
</html>"""
    return html, 200


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False, threaded=True)