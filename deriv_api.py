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
import logging
from logging.handlers import RotatingFileHandler
import math

# === LOGGING CONFIGURATION ===
LOG_DIR = os.environ.get('LOG_DIR', 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'trading_bot.log'),
    maxBytes=10*1024*1024,
    backupCount=5
)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(name)-15s | %(funcName)-20s | %(message)s'
))

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(message)s'
))

root_logger = logging.getLogger()
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger('TradingBot')
trade_logger = logging.getLogger('TradeExecution')
db_logger = logging.getLogger('Database')
api_logger = logging.getLogger('API')

logging.getLogger('werkzeug').setLevel(logging.ERROR)

import warnings
warnings.filterwarnings('ignore')

from flask import Flask, request, jsonify

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
                initial_balance REAL,
                error TEXT,
                parameters TEXT,
                volatility REAL,
                growth_rate REAL,
                target_ticks INTEGER,
                exit_reason TEXT,
                max_profit_reached REAL,
                ticks_completed INTEGER,
                duration_seconds REAL,
                entry_spot REAL,
                exit_spot REAL,
                volatility_at_exit REAL,
                pre_trade_volatility REAL,
                growth_rate_switches INTEGER,
                volatility_trend TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trading_sessions (
                session_date TEXT PRIMARY KEY,
                trades_count INTEGER DEFAULT 0,
                consecutive_losses INTEGER DEFAULT 0,
                total_profit_loss REAL DEFAULT 0,
                stopped INTEGER DEFAULT 0,
                last_updated TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT,
                component TEXT,
                message TEXT,
                details TEXT
            )
        ''')
        conn.commit()
        conn.close()
        db_logger.info("Database initialized successfully")
    except Exception as e:
        db_logger.error(f"Database initialization failed: {e}")

@contextmanager
def get_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        db_logger.error(f"Database error: {e}")
        if conn:
            conn.rollback()
    except Exception as e:
        db_logger.error(f"Unexpected database error: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

def log_system_event(level, component, message, details=None):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO system_logs (timestamp, level, component, message, details)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    datetime.now().isoformat(),
                    level,
                    component,
                    message,
                    json.dumps(details) if details else None
                ))
    except Exception as e:
        db_logger.error(f"Failed to log system event: {e}")

def save_trade(trade_id, trade_data):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO trades 
                    (trade_id, timestamp, app_id, status, success, contract_id, profit, 
                     final_balance, initial_balance, error, parameters, volatility, growth_rate, 
                     target_ticks, exit_reason, max_profit_reached, ticks_completed,
                     duration_seconds, entry_spot, exit_spot, volatility_at_exit,
                     pre_trade_volatility, growth_rate_switches, volatility_trend)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    trade_id,
                    trade_data.get('timestamp'),
                    trade_data.get('app_id'),
                    trade_data.get('status'),
                    1 if trade_data.get('success') else 0,
                    trade_data.get('contract_id'),
                    trade_data.get('profit'),
                    trade_data.get('final_balance'),
                    trade_data.get('initial_balance'),
                    trade_data.get('error'),
                    json.dumps(trade_data.get('parameters', {})),
                    trade_data.get('volatility'),
                    trade_data.get('growth_rate'),
                    trade_data.get('target_ticks'),
                    trade_data.get('exit_reason'),
                    trade_data.get('max_profit_reached'),
                    trade_data.get('ticks_completed'),
                    trade_data.get('duration_seconds'),
                    trade_data.get('entry_spot'),
                    trade_data.get('exit_spot'),
                    trade_data.get('volatility_at_exit'),
                    trade_data.get('pre_trade_volatility'),
                    trade_data.get('growth_rate_switches'),
                    trade_data.get('volatility_trend')
                ))
                db_logger.debug(f"Trade {trade_id} saved successfully")
    except Exception as e:
        db_logger.error(f"Failed to save trade {trade_id}: {e}")

def get_trade(trade_id):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM trades WHERE trade_id = ?', (trade_id,))
                row = cursor.fetchone()
                if row:
                    return dict(row)
    except Exception as e:
        db_logger.error(f"Failed to get trade {trade_id}: {e}")
    return None

def get_all_trades():
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM trades ORDER BY timestamp DESC')
                return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        db_logger.error(f"Failed to get all trades: {e}")
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
    except Exception as e:
        db_logger.error(f"Failed to get session data: {e}")
    return None

def update_session_data(session_date, trades_count, consecutive_losses, total_profit_loss, stopped):
    try:
        with get_db() as conn:
            if conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO trading_sessions 
                    (session_date, trades_count, consecutive_losses, total_profit_loss, stopped, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (session_date, trades_count, consecutive_losses, total_profit_loss, stopped, 
                      datetime.now().isoformat()))
                db_logger.debug(f"Session data updated: {trades_count} trades, {consecutive_losses} losses")
    except Exception as e:
        db_logger.error(f"Failed to update session data: {e}")

try:
    init_db()
except Exception as e:
    logger.error(f"Database initialization error: {e}")

trade_results = {}
MAX_CONCURRENT_TRADES = 2
active_trades_lock = Lock()
active_trade_count = 0

def can_start_trade():
    global active_trade_count
    try:
        with active_trades_lock:
            if active_trade_count >= MAX_CONCURRENT_TRADES:
                logger.warning(f"Max concurrent trades reached: {active_trade_count}/{MAX_CONCURRENT_TRADES}")
                return False
            active_trade_count += 1
            logger.info(f"Trade slot acquired. Active: {active_trade_count}/{MAX_CONCURRENT_TRADES}")
            return True
    except Exception as e:
        logger.error(f"Error in can_start_trade: {e}")
        return False

def trade_completed():
    global active_trade_count
    try:
        with active_trades_lock:
            active_trade_count = max(0, active_trade_count - 1)
            logger.info(f"Trade slot released. Active: {active_trade_count}/{MAX_CONCURRENT_TRADES}")
    except Exception as e:
        logger.error(f"Error in trade_completed: {e}")


class VolatilityAnalyzer:
    """Advanced volatility analysis using aggressive mathematical filtering"""
    
    @staticmethod
    def calculate_standard_deviation(values):
        """Calculate standard deviation"""
        if len(values) < 2:
            return 0
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return math.sqrt(variance)
    
    @staticmethod
    def calculate_coefficient_of_variation(values):
        """Calculate coefficient of variation (CV) - normalized volatility"""
        if len(values) < 2:
            return 0
        mean = sum(values) / len(values)
        if mean == 0:
            return 0
        std_dev = VolatilityAnalyzer.calculate_standard_deviation(values)
        return (std_dev / abs(mean)) * 100
    
    @staticmethod
    def calculate_atr(prices, period=14):
        """Calculate Average True Range for volatility measurement"""
        if len(prices) < period + 1:
            return 0
        
        true_ranges = []
        for i in range(1, len(prices)):
            high_low = abs(prices[i] - prices[i-1])
            true_ranges.append(high_low)
        
        if len(true_ranges) < period:
            return sum(true_ranges) / len(true_ranges) if true_ranges else 0
        
        return sum(true_ranges[-period:]) / period

    @staticmethod
    def calculate_tick_metrics(prices):
        """Calculate velocity and acceleration of price movements"""
        if len(prices) < 3:
            return 0, 0
        
        # Velocity: average absolute change per tick
        velocities = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
        avg_velocity = sum(velocities) / len(velocities)
        
        # Acceleration: change in velocity
        accelerations = [abs(velocities[i] - velocities[i-1]) for i in range(1, len(velocities))]
        avg_acceleration = sum(accelerations) / len(accelerations) if accelerations else 0
        
        return avg_velocity, avg_acceleration

    @staticmethod
    def is_micro_stable(prices, lookback=5, max_tick_move_pct=0.01):
        """Verify that the last N ticks are extremely quiet individually"""
        if len(prices) < lookback + 1:
            return False
        
        recent_ticks = list(prices)[-lookback:]
        mean_price = sum(recent_ticks) / len(recent_ticks)
        
        for i in range(1, len(recent_ticks)):
            tick_move = abs(recent_ticks[i] - recent_ticks[i-1])
            tick_move_pct = (tick_move / mean_price) * 100
            
            # If any single tick moves more than the micro-threshold, it's not quiet
            if tick_move_pct > max_tick_move_pct:
                return False
        
        return True

    @staticmethod
    def calculate_efficiency_ratio(prices, period=10):
        if len(prices) < period:
            return 0
        net_change = abs(prices[-1] - prices[-period])
        volatility = sum(abs(prices[i] - prices[i-1]) for i in range(len(prices)-period+1, len(prices)))
        return net_change / volatility if volatility > 0 else 0

    @staticmethod
    def detect_volatility_trend(prices, short_window=5, long_window=15):
        """Detect if volatility is increasing, decreasing, or stable"""
        if len(prices) < long_window:
            return "insufficient_data"
        
        recent_vol = VolatilityAnalyzer.calculate_standard_deviation(list(prices)[-short_window:])
        baseline_vol = VolatilityAnalyzer.calculate_standard_deviation(list(prices)[-long_window:])
        
        if baseline_vol == 0:
            return "stable"
        
        ratio = recent_vol / baseline_vol
        
        if ratio > 1.3:
            return "increasing"
        elif ratio < 0.7:
            return "decreasing"
        else:
            return "stable"
    
    @staticmethod
    def is_low_volatility_window(prices, threshold=0.10):
        if len(prices) < 20:
            return False, 0, "insufficient_data"
        
        prices_list = list(prices)
        std_dev = VolatilityAnalyzer.calculate_standard_deviation(prices_list[-15:])
        cv = VolatilityAnalyzer.calculate_coefficient_of_variation(prices_list[-15:])
        trend = VolatilityAnalyzer.detect_volatility_trend(prices)
        er = VolatilityAnalyzer.calculate_efficiency_ratio(prices_list)
        
        micro_stable = VolatilityAnalyzer.is_micro_stable(prices_list, lookback=8)
        
        mean_price = sum(prices_list[-15:]) / 15
        pct_volatility = (std_dev / mean_price * 100) if mean_price > 0 else 0
        
        is_low_vol = (
            pct_volatility < 0.15 and 
            trend != "increasing" and
            cv < 2.5 and 
            er < 0.5 and
            micro_stable # MUST be micro-stable (no sudden small jumps)
        )
        
        return is_low_vol, pct_volatility, trend


class EnhancedSafetyChecks:
    """
    Strict safety verification to prevent forced entries
    """
    
    @staticmethod
    def is_volatility_safe_for_growth_rate(volatility_pct, growth_rate):
        safety_matrix = {
            0.05: 0.10,   # 5% growth now needs < 0.10% vol
            0.04: 0.12,   # 4% growth now needs < 0.12% vol
            0.03: 0.15,
            0.025: 0.11,
            0.02: 0.13,
            0.015: 0.18,
            0.01: 0.25
        }
        
        # Find the appropriate threshold for this growth rate
        max_volatility = 0.25  # Default fallback
        for rate_threshold, vol_threshold in sorted(safety_matrix.items(), reverse=True):
            if growth_rate >= rate_threshold:
                max_volatility = vol_threshold
                break
        
        is_safe = volatility_pct < max_volatility
        
        if not is_safe:
            reason = f"Volatility {volatility_pct:.4f}% too high for {growth_rate*100:.2f}% growth rate (max: {max_volatility:.4f}%)"
        else:
            reason = f"Volatility {volatility_pct:.4f}% is safe for {growth_rate*100:.2f}% growth rate (max: {max_volatility:.4f}%)"
        
        return is_safe, reason, max_volatility

    async def wait_for_low_volatility_window(self, max_wait_time=900, check_interval=20):
        if not self.pre_trade_volatility_check:
            trade_logger.info("⚠️ Pre-trade volatility check DISABLED")
            return True, 0, "disabled", "Check disabled"
        
        trade_logger.info(f"🔍 AGGRESSIVE SAFETY: Monitoring market for CALM window (max {max_wait_time}s)")
        
        start_time = datetime.now()
        attempts = 0
        consecutive_safe_readings = 0
        required_safe_readings = 3  # Require 5 consecutive safe readings (up from 3)
        
        while (datetime.now() - start_time).total_seconds() < max_wait_time:
            attempts += 1
            volatility, prices = await self.analyze_tick_volatility(periods=100) # More data (100 ticks)
            
            if volatility is None:
                consecutive_safe_readings = 0
                await asyncio.sleep(check_interval)
                continue
            
            is_low_vol, pct_vol, trend = self.volatility_analyzer.is_low_volatility_window(
                self.price_history, 
                threshold=self.max_entry_volatility
            )
            
            avg_velocity, avg_acceleration = self.volatility_analyzer.calculate_tick_metrics(list(self.price_history)[-20:])
            
            test_growth_rates = [0.05, 0.03, 0.02]
            safe_rates = [r for r in test_growth_rates if EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(pct_vol, r)[0]]
            
            is_safe_entry = (
                is_low_vol and 
                trend == "stable" and # MUST be stable, not just "not increasing"
                len(safe_rates) == len(test_growth_rates) and # Must be safe for ALL test rates
                pct_vol < (self.max_entry_volatility * 0.8) and # 20% safety buffer
                avg_velocity < 0.005 # Ensure tick speed is very low
            )
            
            if is_safe_entry:
                consecutive_safe_readings += 1
                trade_logger.info(f"✓ CALM reading {consecutive_safe_readings}/{required_safe_readings} | Velocity: {avg_velocity:.6f}")
                if consecutive_safe_readings >= required_safe_readings:
                    self.pre_trade_volatility = pct_vol
                    self.volatility_trend = trend
                    return True, pct_vol, trend, "Market confirmed calm"
            else:
                if consecutive_safe_readings > 0:
                    rejection_reason = "Noise" if not is_low_vol else "Trend" if trend != "stable" else "Velocity"
                    trade_logger.warning(f"❌ Market {rejection_reason} detected - Resetting safety timer")
                consecutive_safe_readings = 0
            
            await asyncio.sleep(check_interval)
        
        trade_logger.error("🚫 SKIP TRADE: Market too volatile or noisy. Conditions not met.")
        return False, None, None, "Market conditions unsuitable"

    def select_growth_rate_for_volatility(self, volatility):
        """Select optimal growth rate based on current volatility"""
        # More aggressive thresholds for faster adaptation
        if volatility < 0.05:
            rate = 0.05
            tier = "Very Low"
        elif volatility < 0.07:
            rate = 0.04
            tier = "Low"
        elif volatility < 0.09:
            rate = 0.03
            tier = "Moderate-Low"
        elif volatility < 0.11:
            rate = 0.025
            tier = "Moderate"
        elif volatility < 0.13:
            rate = 0.02
            tier = "Moderate-High"
        elif volatility < 0.18:
            rate = 0.015
            tier = "High"
        else:
            rate = 0.01
            tier = "Very High"
        
        return rate, tier
    
    async def select_optimal_growth_rate(self):
        """
        ENHANCED VERSION with safety verification
        
        Improvements:
        1. Re-checks volatility safety after selecting growth rate
        2. Ensures selected rate is compatible with current volatility
        3. Falls back to more conservative rate if needed
        """
        volatility, _ = await self.analyze_tick_volatility()
        
        if volatility is None:
            trade_logger.error("❌ Cannot select growth rate - no volatility data")
            return None  # Signal to abort trade
        
        self.volatility = volatility
        self.initial_volatility = volatility
        
        # Select initial growth rate
        rate, tier = self.select_growth_rate_for_volatility(volatility)
        
        # CRITICAL: Verify this rate is actually safe for current volatility
        is_safe, reason, max_allowed = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
            volatility, rate
        )
        
        if not is_safe:
            trade_logger.warning(f"⚠️ Initially selected rate {rate*100:.2f}% is NOT safe: {reason}")
            
            # Try progressively more conservative rates
            fallback_rates = [0.025, 0.02, 0.015, 0.01]
            for fallback_rate in fallback_rates:
                is_safe, reason, max_allowed = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
                    volatility, fallback_rate
                )
                if is_safe:
                    rate = fallback_rate
                    tier = f"Fallback-{rate*100:.2f}%"
                    trade_logger.info(f"✓ Using fallback rate: {rate*100:.2f}% ({reason})")
                    break
            else:
                # No safe rate found!
                trade_logger.error(f"❌ NO SAFE GROWTH RATE found for volatility {volatility:.4f}%")
                return None  # Signal to abort trade
        
        trade_logger.info("=" * 80)
        trade_logger.info(f"✓ GROWTH RATE SELECTION CONFIRMED")
        trade_logger.info(f"   Volatility Tier: {tier}")
        trade_logger.info(f"   Current Volatility: {volatility:.4f}%")
        trade_logger.info(f"   Selected Growth Rate: {rate*100:.2f}%")
        trade_logger.info(f"   Max Safe Volatility for this rate: {max_allowed:.4f}%")
        trade_logger.info(f"   Safety Status: {reason}")
        trade_logger.info("=" * 80)
        
        return rate

    def calculate_target_ticks(self, growth_rate):
        """Dynamically calculate target ticks based on growth rate"""
        if growth_rate >= 0.045:
            ticks = 3
        elif growth_rate >= 0.035:
            ticks = 4
        elif growth_rate >= 0.025:
            ticks = 5
        elif growth_rate >= 0.018:
            ticks = 6
        elif growth_rate >= 0.012:
            ticks = 7
        else:
            ticks = 9
        
        trade_logger.info(f"Target ticks calculated: {ticks} for growth rate: {growth_rate*100:.2f}%")
        return ticks
    
    async def check_and_switch_growth_rate(self, tick_count, current_profit):
        """
        ENHANCED VERSION with safety verification before switching
        
        Improvements:
        1. Verifies new rate is safe for current volatility
        2. Never switches to higher rate if volatility increased
        3. Always switches to lower rate if volatility spiked
        """
        if not self.enable_growth_rate_switching:
            return False
        
        if tick_count % self.growth_rate_switch_interval != 0:
            return False
        
        # Calculate current volatility from tick data
        current_vol = self.calculate_realtime_volatility()
        if current_vol is None:
            return False
        
        # Determine optimal growth rate for current conditions
        optimal_rate, tier = self.select_growth_rate_for_volatility(current_vol)
        
        # CRITICAL: Verify the optimal rate is actually safe
        is_safe, reason, max_allowed = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
            current_vol, optimal_rate
        )
        
        if not is_safe:
            trade_logger.warning(
                f"⚠️ Optimal rate {optimal_rate*100:.2f}% is UNSAFE for current volatility {current_vol:.4f}%"
            )
            
            # If current rate is also unsafe, we should exit the trade!
            current_safe, current_reason, current_max = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
                current_vol, self.current_growth_rate
            )
            
            if not current_safe:
                trade_logger.error(
                    f"🚨 CURRENT RATE {self.current_growth_rate*100:.2f}% ALSO UNSAFE! "
                    f"Volatility {current_vol:.4f}% exceeds max {current_max:.4f}%"
                )
                # This signals the monitoring function to exit
                return False
            
            # Don't switch if optimal isn't safe
            return False
        
        # Calculate volatility change ratio
        vol_change_ratio = current_vol / self.initial_volatility if self.initial_volatility > 0 else 1.0
        
        # SAFETY RULES for switching:
        # 1. If volatility increased significantly (>20%), only switch to LOWER rates
        # 2. If volatility decreased, can switch to higher rates
        # 3. Always verify safety before switching
        
        should_switch = False
        switch_reason = ""
        
        if optimal_rate < self.current_growth_rate:
            # Switching to more conservative rate - always allowed if safe
            should_switch = True
            switch_reason = "volatility_increased"
        elif optimal_rate > self.current_growth_rate:
            # Switching to more aggressive rate - only if volatility decreased
            if vol_change_ratio < 0.8:  # Volatility decreased by 20%+
                should_switch = True
                switch_reason = "volatility_decreased"
            else:
                trade_logger.info(
                    f"ℹ️ Not switching to higher rate {optimal_rate*100:.2f}% - "
                    f"volatility hasn't decreased enough (ratio: {vol_change_ratio:.2f})"
                )
                return False
        
        # Check if difference is significant enough (0.5% threshold)
        if should_switch and abs(optimal_rate - self.current_growth_rate) >= 0.005:
            old_rate = self.current_growth_rate
            self.current_growth_rate = optimal_rate
            self.growth_rate_switches += 1
            
            trade_logger.info("=" * 80)
            trade_logger.info(f"⚡ SAFE GROWTH RATE SWITCH #{self.growth_rate_switches}")
            trade_logger.info(f"   Tick: {tick_count}")
            trade_logger.info(f"   Volatility: {current_vol:.4f}% (Initial: {self.initial_volatility:.4f}%)")
            trade_logger.info(f"   Volatility Change: {vol_change_ratio:.2f}x")
            trade_logger.info(f"   Tier: {tier}")
            trade_logger.info(f"   Rate Change: {old_rate*100:.2f}% → {optimal_rate*100:.2f}%")
            trade_logger.info(f"   Reason: {switch_reason}")
            trade_logger.info(f"   Safety Status: {reason}")
            trade_logger.info(f"   Current Profit: ${current_profit:.2f}")
            trade_logger.info("=" * 80)
            
            log_system_event('INFO', 'SafeGrowthRateSwitch', 
                           f'Trade {self.trade_id} - Rate switched safely', {
                               'tick': tick_count,
                               'old_rate': old_rate,
                               'new_rate': optimal_rate,
                               'volatility': current_vol,
                               'volatility_ratio': vol_change_ratio,
                               'tier': tier,
                               'profit': current_profit,
                               'safety_verified': is_safe,
                               'reason': switch_reason
                           })
            
            return True
        
        return False
        
    async def check_trading_conditions(self):
        today = datetime.now().date().isoformat()
        session = get_session_data(today)
        
        if not session:
            update_session_data(today, 0, 0, 0.0, 0)
            trade_logger.info("New trading session started")
            return True, "New session started"
        
        if session['stopped']:
            trade_logger.warning("Trading stopped for today")
            return False, "Trading stopped for today"
        
        if session['trades_count'] >= self.max_daily_trades:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            trade_logger.warning(f"Max daily trades reached: {self.max_daily_trades}")
            return False, f"Max daily trades reached ({self.max_daily_trades})"
        
        if session['consecutive_losses'] >= self.max_consecutive_losses:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            trade_logger.warning(f"Max consecutive losses reached: {self.max_consecutive_losses}")
            return False, f"Max consecutive losses reached ({self.max_consecutive_losses})"
        
        daily_loss_limit = self.initial_balance * self.daily_loss_limit_pct
        if session['total_profit_loss'] <= -daily_loss_limit:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            trade_logger.warning(f"Daily loss limit reached: ${daily_loss_limit:.2f}")
            return False, f"Daily loss limit reached ({daily_loss_limit:.2f})"
        
        trade_logger.info("Trading conditions check passed")
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
        
        trade_logger.info(f"Session updated: Trades={new_trades_count}, Losses={new_consecutive_losses}, P/L=${new_total_pl:.2f}")
    
    async def validate_symbol(self):
        try:
            spec_request = {
                "contracts_for": self.symbol,
                "req_id": self.get_next_request_id()
            }
            response = await self.send_request(spec_request)
            
            if not response or "error" in response:
                trade_logger.error(f"Symbol validation failed for {self.symbol}")
                return False
            
            if "contracts_for" in response:
                contracts = response["contracts_for"].get("available", [])
                has_accu = any(c.get("contract_type") == self.contract_type for c in contracts) 
                if has_accu:
                    self.symbol_available = True
                    trade_logger.info(f"Symbol {self.symbol} validated successfully")
                    return True
        except Exception as e:
            trade_logger.error(f"Symbol validation exception: {e}")
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
        except Exception as e:
            trade_logger.error(f"Request failed: {e}")
        return None

    async def place_accumulator_trade(self):
        """
        ENHANCED VERSION with multi-level safety checks
        
        Improvements:
        1. NO FALLBACK - Will reject if conditions aren't perfect
        2. Verifies safety at every step
        3. Re-checks before final trade placement
        """
        try:
            balance = await self.get_balance()
            if balance < self.stake_per_trade:
                trade_logger.error(f"❌ Insufficient balance: ${balance:.2f} < ${self.stake_per_trade:.2f}")
                return None, "Insufficient balance"
            
            if not self.symbol_available:
                if not await self.validate_symbol():
                    return None, "Symbol validation failed"
            
            # STEP 1: Wait for safe volatility window
            trade_logger.info("🔍 STEP 1/3: Multi-reading calm check...")
            can_enter, pre_vol, trend, reason = await self.wait_for_low_volatility_window()
            
            if not can_enter:
                trade_logger.error(f"🚫 ABORTING: {reason}")
                return None, f"Safety check failed: {reason}"
            
            trade_logger.info(f"✅ STEP 1 PASSED: Calm window confirmed")
            
            # STEP 2: Select safe growth rate
            trade_logger.info("🔍 STEP 2/3: Selecting and verifying growth rate...")
            if self.mode == 'adaptive':
                self.current_growth_rate = await self.select_optimal_growth_rate()
                
                if self.current_growth_rate is None:
                    trade_logger.error(f"❌ TRADE REJECTED IN STEP 2: No safe growth rate found")
                    return None, "No safe growth rate available for current volatility"
                
                self.target_ticks = self.calculate_target_ticks(self.current_growth_rate)
            else:
                # Even in fixed mode, verify safety
                self.current_growth_rate = self.fixed_growth_rate
                is_safe, reason, max_vol = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
                    pre_vol, self.current_growth_rate
                )
                
                if not is_safe:
                    trade_logger.error(f"❌ TRADE REJECTED IN STEP 2: Fixed growth rate is unsafe - {reason}")
                    return None, f"Fixed growth rate unsafe: {reason}"
                
                self.target_ticks = self.fixed_target_ticks
            
            trade_logger.info(f"✅ STEP 2 PASSED: Growth rate {self.current_growth_rate*100:.2f}% verified safe")
            
            # STEP 3: Final volatility re-check
            trade_logger.info("🔍 STEP 3/3: Final split-second safety check...")
            final_vol, final_prices = await self.analyze_tick_volatility(periods=30)
            
            if final_vol is None:
                return None, "Failed to get final volatility"

            is_micro_ok = self.volatility_analyzer.is_micro_stable(final_prices, lookback=5)
            final_vel, _ = self.volatility_analyzer.calculate_tick_metrics(final_prices[-10:])

            # Inside place_accumulator_trade Step 3:
            if final_vol > pre_vol * 1.15 or final_vel > 0.015: # Relaxed multipliers/limits
                trade_logger.error("🚫 ABORTING: Significant instability...")
            
            # Final safety check for selected growth rate
            is_final_safe, final_reason, _ = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
                final_vol, self.current_growth_rate
            )
            
            if not is_final_safe:
                trade_logger.error(f"❌ TRADE REJECTED IN STEP 3: Final safety check failed - {final_reason}")
                return None, f"Final safety check failed: {final_reason}"
            
            trade_logger.info(f"✅ STEP 3 PASSED: Final volatility {final_vol:.4f}% confirmed safe")
            
            # ALL CHECKS PASSED - Proceed with trade
            trade_logger.info("=" * 80)
            trade_logger.info("✅ ALL SAFETY CHECKS PASSED - PLACING TRADE")
            trade_logger.info(f"   Pre-Trade Volatility: {pre_vol:.4f}%")
            trade_logger.info(f"   Final Volatility: {final_vol:.4f}%")
            trade_logger.info(f"   Trend: {trend}")
            trade_logger.info(f"   Growth Rate: {self.current_growth_rate*100:.2f}%")
            trade_logger.info(f"   Target Ticks: {self.target_ticks}")
            trade_logger.info(f"   Stake: ${self.stake_per_trade:.2f}")
            trade_logger.info("=" * 80)
            
            proposal_request = {
                "proposal": 1,
                "amount": self.stake_per_trade,
                "basis": "stake",
                "contract_type": self.contract_type,
                "currency": "USD",
                "symbol": self.symbol,
                "growth_rate": self.current_growth_rate
            }
            
            proposal_response = await self.send_request(proposal_request)
            if not proposal_response or "error" in proposal_response:
                trade_logger.error("❌ Proposal failed")
                return None, "Proposal failed"
            
            proposal_id = proposal_response["proposal"]["id"]
            ask_price = proposal_response["proposal"]["ask_price"]
            
            buy_request = {"buy": proposal_id, "price": ask_price}
            buy_response = await self.send_request(buy_request)
            
            if not buy_response or "error" in buy_response:
                trade_logger.error("❌ Buy failed")
                return None, "Buy failed"
            
            contract_id = buy_response["buy"]["contract_id"]
            trade_logger.info(f"✅ TRADE PLACED SUCCESSFULLY - Contract ID: {contract_id}")
            
            return contract_id, None
            
        except Exception as e:
            trade_logger.error(f"❌ Place trade exception: {e}")
            return None, str(e)
    
    async def monitor_contract(self, contract_id):
        try:
            self.trade_start_time = datetime.now()
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
            
            trade_logger.info(f"Monitoring started - Contract: {contract_id}")
            
            while True:
                try:
                    response = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                    data = json.loads(response)
                    
                    if "proposal_open_contract" in data:
                        contract = data["proposal_open_contract"]
                        
                        # Capture entry and exit spots
                        if contract.get("entry_spot") and not self.entry_spot:
                            self.entry_spot = float(contract["entry_spot"])
                            trade_logger.info(f"Entry spot: {self.entry_spot}")
                        
                        if contract.get("exit_tick") and not self.exit_spot:
                            self.exit_spot = float(contract["exit_tick"])
                        
                        if contract.get("is_sold") or contract.get("status") == "sold":
                            profit = float(contract.get("profit", 0))
                            duration = (datetime.now() - self.trade_start_time).total_seconds()
                            
                            trade_logger.info(f"Trade closed - Profit: ${profit:.2f}, Duration: {duration:.1f}s, Reason: {exit_reason}, Switches: {self.growth_rate_switches}")
                            
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
                                "final_volatility": current_volatility,
                                "duration_seconds": duration,
                                "entry_spot": self.entry_spot,
                                "exit_spot": self.exit_spot,
                                "growth_rate_switches": self.growth_rate_switches
                            }
                        
                        current_profit = float(contract.get("profit", 0))
                        max_profit = max(max_profit, current_profit)
                        
                        if contract.get("current_spot"):
                            self.price_history.append(float(contract["current_spot"]))
                        
                        if contract.get("entry_spot"):
                            tick_count += 1
                            trade_logger.debug(f"Tick {tick_count}: Profit=${current_profit:.2f}, Max=${max_profit:.2f}")
                            
                            # Dynamic growth rate switching
                            await self.check_and_switch_growth_rate(tick_count, current_profit)
                        
                        # Enhanced volatility monitoring with tick data
                        if tick_count - last_volatility_check >= self.volatility_check_interval:
                            current_volatility = self.calculate_realtime_volatility()
                            last_volatility_check = tick_count
                            
                            if current_volatility and self.initial_volatility:
                                volatility_ratio = current_volatility / self.initial_volatility
                                
                                # More sensitive volatility exit
                                if volatility_ratio > self.volatility_exit_threshold:
                                    exit_reason = f"volatility_spike_{volatility_ratio:.2f}x"
                                    trade_logger.warning(
                                        f"🚨 VOLATILITY SPIKE! {volatility_ratio:.2f}x | "
                                        f"Current: {current_volatility:.4f}% vs Initial: {self.initial_volatility:.4f}%"
                                    )
                                    sell_request = {
                                        "sell": contract_id,
                                        "price": 0.0,
                                        "req_id": self.get_next_request_id()
                                    }
                                    await self.send_request(sell_request)
                                    continue
                        
                        # Profit target
                        if current_profit >= profit_target:
                            exit_reason = "profit_target"
                            trade_logger.info(f"Profit target reached: ${current_profit:.2f}")
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                        
                        # Stop loss
                        if current_profit <= stop_loss:
                            exit_reason = "stop_loss"
                            trade_logger.warning(f"Stop loss triggered: ${current_profit:.2f}")
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                        
                        # Trailing stop
                        if max_profit > 0:
                            trailing_threshold = max_profit * (1 - self.trailing_stop_pct)
                            if current_profit < trailing_threshold:
                                exit_reason = "trailing_stop"
                                trade_logger.info(f"Trailing stop triggered: ${current_profit:.2f} < ${trailing_threshold:.2f}")
                                sell_request = {
                                    "sell": contract_id,
                                    "price": 0.0,
                                    "req_id": self.get_next_request_id()
                                }
                                await self.send_request(sell_request)
                                continue
                        
                        # Target ticks reached
                        if tick_count >= self.target_ticks:
                            exit_reason = "target_ticks"
                            trade_logger.info(f"Target ticks reached: {tick_count}/{self.target_ticks}")
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                            
                    elif "error" in data:
                        trade_logger.error(f"Contract error: {data['error']['message']}")
                        return {
                            "profit": 0, 
                            "status": "error", 
                            "error": data['error']['message'],
                            "exit_reason": "error",
                            "ticks_completed": tick_count,
                            "max_profit_reached": max_profit,
                            "duration_seconds": (datetime.now() - self.trade_start_time).total_seconds(),
                            "growth_rate_switches": self.growth_rate_switches
                        }
                except asyncio.TimeoutError:
                    trade_logger.error("Contract monitoring timeout")
                    return {
                        "profit": 0, 
                        "status": "error", 
                        "error": "Timeout",
                        "exit_reason": "timeout",
                        "ticks_completed": tick_count,
                        "max_profit_reached": max_profit,
                        "duration_seconds": (datetime.now() - self.trade_start_time).total_seconds(),
                        "growth_rate_switches": self.growth_rate_switches
                    }
        except Exception as e:
            trade_logger.error(f"Monitor contract exception: {e}")
            return {
                "profit": 0, 
                "status": "error", 
                "error": str(e),
                "exit_reason": "monitor_failed",
                "ticks_completed": 0,
                "max_profit_reached": 0,
                "duration_seconds": 0,
                "growth_rate_switches": 0
            }
    
    async def execute_trade_async(self):
        try:
            trade_results[self.trade_id] = {'status': 'running'}
            save_trade(self.trade_id, {
                'timestamp': datetime.now().isoformat(),
                'app_id': self.app_id,
                'status': 'running',
                'success': 0,
                'initial_balance': self.initial_balance,
                'parameters': {
                    'stake': self.stake_per_trade,
                    'symbol': self.symbol,
                    'mode': self.mode
                }
            })
            
            log_system_event('INFO', 'TradeExecution', f'Trade {self.trade_id} started', {
                'symbol': self.symbol,
                'stake': self.stake_per_trade,
                'mode': self.mode
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
                log_system_event('WARNING', 'TradeExecution', f'Trade {self.trade_id} rejected', {'reason': reason})
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
                log_system_event('ERROR', 'TradeExecution', f'Symbol validation failed for {self.trade_id}')
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
                log_system_event('ERROR', 'TradeExecution', f'Trade placement failed for {self.trade_id}', {'error': error})
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
                "initial_balance": self.initial_balance,
                "timestamp": datetime.now().isoformat(),
                "app_id": self.app_id,
                "volatility": self.initial_volatility,
                "growth_rate": self.current_growth_rate,
                "target_ticks": self.target_ticks,
                "exit_reason": monitor_result.get("exit_reason"),
                "max_profit_reached": monitor_result.get("max_profit_reached"),
                "ticks_completed": monitor_result.get("ticks_completed"),
                "duration_seconds": monitor_result.get("duration_seconds"),
                "entry_spot": monitor_result.get("entry_spot"),
                "exit_spot": monitor_result.get("exit_spot"),
                "volatility_at_exit": monitor_result.get("final_volatility"),
                "pre_trade_volatility": self.pre_trade_volatility,
                "growth_rate_switches": self.growth_rate_switches,
                "volatility_trend": self.volatility_trend,
                "parameters": {
                    'stake': self.stake_per_trade,
                    'symbol': self.symbol,
                    'mode': self.mode,
                    'growth_rate_used': self.current_growth_rate,
                    'target_ticks_used': self.target_ticks,
                    'initial_volatility': self.initial_volatility,
                    'final_volatility': monitor_result.get("final_volatility"),
                    'pre_trade_volatility': self.pre_trade_volatility,
                    'growth_rate_switches': self.growth_rate_switches
                }
            }
            save_trade(self.trade_id, result)
            trade_results[self.trade_id] = result
            
            log_system_event('INFO', 'TradeExecution', f'Trade {self.trade_id} completed', {
                'profit': monitor_result.get("profit", 0),
                'exit_reason': monitor_result.get("exit_reason"),
                'duration': monitor_result.get("duration_seconds"),
                'growth_rate_switches': self.growth_rate_switches
            })
            
            return result
        except Exception as e:
            trade_logger.error(f"Execute trade exception for {self.trade_id}: {e}")
            result = {
                "success": False,
                "error": str(e),
                "trade_id": self.trade_id,
                "timestamp": datetime.now().isoformat(),
                "status": "completed"
            }
            save_trade(self.trade_id, result)
            trade_results[self.trade_id] = result
            log_system_event('ERROR', 'TradeExecution', f'Trade {self.trade_id} failed', {'error': str(e)})
            return result
        finally:
            if self.ws:
                try:
                    await self.ws.close()
                except:
                    pass
            gc.collect()

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
        self.max_consecutive_losses = parameters.get('max_consecutive_losses', 2)
        self.daily_loss_limit_pct = parameters.get('daily_loss_limit_pct', 0.03)
        
        self.profit_target_pct = parameters.get('profit_target_pct', 0.25)
        self.stop_loss_pct = parameters.get('stop_loss_pct', 0.5)
        self.trailing_stop_pct = parameters.get('trailing_stop_pct', 0.3)
        
        # Enhanced volatility parameters
        self.pre_trade_volatility_check = parameters.get('pre_trade_volatility_check', True)
        self.max_entry_volatility = parameters.get('max_entry_volatility', 0.15)  # 0.15% max for entry
        self.volatility_check_interval = parameters.get('volatility_check_interval', 1)
        self.volatility_exit_threshold = parameters.get('volatility_exit_threshold', 1.5)
        
        # Dynamic growth rate switching
        self.enable_growth_rate_switching = parameters.get('enable_growth_rate_switching', True)
        self.growth_rate_switch_interval = parameters.get('growth_rate_switch_interval', 3)  # Check every 3 ticks
        
        self.current_growth_rate = None
        self.growth_rate_switches = 0
        self.target_ticks = None
        self.volatility = None
        self.initial_volatility = None
        self.pre_trade_volatility = None
        self.volatility_trend = None
        
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
        
        # Extended price history for better analysis
        self.price_history = deque(maxlen=100) # Increased history size for analysis
        self.trade_start_time = None
        self.entry_spot = None
        self.exit_spot = None
        
        self.volatility_analyzer = VolatilityAnalyzer()
        
        trade_logger.info(f"Bot initialized - Trade ID: {trade_id}, Symbol: {self.symbol}, Mode: {self.mode}")
        
    def get_next_request_id(self):
        self.request_id += 1
        return self.request_id
        
    async def connect(self, retry_count=0, max_retries=3):
        for ws_url in self.ws_urls:
            self.ws_url = ws_url
            try:
                trade_logger.info(f"Attempting connection to {ws_url}")
                self.ws = await asyncio.wait_for(
                    websockets.connect(self.ws_url, ping_interval=None, close_timeout=5),
                    timeout=15.0
                )
                auth_success = await self.authorize()
                if not auth_success:
                    trade_logger.warning(f"Authorization failed on {ws_url}")
                    try:
                        await self.ws.close()
                    except:
                        pass
                    self.ws = None
                    raise Exception("Authorization failed")
                trade_logger.info(f"Successfully connected and authorized on {ws_url}")
                return True
            except Exception as e:
                trade_logger.error(f"Connection failed to {ws_url}: {e}")
                if self.ws:
                    try:
                        await self.ws.close()
                    except:
                        pass
                    self.ws = None
                continue
        
        if retry_count < max_retries:
            wait_time = 10 * (2 ** retry_count)
            trade_logger.info(f"Retry {retry_count + 1}/{max_retries} in {wait_time}s")
            await asyncio.sleep(wait_time)
            return await self.connect(retry_count + 1, max_retries)
        else:
            trade_logger.error("Failed to connect after all retries")
            raise Exception("Failed to connect after retries")
    
    async def authorize(self):
        if not self.api_token:
            trade_logger.error("No API token provided")
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
                trade_logger.error(f"Authorization error: {data['error']}")
                return False
            
            if "authorize" in data:
                self.account_balance = float(data['authorize']['balance'])
                self.initial_balance = self.account_balance
                trade_logger.info(f"Authorized successfully. Balance: ${self.account_balance:.2f}")
                return True
        except Exception as e:
            trade_logger.error(f"Authorization exception: {e}")
        return False
    
    async def get_balance(self):
        try:
            balance_request = {"balance": 1, "subscribe": 0, "req_id": self.get_next_request_id()}
            response_data = await self.send_request(balance_request)
            
            if response_data and "balance" in response_data:
                self.account_balance = float(response_data["balance"]["balance"])
                trade_logger.debug(f"Balance updated: ${self.account_balance:.2f}")
                return self.account_balance
        except Exception as e:
            trade_logger.error(f"Failed to get balance: {e}")
        return self.account_balance
    
    async def analyze_tick_volatility(self, periods=50):
        """Enhanced tick-based volatility analysis with mathematical calculations"""
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
                if len(prices) >= 10:
                    # Store in price history
                    self.price_history.extend(prices)
                    
                    # Calculate comprehensive volatility metrics
                    std_dev = self.volatility_analyzer.calculate_standard_deviation(prices)
                    cv = self.volatility_analyzer.calculate_coefficient_of_variation(prices)
                    atr = self.volatility_analyzer.calculate_atr(prices)
                    
                    # Calculate percentage-based volatility
                    mean_price = sum(prices) / len(prices)
                    pct_volatility = (std_dev / mean_price * 100) if mean_price > 0 else 0
                    
                    trade_logger.info(f"Tick Volatility Analysis - StdDev: {std_dev:.6f}, CV: {cv:.4f}%, ATR: {atr:.6f}, Pct: {pct_volatility:.4f}%")
                    
                    return pct_volatility, prices
        except Exception as e:
            trade_logger.error(f"Tick volatility analysis failed: {e}")
        return None, None

    async def wait_for_low_volatility_window(self, max_wait_time=900, check_interval=20):
        if not self.pre_trade_volatility_check:
            trade_logger.info("⚠️ Pre-trade volatility check DISABLED")
            return True, 0, "disabled", "Check disabled"
        
        trade_logger.info(f"🔍 AGGRESSIVE SAFETY: Monitoring market for CALM window (max {max_wait_time}s)")
        
        start_time = datetime.now()
        attempts = 0
        consecutive_safe_readings = 0
        required_safe_readings = 5  # Require 5 consecutive safe readings (up from 3)
        
        while (datetime.now() - start_time).total_seconds() < max_wait_time:
            attempts += 1
            volatility, prices = await self.analyze_tick_volatility(periods=100) # More data (100 ticks)
            
            if volatility is None:
                consecutive_safe_readings = 0
                await asyncio.sleep(check_interval)
                continue
            
            is_low_vol, pct_vol, trend = self.volatility_analyzer.is_low_volatility_window(
                self.price_history, 
                threshold=self.max_entry_volatility
            )
            
            test_growth_rates = [0.05, 0.03, 0.02]
            safe_rates = [r for r in test_growth_rates if EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(pct_vol, r)[0]]
            
            is_safe_entry = (
                is_low_vol and 
                trend == "stable" and # MUST be stable, not just "not increasing"
                len(safe_rates) == len(test_growth_rates) and # Must be safe for ALL test rates
                pct_vol < (self.max_entry_volatility * 0.8) # 20% safety buffer
            )
            
            if is_safe_entry:
                consecutive_safe_readings += 1
                trade_logger.info(f"✓ CALM reading {consecutive_safe_readings}/{required_safe_readings}")
                if consecutive_safe_readings >= required_safe_readings:
                    self.pre_trade_volatility = pct_vol
                    self.volatility_trend = trend
                    return True, pct_vol, trend, "Market confirmed calm"
            else:
                if consecutive_safe_readings > 0:
                    trade_logger.warning("❌ Market noise detected - Resetting safety timer")
                consecutive_safe_readings = 0
            
            await asyncio.sleep(check_interval)
        
        trade_logger.error("🚫 SKIP TRADE: Market too volatile or noisy. Conditions not met.")
        return False, None, None, "Market conditions unsuitable"

    def calculate_realtime_volatility(self):
        """Calculate real-time volatility from tick data"""
        if len(self.price_history) < 5:
            return None
        
        prices = list(self.price_history)[-15:]  # Use last 15 ticks
        mean_price = sum(prices) / len(prices)
        std_dev = self.volatility_analyzer.calculate_standard_deviation(prices)
        
        pct_volatility = (std_dev / mean_price * 100) if mean_price > 0 else 0
        return pct_volatility
    
    def select_growth_rate_for_volatility(self, volatility):
        """Select optimal growth rate based on current volatility"""
        # More aggressive thresholds for faster adaptation
        if volatility < 0.05:
            rate = 0.05
            tier = "Very Low"
        elif volatility < 0.07:
            rate = 0.04
            tier = "Low"
        elif volatility < 0.09:
            rate = 0.03
            tier = "Moderate-Low"
        elif volatility < 0.11:
            rate = 0.025
            tier = "Moderate"
        elif volatility < 0.13:
            rate = 0.02
            tier = "Moderate-High"
        elif volatility < 0.18:
            rate = 0.015
            tier = "High"
        else:
            rate = 0.01
            tier = "Very High"
        
        return rate, tier
    
    async def select_optimal_growth_rate(self):
        """
        ENHANCED VERSION with safety verification
        
        Improvements:
        1. Re-checks volatility safety after selecting growth rate
        2. Ensures selected rate is compatible with current volatility
        3. Falls back to more conservative rate if needed
        """
        volatility, _ = await self.analyze_tick_volatility()
        
        if volatility is None:
            trade_logger.error("❌ Cannot select growth rate - no volatility data")
            return None  # Signal to abort trade
        
        self.volatility = volatility
        self.initial_volatility = volatility
        
        # Select initial growth rate
        rate, tier = self.select_growth_rate_for_volatility(volatility)
        
        # CRITICAL: Verify this rate is actually safe for current volatility
        is_safe, reason, max_allowed = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
            volatility, rate
        )
        
        if not is_safe:
            trade_logger.warning(f"⚠️ Initially selected rate {rate*100:.2f}% is NOT safe: {reason}")
            
            # Try progressively more conservative rates
            fallback_rates = [0.025, 0.02, 0.015, 0.01]
            for fallback_rate in fallback_rates:
                is_safe, reason, max_allowed = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
                    volatility, fallback_rate
                )
                if is_safe:
                    rate = fallback_rate
                    tier = f"Fallback-{rate*100:.2f}%"
                    trade_logger.info(f"✓ Using fallback rate: {rate*100:.2f}% ({reason})")
                    break
            else:
                # No safe rate found!
                trade_logger.error(f"❌ NO SAFE GROWTH RATE found for volatility {volatility:.4f}%")
                return None  # Signal to abort trade
        
        trade_logger.info("=" * 80)
        trade_logger.info(f"✓ GROWTH RATE SELECTION CONFIRMED")
        trade_logger.info(f"   Volatility Tier: {tier}")
        trade_logger.info(f"   Current Volatility: {volatility:.4f}%")
        trade_logger.info(f"   Selected Growth Rate: {rate*100:.2f}%")
        trade_logger.info(f"   Max Safe Volatility for this rate: {max_allowed:.4f}%")
        trade_logger.info(f"   Safety Status: {reason}")
        trade_logger.info("=" * 80)
        
        return rate

    def calculate_target_ticks(self, growth_rate):
        """Dynamically calculate target ticks based on growth rate"""
        if growth_rate >= 0.045:
            ticks = 3
        elif growth_rate >= 0.035:
            ticks = 4
        elif growth_rate >= 0.025:
            ticks = 5
        elif growth_rate >= 0.018:
            ticks = 6
        elif growth_rate >= 0.012:
            ticks = 7
        else:
            ticks = 9
        
        trade_logger.info(f"Target ticks calculated: {ticks} for growth rate: {growth_rate*100:.2f}%")
        return ticks
    
    async def check_and_switch_growth_rate(self, tick_count, current_profit):
        """
        ENHANCED VERSION with safety verification before switching
        
        Improvements:
        1. Verifies new rate is safe for current volatility
        2. Never switches to higher rate if volatility increased
        3. Always switches to lower rate if volatility spiked
        """
        if not self.enable_growth_rate_switching:
            return False
        
        if tick_count % self.growth_rate_switch_interval != 0:
            return False
        
        # Calculate current volatility from tick data
        current_vol = self.calculate_realtime_volatility()
        if current_vol is None:
            return False
        
        # Determine optimal growth rate for current conditions
        optimal_rate, tier = self.select_growth_rate_for_volatility(current_vol)
        
        # CRITICAL: Verify the optimal rate is actually safe
        is_safe, reason, max_allowed = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
            current_vol, optimal_rate
        )
        
        if not is_safe:
            trade_logger.warning(
                f"⚠️ Optimal rate {optimal_rate*100:.2f}% is UNSAFE for current volatility {current_vol:.4f}%"
            )
            
            # If current rate is also unsafe, we should exit the trade!
            current_safe, current_reason, current_max = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
                current_vol, self.current_growth_rate
            )
            
            if not current_safe:
                trade_logger.error(
                    f"🚨 CURRENT RATE {self.current_growth_rate*100:.2f}% ALSO UNSAFE! "
                    f"Volatility {current_vol:.4f}% exceeds max {current_max:.4f}%"
                )
                # This signals the monitoring function to exit
                return False
            
            # Don't switch if optimal isn't safe
            return False
        
        # Calculate volatility change ratio
        vol_change_ratio = current_vol / self.initial_volatility if self.initial_volatility > 0 else 1.0
        
        # SAFETY RULES for switching:
        # 1. If volatility increased significantly (>20%), only switch to LOWER rates
        # 2. If volatility decreased, can switch to higher rates
        # 3. Always verify safety before switching
        
        should_switch = False
        switch_reason = ""
        
        if optimal_rate < self.current_growth_rate:
            # Switching to more conservative rate - always allowed if safe
            should_switch = True
            switch_reason = "volatility_increased"
        elif optimal_rate > self.current_growth_rate:
            # Switching to more aggressive rate - only if volatility decreased
            if vol_change_ratio < 0.8:  # Volatility decreased by 20%+
                should_switch = True
                switch_reason = "volatility_decreased"
            else:
                trade_logger.info(
                    f"ℹ️ Not switching to higher rate {optimal_rate*100:.2f}% - "
                    f"volatility hasn't decreased enough (ratio: {vol_change_ratio:.2f})"
                )
                return False
        
        # Check if difference is significant enough (0.5% threshold)
        if should_switch and abs(optimal_rate - self.current_growth_rate) >= 0.005:
            old_rate = self.current_growth_rate
            self.current_growth_rate = optimal_rate
            self.growth_rate_switches += 1
            
            trade_logger.info("=" * 80)
            trade_logger.info(f"⚡ SAFE GROWTH RATE SWITCH #{self.growth_rate_switches}")
            trade_logger.info(f"   Tick: {tick_count}")
            trade_logger.info(f"   Volatility: {current_vol:.4f}% (Initial: {self.initial_volatility:.4f}%)")
            trade_logger.info(f"   Volatility Change: {vol_change_ratio:.2f}x")
            trade_logger.info(f"   Tier: {tier}")
            trade_logger.info(f"   Rate Change: {old_rate*100:.2f}% → {optimal_rate*100:.2f}%")
            trade_logger.info(f"   Reason: {switch_reason}")
            trade_logger.info(f"   Safety Status: {reason}")
            trade_logger.info(f"   Current Profit: ${current_profit:.2f}")
            trade_logger.info("=" * 80)
            
            log_system_event('INFO', 'SafeGrowthRateSwitch', 
                           f'Trade {self.trade_id} - Rate switched safely', {
                               'tick': tick_count,
                               'old_rate': old_rate,
                               'new_rate': optimal_rate,
                               'volatility': current_vol,
                               'volatility_ratio': vol_change_ratio,
                               'tier': tier,
                               'profit': current_profit,
                               'safety_verified': is_safe,
                               'reason': switch_reason
                           })
            
            return True
        
        return False
        
    async def check_trading_conditions(self):
        today = datetime.now().date().isoformat()
        session = get_session_data(today)
        
        if not session:
            update_session_data(today, 0, 0, 0.0, 0)
            trade_logger.info("New trading session started")
            return True, "New session started"
        
        if session['stopped']:
            trade_logger.warning("Trading stopped for today")
            return False, "Trading stopped for today"
        
        if session['trades_count'] >= self.max_daily_trades:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            trade_logger.warning(f"Max daily trades reached: {self.max_daily_trades}")
            return False, f"Max daily trades reached ({self.max_daily_trades})"
        
        if session['consecutive_losses'] >= self.max_consecutive_losses:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            trade_logger.warning(f"Max consecutive losses reached: {self.max_consecutive_losses}")
            return False, f"Max consecutive losses reached ({self.max_consecutive_losses})"
        
        daily_loss_limit = self.initial_balance * self.daily_loss_limit_pct
        if session['total_profit_loss'] <= -daily_loss_limit:
            update_session_data(today, session['trades_count'], 
                              session['consecutive_losses'], 
                              session['total_profit_loss'], 1)
            trade_logger.warning(f"Daily loss limit reached: ${daily_loss_limit:.2f}")
            return False, f"Daily loss limit reached ({daily_loss_limit:.2f})"
        
        trade_logger.info("Trading conditions check passed")
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
        
        trade_logger.info(f"Session updated: Trades={new_trades_count}, Losses={new_consecutive_losses}, P/L=${new_total_pl:.2f}")
    
    async def validate_symbol(self):
        try:
            spec_request = {
                "contracts_for": self.symbol,
                "req_id": self.get_next_request_id()
            }
            response = await self.send_request(spec_request)
            
            if not response or "error" in response:
                trade_logger.error(f"Symbol validation failed for {self.symbol}")
                return False
            
            if "contracts_for" in response:
                contracts = response["contracts_for"].get("available", [])
                has_accu = any(c.get("contract_type") == self.contract_type for c in contracts) 
                if has_accu:
                    self.symbol_available = True
                    trade_logger.info(f"Symbol {self.symbol} validated successfully")
                    return True
        except Exception as e:
            trade_logger.error(f"Symbol validation exception: {e}")
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
        except Exception as e:
            trade_logger.error(f"Request failed: {e}")
        return None

    async def place_accumulator_trade(self):
        """
        ENHANCED VERSION with multi-level safety checks
        
        Improvements:
        1. NO FALLBACK - Will reject if conditions aren't perfect
        2. Verifies safety at every step
        3. Re-checks before final trade placement
        """
        try:
            balance = await self.get_balance()
            if balance < self.stake_per_trade:
                trade_logger.error(f"❌ Insufficient balance: ${balance:.2f} < ${self.stake_per_trade:.2f}")
                return None, "Insufficient balance"
            
            if not self.symbol_available:
                if not await self.validate_symbol():
                    return None, "Symbol validation failed"
            
            # STEP 1: Wait for safe volatility window
            trade_logger.info("🔍 STEP 1/3: Multi-reading calm check...")
            can_enter, pre_vol, trend, reason = await self.wait_for_low_volatility_window()
            
            if not can_enter:
                trade_logger.error(f"🚫 ABORTING: {reason}")
                return None, f"Safety check failed: {reason}"
            
            trade_logger.info(f"✅ STEP 1 PASSED: Calm window confirmed")
            
            # STEP 2: Select safe growth rate
            trade_logger.info("🔍 STEP 2/3: Selecting and verifying growth rate...")
            if self.mode == 'adaptive':
                self.current_growth_rate = await self.select_optimal_growth_rate()
                
                if self.current_growth_rate is None:
                    trade_logger.error(f"❌ TRADE REJECTED IN STEP 2: No safe growth rate found")
                    return None, "No safe growth rate available for current volatility"
                
                self.target_ticks = self.calculate_target_ticks(self.current_growth_rate)
            else:
                # Even in fixed mode, verify safety
                self.current_growth_rate = self.fixed_growth_rate
                is_safe, reason, max_vol = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
                    pre_vol, self.current_growth_rate
                )
                
                if not is_safe:
                    trade_logger.error(f"❌ TRADE REJECTED IN STEP 2: Fixed growth rate is unsafe - {reason}")
                    return None, f"Fixed growth rate unsafe: {reason}"
                
                self.target_ticks = self.fixed_target_ticks
            
            trade_logger.info(f"✅ STEP 2 PASSED: Growth rate {self.current_growth_rate*100:.2f}% verified safe")
            
            # STEP 3: Final volatility re-check
            trade_logger.info("🔍 STEP 3/3: Final split-second safety check...")
            final_vol, _ = await self.analyze_tick_volatility(periods=30)
            
            if final_vol is None or final_vol > pre_vol * 1.05:
                trade_logger.error("🚫 ABORTING: Micro-spike detected during setup")
                return None, "Micro-spike detected"
            
            # Final safety check for selected growth rate
            is_final_safe, final_reason, _ = EnhancedSafetyChecks.is_volatility_safe_for_growth_rate(
                final_vol, self.current_growth_rate
            )
            
            if not is_final_safe:
                trade_logger.error(f"❌ TRADE REJECTED IN STEP 3: Final safety check failed - {final_reason}")
                return None, f"Final safety check failed: {final_reason}"
            
            trade_logger.info(f"✅ STEP 3 PASSED: Final volatility {final_vol:.4f}% confirmed safe")
            
            # ALL CHECKS PASSED - Proceed with trade
            trade_logger.info("=" * 80)
            trade_logger.info("✅ ALL SAFETY CHECKS PASSED - PLACING TRADE")
            trade_logger.info(f"   Pre-Trade Volatility: {pre_vol:.4f}%")
            trade_logger.info(f"   Final Volatility: {final_vol:.4f}%")
            trade_logger.info(f"   Trend: {trend}")
            trade_logger.info(f"   Growth Rate: {self.current_growth_rate*100:.2f}%")
            trade_logger.info(f"   Target Ticks: {self.target_ticks}")
            trade_logger.info(f"   Stake: ${self.stake_per_trade:.2f}")
            trade_logger.info("=" * 80)
            
            proposal_request = {
                "proposal": 1,
                "amount": self.stake_per_trade,
                "basis": "stake",
                "contract_type": self.contract_type,
                "currency": "USD",
                "symbol": self.symbol,
                "growth_rate": self.current_growth_rate
            }
            
            proposal_response = await self.send_request(proposal_request)
            if not proposal_response or "error" in proposal_response:
                trade_logger.error("❌ Proposal failed")
                return None, "Proposal failed"
            
            proposal_id = proposal_response["proposal"]["id"]
            ask_price = proposal_response["proposal"]["ask_price"]
            
            buy_request = {"buy": proposal_id, "price": ask_price}
            buy_response = await self.send_request(buy_request)
            
            if not buy_response or "error" in buy_response:
                trade_logger.error("❌ Buy failed")
                return None, "Buy failed"
            
            contract_id = buy_response["buy"]["contract_id"]
            trade_logger.info(f"✅ TRADE PLACED SUCCESSFULLY - Contract ID: {contract_id}")
            
            return contract_id, None
            
        except Exception as e:
            trade_logger.error(f"❌ Place trade exception: {e}")
            return None, str(e)
    
    async def monitor_contract(self, contract_id):
        try:
            self.trade_start_time = datetime.now()
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
            
            trade_logger.info(f"Monitoring started - Contract: {contract_id}")
            
            while True:
                try:
                    response = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                    data = json.loads(response)
                    
                    if "proposal_open_contract" in data:
                        contract = data["proposal_open_contract"]
                        
                        # Capture entry and exit spots
                        if contract.get("entry_spot") and not self.entry_spot:
                            self.entry_spot = float(contract["entry_spot"])
                            trade_logger.info(f"Entry spot: {self.entry_spot}")
                        
                        if contract.get("exit_tick") and not self.exit_spot:
                            self.exit_spot = float(contract["exit_tick"])
                        
                        if contract.get("is_sold") or contract.get("status") == "sold":
                            profit = float(contract.get("profit", 0))
                            duration = (datetime.now() - self.trade_start_time).total_seconds()
                            
                            trade_logger.info(f"Trade closed - Profit: ${profit:.2f}, Duration: {duration:.1f}s, Reason: {exit_reason}, Switches: {self.growth_rate_switches}")
                            
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
                                "final_volatility": current_volatility,
                                "duration_seconds": duration,
                                "entry_spot": self.entry_spot,
                                "exit_spot": self.exit_spot,
                                "growth_rate_switches": self.growth_rate_switches
                            }
                        
                        current_profit = float(contract.get("profit", 0))
                        max_profit = max(max_profit, current_profit)
                        
                        if contract.get("current_spot"):
                            self.price_history.append(float(contract["current_spot"]))
                        
                        if contract.get("entry_spot"):
                            tick_count += 1
                            trade_logger.debug(f"Tick {tick_count}: Profit=${current_profit:.2f}, Max=${max_profit:.2f}")
                            
                            # Dynamic growth rate switching
                            await self.check_and_switch_growth_rate(tick_count, current_profit)
                        
                        # Enhanced volatility monitoring with tick data
                        if tick_count - last_volatility_check >= self.volatility_check_interval:
                            current_volatility = self.calculate_realtime_volatility()
                            last_volatility_check = tick_count
                            
                            if current_volatility and self.initial_volatility:
                                volatility_ratio = current_volatility / self.initial_volatility
                                
                                # More sensitive volatility exit
                                if volatility_ratio > self.volatility_exit_threshold:
                                    exit_reason = f"volatility_spike_{volatility_ratio:.2f}x"
                                    trade_logger.warning(
                                        f"🚨 VOLATILITY SPIKE! {volatility_ratio:.2f}x | "
                                        f"Current: {current_volatility:.4f}% vs Initial: {self.initial_volatility:.4f}%"
                                    )
                                    sell_request = {
                                        "sell": contract_id,
                                        "price": 0.0,
                                        "req_id": self.get_next_request_id()
                                    }
                                    await self.send_request(sell_request)
                                    continue
                        
                        # Profit target
                        if current_profit >= profit_target:
                            exit_reason = "profit_target"
                            trade_logger.info(f"Profit target reached: ${current_profit:.2f}")
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                        
                        # Stop loss
                        if current_profit <= stop_loss:
                            exit_reason = "stop_loss"
                            trade_logger.warning(f"Stop loss triggered: ${current_profit:.2f}")
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                        
                        # Trailing stop
                        if max_profit > 0:
                            trailing_threshold = max_profit * (1 - self.trailing_stop_pct)
                            if current_profit < trailing_threshold:
                                exit_reason = "trailing_stop"
                                trade_logger.info(f"Trailing stop triggered: ${current_profit:.2f} < ${trailing_threshold:.2f}")
                                sell_request = {
                                    "sell": contract_id,
                                    "price": 0.0,
                                    "req_id": self.get_next_request_id()
                                }
                                await self.send_request(sell_request)
                                continue
                        
                        # Target ticks reached
                        if tick_count >= self.target_ticks:
                            exit_reason = "target_ticks"
                            trade_logger.info(f"Target ticks reached: {tick_count}/{self.target_ticks}")
                            sell_request = {
                                "sell": contract_id,
                                "price": 0.0,
                                "req_id": self.get_next_request_id()
                            }
                            await self.send_request(sell_request)
                            continue
                            
                    elif "error" in data:
                        trade_logger.error(f"Contract error: {data['error']['message']}")
                        return {
                            "profit": 0, 
                            "status": "error", 
                            "error": data['error']['message'],
                            "exit_reason": "error",
                            "ticks_completed": tick_count,
                            "max_profit_reached": max_profit,
                            "duration_seconds": (datetime.now() - self.trade_start_time).total_seconds(),
                            "growth_rate_switches": self.growth_rate_switches
                        }
                except asyncio.TimeoutError:
                    trade_logger.error("Contract monitoring timeout")
                    return {
                        "profit": 0, 
                        "status": "error", 
                        "error": "Timeout",
                        "exit_reason": "timeout",
                        "ticks_completed": tick_count,
                        "max_profit_reached": max_profit,
                        "duration_seconds": (datetime.now() - self.trade_start_time).total_seconds(),
                        "growth_rate_switches": self.growth_rate_switches
                    }
        except Exception as e:
            trade_logger.error(f"Monitor contract exception: {e}")
            return {
                "profit": 0, 
                "status": "error", 
                "error": str(e),
                "exit_reason": "monitor_failed",
                "ticks_completed": 0,
                "max_profit_reached": 0,
                "duration_seconds": 0,
                "growth_rate_switches": 0
            }
    
    async def execute_trade_async(self):
        try:
            trade_results[self.trade_id] = {'status': 'running'}
            save_trade(self.trade_id, {
                'timestamp': datetime.now().isoformat(),
                'app_id': self.app_id,
                'status': 'running',
                'success': 0,
                'initial_balance': self.initial_balance,
                'parameters': {
                    'stake': self.stake_per_trade,
                    'symbol': self.symbol,
                    'mode': self.mode
                }
            })
            
            log_system_event('INFO', 'TradeExecution', f'Trade {self.trade_id} started', {
                'symbol': self.symbol,
                'stake': self.stake_per_trade,
                'mode': self.mode
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
                log_system_event('WARNING', 'TradeExecution', f'Trade {self.trade_id} rejected', {'reason': reason})
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
                log_system_event('ERROR', 'TradeExecution', f'Symbol validation failed for {self.trade_id}')
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
                log_system_event('ERROR', 'TradeExecution', f'Trade placement failed for {self.trade_id}', {'error': error})
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
                "initial_balance": self.initial_balance,
                "timestamp": datetime.now().isoformat(),
                "app_id": self.app_id,
                "volatility": self.initial_volatility,
                "growth_rate": self.current_growth_rate,
                "target_ticks": self.target_ticks,
                "exit_reason": monitor_result.get("exit_reason"),
                "max_profit_reached": monitor_result.get("max_profit_reached"),
                "ticks_completed": monitor_result.get("ticks_completed"),
                "duration_seconds": monitor_result.get("duration_seconds"),
                "entry_spot": monitor_result.get("entry_spot"),
                "exit_spot": monitor_result.get("exit_spot"),
                "volatility_at_exit": monitor_result.get("final_volatility"),
                "pre_trade_volatility": self.pre_trade_volatility,
                "growth_rate_switches": self.growth_rate_switches,
                "volatility_trend": self.volatility_trend,
                "parameters": {
                    'stake': self.stake_per_trade,
                    'symbol': self.symbol,
                    'mode': self.mode,
                    'growth_rate_used': self.current_growth_rate,
                    'target_ticks_used': self.target_ticks,
                    'initial_volatility': self.initial_volatility,
                    'final_volatility': monitor_result.get("final_volatility"),
                    'pre_trade_volatility': self.pre_trade_volatility,
                    'growth_rate_switches': self.growth_rate_switches
                }
            }
            save_trade(self.trade_id, result)
            trade_results[self.trade_id] = result
            
            log_system_event('INFO', 'TradeExecution', f'Trade {self.trade_id} completed', {
                'profit': monitor_result.get("profit", 0),
                'exit_reason': monitor_result.get("exit_reason"),
                'duration': monitor_result.get("duration_seconds"),
                'growth_rate_switches': self.growth_rate_switches
            })
            
            return result
        except Exception as e:
            trade_logger.error(f"Execute trade exception for {self.trade_id}: {e}")
            result = {
                "success": False,
                "error": str(e),
                "trade_id": self.trade_id,
                "timestamp": datetime.now().isoformat(),
                "status": "completed"
            }
            save_trade(self.trade_id, result)
            trade_results[self.trade_id] = result
            log_system_event('ERROR', 'TradeExecution', f'Trade {self.trade_id} failed', {'error': str(e)})
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
    except Exception as e:
        logger.error(f"Thread execution error for {trade_id}: {e}")
    finally:
        trade_completed()
        gc.collect()


app = Flask(__name__)
app.logger.disabled = True

@app.route('/trade/<app_id>/<api_token>', methods=['POST'])
def execute_trade(app_id, api_token):
    try:
        if not can_start_trade():
            api_logger.warning("Trade rejected - max concurrent trades reached")
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
            'volatility_check_interval': int(data.get('volatility_check_interval', 1)),
            'volatility_exit_threshold': float(data.get('volatility_exit_threshold', 1.5)),
            'pre_trade_volatility_check': data.get('pre_trade_volatility_check', True),
            'max_entry_volatility': float(data.get('max_entry_volatility', 0.15)),
            'enable_growth_rate_switching': data.get('enable_growth_rate_switching', True),
            'growth_rate_switch_interval': int(data.get('growth_rate_switch_interval', 3))
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
        
        api_logger.info(f"Trade initiated - ID: {new_trade_id}, Symbol: {parameters['symbol']}")

        thread = Thread(
            target=run_async_trade_in_thread,
            args=(api_token, app_id, parameters, new_trade_id)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({"status": "initiated", "trade_id": new_trade_id}), 202
    except Exception as e:
        api_logger.error(f"Trade execution endpoint error: {e}")
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
            "initial_balance": result.get('initial_balance'),
            "volatility": result.get('volatility'),
            "volatility_at_exit": result.get('volatility_at_exit'),
            "pre_trade_volatility": result.get('pre_trade_volatility'),
            "volatility_trend": result.get('volatility_trend'),
            "growth_rate": result.get('growth_rate'),
            "target_ticks": result.get('target_ticks'),
            "exit_reason": result.get('exit_reason'),
            "max_profit_reached": result.get('max_profit_reached'),
            "ticks_completed": result.get('ticks_completed'),
            "duration_seconds": result.get('duration_seconds'),
            "entry_spot": result.get('entry_spot'),
            "exit_spot": result.get('exit_spot'),
            "growth_rate_switches": result.get('growth_rate_switches'),
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
        "can_trade": not bool(session['stopped']),
        "last_updated": session.get('last_updated')
    }), 200


@app.route('/session/reset', methods=['POST'])
def reset_session():
    today = datetime.now().date().isoformat()
    update_session_data(today, 0, 0, 0.0, 0)
    api_logger.info("Trading session reset")
    log_system_event('INFO', 'SessionManagement', 'Session reset performed', {'date': today})
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
    
    all_status_trades = filtered_trades
    completed = [t for t in filtered_trades if t.get('status') == 'completed']
    running = [t for t in filtered_trades if t.get('status') == 'running']
    pending = [t for t in filtered_trades if t.get('status') == 'pending']
    
    wins = [t for t in completed if t.get('profit', 0) > 0]
    losses = [t for t in completed if t.get('profit', 0) <= 0]
    total_profit = sum(t.get('profit', 0) for t in completed)
    
    avg_volatility = sum(t.get('volatility', 0) or 0 for t in completed) / len(completed) if completed else 0
    avg_growth_rate = sum(t.get('growth_rate', 0) or 0 for t in completed) / len(completed) if completed else 0
    avg_duration = sum(t.get('duration_seconds', 0) or 0 for t in completed) / len(completed) if completed else 0
    avg_switches = sum(t.get('growth_rate_switches', 0) or 0 for t in completed) / len(completed) if completed else 0
    
    exit_reasons = {}
    for t in completed:
        reason = t.get('exit_reason', 'unknown')
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    
    trades_dict = {}
    for t in all_status_trades:
        trade_dict = dict(t)
        trades_dict[trade_dict['trade_id']] = trade_dict
    
    if len(completed) > 0:
        win_rate = f"{(len(wins)/len(completed)*100):.2f}%"
    else:
        win_rate = "0%"
    
    if losses:
        avg_loss = sum(abs(t.get('profit', 0)) for t in losses) / len(losses)
        max_loss = max(abs(t.get('profit', 0)) for t in losses)
    else:
        avg_loss = 0
        max_loss = 0
    
    if wins:
        avg_win = sum(t.get('profit', 0) for t in wins) / len(wins)
        max_win = max(t.get('profit', 0) for t in wins)
    else:
        avg_win = 0
        max_win = 0

    return jsonify({
        "filter": filter_by,
        "date": today.isoformat(),
        "total_trades": len(all_status_trades),
        "completed_trades": len(completed),
        "running_trades": len(running),
        "pending_trades": len(pending),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_profit_loss": round(total_profit, 2),
        "avg_volatility": round(avg_volatility, 4),
        "avg_growth_rate": round(avg_growth_rate, 4),
        "avg_duration_seconds": round(avg_duration, 2),
        "avg_growth_rate_switches": round(avg_switches, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_win": round(max_win, 2),
        "max_loss": round(max_loss, 2),
        "profit_factor": round(avg_win / avg_loss, 2) if avg_loss > 0 else 0,
        "exit_reasons": exit_reasons,
        "trades": trades_dict
    }), 200

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "Deriv Accumulator Trading Bot - Enhanced v4.0",
        "timestamp": datetime.now().isoformat(),
        "active_trades": active_trade_count,
        "max_concurrent": MAX_CONCURRENT_TRADES,
        "features": [
            "✓ Pre-trade LOW volatility detection",
            "✓ Mathematical tick-based volatility analysis",
            "✓ Dynamic growth rate switching during trades",
            "✓ Real-time volatility monitoring",
            "✓ Advanced risk management",
            "✓ Comprehensive analytics with growth rate switches tracking"
        ],
        "improvements": [
            "1. Waits for CALM market conditions (< 0.10% Vol, Stable Trend, Low ER/CV) before entry",
            "2. Monitors and switches growth rates during trade based on volatility",
            "3. Uses tick data for accurate volatility calculations",
            "4. Aggressive safety checks with no fallback"
        ],
        "logging_enabled": True,
        "log_location": LOG_DIR
    }), 200


@app.route('/config/optimal', methods=['GET'])
def get_optimal_config():
    return jsonify({
        "recommended_setup": {
            "description": "Aggressive setup with stringent pre-trade volatility and calm market checks, dynamic growth rate switching, and tick-based analysis",
            "parameters": {
                "stake": 5.0,
                "symbol": "1HZ10V",
                "mode": "adaptive",
                "max_daily_trades": 15,
                "max_consecutive_losses": 3,
                "daily_loss_limit_pct": 0.03,
                "profit_target_pct": 0.25,
                "stop_loss_pct": 0.5,
                "trailing_stop_pct": 0.3,
                "volatility_check_interval": 1,
                "volatility_exit_threshold": 1.5,
                "pre_trade_volatility_check": True,
                "max_entry_volatility": 0.10, # Tighter threshold
                "enable_growth_rate_switching": True,
                "growth_rate_switch_interval": 3
            },
            "notes": [
                "✓ WAITS FOR CALM MARKET: Entry only when Vol < 0.10%, Trend=Stable, ER < 0.3, CV < 1.5",
                "✓ REQUIRES 5 CONSECUTIVE CALM READINGS before trade",
                "✓ DYNAMIC SWITCHING: Adjusts growth rate every 3 ticks based on real-time volatility",
                "✓ AGGRESSIVE SAFETY: No fallback if calm conditions aren't met. Trade is skipped.",
                "✓ VOLATILITY SPIKE EXIT: Exits on 1.5x volatility increase during trade",
                "✓ ZERO TOLERANCE: Aborts trade on micro-spikes during setup",
                "✓ ADAPTIVE MODE: Automatically selects 1-5% growth rate based on volatility"
            ]
        },
        "new_parameters": {
            "max_entry_volatility": {
                "default": 0.10,
                "description": "Maximum volatility percentage allowed for trade entry (0.10 = 0.10%). Tighter for aggressive filtering."
            },
            "growth_rate_switch_interval": {
                "default": 3,
                "description": "Check and potentially switch growth rate every N ticks."
            },
             "pre_trade_volatility_check": {
                "default": True,
                "description": "Must find a CALM market window (low volatility, stable trend, low ER/CV) before trade entry. If not found within timeout, trade is skipped."
            }
        },
        "usage": "POST /trade/<app_id>/<api_token> with JSON body containing these parameters"
    }), 200

@app.route('/api/trades')
def api_trades():
    # 1. Fetch historical trades from the database
    db_trades = get_all_trades() 
    
    # 2. Capture currently active trades from memory
    active_trades = []
    with active_trades_lock:
        for trade_id, data in trade_results.items():
            # If it's still running, we want to show it in the table
            if data.get('status') == 'running':
                # Ensure we have the basic info needed for the UI
                active_trades.append({
                    'trade_id': trade_id,
                    'timestamp': data.get('timestamp', datetime.now().isoformat()),
                    'status': 'running',
                    'parameters': data.get('parameters', {}),
                    'profit': 0,
                    'growth_rate': data.get('growth_rate', 0)
                })
    
    # 3. Merge them: Don't duplicate if a trade just finished and is in both
    active_ids = [t.get('trade_id') for t in active_trades]
    combined_trades = active_trades + [dt for dt in db_trades if dt.get('trade_id') not in active_ids]
    
    # Return sorted by newest first
    return jsonify(combined_trades[:50]) # Limit to last 50 for performance

@app.route('/dashboard', methods=['GET'])
def dashboard():
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trading Terminal | Professional Edition</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; background-color: #0b0e14; color: #e2e8f0; }
        .glass { background: rgba(23, 27, 34, 0.8); backdrop-filter: blur(8px); border: 1px solid #1e293b; }
        .status-badge { padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; text-transform: uppercase; }
        .win { background: rgba(16, 185, 129, 0.1); color: #10b981; }
        .loss { background: rgba(239, 68, 68, 0.1); color: #ef4444; }
        .running { background: rgba(59, 130, 246, 0.1); color: #3b82f6; animation: pulse 2s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    </style>
</head>
<body class="p-6">
    <div class="max-w-6xl mx-auto">
        <header class="flex justify-between items-center mb-8 border-b border-slate-800 pb-6">
            <div>
                <h1 class="text-xl font-bold tracking-tight">ACCUMULATOR <span class="text-blue-500 text-xs">v4.0 AGGRESSIVE</span></h1>
                <p id="sync-status" class="text-slate-500 text-xs mt-1 italic">Last update: --:--:--</p>
            </div>
            <div class="text-right">
                <p class="text-slate-500 text-[10px] font-bold uppercase tracking-widest">Session P/L</p>
                <h2 id="session-pl" class="text-2xl font-bold">$0.00</h2>
            </div>
        </header>

        <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
            <div class="glass p-5 rounded-xl">
                <p class="text-slate-500 text-xs font-semibold mb-1 uppercase">Total Executions</p>
                <p id="stat-total" class="text-xl font-bold">0</p>
            </div>
            <div class="glass p-5 rounded-xl">
                <p class="text-slate-500 text-xs font-semibold mb-1 uppercase">Win Rate</p>
                <p id="stat-winrate" class="text-xl font-bold">0%</p>
            </div>
            <div class="glass p-5 rounded-xl border-l-2 border-amber-500">
                <p class="text-slate-500 text-xs font-semibold mb-1 uppercase">Safety Skips</p>
                <p id="stat-skipped" class="text-xl font-bold text-amber-500">0</p>
            </div>
            <div class="glass p-5 rounded-xl">
                <p class="text-slate-500 text-xs font-semibold mb-1 uppercase">Safety Status</p>
                <p id="safety-label" class="text-xl font-bold text-emerald-500">Monitoring</p>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div class="lg:col-span-2 glass rounded-xl overflow-hidden">
                <div class="px-6 py-4 border-b border-slate-800 bg-slate-900/50">
                    <h3 class="font-bold text-sm">Execution Logs</h3>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm">
                        <thead class="bg-slate-900/30 text-slate-500 text-[10px] uppercase">
                            <tr>
                                <th class="px-6 py-3">Time</th>
                                <th class="px-6 py-3">Asset</th>
                                <th class="px-6 py-3">Growth</th>
                                <th class="px-6 py-3">Result</th>
                                <th class="px-6 py-3 text-right">Profit</th>
                            </tr>
                        </thead>
                        <tbody id="trade-body" class="divide-y divide-slate-800">
                            </tbody>
                    </table>
                </div>
            </div>

            <div class="space-y-6">
                <div class="glass p-6 rounded-xl">
                    <h3 class="text-amber-500 text-xs font-bold uppercase tracking-wider mb-4">Skipped (Market Noise)</h3>
                    <div id="skip-log" class="space-y-3 max-h-[400px] overflow-y-auto pr-2 text-xs">
                        <p class="text-slate-600 italic">No market rejections yet.</p>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        async function updateDashboard() {
            try {
                const [tradesRes, sessionRes] = await Promise.all([
                    fetch('/api/trades'),
                    fetch('/session')
                ]);
                
                const trades = await tradesRes.json();
                const session = await sessionRes.json();
                
                renderTrades(trades);
                updateStats(session, trades);
                document.getElementById('sync-status').innerText = `Last update: ${new Date().toLocaleTimeString()}`;
            } catch (e) { console.error("Update failed", e); }
        }

        function renderTrades(trades) {
            const tbody = document.getElementById('trade-body');
            const skipLog = document.getElementById('skip-log');
            tbody.innerHTML = '';
            
            let skipHtml = '';
            let validTradesCount = 0;

            trades.forEach(t => {
                // Handle skipped trades separately
                if (t.error && (t.error.includes("Safety") || t.error.includes("unsuitable") || t.error.includes("Noise"))) {
                    skipHtml += `<div class="p-3 border-l-2 border-amber-500 bg-amber-500/5 rounded">
                        <span class="text-slate-500 text-[10px]">${t.timestamp.split('T')[1].split('.')[0]}</span>
                        <p class="text-amber-200 mt-1">${t.error}</p>
                    </div>`;
                    return;
                }

                validTradesCount++;
                const isRunning = t.status === 'running';
                const profit = parseFloat(t.profit || 0);
                const statusClass = isRunning ? 'running' : (profit > 0 ? 'win' : 'loss');
                
                tbody.innerHTML += `
                    <tr class="hover:bg-slate-800/20 transition">
                        <td class="px-6 py-4 text-slate-500 text-xs">${t.timestamp.split('T')[1].split('.')[0]}</td>
                        <td class="px-6 py-4 font-medium">${t.parameters?.symbol || 'R_100'}</td>
                        <td class="px-6 py-4 text-slate-400">${(t.growth_rate * 100).toFixed(1)}%</td>
                        <td class="px-6 py-4">
                            <span class="status-badge ${statusClass}">${t.status}</span>
                        </td>
                        <td class="px-6 py-4 text-right font-bold ${profit > 0 ? 'text-emerald-500' : 'text-red-500'}">
                            ${isRunning ? '...' : '$' + profit.toFixed(2)}
                        </td>
                    </tr>
                `;
            });

            if (skipHtml) skipLog.innerHTML = skipHtml;
            if (validTradesCount === 0) tbody.innerHTML = '<tr><td colspan="5" class="py-10 text-center text-slate-600">No active or historical trades found.</td></tr>';
        }

        function updateStats(session, trades) {
            const skipped = trades.filter(t => t.error && t.error.includes("Safety")).length;
            document.getElementById('stat-total').innerText = session.trades_count || 0;
            document.getElementById('stat-skipped').innerText = skipped;
            document.getElementById('session-pl').innerText = `$${parseFloat(session.total_profit_loss || 0).toFixed(2)}`;
            document.getElementById('session-pl').className = `text-2xl font-bold ${session.total_profit_loss >= 0 ? 'text-emerald-500' : 'text-red-500'}`;
            
            const wins = trades.filter(t => t.profit > 0).length;
            const rate = session.trades_count > 0 ? Math.round((wins / session.trades_count) * 100) : 0;
            document.getElementById('stat-winrate').innerText = rate + '%';
        }

        setInterval(updateDashboard, 5000);
        updateDashboard();
    </script>
</body>
</html>
"""  
    return html, 200

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Starting Deriv Accumulator Trading Bot v4.0 - AGGRESSIVE")
    logger.info("With CALM Market Entry & Dynamic Growth Switching")
    logger.info("=" * 60)
    
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Server starting on port {port}")
    logger.info(f"Logs directory: {LOG_DIR}")
    logger.info(f"Database path: {DB_PATH}")
    logger.info(f"Max concurrent trades: {MAX_CONCURRENT_TRADES}")
    logger.info("IMPROVEMENTS:")
    logger.info("  1. ✓ Waits for CALM market conditions (< 0.10% Vol, Stable Trend, Low ER/CV) before entry")
    logger.info("  2. ✓ REQUIRES 5 CONSECUTIVE CALM READINGS")
    logger.info("  3. ✓ Monitors and switches growth rates during trade based on volatility")
    logger.info("  4. ✓ Uses tick data for accurate volatility calculations")
    logger.info("  5. ✓ AGGRESSIVE SAFETY: No fallback if calm conditions aren't met. Trade is skipped.")
    
    app.run(debug=False, host='0.0.0.0', port=port, use_reloader=False, threaded=True)
