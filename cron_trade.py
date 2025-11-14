import asyncio
import json
import websockets
from datetime import datetime
import sys
import os

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
        
        # Bot Configuration from environment variables
        self.stake_per_trade = float(os.getenv('STAKE', '10.0'))
        self.target_ticks = int(os.getenv('TARGET_TICKS', '1'))
        self.accumulator_range = float(os.getenv('GROWTH_RATE', '0.03'))
        self.symbol = os.getenv('SYMBOL', '1HZ10V')
        self.contract_type = "ACCU"
        
        self.account_balance = 0.0
        self.symbol_available = False
        
    def get_next_request_id(self):
        """Get next request ID for tracking responses"""
        self.request_id += 1
        return self.request_id
        
    async def connect(self, retry_count=0, max_retries=3):
        """Connect to Deriv WebSocket API with retry logic"""
        for ws_url in self.ws_urls:
            self.ws_url = ws_url
            try:
                print(f"[{datetime.now()}] Connecting to {ws_url}...")
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
                print(f"⏱️ Connection timeout on {ws_url}")
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
            print(f"⏳ Retrying in {wait_time} seconds...")
            await asyncio.sleep(wait_time)
            return await self.connect(retry_count=retry_count + 1, max_retries=max_retries)
        else:
            raise Exception("Failed to connect to Deriv API")
    
    async def authorize(self):
        """Authorize with API token"""
        if not self.api_token:
            print("❌ API token is empty")
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
            print(f"❌ Authorization timeout")
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
            pass
        
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
                has_accu = any(c.get("contract_type") == "ACCU" for c in contracts) 
                
                if has_accu:
                    print(f"✅ Symbol '{self.symbol}' supports Accumulator contracts")
                    self.symbol_available = True
                    return True
                else:
                    print(f"❌ Symbol '{self.symbol}' doesn't support Accumulator contracts")
                    return False
                    
        except asyncio.TimeoutError:
            print(f"⏱️ Symbol validation timeout")
        except Exception as e:
            print(f"❌ Symbol validation error: {str(e)}")
        
        return False
    
    async def send_request(self, request):
        """Send request and get response"""
        req_id = self.get_next_request_id()
        request["req_id"] = req_id
        
        try:
            await self.ws.send(json.dumps(request))
        except Exception as e:
            print(f"❌ Failed to send request: {e}")
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
            print(f"⏱️ Request timeout")
            return None
        except Exception as e:
            print(f"❌ Error receiving response: {e}")
            return None

    async def place_accumulator_trade(self):
        """Place an Accumulator trade"""
        balance = await self.get_balance()
        if balance < self.stake_per_trade + 20.0:
            print(f"❌ Insufficient balance: ${balance:.2f}")
            return None
        
        if not self.symbol_available:
            if not await self.validate_symbol():
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
        
        print(f"📊 Requesting price proposal...")
        proposal_response = await self.send_request(proposal_request)
        
        if not proposal_response or "error" in proposal_response:
            error_msg = proposal_response.get("error", {}).get("message", "Unknown error") if proposal_response else "No response"
            print(f"❌ Proposal Error: {error_msg}")
            return None
        
        if "proposal" not in proposal_response:
            print(f"❌ Invalid proposal response")
            return None
        
        proposal_id = proposal_response["proposal"]["id"]
        payout = proposal_response["proposal"]["payout"]
        ask_price = proposal_response["proposal"]["ask_price"]
        potential_profit = payout - ask_price
        
        print(f"✅ Proposal received - Stake: ${ask_price:.2f}, Potential Profit: ${potential_profit:.2f}")
        
        buy_request = {
            "buy": proposal_id,
            "price": ask_price
        }
        
        print(f"🔄 Placing ACCUMULATOR trade...")
        buy_response = await self.send_request(buy_request)
        
        if not buy_response or "error" in buy_response:
            error_msg = buy_response.get("error", {}).get("message", "Unknown error") if buy_response else "No response"
            print(f"❌ Buy Error: {error_msg}")
            return None
        
        if "buy" not in buy_response:
            print(f"❌ Invalid buy response")
            return None
        
        contract_id = buy_response["buy"]["contract_id"]
        
        print(f"\n✅ Trade Placed!")
        print(f"   Contract ID: {contract_id}")
        print(f"   Stake: ${ask_price:.2f}")
        print(f"   Target Ticks: {self.target_ticks}")
        print(f"   Time: {datetime.now().strftime('%H:%M:%S')}")
        
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
                        
                        forget_request = {
                            "forget": contract.get("subscription", {}).get("id", contract_id), 
                            "req_id": self.get_next_request_id()
                        }
                        await self.ws.send(json.dumps(forget_request))
                        
                        if profit > 0:
                            print(f"\n✅ PROFIT: ${profit:.2f}")
                        else:
                            print(f"\n❌ LOSS: ${profit:.2f}")
                        
                        return profit
                    
                    tick_count += 1
                    current_profit = float(contract.get("profit", 0))
                    print(f"⏳ Tick {tick_count}/{self.target_ticks} | Profit: ${current_profit:.2f}")

                    if tick_count >= self.target_ticks:
                        print(f"\n⏱️ Target ticks reached. Selling...")
                        
                        sell_request = {
                            "sell": contract_id,
                            "price": 0.0,
                            "req_id": self.get_next_request_id()
                        }
                        await self.send_request(sell_request)
                        
                elif "error" in data:
                    print(f"\n❌ Monitoring Error: {data['error']['message']}")
                    return None

            except asyncio.TimeoutError:
                print(f"\n⏱️ Contract monitoring timeout")
                return None
            except Exception as e:
                print(f"\n❌ Monitoring error: {e}")
                return None
    
    async def execute_single_trade(self):
        """Execute a single trade"""
        try:
            await self.connect()
            
            if not await self.validate_symbol():
                print("❌ Symbol validation failed")
                return False
            
            contract_id = await self.place_accumulator_trade()
            
            if not contract_id:
                print("❌ Failed to place trade")
                return False
            
            profit = await self.monitor_contract(contract_id)
            
            if profit is not None:
                balance = await self.get_balance()
                print(f"\n💰 Final Balance: ${balance:.2f}")
                return True
            
            return False
            
        except Exception as e:
            print(f"\n❌ Error: {str(e)}")
            return False
        finally:
            if self.ws:
                await self.ws.close()
                print("🔌 Disconnected")


async def main():
    # Get credentials from environment variables
    API_TOKEN = os.getenv('API_TOKEN')
    APP_ID = os.getenv('APP_ID', '110971')
    
    if not API_TOKEN:
        print("❌ ERROR: API_TOKEN environment variable not set")
        sys.exit(1)
    
    print(f"""
    ⚠️  CRON JOB STARTED
    ==========================================
    Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    App ID: {APP_ID}
    Stake: ${os.getenv('STAKE', '10.0')}
    Target Ticks: {os.getenv('TARGET_TICKS', '1')}
    Growth Rate: {os.getenv('GROWTH_RATE', '0.03')}
    Symbol: {os.getenv('SYMBOL', '1HZ10V')}
    ==========================================
    """)
    
    bot = DerivAccumulatorBot(api_token=API_TOKEN, app_id=APP_ID)
    success = await bot.execute_single_trade()
    
    if success:
        print("\n✅ Cron job completed successfully")
        sys.exit(0)
    else:
        print("\n❌ Cron job failed")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())