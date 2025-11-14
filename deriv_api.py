import json
import websockets
from datetime import datetime
from threading import Thread
import uuid
import asyncio
import logging
import traceback

from flask import Flask, request, jsonify

# --- Global Configuration ---
# Set logging level to INFO. This suppresses any log messages using logging.DEBUG(), 
# preventing the "output too large" error from external schedulers.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
                # CRITICAL FIX: Replaced print() with logging.info()
                logging.info(f"[{self.trade_id}] ✅ Connected to Deriv API via {self.ws_url}")

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
            except Exception as e:
                logging.warning(f"[{self.trade_id}] Connection attempt failed: {e}")
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
            logging.error(f"[{self.trade_id}] Authorization response timeout.")
            return False
        except Exception as e:
            logging.error(f"[{self.trade_id}] Authorization failed: {e}")
            return False
        
        if "error" in data:
            logging.error(f"[{self.trade_id}] Authorization error: {data['error']['message']}")
            return False
        
        if "authorize" in data:
            self.account_balance = float(data['authorize']['balance'])
            logging.info(f"[{self.trade_id}] Account authorized. Balance: {self.account_balance}")
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
                logging.error(f"[{self.trade_id}] Symbol validation error: {response.get('error', {}).get('message', 'Unknown response')}")
                return False
            
            if "contracts_for" in response:
                contracts = response["contracts_for"].get("available", [])
                has_accu = any(c.get("contract_type") == self.contract_type for c in contracts) 
                
                if has_accu:
                    self.symbol_available = True
                    return True
                else:
                    logging.error(f"[{self.trade_id}] Symbol {self.symbol} available, but {self.contract_type} contract type is missing.")
                    return False
            
        except Exception as e:
            logging.error(f"[{self.trade_id}] Error validating symbol: {e}")
            pass
        
        return False
    
    async def send_request(self, request):
        """Send request and get response directly"""
        req_id = self.get_next_request_id()
        request["req_id"] = req_id
        
        try:
            await self.ws.send(json.dumps(request))
        except Exception as e:
            logging.error(f"[{self.trade_id}] WebSocket send failed: {e}")
            return None
        
        try:
            while True:
                response_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                data = json.loads(response_text)
                
                if data.get("req_id") == req_id:
                    return data
                    
                if "subscription" in data:
                    # Ignore subscription updates if we are waiting for a specific req_id
                    continue
                    
        except asyncio.TimeoutError:
            logging.error(f"[{self.trade_id}] Response timeout for request ID {req_id}")
            return None
        except Exception as e:
            logging.error(f"[{self.trade_id}] Response processing error: {e}")
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
            "currency": "USD", # Assuming USD, adjust if necessary
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
        logging.info(f"[{self.trade_id}] Trade placed. Contract ID: {contract_id}")
        
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
                        
                        logging.info(f"[{self.trade_id}] Contract sold. Profit: {profit}")
                        
                        return {
                            "profit": profit,
                            "status": "win" if profit > 0 else "loss",
                            "contract_id": contract_id
                        }
                    
                    # Only count ticks if entry spot is set (meaning contract is active)
                    if contract.get("entry_spot"):
                        tick_count += 1
                    
                    # CRITICAL FIX: Changed to logging.debug() to suppress verbose output
                    logging.debug(f"[{self.trade_id}] Tick {tick_count}/{self.target_ticks}. Current profit: {contract.get('profit', 'N/A')}")

                    if tick_count >= self.target_ticks:
                        logging.info(f"[{self.trade_id}] Target ticks ({self.target_ticks}) reached. Attempting to sell.")
                        sell_request = {
                            "sell": contract_id,
                            "price": 0.0, # Sell at market price
                            "req_id": self.get_next_request_id()
                        }
                        await self.send_request(sell_request)
                        # Continue loop to wait for the "is_sold" message
                        
                elif "error" in data:
                    logging.error(f"[{self.trade_id}] Monitoring error: {data['error']['message']}")
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
        """Execute a single trade and return results, running inside a thread."""
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
            logging.critical(f"[{self.trade_id}] CRITICAL: Trade thread crashed with error: {e}")
            traceback.print_exc()
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
    """
    Thread target: Creates a new event loop and runs the async bot execution.
    """
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
    
    # Store result using the correct ID
    trade_results[trade_id].update(result)
    
    loop.close()


app = Flask(__name__)

@app.route('/trade/<app_id>/<api_token>', methods=['POST'])
def execute_trade(app_id, api_token):
    """
    POST /trade/<app_id>/<api_token>
    Initiates a single accumulator trade in a non-blocking thread and immediately 
    returns a 202 Accepted status.
    """
    try:
        # Get optional parameters from JSON body
        data = request.get_json(silent=True) or {}
        
        stake = float(data.get('stake', 10.0))
        target_ticks = int(data.get('target_ticks', 1))
        growth_rate = float(data.get('growth_rate', 0.03))
        symbol = data.get('symbol', '1HZ10V')
        
        # 1. Generate unique trade ID
        new_trade_id = str(uuid.uuid4())
        
        # 2. Store initial PENDING status
        trade_results[new_trade_id] = {
            "status": "pending", 
            "timestamp": datetime.now().isoformat(),
            "app_id": app_id,
            "parameters": {'stake': stake, 'target_ticks': target_ticks, 'growth_rate': growth_rate, 'symbol': symbol}
        }
        
        logging.info(f"[{new_trade_id}] Trade initiated via API. Starting background thread.")

        # 3. Start the trade in a separate thread
        thread = Thread(
            target=run_async_trade_in_thread,
            args=(api_token, app_id, stake, target_ticks, growth_rate, symbol, new_trade_id)
        )
        thread.daemon = True # Allows server to exit even if thread is running
        thread.start()
        
        # 4. CRITICAL FIX: Return 202 ACCEPTED immediately to the cron job
        return jsonify({
            "status": "initiated",
            "message": "Trade request accepted and is running in the background.",
            "trade_id": new_trade_id,
            "check_url": f"/trade/{new_trade_id}"
        }), 202
        
    except Exception as e:
        logging.error(f"Flask execution error: {e}")
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": "Internal Server Error during initiation. Check Render logs."
        }), 500


@app.route('/trade/<trade_id>', methods=['GET'])
def get_trade_result(trade_id):
    """
    GET /trade/<trade_id>
    Get the result of a specific trade
    """
    result = trade_results.get(trade_id)
    
    if not result:
        return jsonify({
            "success": False,
            "error": "Trade ID not found"
        }), 404
    
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
    # Print warnings only when running locally for user safety
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