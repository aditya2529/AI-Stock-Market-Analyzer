"""Nifty 500 trading universe + morning scanner.

Morning filter picks top N stocks each day by:
    1. Liquidity   — previous day volume > threshold
    2. Volatility  — ATR% high enough to make intraday profit
    3. Momentum    — price above 20-bar MA (trending, not dead)
"""
import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ── Nifty 500 universe (200 most liquid, Yahoo Finance format) ─────────
NIFTY_500_SYMBOLS = [
    # Nifty 50
    "RELIANCE.NS","TCS.NS","HDFCBANK.NS","INFY.NS","ICICIBANK.NS",
    "HINDUNILVR.NS","SBIN.NS","BHARTIARTL.NS","KOTAKBANK.NS","ITC.NS",
    "LT.NS","AXISBANK.NS","ASIANPAINT.NS","MARUTI.NS","SUNPHARMA.NS",
    "TITAN.NS","BAJFINANCE.NS","WIPRO.NS","HCLTECH.NS","ULTRACEMCO.NS",
    "ONGC.NS","NTPC.NS","POWERGRID.NS","ADANIENT.NS","ADANIPORTS.NS",
    "NESTLEIND.NS","TECHM.NS","M&M.NS","BAJAJFINSV.NS","JSWSTEEL.NS",
    "TATAMOTORS.NS","HINDALCO.NS","TATASTEEL.NS","DRREDDY.NS","CIPLA.NS",
    "COALINDIA.NS","BPCL.NS","DIVISLAB.NS","EICHERMOT.NS","BRITANNIA.NS",
    "GRASIM.NS","APOLLOHOSP.NS","BAJAJ-AUTO.NS","INDUSINDBK.NS","TATACONSUM.NS",
    "HEROMOTOCO.NS","SBILIFE.NS","HDFCLIFE.NS","ICICIGI.NS","LTIM.NS",
    # Nifty Next 50
    "ADANIGREEN.NS","ADANITRANS.NS","AMBUJACEM.NS","AUROPHARMA.NS","BANDHANBNK.NS",
    "BERGEPAINT.NS","BOSCHLTD.NS","CANBK.NS","CHOLAFIN.NS","COLPAL.NS",
    "DABUR.NS","DMART.NS","DLF.NS","FEDERALBNK.NS","GAIL.NS",
    "GODREJCP.NS","HAVELLS.NS","ICICIGI.NS","IDFCFIRSTB.NS","INDUSTOWER.NS",
    "IRCTC.NS","JUBLFOOD.NS","LICHSGFIN.NS","LUPIN.NS","MARICO.NS",
    "MCDOWELL-N.NS","MPHASIS.NS","NAUKRI.NS","OBEROIRLTY.NS","OFSS.NS",
    "PAGEIND.NS","PEL.NS","PERSISTENT.NS","PETRONET.NS","PIDILITIND.NS",
    "PNB.NS","RECLTD.NS","SAIL.NS","SHREECEM.NS","SIEMENS.NS",
    "SRF.NS","TORNTPHARM.NS","TRENT.NS","UBL.NS","UNITDSPR.NS",
    "UPL.NS","VEDL.NS","VOLTAS.NS","WHIRLPOOL.NS","ZOMATO.NS",
    # Mid-cap liquid
    "ABCAPITAL.NS","ABFRL.NS","ALKEM.NS","APLLTD.NS","ASHOKLEY.NS",
    "ASTRAL.NS","ATUL.NS","AUBANK.NS","BALKRISIND.NS","BATAINDIA.NS",
    "BHEL.NS","BIOCON.NS","CANFINHOME.NS","CASTROLIND.NS","CESC.NS",
    "CHAMBLFERT.NS","CONCOR.NS","CROMPTON.NS","CUMMINSIND.NS","DALBHARAT.NS",
    "DEEPAKNTR.NS","DIXON.NS","ESCORTS.NS","EXIDEIND.NS","GLENMARK.NS",
    "GMRINFRA.NS","GODREJIND.NS","GODREJPROP.NS","GRANULES.NS","GSPL.NS",
    "GUJGASLTD.NS","HDFCAMC.NS","HINDPETRO.NS","HONAUT.NS","IBREALEST.NS",
    "IDBI.NS","INDHOTEL.NS","INDIGO.NS","IOC.NS","IGL.NS",
    "JKCEMENT.NS","JUBLINGREA.NS","KAJARIACER.NS","KPITTECH.NS","LALPATHLAB.NS",
    "LAURUSLABS.NS","LICI.NS","LINDEINDIA.NS","MGL.NS","METROPOLIS.NS",
    "MFSL.NS","MINDTREE.NS","MOTHERSON.NS","MUTHOOTFIN.NS","NATIONALUM.NS",
    "NBCC.NS","NLCINDIA.NS","NCC.NS","NMDC.NS","NTPCGREEN.NS",
    "NYKAA.NS","PAYTM.NS","PFIZER.NS","PFC.NS","PHOENIXLTD.NS",
    "PIIND.NS","POLICYBZR.NS","POLYCAB.NS","RAIN.NS","RAMCOCEM.NS",
    "RBLBANK.NS","REDINGTON.NS","ROUTE.NS","SAFARI.NS","SCHAEFFLER.NS",
    "SJVN.NS","SKFINDIA.NS","SOBHA.NS","STAR.NS","SUNTV.NS",
    "SUPREMEIND.NS","SYNGENE.NS","TATACHEM.NS","TATACOMM.NS","TATAELXSI.NS",
    "TATAINVEST.NS","TATAPOWER.NS","TECHM.NS","THERMAX.NS","TIINDIA.NS",
    "TIMKEN.NS","TRIDENT.NS","TTKPRESTIG.NS","UJJIVANSFB.NS","VAIBHAVGBL.NS",
    "VGUARD.NS","VINATIORGA.NS","WELCORP.NS","WIPRO.NS","ZEEL.NS",
]


