import time
import requests
import os
from datetime import datetime
import sys

# Suppress output for Railway
sys.stdout.flush()

# Configuration
API_URL = os.environ.get('API_URL', 'http://localhost:5000')
APP_ID = os.environ.get('APP_ID')
API_TOKEN = os.environ.get('API_TOKEN')
STAKE = float(os.environ.get('STAKE', '10.0'))
TARGET_TICKS = int(os.environ.get('TARGET_TICKS', '1'))
GROWTH_RATE = float(os.environ.get('GROWTH_RATE', '0.03'))
SYMBOL = os.environ.get('SYMBOL', '1HZ10V')
INTERVAL_MINUTES = int(os.environ.get('INTERVAL_MINUTES', '15'))

def execute_trade():
    """Execute a single trade via API"""
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{timestamp}] Executing trade...")
        
        url = f"{API_URL}/trade/{APP_ID}/{API_TOKEN}"
        payload = {
            "stake": STAKE,
            "target_ticks": TARGET_TICKS,
            "growth_rate": GROWTH_RATE,
            "symbol": SYMBOL
        }
        
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 202:
            data = response.json()
            trade_id = data.get('trade_id')
            print(f"✓ Trade initiated: {trade_id[:8]}...")
            
            # Wait for trade to complete
            time.sleep(30)
            check_url = f"{API_URL}/trade/{trade_id}"
            result = requests.get(check_url, timeout=10)
            
            if result.status_code == 200:
                trade_data = result.json()
                status = trade_data.get('status')
                
                if status == 'completed' and trade_data.get('success'):
                    profit = trade_data.get('profit_loss', 0)
                    result_text = trade_data.get('result', 'UNKNOWN')
                    balance = trade_data.get('final_balance', 0)
                    print(f"✓ {result_text} | P/L: ${profit:.2f} | Balance: ${balance:.2f}")
                else:
                    print(f"⚠ Trade status: {status}")
            
            return True
        elif response.status_code == 429:
            print("⚠ Too many concurrent trades, skipping...")
            return False
        else:
            print(f"✗ Error: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"✗ Exception: {str(e)}")
        return False

def main():
    """Main loop - runs every 15 minutes"""
    print("=" * 50)
    print("Trading Bot Worker Started")
    print(f"Interval: Every {INTERVAL_MINUTES} minutes")
    print(f"Symbol: {SYMBOL} | Stake: ${STAKE}")
    print("=" * 50)
    
    while True:
        try:
            execute_trade()
            sleep_seconds = INTERVAL_MINUTES * 60
            print(f"Sleeping for {INTERVAL_MINUTES} minutes...\n")
            time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            print("\nShutting down...")
            break
        except Exception as e:
            print(f"Error in main loop: {e}")
            time.sleep(60)  # Wait 1 minute on error

if __name__ == '__main__':
    main()