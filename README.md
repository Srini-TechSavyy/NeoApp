# Scalper & Monitor Pro

A professional trading application for scalping and real-time PnL monitoring, built for the Neo API platform.

## Features

### 🎯 Scalper
- **Quick Order Execution**: Fast BUY/EXIT buttons for scalping strategies
- **Strike Price Controls**: Easy up/down arrows to adjust strike prices
- **CE/PE Toggle**: Quick switch between Call and Put options
- **Lot Size Selection**: Predefined lot sizes (1, 2, 3, 5, 10)
- **Symbol History**: Remembers your last 3 traded symbols
- **Auto-Login**: Automatic authentication on startup
- **Scrip Master Integration**: Automatic token lookup for trading symbols

### 📊 PnL Monitor
- **Real-time Trade Tracking**: Live monitoring of completed trades
- **Comprehensive Statistics**:
  - Total trades count
  - Win/Loss ratio and win rate percentage
  - Gross PnL, Net PnL, and total charges
  - Last 5 trades display
- **Color-coded Display**: Green for profits, red for losses
- **Auto-refresh**: Updates every 5 seconds
- **CSV Export**: Automatically saves trades to daily CSV files in `logs/` directory

## Installation

### Prerequisites
- Python 3.11+
- Conda/Miniforge environment manager

### Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/SriniArch/NeoApp.git
   cd NeoApp2
   ```

2. **Create and activate the environment**
   ```bash
   conda create -n neo python=3.11
   conda activate neo
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   
   Create a `.env` file in the project root with your Neo API credentials:
   ```env
   CONSUMER_KEY=your_consumer_key
   MOBILE=your_mobile_number
   UCC=your_ucc
   MPIN=your_mpin
   TOTP_SECRET=your_totp_secret
   NIFTY_LOT_SIZE=75
   ```

5. **Add scrip master CSV files**
   
   Place your scrip master files in the project root:
   - `nse_fo.csv` - NSE Futures & Options scrip master
   - `bse_fo.csv` - BSE Futures & Options scrip master

## Usage

### Running the Application

```bash
python run.py
```

### Web Mode (Initial Implementation)

An initial web stack is now available with FastAPI + worker:

- API: [web/backend/main.py](web/backend/main.py)
- Worker: [web/worker/main.py](web/worker/main.py)
- Shared snapshot/state helpers: [web/shared/monitor_snapshot.py](web/shared/monitor_snapshot.py), [web/shared/state_store.py](web/shared/state_store.py)

Install web dependencies:

```bash
pip install -r requirements-web.txt
```

Run worker (builds monitor snapshot from broker orders):

```bash
python -m web.worker.main
```

Run API server:

```bash
uvicorn web.backend.main:app --host 0.0.0.0 --port 8000
```

Key endpoints:

- `GET /health`
- `GET /api/system/status`
- `GET /api/monitor/snapshot`
- `POST /api/trade/action` (`BUY` / `EXIT`)
- `POST /api/symbol/suggest` (ATM/offset symbol suggestion)
- `WS /ws/monitor`

Recommended `.env` flags for web mode:

```env
WEB_API_TOKEN=change_this_to_a_long_random_token
TRADING_ENABLED=false
WEB_POLL_SECONDS=5
WORKER_HEALTH_WINDOW_SECONDS=30
WEB_LOG_LEVEL=INFO

# Optional automation flags
AUTO_BUY_ENABLED=false
AUTO_SELL_ENABLED=false
AUTO_BASE_INDEX=NIFTY
AUTO_STRIKE_OFFSET=0
AUTO_LOTS=1
AUTO_TARGET=2
AUTO_SL=1.5
AUTO_TSL_STEP=1
AUTO_PT_STEP=1
```

Notes:

- `WEB_API_TOKEN` is enforced by default.
- `TRADING_ENABLED=false` blocks live order placement via web API.
- Set `WEB_ALLOW_LOCAL_NOAUTH=true` only for local debugging.

Web mode currently includes:

- Position-safe order execution checks (single open derivative position guard)
- Order completion verification via order history/report
- Risk state in snapshot (progressive lockouts + cool-off status)
- Rich monitor payload (`indicator`, `position`, `risk`, `automation`)
- Trade journal append (`logs/trades_web_YYYY-MM-DD.csv`)
- Strike helper (base + LTP + CE/PE + offset)

### GitHub Actions Deployment (Azure VM)

Deployment workflow:

- [.github/workflows/deploy-azure-vm.yml](.github/workflows/deploy-azure-vm.yml)