def get_morning_universe(top_n: int = 50, resolution: str = "5m") -> list[str]:
    """
    Every morning: filter NIFTY_500_SYMBOLS down to top_n tradeable stocks.

    Scoring criteria (all from previous day's data — no lookahead):
        - Volume score:    previous day volume vs 20-day average
        - Volatility score: ATR% — how much the stock moves (need movement to profit)
        - Momentum score:  close vs 20-bar MA — trending stocks only

    Returns list of top_n symbols sorted by composite score.
    """
    import yfinance as yf

    logger.info("Scanning universe — fetching previous day data for %d symbols …",
                len(NIFTY_500_SYMBOLS))

    scores = []
    # Fetch 1-day data for all symbols in one batch (fast)
    try:
        raw = yf.download(
            NIFTY_500_SYMBOLS,
            period="30d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            group_by="ticker",
        )
    except Exception as e:
        logger.warning("Universe batch fetch failed: %s — using default 25 symbols", e)
        from config import DEFAULT_SYMBOLS
        return DEFAULT_SYMBOLS[:top_n]

    for sym in NIFTY_500_SYMBOLS:
        try:
            if sym in raw.columns.get_level_values(0):
                df = raw[sym].dropna()
            else:
                continue
            if len(df) < 5:
                continue

            close  = df["Close"]
            volume = df["Volume"]
            high   = df["High"]
            low    = df["Low"]

            # Volume score: latest day vs 20-day average
            avg_vol = volume.mean()
            if avg_vol < 500_000:   # skip illiquid stocks
                continue
            vol_score = float(volume.iloc[-1] / (avg_vol + 1))

            # Volatility score: ATR% over last 5 days
            tr = (high - low).tail(5).mean()
            atr_pct = float(tr / (close.iloc[-1] + 1e-9))
            if atr_pct < 0.005:     # skip stocks moving less than 0.5%/day
                continue

            # Momentum score: close vs 20-day MA
            ma20 = close.rolling(20).mean().iloc[-1]
            mom_score = float(close.iloc[-1] / (ma20 + 1e-9))

            composite = vol_score * 0.4 + atr_pct * 100 * 0.4 + mom_score * 0.2
            scores.append((sym, composite))

        except Exception:
            continue

    if not scores:
        logger.warning("Universe scan returned no results — using default symbols")
        from config import DEFAULT_SYMBOLS
        return DEFAULT_SYMBOLS[:top_n]

    scores.sort(key=lambda x: -x[1])
    selected = [s[0] for s in scores[:top_n]]
    logger.info("Universe scan complete — selected %d symbols. Top 5: %s",
                len(selected), selected[:5])
    return selected
