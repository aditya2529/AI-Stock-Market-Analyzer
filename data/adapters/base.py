from abc import ABC, abstractmethod
import pandas as pd


class DataAdapter(ABC):
    """Abstract base for all market data adapters.

    Concrete subclasses must return a DataFrame with columns:
        time (datetime), open, high, low, close, volume
    indexed by an integer range index (time is a plain column, not index).
    """

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        years: int = 3,
        resolution: str = "1d",
    ) -> pd.DataFrame:
        """Fetch historical OHLCV data.

        Args:
            symbol: Ticker string in the adapter's native format.
            years:  Number of years of history to fetch.
            resolution: Bar resolution — '1d' daily, '1h' hourly.

        Returns:
            DataFrame with columns [time, open, high, low, close, volume].
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the adapter is properly configured and reachable."""
        ...
