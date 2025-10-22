Trading 212 Gap-Fill Bot
An automated trading bot for the Trading 212 platform that executes a "gap-down" strategy on the London Stock Exchange (LSE).

Overview
This bot identifies stocks in a predefined universe that have gapped down at the market open. It buys these stocks, aiming to profit from a potential "gap fill" (a bounce back towards the previous day's closing price). The strategy includes filters for gap size and the Relative Strength Index (RSI) to improve trade selection, and it uses stop-loss orders to manage risk.

⚠️ Disclaimer: This bot is for educational and research purposes only. Automated trading carries significant risk, including the potential for substantial financial loss. You are solely responsible for any trading decisions and financial outcomes. Do not run with real money until you have thoroughly tested and understand the risks.

Features
Automated Trading: Connects to the Trading 212 API to place orders automatically.
Gap-Down Strategy: Targets stocks that open lower than the previous day's close.
Market & Data Filters:
Configurable minimum and maximum gap percentage.
RSI filter to avoid overbought stocks.
Risk Management:
Calculates position size based on a percentage of total capital.
Places protective stop-loss orders on every trade.
Market Clock: Operates only during London Stock Exchange (LSE) trading hours.
End-of-Day Exit: Automatically closes any open positions at the end of the trading day.
Prerequisites
Python 3.8+
A Trading 212 Account (Demo or Live).
Your Trading 212 API Key and API Secret.
Installation
Clone the repository:

git clone <your-repo-url>
cd <your-repo-directory>
Install the required Python packages:

pip install requests yfinance pandas numpy python-dotenv pytz
Configure Environment Variables:
Create a file named .env in the same directory as your script and add your configuration. Use the .env.example below as a template.
.env.example


# Trading 212 API Credentials (REQUIRED)
T212_API_KEY=your_trading212_api_key
T212_API_SECRET=your_trading212_api_secret

# Optional: Use the demo environment. Default is the demo URL.
# T212_BASE_URL=https://demo.trading212.com/api/v0

# Account & Strategy Settings
ACCOUNT_CURRENCY=GBP
TICKERS=VUSA,VUAG,ISF,VUKE,VMID
TOTAL_BUDGET_GBP=2000
PER_TRADE_RISK_PCT=0.005

# Gap & RSI Filters
MIN_GAP_DOWN=-0.003
MAX_GAP_DOWN=-0.010
RSI_MAX=40

# Other Settings
SLIPPAGE_BP=5
TIMEZONE=Europe/London
LSE_OPEN_HHMM=08:00
LSE_CLOSE_HHMM=16:30
Running the Bot
Save the provided Python code as a file (e.g., bot.py) and run it from your terminal:

python bot.py
The bot will print its status to the console, wait for the market to open, and then begin its trading logic for the day.

How It Works
Wait for Market Open: The script checks the current time against the LSE trading hours defined in the .env file. It will sleep until the market opens.
Scan for Opportunities: After the open, it iterates through each ticker in your UNIVERSE.
Create a Plan: For each ticker, it fetches the previous day's close and the current intraday price. If the gap down and RSI conditions are met, it creates a Plan object with:
An entry price (the current price).
A target price (the previous close).
A stop-loss price.
A calculated quantity to buy based on your risk settings.
Execute Trade: If a valid plan is created, the bot places a market buy order.
Monitor & Exit:
It then watches the price. If the target price is hit, it places a market sell order to close the position for a profit.
It also places a stop-loss order to protect against downside risk.
End-of-Day Cleanup: At the end of the trading session, any remaining open positions are automatically closed. The bot then sleeps and waits for the next trading day.
