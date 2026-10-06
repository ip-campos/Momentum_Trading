from django.conf import settings
from massive import RESTClient
from datetime import datetime, timedelta
import time
import logging
from urllib3.exceptions import MaxRetryError
from massive.exceptions import BadResponse
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)

class MassiveAPIClient:
    def __init__(self, api_key:str = None, requests_per_minute: int = 5):
        self.api_key = api_key or settings.MASSIVE_API_KEY
        if not self.api_key:
            raise ValueError("Massive API key is required")
        self.client = RESTClient(self.api_key)
        self.requests_per_minute = requests_per_minute

        self.request_times = []
        self.cache = {} # In-memory cache

    def _get_cache_key(self, ticker: str, start_date: str, end_date: str) -> str:
        """Generate cache key for API requests"""
        return f"{ticker}:{start_date}:{end_date}"

    def _rate_limit(self):
        """Rate limit to avoid hitting API limits"""
        current_time = time.monotonic()
        # Remove requests older than 1 minute
        self.request_times = [t for t in self.request_times if current_time - t < 60]
        if len(self.request_times) >= self.requests_per_minute:
            sleep_time = 60 - (current_time - self.request_times[0]) + 1
            if sleep_time > 0:
                logger.info(f"Rate limiting: sleeping for {sleep_time:.2f} seconds")
                time.sleep(sleep_time)

        current_time = time.monotonic()
        self.request_times = [t for t in self.request_times if current_time - t < 60]
        self.request_times.append(current_time)

    def fetch_stock_data(
            self,
            ticker: str,
            start_date: str,
            end_date: str,
            multiplier: int = 1,
            timespan: str = "day",
            adjusted: bool = True,
            limit: int = 50000,
            use_cache: bool = True,
            max_retries: int = 3,
            retry_delay: int = 60
    ) -> list[dict]:
        cache_key = self._get_cache_key(ticker, start_date, end_date)

        # Check cache first
        if use_cache and cache_key in self.cache:
            logger.info(f"Using cached data for {ticker}")
            return self.cache[cache_key]
        for attempt in range(max_retries):
            self._rate_limit()
            try:
                aggs = []
                for agg in self.client.list_aggs(
                    ticker,
                    multiplier,
                    timespan,
                    start_date,
                    end_date,
                    adjusted=adjusted,
                    limit=limit
                ):
                    aggs.append(
                        {
                            "date": datetime.fromtimestamp(agg.timestamp / 1000).date(),
                            "open": agg.open,
                            "high": agg.high,
                            "low": agg.low,
                            "close": agg.close,
                            "volume": agg.volume,
                            "vwap": getattr(agg, "vwap", None),
                            "transactions": getattr(agg, "transactions", None)
                        }
                    )
                if use_cache:
                    self.cache[cache_key] = aggs

                logger.info(f"Fetched {len(aggs)} data points for {ticker}")
                return aggs
            except MaxRetryError as e:
                if "429" in str(e) and attempt < max_retries - 1:
                    logger.warning(
                        f"Rate limited on attempt {attempt+1} for {ticker}, retrying in {retry_delay} seconds"
                    )
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.error(f"Max retries exceeded for {ticker}: {str(e)}")
                    raise
            except (ValueError, TypeError, BadResponse) as e:
                logger.error(f"Error fetching data for {ticker}: {str(e)}")
                raise
    
    def fetch_bulk_momentum_data(
            self, 
            tickers: list[str], 
            calculation_date: datetime = None
            ) -> dict[str, dict[str, Optional[float]]]:
        """Bulk fetch using Massive's grouped daily API - only 2 API calls total!
        Fetches all tickers for 12-month and 1-month dates simultaneously.
        Returns:
        Dict with ticker as key and dict with 'price_12mo' and 'price_1mo' as values
        """
        if calculation_date is None:
            calculation_date = datetime.now().date()

        twelve_months_ago = calculation_date - timedelta(days=365)
        one_month_ago = calculation_date - timedelta(days=30)

        logger.info(
            f"TRUE bulk fetching momentum data for {len(tickers)} stocks using grouped daily API"
        )
        logger.info(
            f"Target dates: 12mo={twelve_months_ago}, 1mo={one_month_ago}"
        )

        momentum_data = {}

        # Initialize all ticker with None values
        for ticker in tickers:
            momentum_data[ticker] = {"price_12mo": None, "price_1mo": None}

        try:
            # Apply rate limiting before API calls
            self._rate_limit()

            # CALL 1: Get all stocks' prices for 12 months ago (1 API call)
            logger.info(f"Fetching grouped daily data for 12 months ago ({twelve_months_ago})")

            twelve_months_data = self.client.get_grouped_daily_aggs(
                date=twelve_months_ago, adjusted=True
            )

            # Process 12-month data
            twelve_months_prices = {}
            for agg in twelve_months_data:
                if hasattr(agg, "ticker") and agg.ticker in tickers:
                    twelve_months_prices[agg.ticker] = float(agg.close)

            logger.info(f"Found 12-month prices for {len(twelve_months_prices)} stocks")

            # Apply rate limiting before second API call
            self._rate_limit()

            # Call 2: Get all stocks' prices for 1 month ago (1 API call)
            logger.info(f"Fetching grouped daily data for 1 month ago ({one_month_ago})")
            
            one_month_data = self.client.get_grouped_daily_aggs(
                date=one_month_ago, adjusted=True
            )

            # Process 1-month data
            one_month_prices = {}
            for agg in one_month_data:
                if hasattr(agg, "ticker") and agg.ticker in tickers:
                    one_month_prices[agg.ticker] = float(agg.close)

            logger.info(f"Found 1-month prices for {len(one_month_prices)} stocks")

            # Combine Results
            for ticker in tickers:
                momentum_data[ticker] = {
                    "price_12mo": twelve_months_prices.get(ticker),
                    "price_1mo": one_month_prices.get(ticker)
                }

                logger.debug(
                    f"Momentum data for {ticker}:"
                    f"12mo=${momentum_data[ticker]["price_12mo"]}"
                    f"1mo=${momentum_data[ticker]["price_1mo"]}"
                )

            # Handle weekend/holiday fallback if needed
            missing_tickers = [
                t for t in tickers 
                if not momentum_data[t]["price_12mo"] or not momentum_data[t]["price_1mo"]
                ]
            if missing_tickers:
                logger.info(f"Handling {len(missing_tickers)} stocks with missing data using fallback dates")
                momentum_data = self._handle_missing_data_fallback(
                    momentum_data, missing_tickers, calculation_date
                )

            complete_count = sum(
                values["price_12mo"] is not None and values["price_1mo"] is not None
                for values in momentum_data.values()
            )
            logger.info("Momentum data complete for %d/%d tickers", complete_count, len(tickers))

        except Exception as e:
            logger.error(
                f"Error with grouped daily API, falling back to individual calls: {str(e)}"
            )
            # Fallback to the old method if grouped daily fails
            return self._fetch_bulk_momentum_data_fallback(tickers, calculation_date)
        return momentum_data

    def _fetch_bulk_momentum_data_fallback(
            self, tickers: list[str], calculation_date: datetime
    ) -> dict[str, dict[str, Optional[float]]]:
        """
        Fallback to the old method if grouped daily API fails
        """
        logger.warning("Using fallback method - this will be slower")

        twelve_months_ago = calculation_date-timedelta(days=365)
        one_month_ago = calculation_date-timedelta(days=30)

        # Use broader date range to capture both periods in one API call
        start_date = twelve_months_ago-timedelta(days=30) # Buffer for weekends/holidays
        end_date = calculation_date+timedelta(days=7) # Buffer for current date

        logger.info(
            f"Fallback: Fetching momentum data for {len(tickers)} stocks from {start_date} to {end_date}"
        )

        # Fetch all data with batching
        all_data = self.fetch_multiple_stocks(
            tickers = tickers,
            start_date = start_date.strftime("%Y-%m-%d"),
            end_date = end_date.strftime("%Y-%m-%d"),
            batch_size = 5, # Smaller batch for bulk operations
            delay_between_batches = 15
        )

        # Extract prices for momentum calculation
        momentum_data = {}

        for ticker in tickers:
            data = all_data.get(ticker, [])
            if not data:
                momentum_data[ticker] = {"price_12mo": None, "price_1mo": None}
                continue
            # Convert to DataFrame for easier date filtering
            df = pd.DataFrame(data)
            if df.empty:
                momentum_data[ticker] = {"price_12mo": None, "price_1mo": None}
                continue

            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")

            # Find closest prices to target dates
            price_12mo = self._find_closest_price(df, twelve_months_ago)
            price_1mo = self._find_closest_price(df, one_month_ago)

            momentum_data[ticker] = {
                "price_12mo": price_12mo,
                "price_1mo": price_1mo
            } 

            logger.debug(f"Momentum data for {ticker}: 12mo=${price_12mo}, 1mo=${price_1mo}")

        return  momentum_data

    def _handle_missing_data_fallback(
            self,
            momentum_data: dict[str, dict[str, Optional[float]]],
            missing_tickers: list[str],
            calculation_date: datetime
    ) -> dict[str, dict[str, Optional[float]]]:
        """
        Fill missing prices using up to seven calendar days before each target.
        """
        twelve_months_ago = calculation_date-timedelta(days=365)
        one_month_ago = calculation_date-timedelta(days=30)

        target_dates = (
            ("price_12mo", twelve_months_ago),
            ("price_1mo", one_month_ago),
        )

        for days_back in range(1, 8):
            for price_key, target_date in target_dates:
                pending = {
                    ticker for ticker in missing_tickers
                    if momentum_data[ticker][price_key] is None
                }
                if not pending:
                    continue

                fallback_date = target_date - timedelta(days=days_back)
                # US stock daily bars do not need weekend requests.
                if fallback_date.weekday() >= 5:
                    continue

                try:
                    self._rate_limit()
                    rows = list(self.client.get_grouped_daily_aggs(
                        date=fallback_date, adjusted=True
                    ) or [])
                    found = 0
                    for agg in rows:
                        ticker = getattr(agg, "ticker", None)
                        close = getattr(agg, "close", None)
                        if ticker in pending and close is not None:
                            momentum_data[ticker][price_key] = float(close)
                            pending.remove(ticker)
                            found += 1

                    if pending:
                        logger.warning(
                            "MOMENTUM: %s date=%s: %d rows, %d prices found, "
                            "%d tickers still missing",
                            price_key, fallback_date, len(rows), found, len(pending),
                        )
                    else:
                        logger.info(
                            "MOMENTUM: %s date=%s: %d prices found",
                            price_key, fallback_date, found,
                        )
                except Exception:
                    logger.exception(
                        "MOMENTUM: API error fetching %s date=%s",
                        price_key, fallback_date,
                    )

            missing_tickers = [
                ticker for ticker in missing_tickers
                if (
                    momentum_data[ticker]["price_12mo"] is None
                    or momentum_data[ticker]["price_1mo"] is None
                )
            ]
            if not missing_tickers:
                break

        if missing_tickers:
            logger.warning(
                "MOMENTUM: missing prices after the 7-day search: %s",
                ", ".join(missing_tickers),
            )

        return momentum_data

    def _find_closest_price(
        self, df: pd.DataFrame, target_date: datetime, tolerance_days: int = 7
    ) -> Optional[float]:
        """Return the latest available close on or before the target date."""
        if df.empty:
            return None
        if isinstance(target_date, datetime):
            target_date = target_date.date()
        target = pd.Timestamp(target_date)
        dates = pd.to_datetime(df["date"])
        eligible = df.loc[
            (dates >= target - timedelta(days=tolerance_days))
            & (dates <= target)
            & df["close"].notna()
        ].copy()
        if eligible.empty:
            return None
        eligible["date"] = pd.to_datetime(eligible["date"])
        row = eligible.sort_values("date").iloc[-1]
        return float(row["close"])

    def fetch_multiple_stocks(
            self,
            tickers: list[str],
            start_date: str,
            end_date: str,
            adjusted: bool = True,
            batch_size: int = 10,
            delay_between_batches: int = 12
    ) -> dict[str, list[dict]]:
        """
        Fetch data for multiple stocks with intelligent batching and rate limiting.

        Args:
            tickers: list of stock tickers
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            adjusted: wether to use adjusted prices
            batch_size: Number of stocks to process in each batch
            delay_between_batches: seconds to wait between batches
        """
        results = {}
        total_tickers = len(tickers)

        logger.info(f"Fetching data for {total_tickers} stocks in batches of {batch_size}")

        # Process tickers in batches
        for i in range(0, total_tickers, batch_size):
            batch_tickers = tickers[i:i+batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (total_tickers + batch_size - 1) // batch_size

            logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch_tickers)} stocks)")

            for ticker in batch_tickers:
                try:
                    results[ticker] = self.fetch_stock_data(
                        ticker=ticker,
                        start_date=start_date,
                        end_date=end_date,
                        adjusted=adjusted
                    )
                    logger.info(f"Successfully fetched data for {ticker}")
                except (MaxRetryError, ValueError, TypeError, BadResponse) as e:
                    logger.error(f"Failed to fetch data for {ticker}: {str(e)}")
                    results[ticker] = []

            # Delay between batches to avoid rate limits
            if i + batch_size < total_tickers:
                logger.info(f"Waiting {delay_between_batches} seconds before next batch...")
                time.sleep(delay_between_batches)
        return results

    def get_price_on_date(
        self, 
        ticker: str, 
        target_date: datetime, 
        tolerance_days: int = 7
    ) -> Optional[float]:
        start_date = target_date - timedelta(days=tolerance_days)
        end_date = target_date
        
        try:
            data = self.fetch_stock_data(
                ticker=ticker,
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d')
            )
            
            if not data:
                return None
            
            # Find the closest date
            target_date_obj = target_date if hasattr(target_date, 'date') else target_date
            if hasattr(target_date_obj, 'date'):
                target_date_obj = target_date_obj.date()
                
            closest_data = min(
                data, 
                key=lambda x: abs((x['date'] - target_date_obj).days)
            )
            
            return float(closest_data['close'])
            
        except (ValueError, TypeError) as e:
            logger.error(f"Error getting price for {ticker} on {target_date}: {str(e)}")
            return None

    def get_sp500_tickers(self) -> list[str]:
        return [
            "AAPL",
            "MSFT",
            "NVDA",
            "JNJ",
            "GOOGL",
            "GOOG",
            "AMZN",
            "META",
            "TSLA",
            "BRK.B",
            "UNH",
            "JPM",
            "V",
            "PG",
            "XOM",
            "HD",
            "CVX",
            "MA",
            "BAC",
            "ABBV",
            "PFE",
            "AVGO",
            "KO",
            "COST",
            "DIS",
            "TMO",
            "WMT",
            "DHR",
            "NEE",
            "VZ",
            "ABT",
            "MRK",
            "ADBE",
            "CRM",
            "NFLX",
            "NKE",
            "INTC",
            "AMD",
            "T",
            "TXN",
            "COP",
            "LLY",
            "PM",
            "RTX",
            "HON",
            "CMCSA",
            "UPS",
            "QCOM",
            "SBUX",
            "LOW"
        ]

                
def get_massive_client() -> MassiveAPIClient:
    return MassiveAPIClient()
