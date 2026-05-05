#!/usr/bin/env python3
"""
Market Closure Detector for U.S. Stock Markets
Detects holidays, weekends, and market hours
"""

import datetime
from datetime import date, time
import holidays

class MarketClosureDetector:
    """Detects if U.S. stock markets are open"""
    
    def __init__(self):
        # U.S. stock market hours (ET)
        self.market_open = time(9, 30)   # 9:30 AM ET
        self.market_close = time(16, 0)  # 4:00 PM ET
        
        # U.S. stock market holidays (NYSE/NASDAQ)
        self.us_holidays = holidays.US(years=[2025, 2026, 2027])
        
        # Additional trading holidays (markets closed)
        self.trading_holidays = [
            # 2026 holidays
            date(2026, 1, 1),    # New Year's Day
            date(2026, 1, 19),   # Martin Luther King Jr. Day
            date(2026, 2, 16),   # Presidents' Day
            date(2026, 4, 3),    # Good Friday (TODAY - MARKET CLOSED)
            date(2026, 5, 25),   # Memorial Day
            date(2026, 7, 3),    # Independence Day (observed)
            date(2026, 9, 7),    # Labor Day
            date(2026, 11, 26),  # Thanksgiving Day
            date(2026, 12, 25),  # Christmas Day
            
            # 2025 holidays (for reference)
            date(2025, 1, 1),    # New Year's Day
            date(2025, 1, 20),   # Martin Luther King Jr. Day
            date(2025, 2, 17),   # Presidents' Day
            date(2025, 4, 18),   # Good Friday
            date(2025, 5, 26),   # Memorial Day
            date(2025, 7, 4),    # Independence Day
            date(2025, 9, 1),    # Labor Day
            date(2025, 11, 27),  # Thanksgiving Day
            date(2025, 12, 25),  # Christmas Day
        ]
    
    def is_market_holiday(self, check_date=None):
        """Check if date is a market holiday"""
        if check_date is None:
            check_date = datetime.date.today()
        
        # Check standard US holidays
        if check_date in self.us_holidays:
            return True
        
        # Check trading-specific holidays
        if check_date in self.trading_holidays:
            return True
        
        # Good Friday — compute dynamically for any year using Easter algorithm
        # Good Friday is the Friday before Easter Sunday
        try:
            from dateutil.easter import easter
            easter_date = easter(check_date.year)
            good_friday = easter_date - datetime.timedelta(days=2)
            if check_date == good_friday:
                return True
        except ImportError:
            # Fallback: hardcoded Good Friday dates if dateutil unavailable
            _good_fridays = {
                date(2025, 4, 18), date(2026, 4, 3), date(2027, 3, 26),
                date(2028, 4, 14), date(2029, 3, 30), date(2030, 4, 19),
            }
            if check_date in _good_fridays:
                return True

        return False
    
    def is_weekend(self, check_date=None):
        """Check if date is weekend"""
        if check_date is None:
            check_date = datetime.date.today()
        
        # Monday = 0, Sunday = 6
        return check_date.weekday() >= 5  # Saturday or Sunday
    
    def is_market_hours(self, check_time=None):
        """Check if current time is within market hours (ET)"""
        if check_time is None:
            # Get current ET time — use pytz for correct EST/EDT handling
            import pytz
            et = pytz.timezone('America/New_York')
            et_now = datetime.datetime.now(et)
            check_time = et_now.time()
        
        return self.market_open <= check_time <= self.market_close
    
    def is_market_open(self, check_datetime=None):
        """
        Comprehensive check if market is open
        Returns: (is_open, reason)
        """
        if check_datetime is None:
            check_datetime = datetime.datetime.now()
        
        # Extract date and time
        check_date = check_datetime.date()
        check_time = check_datetime.time()
        
        # Check weekend
        if self.is_weekend(check_date):
            return False, "Weekend"
        
        # Check holiday
        if self.is_market_holiday(check_date):
            holiday_name = "Holiday"
            if check_date in self.trading_holidays:
                holiday_name = "Trading Holiday"
            elif check_date in self.us_holidays:
                holiday_name = self.us_holidays.get(check_date, "Holiday")
            return False, holiday_name
        
        # Check market hours
        if not self.is_market_hours(check_time):
            if check_time < self.market_open:
                return False, "Before market open"
            else:
                return False, "After market close"
        
        return True, "Market is open"
    
    def get_next_market_open(self):
        """Get next date/time when market will be open"""
        now = datetime.datetime.now()
        current_date = now.date()
        
        # Check next 10 days
        for days_ahead in range(1, 11):
            next_date = current_date + datetime.timedelta(days=days_ahead)
            
            # Skip weekends
            if self.is_weekend(next_date):
                continue
            
            # Skip holidays
            if self.is_market_holiday(next_date):
                continue
            
            # Market opens at 9:30 AM ET
            market_open_dt = datetime.datetime.combine(next_date, self.market_open)
            return market_open_dt
        
        return None
    
    def should_skip_trading_activity(self):
        """Main function for cron jobs to check"""
        is_open, reason = self.is_market_open()
        
        if not is_open:
            print(f"⚠️ Market closed: {reason}")
            print(f"Current time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")
            
            next_open = self.get_next_market_open()
            if next_open:
                print(f"Next market open: {next_open.strftime('%Y-%m-%d %H:%M:%S ET')}")
            
            return True, reason
        
        return False, "Proceed with trading activity"


def main():
    """Test function"""
    detector = MarketClosureDetector()
    
    print("=" * 60)
    print("MARKET CLOSURE DETECTOR TEST")
    print("=" * 60)
    
    # Test current status
    skip, reason = detector.should_skip_trading_activity()
    
    if skip:
        print(f"❌ SKIP TRADING ACTIVITIES: {reason}")
    else:
        print(f"✅ PROCEED WITH TRADING: {reason}")
    
    print("\n" + "=" * 60)
    print("DETAILED STATUS:")
    print("=" * 60)
    
    # Detailed checks
    today = datetime.date.today()
    print(f"Today: {today.strftime('%Y-%m-%d')}")
    print(f"Weekend: {'Yes' if detector.is_weekend() else 'No'}")
    print(f"Holiday: {'Yes' if detector.is_market_holiday() else 'No'}")
    
    if detector.is_market_holiday():
        if today in detector.trading_holidays:
            print(f"Holiday type: Trading Holiday")
        elif today in detector.us_holidays:
            print(f"Holiday: {detector.us_holidays.get(today, 'Unknown')}")
    
    print(f"Market hours: {'Yes' if detector.is_market_hours() else 'No'}")
    
    # Next market open
    next_open = detector.get_next_market_open()
    if next_open:
        print(f"\nNext market open: {next_open.strftime('%Y-%m-%d %H:%M:%S ET')}")
    
    return skip


if __name__ == "__main__":
    try:
        result = main()
        exit(result if isinstance(result, int) else 0)
    except KeyboardInterrupt:
        exit(0)
    except Exception as _e:
        import traceback; traceback.print_exc(); exit(2)