Full runbook: [deploy/AZURE_DEPLOY.md](deploy/AZURE_DEPLOY.md)

This workflow runs on a **self-hosted GitHub Actions runner** installed on the Azure VM (labels: `self-hosted`, `linux`, `x64`, `neoapp2`). It:

1. Creates a release bundle under `/opt/neoapp2/releases/`
2. Symlinks persistent `shared/logs` into each release
3. Installs Python dependencies into `/opt/neoapp2/venv`
4. Switches the `/opt/neoapp2/current` symlink
5. Restarts `neo-worker.service` and `neo-fastapi.service`
6. Calls `/health` (rolls back to the previous release on failure)

**VM setup:** run [scripts/azure/setup_vm.sh](scripts/azure/setup_vm.sh), configure `/etc/neoapp2/neoapp.env` from [deploy/env.example](deploy/env.example), register the self-hosted runner, and grant passwordless `systemctl`/`nginx` sudo for the runner user (see runbook).

**Defaults:** `TRADING_ENABLED=false`, `WEB_ALLOW_LOCAL_NOAUTH=false`. SSH-based deploy secrets (`AZURE_VM_*`) are not used by the current workflow; they are only needed if you add a separate SSH deploy job later.

The application will:
1. Auto-login using your credentials
2. Load the scrip master data
3. Display the Scalper & Monitor interface

### Trading Workflow

1. **Select Symbol**: Enter or select a trading symbol (e.g., `SENSEX2610184900PE`)
2. **Adjust Strike**: Use ▲/▼ buttons to change strike price
3. **Choose Option Type**: Toggle between CE (Call) and PE (Put)
4. **Set Lot Size**: Select from dropdown (1, 2, 3, 5, 10)
5. **Execute Trade**: 
   - Click **BUY** to enter position
   - Click **EXIT** to close position
6. **Monitor PnL**: Watch real-time statistics in the right panel

### CSV Export

Completed trades are automatically saved to:
```
logs/trades_YYYY-MM-DD.csv
```

CSV includes:
- Symbol, Date, Buy/Sell Times
- Quantity, Buy/Sell Prices
- Gross PnL, Charges, Net PnL

## Project Structure

```
NeoApp2/
├── run.py                  # Main entry point
├── scalper/
│   ├── neoscalper.py      # Main UI and trading logic
│   └── ltp.py             # Live price fetching
├── monitor/
│   ├── neomonitor.py      # Standalone monitor (legacy)
│   └── pnl_engine.py      # PnL calculation engine
├── bot/
│   └── neobot.py          # Automated trading bot
├── common/
│   ├── config.py          # Configuration management
│   ├── neo_login.py       # Authentication
│   ├── orders.py          # Order execution
│   ├── scrip_master.py    # Symbol lookup
│   └── utils.py           # Utility functions
├── assets/
│   └── scalper2.png       # App icon
├── logs/                  # Log files and trade CSVs
└── .env                   # Environment variables (not in repo)
```

## Configuration

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `CONSUMER_KEY` | Neo API consumer key | Yes |
| `MOBILE` | Registered mobile number | Yes |
| `UCC` | Unique Client Code | Yes |
| `MPIN` | Mobile PIN | Yes |
| `TOTP_SECRET` | TOTP secret for 2FA | Yes |
| `NIFTY_LOT_SIZE` | Default lot size | No (default: 75) |
| `NSE_SCRIP_MASTER_PATH` | Path to NSE CSV | No (default: nse_fo.csv) |
| `BSE_SCRIP_MASTER_PATH` | Path to BSE CSV | No (default: bse_fo.csv) |

## Security

⚠️ **Important**: Never commit your `.env` file or credentials to version control.

The `.gitignore` file is configured to exclude:
- `.env` and `.envrc`
- `*.csv` files (scrip masters)
- Log files
- Sensitive data

## Troubleshooting

### "Login validation completed but access_token is MISSING!"
This is a debug message and can be safely ignored if the Monitor displays data correctly. The Neo API client may store the token internally.

### Monitor shows no data
- Ensure you have completed at least one trade
- Check that your API credentials are valid
- Verify network connectivity

### Symbol not found
- Ensure scrip master CSV files are present and up-to-date
- Check that the trading symbol format matches the scrip master

## License

This project is for personal use only.

## Disclaimer

This software is provided as-is for educational and personal trading purposes. Use at your own risk. The authors are not responsible for any financial losses incurred through the use of this software.

---

**Happy Trading! 📈**
++
