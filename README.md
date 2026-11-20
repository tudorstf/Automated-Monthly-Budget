# Receipt Budget Automator — Google Drive & Gemini Vision to Excel

Automatically extracts line items from receipt photos (`.jpg`, `.png`, `.webp`, `.heic`, `.heif`) and digital PDFs (`.pdf`) using **Gemini Vision**, logs everything into [budget_tracker.xlsx](budget_tracker.xlsx), and lets you manage, edit, and analyze your spending from a **Local Web App Dashboard** (`http://localhost:8765`) or via automatic **Google Drive** folder polling (which permanently deletes processed files from Drive after saving to Excel).

---

## Features

- **Local Web App Dashboard (`http://localhost:8765`)**:
  - **3-Step Setup & Connections Wizard**: Guided onboarding for your Gemini API key, 1-click Google Drive Sign-In, and folder/polling settings.
  - **1-Click Google Drive Sign-In (Built-In OAuth Client)**: Sign in directly from the browser with zero Google Cloud Console setup required, or optionally upload your own custom `credentials.json`.
  - **Drag-and-Drop Local Upload**: Drop receipt photos or PDFs directly in the dashboard to extract and log them immediately—no Google Drive required.
  - **Interactive Analytics & Two-Way Excel Editing**: Filter KPIs by month (`Total Spent`, `Month Spent`, `Discounts Saved`, `SGR Bottle Deposits`, `Receipts Logged` / `Needs Review`), inspect category/subcategory shares, monthly trends, and top stores, edit receipt metadata or individual line items inline (auto-recalculating parent totals and rebuilding the `Monthly Dashboard` sheet), delete receipts, and download `budget_tracker.xlsx` anytime.
  - **Live Watcher Controls & Activity Feed**: Start/pause background Google Drive polling, trigger **Sync Drive Now**, and monitor live timestamped events.
- **Romanian (`RON`) Receipt Parsing**: Expands abbreviated thermal receipt names into clean Romanian product names, folds inline loyalty/promo discounts (`Lidl Plus`, `Kaufland Card`, `Reducere`) into net item totals, and tracks **Garanție SGR** bottle deposits.
- **Two-Level Categories**: Every line item gets both a **Main Category** (`Groceries`, `Household`, `Personal Care`, `Dining & Drinks`, `Health & Pharmacy`, `Pets`, `Electronics & Home`, `Other`) and a **Subcategory** (`Dairy & Eggs`, `Meat & Fish`, `Produce (Fruits & Veg)`, `Bakery & Bread`, `Bottle Deposit (SGR)`, etc.).
- **Three-Tab Excel Workbook (`budget_tracker.xlsx`)**:
  - `Monthly Dashboard`: Top KPIs + month-over-month category/subcategory matrices + store summary.
  - `Receipts`: One row per trip with payment method, discounts saved, SGR deposits, total paid, and reconciliation status (`OK` vs `Needs Review`).
  - `Items`: Filterable table of every item with raw vs. clean names, categories, quantities, discounts, and net totals.
- **Safe Cloud Cleanup & Lock Protection**: Permanently deletes files from Google Drive **only after** `budget_tracker.xlsx` is atomically saved (archiving unreadable files to `failed_receipts/`). Detects `.~lock.budget_tracker.xlsx#` / `~$budget_tracker.xlsx` when open in LibreOffice or Excel, shows a warning banner in the UI, and returns HTTP `423` on writes until you close the spreadsheet.

---

## Quickstart: Local Web App Dashboard

### 1. Launch the App
```bash
chmod +x start_app.sh
./start_app.sh
```
`start_app.sh` creates `.venv` and installs `requirements.txt` automatically if needed, then starts the FastAPI server and opens **`http://localhost:8765`** in your browser.

You can also invoke `app.py` directly:
```bash
.venv/bin/python app.py --open-browser          # Default: 127.0.0.1:8765
.venv/bin/python app.py --host 0.0.0.0 --port 8765
.venv/bin/python app.py --no-watcher            # Start dashboard without auto-starting Drive polling
```

### 2. Complete the 3-Step Setup Wizard in Your Browser
On first launch, **Setup & Connections** opens automatically (and can be reopened anytime from the header):

1. **Step 1 — Free Gemini API Key**:
   - Click **Get a Free Gemini API Key from Google AI Studio ↗** (`https://aistudio.google.com/apikey`), create a key, paste it in, choose your Gemini Vision model (default: `gemini-3.8-flash`), and click **Verify & Save Gemini Key**.
   - Saved locally to `.env`.
2. **Step 2 — 1-Click Google Drive Permission** *(optional if you only upload local files)*:
   - Click **Connect Google Drive (1-Click Sign In)** to authorize access to your Drive `receipts` folder directly from the browser (`token.json` is stored locally with `0600` permissions).
   - **Google OAuth `credentials.json`**: Expand **Advanced: Use Custom Google OAuth credentials.json** in Step 2 to upload your Google Cloud Desktop OAuth client JSON (`installed` or `web` JSON, stored locally with `0600` permissions), or set `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in `.env`.
   - *Tip*: If your Google Cloud OAuth consent screen is in **Testing** mode, only listed Test Users can sign in and refresh tokens expire after 7 days; publish the OAuth app to **In Production** for long-lived tokens.
3. **Step 3 — Watched Folder & Automation Settings**:
   - Customize the Google Drive folder name (default `receipts`) and polling interval in seconds (default `20`, minimum `5`).

---

## Docker Usage

Build and run the web dashboard + Drive watcher in a container exposing port `8765`:

```bash
docker build -t receipt-budget-automator .
```

To persist `.env`, `token.json`, `budget_tracker.xlsx`, `data/`, `failed_receipts/`, and `logs/` across container restarts, mount the project directory (or individual persistence directories/files):

```bash
docker run -d \
  --name receipt-watcher \
  -p 8765:8765 \
  -v "$(pwd):/app" \
  receipt-budget-automator
```

Then open **`http://localhost:8765`** in your browser.

*(Alternatively, pass `--env-file .env` and mount `-v "$(pwd)/data:/app/data" -v "$(pwd)/logs:/app/logs" -v "$(pwd)/failed_receipts:/app/failed_receipts"` if you do not want to bind-mount the full repository.)*

---

## Background `systemd --user` Service (Linux)

The user service (`systemd/receipt-watcher.service`) runs `app.py` on `http://127.0.0.1:8765`, keeping both the background Google Drive watcher and the local Web Dashboard available automatically on login:

```bash
chmod +x scripts/install_service.sh
./scripts/install_service.sh
```

Useful service commands:
- Check status: `systemctl --user status receipt-watcher.service`
- Follow live logs: `journalctl --user -u receipt-watcher.service -f`
- Restart after updates: `systemctl --user restart receipt-watcher.service`

---

## CLI Mode (Headless / One-Off)

The original CLI entrypoint (`main.py`) remains fully supported:
- Interactive terminal setup: `.venv/bin/python main.py --setup`
- Process pending Google Drive receipts once: `.venv/bin/python main.py --once`
- Watch Google Drive in the foreground: `.venv/bin/python main.py --watch`
- Process a single local receipt photo or PDF: `.venv/bin/python main.py --local path/to/receipt.jpg`

---

## Running Tests

Run the full automated test suite (Excel writer, config helpers, reconciliation models, Drive/Gemini pipeline, and FastAPI web endpoints):

```bash
.venv/bin/python -m pytest -v
```
