import json
import websockets
from datetime import datetime
from threading import Thread
import uuid
import asyncio
import logging
import os

from flask import Flask, request, jsonify

# --- CRITICAL: Suppress ALL output for cron jobs ---
# 1. Set logging to CRITICAL only (even warnings suppressed)
logging.basicConfig(level=logging.CRITICAL, format='%(asctime)s - %(levelname)s - %(message)s')

# 2. Suppress websockets library logging
logging.getLogger('websockets').setLevel(logging.CRITICAL)
logging.getLogger('websockets.client').setLevel(logging.CRITICAL)
logging.getLogger('websockets.protocol').setLevel(logging.CRITICAL)

# 3. Suppress werkzeug (Flask's server) logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.CRITICAL)

# In-memory storage for trade results
trade_results = {}

class DerivAccumulatorBot:
    def __init__(self, api_token, app_id, trade_id, parameters):
        """Initialize the Deriv Accumulator Bot"""
        self.api_token = api_token
        self.app_id = app_id
        self.trade_id = trade_id
        
        # Trading parameters are set from the API request
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
        """Get next request ID for tracking responses"""
        self.request_id += 1
        return self.request_id
        
    async def connect(self, retry_count=0, max_retries=3):
        """Connect to Deriv WebSocket API with retry logic"""
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
                    raise Exception("Authorization failed or API token is invalid.")
                
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
            wait_time = 10 * (2 ** retry_count)
            await asyncio.sleep(wait_time)
            return await self.connect(retry_count=retry_count + 1, max_retries=max_retries)
        else:
            raise Exception("Failed to connect to Deriv API after all retries.")
    
    async def authorize(self):
        """Authorize with API token"""
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
            
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False
        
        if "error" in data:
            return False
        
        if "authorize" in data:
            self.account_balance = float(data['authorize']['balance'])
            return True
        
        return False
    
    async def get_balance(self):
        """Get current account balance"""
        balance_request = {"balance": 1, "subscribe": 0, "req_id": self.get_next_request_id()}
        response_data = await self.send_request(balance_request)
        
        if response_data and "balance" in response_data:
            self.account_balance = float(response_data["balance"]["balance"])
            return self.account_balance
        
        return self.account_balance
    
    async def validate_symbol(self):
        """Check if the symbol is available for trading"""
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
                else:
                    return False
            
        except Exception:
            pass
        
        return False
    
    async def send_request(self, request):
        """Send request and get response directly"""
        req_id = self.get_next_request_id()
        request["req_id"] = req_id
        
        try:
            await self.ws.send(json.dumps(request))
        except Exception:
            return None
        
        try:
            while True:
                response_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                data = json.loads(response_text)
                
                if data.get("req_id") == req_id:
                    return data
                    
                if "subscription" in data:
                    continue
                    
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

    async def place_accumulator_trade(self):
        """Place an Accumulator trade"""
        balance = await self.get_balance()
        if balance < self.stake_per_trade:
            return None, "Insufficient balance to place the trade."
        
        if not self.symbol_available:
            if not await self.validate_symbol():
                return None, "Symbol validation failed."
        
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
            error_msg = proposal_response.get("error", {}).get("message", "Unknown proposal error") if proposal_response else "No proposal response"
            return None, f"Proposal Error: {error_msg}"
        
        proposal_id = proposal_response["proposal"]["id"]
        ask_price = proposal_response["proposal"]["ask_price"]
        
        buy_request = {
            "buy": proposal_id,
            "price": ask_price
        }
        
        buy_response = await self.send_request(buy_request)
        
        if not buy_response or "error" in buy_response:
            error_msg = buy_response.get("error", {}).get("message", "Unknown buy error") if buy_response else "No buy response"
            return None, f"Buy Error: {error_msg}"
        
        contract_id = buy_response["buy"]["contract_id"]
        
        return contract_id, None
    
    async def monitor_contract(self, contract_id):
        """Monitor a contract until completion, selling after target_ticks"""
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
                        
                        # Forget the subscription to clean up the connection
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
                    
                    # Only count ticks if entry spot is set (meaning contract is active)
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
                    return {
                        "profit": 0,
                        "status": "error",
                        "error": data['error']['message']
                    }

            except asyncio.TimeoutError:
                return {
                    "profit": 0,
                    "status": "timeout",
                    "error": "Contract monitoring timeout"
                }
            except Exception as e:
                return {
                    "profit": 0,
                    "status": "error",
                    "error": str(e)
                }
    
    async def execute_trade_async(self):
        """Execute a single trade and return results"""
        trade_results[self.trade_id]['status'] = 'running'
        
        try:
            await self.connect()
            
            if not await self.validate_symbol():
                return {
                    "success": False,
                    "error": "Symbol validation failed",
                    "trade_id": self.trade_id
                }
            
            contract_id, error = await self.place_accumulator_trade()
            
            if error:
                return {
                    "success": False,
                    "error": error,
                    "trade_id": self.trade_id
                }
            
            result = await self.monitor_contract(contract_id)
            
            balance = await self.get_balance()
            
            return {
                "success": True,
                "trade_id": self.trade_id,
                "contract_id": contract_id,
                "profit": result.get("profit", 0),
                "status": result.get("status", "unknown"),
                "final_balance": balance,
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "trade_id": self.trade_id
            }
        finally:
            if self.ws:
                await self.ws.close()
            trade_results[self.trade_id]['status'] = 'completed'


