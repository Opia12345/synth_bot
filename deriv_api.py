from flask import Flask, request, jsonify
import asyncio
import json
import websockets
from datetime import datetime
from threading import Thread
import uuid

app = Flask(__name__)

# In-memory storage for trade results
trade_results = {}

class DerivAccumulatorBot:
    def __init__(self, api_token, app_id):
        """Initialize the Deriv Accumulator Bot"""
        self.api_token = api_token
        self.app_id = app_id
        self.ws_urls = [
            f"wss://ws.derivws.com/websockets/v3?app_id={app_id}",
            f"wss://wscluster1.deriv.com/websockets/v3?app_id={app_id}",
            f"wss://wscluster2.deriv.com/websockets/v3?app_id={app_id}",
        ]
        self.ws_url = self.ws_urls[0]
        self.ws = None
        self.request_id = 0
        
        # Bot Configuration 
        self.stake_per_trade = 10.0
        self.target_ticks = 1
        self.accumulator_range = 0.03
        self.symbol = "1HZ10V"
        self.contract_type = "ACCU"
        
        # Trading state
        self.account_balance = 0.0
        self.symbol_available = False
        self.trade_id = str(uuid.uuid4())
        
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
                print(f"✅ Connected to Deriv API")

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
            except Exception as e:
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                continue
        
        if retry_count < max_retries:
            wait_time = 10 * (2 ** retry_count)
            await asyncio.sleep(wait_time)
            return await self.connect(retry_count=retry_count + 1, max_retries=max_retries)
        else:
            raise Exception("Failed to connect to Deriv API")
    
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
        
        if "error" in data:
            return False
        
        if "authorize" in data:
            self.account_balance = float(data['authorize']['balance'])
            return True
        
        return False
    
    async def get_balance(self):
        """Get current account balance"""
        balance_request = {"balance": 1, "subscribe": 0, "req_id": self.get_next_request_id()}
        await self.ws.send(json.dumps(balance_request))
        
        try:
            response = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
            data = json.loads(response)
            
            if "balance" in data:
                self.account_balance = float(data["balance"]["balance"])
                return self.account_balance
        except asyncio.TimeoutError:
            pass
        
        return self.account_balance
    
    async def validate_symbol(self):
        """Check if the symbol is available for trading"""
        try:
            spec_request = {
                "contracts_for": self.symbol,
                "req_id": self.get_next_request_id()
            }
            
            await self.ws.send(json.dumps(spec_request))
            response = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
            data = json.loads(response)
            
            if "error" in data:
                return False
            
            if "contracts_for" in data:
                contracts = data["contracts_for"].get("available", [])
                has_accu = any(c.get("contract_type") == "ACCU" for c in contracts) 
                
                if has_accu:
                    self.symbol_available = True
                    return True
                else:
                    return False
                    
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            pass
        
        return False
    
    async def send_request(self, request):
        """Send request and get response directly"""
        req_id = self.get_next_request_id()
        request["req_id"] = req_id
        
        try:
            await self.ws.send(json.dumps(request))
        except Exception as e:
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
        except Exception as e:
            return None

    async def place_accumulator_trade(self):
        """Place an Accumulator trade"""
        balance = await self.get_balance()
        if balance < self.stake_per_trade + 20.0:
            return None, "Insufficient balance"
        
        if not self.symbol_available:
            if not await self.validate_symbol():
                return None, "Symbol validation failed"
        
        contract_type = "ACCU" 
        
        proposal_request = {
            "proposal": 1,
            "amount": self.stake_per_trade,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "symbol": self.symbol,
            "growth_rate": self.accumulator_range
        }
        
        proposal_response = await self.send_request(proposal_request)
        
        if not proposal_response or "error" in proposal_response:
            error_msg = proposal_response.get("error", {}).get("message", "Unknown error") if proposal_response else "No response"
            return None, f"Proposal Error: {error_msg}"
        
        if "proposal" not in proposal_response:
            return None, "Invalid proposal response"
        
        proposal_id = proposal_response["proposal"]["id"]
        ask_price = proposal_response["proposal"]["ask_price"]
        
        buy_request = {
            "buy": proposal_id,
            "price": ask_price
        }
        
        buy_response = await self.send_request(buy_request)
        
        if not buy_response or "error" in buy_response:
            error_msg = buy_response.get("error", {}).get("message", "Unknown error") if buy_response else "No response"
            return None, f"Buy Error: {error_msg}"
        
        if "buy" not in buy_response:
            return None, "Invalid buy response"
        
        contract_id = buy_response["buy"]["contract_id"]
        
        return contract_id, None
    
    async def monitor_contract(self, contract_id):
        """Monitor a contract until completion"""
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
                            "forget": contract.get("subscription", {}).get("id", contract_id), 
                            "req_id": self.get_next_request_id()
                        }
                        await self.ws.send(json.dumps(forget_request))
                        
                        return {
                            "profit": profit,
                            "status": "win" if profit > 0 else "loss",
                            "contract_id": contract_id
                        }
                    
                    tick_count += 1
                    current_profit = float(contract.get("profit", 0))

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
    
    async def execute_trade(self):
        """Execute a single trade and return results"""
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


