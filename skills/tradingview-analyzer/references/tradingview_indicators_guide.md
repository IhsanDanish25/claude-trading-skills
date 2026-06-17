# TradingView Indicators Interpretation Guide

## Recommendation Summary

TradingView computes an aggregate recommendation from oscillators and moving averages:

| Recommendation | Meaning |
|---|---|
| STRONG_BUY | Majority of indicators bullish, strong upward momentum |
| BUY | More bullish signals than bearish |
| NEUTRAL | Mixed signals, no clear direction |
| SELL | More bearish signals than bullish |
| STRONG_SELL | Majority of indicators bearish, strong downward momentum |

The summary breaks down into three sub-scores: **Oscillators**, **Moving Averages**, and **Overall**. Each counts the number of BUY/SELL/NEUTRAL signals from its component indicators.

## Oscillators

### RSI (Relative Strength Index)
- **Period**: 14
- **Overbought**: > 70 (potential sell signal)
- **Oversold**: < 30 (potential buy signal)
- **Neutral zone**: 30-70
- **Divergence**: When price makes new high but RSI doesn't → bearish divergence (and vice versa)

### Stochastic %K
- **Overbought**: > 80
- **Oversold**: < 20
- **Signal**: %K crossing above %D from oversold = bullish; crossing below from overbought = bearish

### CCI (Commodity Channel Index)
- **Period**: 20
- **Overbought**: > +100
- **Oversold**: < -100
- **Trend**: Sustained readings above 0 = bullish trend; below 0 = bearish

### ADX (Average Directional Index)
- **Period**: 14
- **Strong trend**: > 25
- **Weak/no trend**: < 20
- **Rising ADX**: Trend strengthening regardless of direction
- **Note**: ADX measures trend strength, not direction. Use +DI/-DI for direction.

### Awesome Oscillator (AO)
- **Bullish**: Crossing above zero, or twin peaks pattern below zero
- **Bearish**: Crossing below zero, or twin peaks pattern above zero
- **Saucer**: Small pullback in direction of trend = continuation

### Momentum
- **Period**: 10
- **Bullish**: Above zero and rising
- **Bearish**: Below zero and falling
- **Divergence**: Key reversal signal when momentum diverges from price

### MACD
- **Signal line crossover**: MACD crossing above signal = bullish; below = bearish
- **Zero line crossover**: MACD crossing above zero = bullish momentum shift
- **Histogram**: Increasing = trend accelerating; decreasing = trend decelerating
- **Divergence**: Price vs MACD divergence is a strong reversal indicator

### Williams %R
- **Overbought**: > -20 (note: scale is -100 to 0)
- **Oversold**: < -80
- **Failure swings**: More reliable than simple threshold crossings

### Bull/Bear Power
- **Bull Power > 0**: Buyers can push price above EMA
- **Bear Power < 0**: Sellers can push price below EMA
- **Best signals**: Bull power negative but rising (bottom), bear power positive but falling (top)

### Ultimate Oscillator (UO)
- **Periods**: 7, 14, 28 (multi-timeframe)
- **Overbought**: > 70
- **Oversold**: < 30
- **Buy signal**: Bullish divergence + UO breaks above divergence high

## Moving Averages

TradingView evaluates these moving averages and reports BUY (price above MA), SELL (price below MA), or NEUTRAL:

### Simple Moving Averages (SMA)
| Period | Use |
|---|---|
| SMA 10 | Short-term trend |
| SMA 20 | Near-term trend, swing trading |
| SMA 30 | Intermediate short-term |
| SMA 50 | Intermediate trend, institutional level |
| SMA 100 | Long-term intermediate |
| SMA 200 | Long-term trend, bull/bear market line |

### Exponential Moving Averages (EMA)
Same periods as SMA but with more weight on recent prices. EMAs react faster to price changes.

### Key MA Relationships
- **Golden Cross**: 50 SMA crosses above 200 SMA → bullish long-term signal
- **Death Cross**: 50 SMA crosses below 200 SMA → bearish long-term signal
- **Price vs 200 SMA**: Above = bull market; below = bear market (institutional benchmark)
- **MA Stack**: All MAs aligned short→long (10>20>50>100>200) = strong trend

### Ichimoku Cloud
- **Conversion Line (Tenkan)**: 9-period midpoint, fast signal
- **Base Line (Kijun)**: 26-period midpoint, trend direction
- **Leading Span A**: Midpoint of Tenkan/Kijun, forward 26 periods
- **Leading Span B**: 52-period midpoint, forward 26 periods
- **Cloud (Kumo)**: Area between Span A and B
  - Price above cloud = bullish
  - Price below cloud = bearish
  - Price in cloud = consolidation
  - Cloud color change = potential trend reversal

### VWAP (Volume Weighted Average Price)
- **Intraday benchmark**: Fair value for the day
- **Above VWAP**: Bullish intraday bias
- **Below VWAP**: Bearish intraday bias
- **Touch and bounce**: VWAP often acts as dynamic support/resistance

## Pivot Points

TradingView computes classic pivot points:
- **Pivot (P)**: (High + Low + Close) / 3
- **R1, R2, R3**: Resistance levels above pivot
- **S1, S2, S3**: Support levels below pivot

## Interpreting Combined Signals

### Strong Consensus (High Confidence)
- Oscillators and MAs both STRONG_BUY/STRONG_SELL
- RSI, MACD, and Stochastic all agree
- Price above/below all major MAs

### Divergent Signals (Caution)
- Oscillators say SELL but MAs say BUY → potential top forming
- Oscillators say BUY but MAs say SELL → potential bottom but trend still down
- Short-term MAs bullish but long-term bearish → counter-trend bounce

### Timeframe Alignment
- Check multiple intervals (1D, 1W, 1M) for confirmation
- Weekly and monthly alignment with daily = highest confidence
- Daily signal against weekly trend = lower probability trade

## Available Intervals

| Code | Timeframe |
|---|---|
| 1m, 5m, 15m, 30m | Intraday |
| 1h, 2h, 4h | Short-term |
| 1d | Daily |
| 1W | Weekly |
| 1M | Monthly |

## Exchange Codes

| Exchange | Code | Notes |
|---|---|---|
| NASDAQ | NASDAQ | US tech-heavy |
| NYSE | NYSE | US large-cap |
| AMEX | AMEX | US small/mid-cap, ETFs |
| TSX | TSX | Canada |
| LSE | LSE | London |
| ASX | ASX | Australia |
| NSE | NSE | India |
| BSE | BSE | India |
| HKEX | HKEX | Hong Kong |
| TSE | TSE | Tokyo |
| BINANCE | BINANCE | Crypto |
| COINBASE | COINBASE | Crypto |

## Screener Scan Interpretation

### Oversold Scan
Stocks with RSI < 30 and/or Stochastic < 20. Potential bounce candidates but verify the trend isn't terminal (check 200 SMA, volume).

### Overbought Scan
Stocks with RSI > 70 and/or Stochastic > 80. May pull back but strong trends stay overbought for extended periods.

### Strong Buy / Strong Sell
Aggregate TradingView recommendation. Multiple indicators aligning creates higher-confidence signals.

### High Volume
Unusual volume activity. Cross-reference with price action: high volume + breakout = confirmation; high volume + reversal candle = potential top/bottom.

### Trending Up / Trending Down
Stocks with aligned moving averages and positive/negative momentum. Best used for trend-following strategies.