def run_async_trade_in_thread(api_token, app_id, stake, target_ticks, growth_rate, symbol, trade_id):
    """Thread target: Creates a new event loop and runs the async bot execution."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    parameters = {
        'stake': stake,
        'target_ticks': target_ticks,
        'growth_rate': growth_rate,
        'symbol': symbol
    }
    
    bot = DerivAccumulatorBot(api_token, app_id, trade_id, parameters)
    result = loop.run_until_complete(bot.execute_trade_async())
    
    trade_results[trade_id].update(result)
    
    loop.close()


app = Flask(__name__)

@app.route('/trade/<app_id>/<api_token>', methods=['POST'])
def execute_trade(app_id, api_token):
    """POST /trade/<app_id>/<api_token> - Initiates a trade in background thread"""
    try:
        data = request.get_json(silent=True) or {}
        
        stake = float(data.get('stake', 10.0))
        target_ticks = int(data.get('target_ticks', 1))
        growth_rate = float(data.get('growth_rate', 0.03))
        symbol = data.get('symbol', '1HZ10V')
        
        new_trade_id = str(uuid.uuid4())
        
        trade_results[new_trade_id] = {
            "status": "pending", 
            "timestamp": datetime.now().isoformat(),
            "app_id": app_id,
            "parameters": {'stake': stake, 'target_ticks': target_ticks, 'growth_rate': growth_rate, 'symbol': symbol}
        }

        thread = Thread(
            target=run_async_trade_in_thread,
            args=(api_token, app_id, stake, target_ticks, growth_rate, symbol, new_trade_id)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "status": "initiated",
            "trade_id": new_trade_id,
        }), 202
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": "Internal Server Error"
        }), 500


@app.route('/trade/<trade_id>', methods=['GET'])
def get_trade_result(trade_id):
    """GET /trade/<trade_id> - Get result of specific trade"""
    result = trade_results.get(trade_id)
    
    if not result:
        return jsonify({"success": False, "error": "Trade ID not found"}), 404
    
    response = {
        "trade_id": trade_id,
        "status": result.get('status', 'unknown'),
        "timestamp": result.get('timestamp')
    }
    
    if result.get('success') is not None:
        profit = result.get('profit', 0)
        response.update({
            "success": result.get('success'),
            "profit_loss": profit,
            "result": "PROFIT" if profit > 0 else "LOSS",
            "amount": abs(profit),
            "contract_id": result.get('contract_id'),
            "final_balance": result.get('final_balance'),
            "error_details": result.get('error')
        })
    
    return jsonify(response), 200


@app.route('/trades', methods=['GET'])
def get_all_trades():
    """GET /trades - Get all trade results with summary statistics"""
    completed = [t for t in trade_results.values() if t.get('status') == 'completed' and t.get('success')]
    
    total_profit = sum(t.get('profit', 0) for t in completed)
    wins = [t for t in completed if t.get('profit', 0) > 0]
    losses = [t for t in completed if t.get('profit', 0) <= 0]
    
    return jsonify({
        "total_trades": len(trade_results),
        "completed_trades": len(completed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": f"{(len(wins)/len(completed)*100):.2f}%" if completed else "0%",
        "total_profit_loss": round(total_profit, 2),
        "trades": trade_results
    }), 200


@app.route('/trades/summary', methods=['GET'])
def get_trades_summary():
    """GET /trades/summary - Get detailed trading statistics"""
    completed = [t for t in trade_results.values() if t.get('status') == 'completed' and t.get('success')]
    
    if not completed:
        return jsonify({
            "message": "No completed trades yet",
            "total_trades": len(trade_results)
        }), 200
    
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
                "trade_id": tid,
                "timestamp": t.get('timestamp'),
                "profit": t.get('profit', 0),
                "result": "WIN" if t.get('profit', 0) > 0 else "LOSS",
                "contract_id": t.get('contract_id')
            }
            for tid, t in sorted(
                [(k, v) for k, v in trade_results.items() if v.get('status') == 'completed' and v.get('success')],
                key=lambda x: x[1].get('timestamp', ''),
                reverse=True
            )[:10]
        ]
    }), 200


@app.route('/trades/export', methods=['GET'])
def export_trades():
    """GET /trades/export - Export all trades as CSV format"""
    completed = [(tid, t) for tid, t in trade_results.items() 
                 if t.get('status') == 'completed' and t.get('success')]
    
    if not completed:
        return jsonify({"message": "No completed trades to export"}), 200
    
    csv_lines = ["Trade_ID,Timestamp,Contract_ID,Profit_Loss,Result,Final_Balance"]
    
    for trade_id, trade in sorted(completed, key=lambda x: x[1].get('timestamp', '')):
        profit = trade.get('profit', 0)
        csv_lines.append(
            f"{trade_id},"
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
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "Deriv Accumulator Trading API",
        "timestamp": datetime.now().isoformat()
    }), 200


@app.route('/dashboard', methods=['GET'])
def dashboard():
    """Web dashboard to view trading results"""
    html = """
    <!DOCTYPE html>
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
            </div>
            
            <button class="refresh-btn" onclick="loadData()">🔄 Refresh Data</button>
            <button class="export-btn" onclick="exportCSV()">📥 Export CSV</button>
            
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
            async function loadData() {
                try {
                    const response = await fetch('/trades');
                    const data = await response.json();
                    
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
                        .filter(([_, t]) => t.status === 'completed' && t.success)
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
                    alert('Failed to load trading data');
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
    </html>
    """
    return html, 200


if __name__ == '__main__':
    # CRITICAL: Disable debug mode and request logging for cron jobs
    app.run(
        debug=False,  # Changed from True
        host='0.0.0.0', 
        port=5000,
        use_reloader=False  # Prevents double output
    )