def run_async_trade(api_token, app_id, stake, target_ticks, growth_rate, symbol):
    """Run async trade in a new event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    bot = DerivAccumulatorBot(api_token, app_id)
    bot.stake_per_trade = stake
    bot.target_ticks = target_ticks
    bot.accumulator_range = growth_rate
    bot.symbol = symbol
    
    result = loop.run_until_complete(bot.execute_trade())
    
    # Store result
    trade_results[result['trade_id']] = result
    
    loop.close()
    return result


@app.route('/trade/<app_id>/<api_token>', methods=['POST'])
def execute_trade(app_id, api_token):
    """
    POST /trade/<app_id>/<api_token>
    Execute a single accumulator trade
    
    Route Parameters:
    - app_id (required): Deriv app ID
    - api_token (required): Deriv API token
    
    JSON Body Parameters (optional):
    - stake (optional): Stake amount per trade (default: 10.0)
    - target_ticks (optional): Number of ticks before selling (default: 1)
    - growth_rate (optional): Accumulator growth rate (default: 0.03)
    - symbol (optional): Trading symbol (default: "1HZ10V")
    """
    try:
        # Get optional parameters from JSON body
        data = request.json if request.json else {}
        
        stake = float(data.get('stake', 10.0))
        target_ticks = int(data.get('target_ticks', 1))
        growth_rate = float(data.get('growth_rate', 0.03))
        symbol = data.get('symbol', '1HZ10V')
        
        # Run trade in a separate thread to avoid blocking
        result = run_async_trade(api_token, app_id, stake, target_ticks, growth_rate, symbol)
        
        return jsonify(result), 200 if result['success'] else 400
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/trade/<trade_id>', methods=['GET'])
def get_trade_result(trade_id):
    """
    GET /trade/<trade_id>
    Get the result of a specific trade
    
    Returns trade status, profit/loss, and details
    """
    if trade_id not in trade_results:
        return jsonify({
            "success": False,
            "error": "Trade ID not found"
        }), 404
    
    result = trade_results[trade_id]
    
    response = {
        "trade_id": trade_id,
        "found": True
    }
    
    if result.get('success'):
        profit = result.get('profit', 0)
        response.update({
            "status": result.get('status'),
            "profit_loss": profit,
            "result": "PROFIT" if profit > 0 else "LOSS",
            "amount": abs(profit),
            "contract_id": result.get('contract_id'),
            "final_balance": result.get('final_balance'),
            "timestamp": result.get('timestamp')
        })
    else:
        response.update({
            "status": "failed",
            "error": result.get('error')
        })
    
    return jsonify(response), 200


@app.route('/trades', methods=['GET'])
def get_all_trades():
    """
    GET /trades
    Get all trade results
    """
    return jsonify({
        "total_trades": len(trade_results),
        "trades": trade_results
    }), 200


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "Deriv Accumulator Trading API",
        "timestamp": datetime.now().isoformat()
    }), 200


if __name__ == '__main__':
    print("""
    ⚠️  IMPORTANT WARNINGS:
    ==========================================
    1. TEST ON DEMO ACCOUNT FIRST!
    2. Accumulator trading is HIGH RISK
    3. You can lose your entire stake
    4. Never trade with money you can't afford to lose
    ==========================================
    
    🚀 API Server Starting...
    📍 Base URL: http://localhost:5000
    
    API Endpoints:
    - POST   http://localhost:5000/trade/<app_id>/<api_token>
    - GET    http://localhost:5000/trade/<trade_id>
    - GET    http://localhost:5000/trades
    - GET    http://localhost:5000/health
    
    Example:
    POST http://localhost:5000/trade/110971/YOUR_API_TOKEN
    ==========================================
    """)
    
    app.run(debug=True, host='0.0.0.0', port=5000)