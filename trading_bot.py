import asyncio
import json
import websockets
import socket
from datetime import datetime
from collections import deque
import statistics

class DerivAccumulatorBot:
    def __init__(self, api_token="TzNY35kJ0wy78CI", app_id=110971):
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
        self.pending_requests = {}
        
        # Bot Configuration 
        self.stake_per_trade = 20.0
        self.daily_profit_target = 2.0
        self.max_daily_loss = 20.0
        self.target_ticks = 1        # <-- NEW: The number of ticks to wait before selling
        self.accumulator_range = 0.03 # <--- GROWTH RATE
        self.symbol = "1HZ10V"       # Volatility 10 (1s) Index
        self.contract_type = "ACCU"  # <--- Contract type is now ACCU
        
        self.min_balance_buffer = 10.0
        
        # Trading state
        self.daily_profit = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.is_trading = False
        self.account_balance = 0.0
        self.symbol_available = False
        self.consecutive_errors = 0
        self.max_consecutive_errors = 3
        
    def get_next_request_id(self):
        """Get next request ID for tracking responses"""
        self.request_id += 1
        return self.request_id
        
    async def connect(self, retry_count=0, max_retries=3):
        """Connect to Deriv WebSocket API with retry logic"""
        if retry_count > 0:
            print(f"🔄 Retry attempt {retry_count}/{max_retries}...")
        
        for ws_url in self.ws_urls:
            self.ws_url = ws_url
            try:
                print(f"[v0] Attempting connection to {ws_url}")
                self.ws = await asyncio.wait_for(
                    websockets.connect(self.ws_url),
                    timeout=15.0
                )
                print(f"✅ Connected to Deriv API at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

                auth_success = await self.authorize()
                if not auth_success:
                    await self.ws.close()
                    self.ws = None
                    raise Exception("Authorization failed")
                
                # We can remove the background listener as we only run once
                # asyncio.create_task(self.listen_for_responses()) 
                
                return True
                    
            except asyncio.TimeoutError:
                print(f"⏱️  Connection timeout on {ws_url}")
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                continue
            except Exception as e:
                print(f"❌ Error on {ws_url}: {str(e)}")
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                continue
        
        if retry_count < max_retries:
            wait_time = 10 * (2 ** retry_count)
            print(f"\n⏳ All connection attempts failed. Retrying in {wait_time} seconds...")
            await asyncio.sleep(wait_time)
            return await self.connect(retry_count=retry_count + 1, max_retries=max_retries)
        else:
            print(f"\n❌ Connection Error: Could not connect after {max_retries} retries")
            raise Exception("Failed to connect to Deriv API")
    
    async def authorize(self):
        """Authorize with API token"""
        if not self.api_token:
            print("❌ INVALID TOKEN: API token is empty")
            return False
        
        auth_request = {
            "authorize": self.api_token,
            "req_id": self.get_next_request_id()
        }
        
        print(f"[v0] Sending authorization request...")
        await self.ws.send(json.dumps(auth_request))
        
        try:
            response = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            data = json.loads(response)
            
        except asyncio.TimeoutError:
            print(f"❌ Authorization timeout: No response from Deriv within 10 seconds")
            return False
        
        if "error" in data:
            error_msg = data['error']['message']
            print(f"❌ Authorization failed: {error_msg}")
            return False
        
        if "authorize" in data:
            print(f"✅ Authorized! Account: {data['authorize']['loginid']}")
            self.account_balance = float(data['authorize']['balance'])
            print(f"💰 Balance: ${self.account_balance:.2f} {data['authorize']['currency']}")
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
            print(f"[v0] Balance timeout")
        
        return self.account_balance
    
    async def validate_symbol(self):
        """Check if the symbol is available for trading"""
        print(f"🔍 Validating symbol '{self.symbol}'...")
        
        try:
            spec_request = {
                "contracts_for": self.symbol,
                "req_id": self.get_next_request_id()
            }
            
            await self.ws.send(json.dumps(spec_request))
            response = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
            data = json.loads(response)
            
            if "error" in data:
                print(f"❌ Symbol validation failed: {data['error']['message']}")
                return False
            
            if "contracts_for" in data:
                contracts = data["contracts_for"].get("available", [])
                # Check for the correct ACCU contract type
                has_accu = any(c.get("contract_type") == "ACCU" for c in contracts) 
                
                if has_accu:
                    print(f"✅ Symbol '{self.symbol}' supports Accumulator contracts")
                    self.symbol_available = True
                    return True
                else:
                    print(f"❌ Symbol '{self.symbol}' doesn't support Accumulator contracts")
                    return False
                    
        except asyncio.TimeoutError:
            print(f"[v0] Symbol validation timeout")
        except Exception as e:
            print(f"❌ Symbol validation error: {str(e)}")
        
        return False
    
    async def check_trade_validity(self):
        """Perform pre-trade checks before placing any trade"""
        current_balance = await self.get_balance()
        required_balance = self.stake_per_trade + self.min_balance_buffer
        
        if current_balance < required_balance:
            print(f"⚠️  Insufficient balance: ${current_balance:.2f} (need ${required_balance:.2f})")
            return False
        
        if not self.symbol_available:
            if not await self.validate_symbol():
                return False
        
        # Removed daily profit/loss checks as we only run once
        
        return True
    
    async def send_request(self, request):
        """Send request and get response directly without background listener"""
        req_id = self.get_next_request_id()
        request["req_id"] = req_id
        
        try:
            await self.ws.send(json.dumps(request))
            print(f"[v0] Sent request {req_id}: {request.get('proposal', request.get('buy', 'unknown'))}")
        except Exception as e:
            print(f"[v0] Failed to send request: {e}")
            return None
        
        # Wait for matching response
        try:
            while True:
                response_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                data = json.loads(response_text)
                
                print(f"[v0] Received response for req_id: {data.get('req_id', 'N/A')}")
                
                # Check if this is our response
                if data.get("req_id") == req_id:
                    return data
                    
                # If it's a subscription update (like the one in monitor_contract), ignore and continue
                if "subscription" in data:
                    continue
                    
        except asyncio.TimeoutError:
            print(f"[v0] Request {req_id} timed out after 10s")
            return None
        except Exception as e:
            print(f"[v0] Error receiving response: {e}")
            return None

    # The listen_for_responses method is no longer needed as we'll close after one trade.
    # It has been commented out/removed.

    async def place_accumulator_trade(self):
        """Place an Accumulator trade"""
        if not await self.check_trade_validity():
            return None
        
        contract_type = "ACCU" 
        
        proposal_request = {
            "proposal": 1,
            "amount": self.stake_per_trade,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            # Duration parameters are NOT included for Accumulator contracts
            "symbol": self.symbol,
            "growth_rate": self.accumulator_range # Required for ACCU contracts
        }
        
        print(f"[v0] Requesting price proposal for ACCUMULATOR...")
        proposal_response = await self.send_request(proposal_request)
        
        if not proposal_response or "error" in proposal_response:
            error_msg = proposal_response.get("error", {}).get("message", "Unknown error") if proposal_response else "No response"
            print(f"❌ Proposal Error: {error_msg}")
            self.consecutive_errors += 1
            return None
        
        if "proposal" not in proposal_response:
            print(f"[v0] Invalid proposal response")
            return None
        
        proposal_id = proposal_response["proposal"]["id"]
        payout = proposal_response["proposal"]["payout"]
        ask_price = proposal_response["proposal"]["ask_price"]
        potential_profit = payout - ask_price
        
        print(f"✅ Proposal received - Stake: ${ask_price:.2f}, Potential Profit: ${potential_profit:.2f}, Growth Rate: {self.accumulator_range}")
        
        buy_request = {
            "buy": proposal_id,
            "price": ask_price
        }
        
        print(f"[v0] Placing ACCUMULATOR trade using proposal {proposal_id}...")
        buy_response = await self.send_request(buy_request)
        
        if not buy_response or "error" in buy_response:
            error_msg = buy_response.get("error", {}).get("message", "Unknown error") if buy_response else "No response"
            print(f"❌ Buy Error: {error_msg}")
            self.consecutive_errors += 1
            return None
        
        if "buy" not in buy_response:
            print(f"[v0] Invalid buy response")
            self.consecutive_errors += 1
            return None
        
        self.consecutive_errors = 0
        contract_id = buy_response["buy"]["contract_id"]
        
        print(f"\n📊 Trade #{self.total_trades + 1} Placed - ACCUMULATOR")
        print(f"   Contract ID: {contract_id}")
        print(f"   Stake: ${ask_price:.2f}")
        print(f"   Potential Payout: ${payout:.2f}")
        print(f"   Potential Profit: ${potential_profit:.2f}")
        print(f"   Target Ticks: {self.target_ticks}")
        print(f"   Growth Rate: {self.accumulator_range}")
        print(f"   Symbol: {self.symbol}")
        print(f"   Time: {datetime.now().strftime('%H:%M:%S')}")
        
        return contract_id
    
    async def monitor_contract(self, contract_id):
        """Monitor a contract until completion, selling after a fixed number of ticks."""
        req_id = self.get_next_request_id()
        proposal_request = {
            "proposal_open_contract": 1,
            "contract_id": contract_id,
            "subscribe": 1,
            "req_id": req_id
        }
        
        await self.ws.send(json.dumps(proposal_request))
        
        tick_count = 0 # Initialize tick counter
        
        while True:
            try:
                # Wait for next contract update
                response = await asyncio.wait_for(self.ws.recv(), timeout=20.0) 
                data = json.loads(response)
                
                if "proposal_open_contract" in data:
                    contract = data["proposal_open_contract"]
                    
                    # 1. Check if contract is already finished (Loss or confirmation of Sell)
                    if contract.get("is_sold") or contract.get("status") == "sold":
                        profit = float(contract.get("profit", 0))
                        self.daily_profit += profit
                        self.total_trades += 1
                        
                        if profit > 0:
                            self.winning_trades += 1
                            print(f"\n✅ PROFIT: ${profit:.2f}")
                        else:
                            self.losing_trades += 1
                            print(f"\n❌ LOSS: ${profit:.2f}")
                        
                        print(f"📈 Total P&L: ${self.daily_profit:.2f}")
                        print(f"📊 Win Rate: {self.winning_trades}/{self.total_trades} ({(self.winning_trades/self.total_trades*100) if self.total_trades > 0 else 0:.1f}%)")
                        
                        # Unsubscribe from updates
                        forget_request = {"forget": contract.get("subscription", {}).get("id", contract_id), "req_id": self.get_next_request_id()}
                        await self.ws.send(json.dumps(forget_request))
                        
                        return profit
                    
                    # 2. Accumulator Tick Counting and Selling Logic
                    tick_count += 1
                    current_profit = float(contract.get("profit", 0))
                    current_payout = float(contract.get("current_stake", 0)) 
                    
                    # Use print to show progress and flush the output
                    print(f"   ⏳ Tick {tick_count}/{self.target_ticks} | Profit: ${current_profit:.2f} (Payout: ${current_payout:.2f})", end="\r", flush=True)

                    if tick_count >= self.target_ticks:
                        # Clear the last line for cleaner output
                        print(" " * 80, end="\r") 
                        print(f"\n⏱️  TARGET TICKS ({self.target_ticks}) REACHED. Selling contract {contract_id}...")
                        
                        # --- SEND THE SELL REQUEST ---
                        sell_request = {
                            "sell": contract_id,
                            "price": 0.0, # 0.0 means 'sell at current market price'
                            "req_id": self.get_next_request_id()
                        }
                        # We use send_request to ensure the sell request is processed and we get a response
                        await self.send_request(sell_request)
                        # The final result will be confirmed in the next 'proposal_open_contract' response.
                        
                elif "error" in data:
                    print(f"\n❌ Monitoring Error: {data['error']['message']}")
                    # Unsubscribe if an error occurs
                    if data.get('echo_req', {}).get('proposal_open_contract'):
                          forget_id = data.get("echo_req", {}).get('contract_id', contract_id)
                          forget_request = {"forget": forget_id, "req_id": self.get_next_request_id()}
                          await self.ws.send(json.dumps(forget_request))
                    return None

            except asyncio.TimeoutError:
                print(f"\n[v0] Contract monitoring timeout. Trade result uncertain.")
                return None
            except Exception as e:
                print(f"\n[v0] Monitoring error: {e}")
                return None
    
    async def run_trading_session(self):
        """Run the main trading session (MODIFIED TO RUN ONLY ONCE)"""
        if not await self.validate_symbol():
            print("❌ Cannot start trading: Symbol validation failed")
            return
        
        print("\n" + "="*60)
        print("🤖 DERIV ACCUMULATOR BOT - SINGLE-TRADE MODE")
        print("="*60)
        print(f"💵 Stake per trade: ${self.stake_per_trade}")
        print(f"📊 Contract: ACCUMULATOR (ACCU)")
        print(f"⏱️  Target Ticks: {self.target_ticks}")
        print(f"📈 Growth Rate: {self.accumulator_range}")
        print(f"✅ Symbol: {self.symbol}")
        print("="*60 + "\n")
        
        print("✅ Ready to trade! Starting single trade...\n")
        
        try:
            # --- START SINGLE TRADE LOGIC ---
            contract_id = await self.place_accumulator_trade()
            
            if contract_id:
                await self.monitor_contract(contract_id)
                
                # Removed wait time after successful trade
            else:
                # Handle single trade failure logic
                wait_time = 2 + (self.consecutive_errors * 2) 
                print(f"⏸️  Failed to place trade. Waiting {wait_time} seconds before exit...")
                await asyncio.sleep(wait_time)
                
            # --- END SINGLE TRADE LOGIC ---

        except KeyboardInterrupt:
            print("\n\n⚠️  Bot stopped by user")
        except Exception as e:
            print(f"\n\n❌ Error: {str(e)}")
        finally:
            await self.show_summary()
            
    async def show_summary(self):
        """Show trading session summary"""
        print("\n" + "="*60)
        print("📊 TRADING SESSION SUMMARY")
        print("="*60)
        print(f"Total Trades: {self.total_trades}")
        print(f"Winning Trades: {self.winning_trades}")
        print(f"Losing Trades: {self.losing_trades}")
        
        if self.total_trades > 0:
            win_rate = (self.winning_trades / self.total_trades) * 100
            print(f"Win Rate: {win_rate:.1f}%")
        
        print(f"\n{'TOTAL PROFIT' if self.daily_profit > 0 else 'TOTAL LOSS'}: ${abs(self.daily_profit):.2f}")
        print("="*60 + "\n")
        
        balance = await self.get_balance()
        print(f"💰 Final Balance: ${balance:.2f}")
    
    async def start(self):
        """Start the bot"""
        try:
            await self.connect()
            await self.run_trading_session()
        finally:
            if self.ws:
                await self.ws.close()
                print("🔌 Disconnected from Deriv API")


# Main execution
async def main():
    API_TOKEN = "TzNY35kJ0wy78CI"
    
    # ⚠️ IMPORTANT: Please replace the placeholder API_TOKEN with your actual token.
    # The script will run with the placeholder, but the authorization will fail.
    
    if not API_TOKEN:
        print("❌ ERROR: Please set your API token")
        return
    
    bot = DerivAccumulatorBot(api_token=API_TOKEN)
    await bot.start()


if __name__ == "__main__":
    print("""
    ⚠️  IMPORTANT WARNINGS:
    ==========================================
    1. TEST ON DEMO ACCOUNT FIRST!
    2. Accumulator trading is HIGH RISK
    3. You can lose your entire stake
    4. Past performance ≠ future results
    5. Never trade with money you can't afford to lose
    ==========================================
    """)
    
    asyncio.run(main())