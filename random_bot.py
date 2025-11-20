import asyncio
import json
import websockets
import os
import sys
import random
from datetime import datetime, timedelta
from pathlib import Path

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
        
        # Bot Configuration 
        self.stake_per_trade = 20.0
        self.daily_profit_target = 2.0
        self.max_daily_loss = 20.0
        self.target_ticks = 1
        self.accumulator_range = 0.03
        self.symbol = "1HZ10V"
        self.contract_type = "ACCU"
        self.min_balance_buffer = 10.0
        
        # Random timing configuration (in seconds)
        self.min_wait_seconds = 180   # 3 minutes minimum between trades
        self.max_wait_seconds = 900   # 15 minutes maximum between trades
        
        # Trading state
        self.daily_profit = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.is_trading = True
        self.account_balance = 0.0
        self.symbol_available = False
        self.consecutive_errors = 0
        self.max_consecutive_errors = 3
        
        # Logging
        self.log_file = Path(__file__).parent / "deriv_bot.log"
        
    def log(self, message):
        """Log messages to both console and file"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_message = f"[{timestamp}] {message}"
        print(log_message)
        
        try:
            with open(self.log_file, 'a') as f:
                f.write(log_message + '\n')
        except Exception as e:
            print(f"Failed to write to log file: {e}")
    
    def get_random_wait_time(self):
        """Get random wait time between trades"""
        wait_seconds = random.randint(self.min_wait_seconds, self.max_wait_seconds)
        return wait_seconds
        
    def get_next_request_id(self):
        """Get next request ID for tracking responses"""
        self.request_id += 1
        return self.request_id
        
    async def connect(self, retry_count=0, max_retries=3):
        """Connect to Deriv WebSocket API with retry logic"""
        if retry_count > 0:
            self.log(f"🔄 Retry attempt {retry_count}/{max_retries}...")
        
        for ws_url in self.ws_urls:
            self.ws_url = ws_url
            try:
                self.log(f"Attempting connection to {ws_url}")
                self.ws = await asyncio.wait_for(
                    websockets.connect(self.ws_url),
                    timeout=15.0
                )
                self.log(f"✅ Connected to Deriv API")

                auth_success = await self.authorize()
                if not auth_success:
                    await self.ws.close()
                    self.ws = None
                    raise Exception("Authorization failed")
                
                return True
                    
            except asyncio.TimeoutError:
                self.log(f"⏱️  Connection timeout on {ws_url}")
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                continue
            except Exception as e:
                self.log(f"❌ Error on {ws_url}: {str(e)}")
                if self.ws:
                    await self.ws.close()
                    self.ws = None
                continue
        
        if retry_count < max_retries:
            wait_time = 10 * (2 ** retry_count)
            self.log(f"⏳ All connection attempts failed. Retrying in {wait_time} seconds...")
            await asyncio.sleep(wait_time)
            return await self.connect(retry_count=retry_count + 1, max_retries=max_retries)
        else:
            self.log(f"❌ Connection Error: Could not connect after {max_retries} retries")
            raise Exception("Failed to connect to Deriv API")
    
    async def authorize(self):
        """Authorize with API token"""
        if not self.api_token:
            self.log("❌ INVALID TOKEN: API token is empty")
            return False
        
        auth_request = {
            "authorize": self.api_token,
            "req_id": self.get_next_request_id()
        }
        
        self.log(f"Sending authorization request...")
        await self.ws.send(json.dumps(auth_request))
        
        try:
            response = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            data = json.loads(response)
        except asyncio.TimeoutError:
            self.log(f"❌ Authorization timeout")
            return False
        
        if "error" in data:
            error_msg = data['error']['message']
            self.log(f"❌ Authorization failed: {error_msg}")
            return False
        
        if "authorize" in data:
            self.log(f"✅ Authorized! Account: {data['authorize']['loginid']}")
            self.account_balance = float(data['authorize']['balance'])
            self.log(f"💰 Balance: ${self.account_balance:.2f} {data['authorize']['currency']}")
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
            self.log(f"Balance timeout")
        
        return self.account_balance
    
    async def validate_symbol(self):
        """Check if the symbol is available for trading"""
        self.log(f"🔍 Validating symbol '{self.symbol}'...")
        
        try:
            spec_request = {
                "contracts_for": self.symbol,
                "req_id": self.get_next_request_id()
            }
            
            await self.ws.send(json.dumps(spec_request))
            response = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
            data = json.loads(response)
            
            if "error" in data:
                self.log(f"❌ Symbol validation failed: {data['error']['message']}")
                return False
            
            if "contracts_for" in data:
                contracts = data["contracts_for"].get("available", [])
                has_accu = any(c.get("contract_type") == "ACCU" for c in contracts) 
                
                if has_accu:
                    self.log(f"✅ Symbol '{self.symbol}' supports Accumulator contracts")
                    self.symbol_available = True
                    return True
                else:
                    self.log(f"❌ Symbol '{self.symbol}' doesn't support Accumulator contracts")
                    return False
                    
        except asyncio.TimeoutError:
            self.log(f"Symbol validation timeout")
        except Exception as e:
            self.log(f"❌ Symbol validation error: {str(e)}")
        
        return False
    
    async def check_trade_validity(self):
        """Perform pre-trade checks before placing any trade"""
        current_balance = await self.get_balance()
        required_balance = self.stake_per_trade + self.min_balance_buffer
        
        if current_balance < required_balance:
            self.log(f"⚠️  Insufficient balance: ${current_balance:.2f} (need ${required_balance:.2f})")
            return False
        
        if not self.symbol_available:
            if not await self.validate_symbol():
                return False
        
        # Check daily profit target
        if self.daily_profit >= self.daily_profit_target:
            self.log(f"✅ Daily profit target reached: ${self.daily_profit:.2f} >= ${self.daily_profit_target:.2f}")
            self.is_trading = False
            return False
        
        # Check daily loss limit
        if self.daily_profit <= -self.max_daily_loss:
            self.log(f"⚠️  Daily loss limit reached: ${self.daily_profit:.2f} <= -${self.max_daily_loss:.2f}")
            self.is_trading = False
            return False
        
        return True
    
    async def send_request(self, request):
        """Send request and get response"""
        req_id = self.get_next_request_id()
        request["req_id"] = req_id
        
        try:
            await self.ws.send(json.dumps(request))
        except Exception as e:
            self.log(f"Failed to send request: {e}")
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
            self.log(f"Request {req_id} timed out")
            return None
        except Exception as e:
            self.log(f"Error receiving response: {e}")
            return None

    async def place_accumulator_trade(self):
        """Place an Accumulator trade"""
        if not await self.check_trade_validity():
            return None
        
        proposal_request = {
            "proposal": 1,
            "amount": self.stake_per_trade,
            "basis": "stake",
            "contract_type": "ACCU",
            "currency": "USD",
            "symbol": self.symbol,
            "growth_rate": self.accumulator_range
        }
        
        self.log(f"Requesting price proposal for ACCUMULATOR...")
        proposal_response = await self.send_request(proposal_request)
        
        if not proposal_response or "error" in proposal_response:
            error_msg = proposal_response.get("error", {}).get("message", "Unknown error") if proposal_response else "No response"
            self.log(f"❌ Proposal Error: {error_msg}")
            self.consecutive_errors += 1
            return None
        
        if "proposal" not in proposal_response:
            self.log(f"Invalid proposal response")
            return None
        
        proposal_id = proposal_response["proposal"]["id"]
        payout = proposal_response["proposal"]["payout"]
        ask_price = proposal_response["proposal"]["ask_price"]
        potential_profit = payout - ask_price
        
        self.log(f"✅ Proposal received - Stake: ${ask_price:.2f}, Potential Profit: ${potential_profit:.2f}")
        
        buy_request = {
            "buy": proposal_id,
            "price": ask_price
        }
        
        self.log(f"Placing ACCUMULATOR trade...")
        buy_response = await self.send_request(buy_request)
        
        if not buy_response or "error" in buy_response:
            error_msg = buy_response.get("error", {}).get("message", "Unknown error") if buy_response else "No response"
            self.log(f"❌ Buy Error: {error_msg}")
            self.consecutive_errors += 1
            return None
        
        if "buy" not in buy_response:
            self.log(f"Invalid buy response")
            self.consecutive_errors += 1
            return None
        
        self.consecutive_errors = 0
        contract_id = buy_response["buy"]["contract_id"]
        
        self.log(f"\n📊 Trade #{self.total_trades + 1} Placed - ACCUMULATOR")
        self.log(f"   Contract ID: {contract_id}")
        self.log(f"   Stake: ${ask_price:.2f}")
        self.log(f"   Potential Payout: ${payout:.2f}")
        self.log(f"   Target Ticks: {self.target_ticks}")
        self.log(f"   Time: {datetime.now().strftime('%H:%M:%S')}")
        
        return contract_id
    
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
                        self.daily_profit += profit
                        self.total_trades += 1
                        
                        if profit > 0:
                            self.winning_trades += 1
                            self.log(f"\n✅ PROFIT: ${profit:.2f}")
                        else:
                            self.losing_trades += 1
                            self.log(f"\n❌ LOSS: ${profit:.2f}")
                        
                        self.log(f"📈 Daily P&L: ${self.daily_profit:.2f}")
                        self.log(f"📊 Win Rate: {self.winning_trades}/{self.total_trades} ({(self.winning_trades/self.total_trades*100) if self.total_trades > 0 else 0:.1f}%)")
                        
                        forget_request = {"forget": contract.get("subscription", {}).get("id", contract_id), "req_id": self.get_next_request_id()}
                        await self.ws.send(json.dumps(forget_request))
                        
                        return profit
                    
                    tick_count += 1
                    current_profit = float(contract.get("profit", 0))
                    current_payout = float(contract.get("current_stake", 0))
                    
                    print(f"   ⏳ Tick {tick_count}/{self.target_ticks} | Profit: ${current_profit:.2f} (Payout: ${current_payout:.2f})", end="\r", flush=True)

                    if tick_count >= self.target_ticks:
                        print(" " * 80, end="\r")
                        self.log(f"\n⏱️  TARGET TICKS ({self.target_ticks}) REACHED. Selling contract...")
                        
                        sell_request = {
                            "sell": contract_id,
                            "price": 0.0,
                            "req_id": self.get_next_request_id()
                        }
                        await self.send_request(sell_request)
                        
                elif "error" in data:
                    self.log(f"\n❌ Monitoring Error: {data['error']['message']}")
                    return None

            except asyncio.TimeoutError:
                self.log(f"\nContract monitoring timeout")
                return None
            except Exception as e:
                self.log(f"\nMonitoring error: {e}")
                return None
    
    async def run_trading_session(self):
        """Run continuous trading session with random intervals"""
        if not await self.validate_symbol():
            self.log("❌ Cannot start trading: Symbol validation failed")
            return
        
        self.log("\n" + "="*60)
        self.log("🤖 DERIV ACCUMULATOR BOT - RANDOM TIMING MODE")
        self.log("="*60)
        self.log(f"💵 Stake per trade: ${self.stake_per_trade}")
        self.log(f"🎯 Daily profit target: ${self.daily_profit_target}")
        self.log(f"🛡️  Max daily loss: ${self.max_daily_loss}")
        self.log(f"⏱️  Random wait time: {self.min_wait_seconds//60}-{self.max_wait_seconds//60} minutes")
        self.log(f"📊 Contract: ACCUMULATOR (ACCU)")
        self.log(f"📈 Growth Rate: {self.accumulator_range}")
        self.log(f"✅ Symbol: {self.symbol}")
        self.log("="*60 + "\n")
        
        self.log("✅ Bot is ready! Trading will start with random intervals...\n")
        
        try:
            while self.is_trading:
                # Check for too many consecutive errors
                if self.consecutive_errors >= self.max_consecutive_errors:
                    self.log(f"⚠️  Too many consecutive errors ({self.consecutive_errors}). Pausing for 5 minutes...")
                    await asyncio.sleep(300)
                    self.consecutive_errors = 0
                    continue
                
                # Place and monitor trade
                contract_id = await self.place_accumulator_trade()
                
                if contract_id:
                    await self.monitor_contract(contract_id)
                    
                    # Random wait time before next trade
                    wait_seconds = self.get_random_wait_time()
                    wait_minutes = wait_seconds / 60
                    next_trade_time = datetime.now() + timedelta(seconds=wait_seconds)
                    
                    self.log(f"\n🎲 Waiting {wait_minutes:.1f} minutes until next trade...")
                    self.log(f"⏰ Next trade at approximately: {next_trade_time.strftime('%H:%M:%S')}\n")
                    
                    await asyncio.sleep(wait_seconds)
                else:
                    # If trade failed, wait shorter time before retry
                    wait_time = 30 + (self.consecutive_errors * 30)
                    self.log(f"⏸️  Waiting {wait_time} seconds before retry...\n")
                    await asyncio.sleep(wait_time)

        except KeyboardInterrupt:
            self.log("\n\n⚠️  Bot stopped by user")
        except Exception as e:
            self.log(f"\n\n❌ Error: {str(e)}")
        finally:
            await self.show_summary()
            
    async def show_summary(self):
        """Show trading session summary"""
        self.log("\n" + "="*60)
        self.log("📊 TRADING SESSION SUMMARY")
        self.log("="*60)
        self.log(f"Total Trades: {self.total_trades}")
        self.log(f"Winning Trades: {self.winning_trades}")
        self.log(f"Losing Trades: {self.losing_trades}")
        
        if self.total_trades > 0:
            win_rate = (self.winning_trades / self.total_trades) * 100
            self.log(f"Win Rate: {win_rate:.1f}%")
        
        self.log(f"\n{'TOTAL PROFIT' if self.daily_profit > 0 else 'TOTAL LOSS'}: ${abs(self.daily_profit):.2f}")
        self.log("="*60 + "\n")
        
        balance = await self.get_balance()
        self.log(f"💰 Final Balance: ${balance:.2f}")
    
    async def start(self):
        """Start the bot"""
        try:
            await self.connect()
            await self.run_trading_session()
        finally:
            if self.ws:
                await self.ws.close()
                self.log("🔌 Disconnected from Deriv API")


async def main():
    # Get API token from environment variable or hardcode it
    API_TOKEN = os.environ.get('DERIV_API_TOKEN', "TzNY35kJ0wy78CI")
    
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
    4. Bot runs continuously with random intervals
    5. Press Ctrl+C to stop the bot
    6. Never trade with money you can't afford to lose
    ==========================================
    """)
    
    asyncio.run(main())