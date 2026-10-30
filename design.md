# System Design Document: AI-Based Smart Printer Monitoring and Tracking System

## 1. Project Overview
The **AI-Based Smart Printer Monitoring and Tracking System** is a real-time IoT and software application designed to monitor printer health, track usage patterns, predict hardware or consumable failures using AI, and provide a centralized web dashboard for administrators.

---

## 2. System Architecture
The application follows a lightweight **Client-Server Architecture** with real-time bidirectional communication using WebSockets.

```
[Printer / Network Agent]
│ (SNMP / Local Polling)
▼
[Python Flask Backend] ──> [AI Predictive Engine] ──> [Database / In-Memory State]
│
├─ (WebSockets / Socket.IO)
▼
[Web Dashboard (Frontend UI)]
```

Persistent stores:
```
  SQLite  (printer_system.db) -> printers, print_jobs, telemetry_history, alerts
  MongoDB (smartprint)        -> users (accounts), activity_log (operator audit trail)
```

---

## 3. Tech Stack
* **Backend Framework:** Python, Flask, Flask-SocketIO
* **Network Protocol:** PySNMP (for querying real network printers) or Simulation Logic
* **AI & Analytics:** Custom heuristic rules & Scikit-learn (for predictive maintenance and anomaly detection)
* **Authentication & Storage:** Session-based auth with Werkzeug password hashing. **MongoDB** (`mongodb://localhost:27018/smartprint`) holds the `users` accounts and the `activity_log` audit trail, while **SQLite** (`printer_system.db`) holds all printer telemetry.
* **Frontend Dashboard:** HTML5, CSS3, JavaScript, Socket.IO Client, Chart.js, Lucide/FontAwesome icons
* **Communication:** WebSockets for real-time updates without page reloads

---

## 4. Core Modules & Components

### A. Authentication, User Management & Audit Trail (`mongo_db.py` + `database.py`)
* Secure login, logout and registration with hashed passwords (Werkzeug security), persisted in the MongoDB `users` collection; the default `admin / admin123` account is seeded automatically while `users` is empty.
* Role-based access (admin and operator roles) and session security.
* MongoDB `activity_log` collection recording every operator action (login, registration, refill paper, clear jam, cool fuser, add printer, delete printer, resolve alert, test print job) with username, action, printer id and timestamp.
* SQLite schema persisting printers, print jobs, metrics history and predictive maintenance alerts (telemetry layer, unchanged).
* Fail-safe design: when MongoDB is unreachable the app prints a clear error, keeps the dashboard fully operational, and temporarily falls back to the legacy SQLite `users` table for login and registration.

### B. Data Acquisition Layer (`snmp_scanner.py` + `monitor.py`)
* **Real Network Mode (`snmp_scanner.py`):** Connects to physical network printers over SNMP v1/v2c (`UDP 161`, standard Printer MIB OIDs `1.3.6.1.2.1.43...` + Host Resources `1.3.6.1.2.1.25.3.5...`) using the pysnmp asyncio API. Returns a structured report: model, system name, total pages, printer status, findings (severity-sorted, plain-language, each with a "what to do" instruction), per-colour supply levels, paper trays, cover states and raw alert descriptions. If SNMP gets no response, a hand-built IPP `Get-Printer-Attributes` request over `TCP 631` is attempted and the report's `method` marks which protocol answered.
  * **Honesty first:** error bits come only from `hrPrinterDetectedErrorState`, over-temperature is only reported when the printer's alert table says so, and anything the device does not report (e.g. fuser temperature, drum wear - not available over standard SNMP) is listed under "not reported" instead of being guessed. No vendor-specific OIDs are queried.
  * **Target validation:** only private / link-local / loopback IPv4 addresses (10.x, 172.16-31.x, 192.168.x, 169.254.x) are accepted for scanning; community strings are kept out of telemetry broadcasts and are never logged or echoed.
* **Polling (`monitor.py`):** Simulation printers advance on the 3.5 s dashboard tick as before. SNMP printers are scanned in a background thread pool on a slower interval (~12 s), so an offline printer's timeouts never stall the loop; the first tick before any scan completes shows an explicit unknown state. Findings are fed into the maintenance alert table (de-duplicated by printer + finding code while unresolved).
* **Simulation Mode (Fallback):** Generates realistic printer telemetry streams (pages printed, toner depletion, fuser temperature, drum wear, paper levels, paper jams) for testing without physical hardware constraints. Supports multi-printer fleets.

### C. AI & Predictive Maintenance Engine (`ai_engine.py`)
* **Simulation printers:** analyzed against thresholds and trained machine learning models:
  * **Toner Depletion Forecasting:** Linear regression / rate estimation predicting exact days and pages until toner exhaustion.
  * **Wear & Tear Tracking:** Evaluates fuser wear, roller friction, drum life, and page count intervals to forecast replacement schedules.
  * **Anomaly & Failure Risk Detection:** Scikit-learn Random Forest / Isolation Forest classifier computing overall Failure Probability (0-100%) and categorizing risk (Healthy, Moderate, Critical).
  * **Smart Actionable Recommendations:** Generates contextual alerts (e.g., "Order Toner Cartridge HP 85A", "Schedule Drum Replacement within 1,200 pages").
* **Real (SNMP/IPP) printers:** the health score is built only from what the scan reported (findings by severity plus reported toner/paper levels); the recommendations are the scan findings' plain message + what-to-do steps. Missing metrics are excluded and listed in `excluded_metrics`; because real printers never report fuser temperature or drum wear, the Random Forest input is incomplete, `failure_probability` is `None`, and the dashboard shows "model excluded" instead of a fabricated number.

### D. Real-Time Backend Service (`app.py`)
* Runs a background thread to continuously poll printer telemetry at configurable intervals (e.g., every 3 to 5 seconds for simulations, ~12 s for SNMP scans).
* Emits live updates instantly to all authenticated connected clients using **Flask-SocketIO**.
* REST APIs for printer CRUD operations, manual telemetry trigger, print job simulation, logs, and report export.
* Scan-related endpoints (login required): `POST /api/printers/test-connection` (one-off scan of `{ip, community, port}` for the Add Printer dialog), `POST /api/printers/<id>/diagnose` (immediate on-demand scan) and `GET /api/printers/<id>/report` (stored diagnostic report). All of them validate the target IP (private/link-local IPv4 only). The simulated repair endpoints (`/job`, `/replenish`, `/clear-jam`, `/cool-fuser`) return `400` for real printers, since those actions must be performed at the physical device.

### E. Frontend Web Dashboard (`templates/` & `static/`)
* **Login & Register Views (`login.html`, `register.html`):** Clean, modern, responsive glassmorphic interfaces.
* **Main Dashboard (`index.html`):**
  * Real-time KPI cards (Total Printers, Online Status, Active Print Jobs, Critical Alerts).
  * Live interactive Printer Cards with status pills (Idle, Printing, Jammed, Warning, Offline), toner gauge meters, paper trays, drum health, and temperature indicators.
  * Real-printer cards render the scan's most important finding with its what-to-do text, a findings-count badge, and a **Diagnose** button opening the full report modal (method, last scan time, findings, supplies per colour, trays, covers, "not reported" list, troubleshooting). Metrics the printer did not report are shown as `n/a`; a printer without a scan yet shows "Status Not Available" instead of invented values.
  * Add Printer dialog with SNMP community/port fields and a **Test Connection** preview before saving.
  * AI Predictions & Health Insights Panel with failure probability gauge and proactive maintenance checklist.
  * Real-time Analytics Charts (Toner consumption trends, print volume over time).
  * Manual Actions: Add Printer, Send Test Print Job, Clear Jam, Trigger Diagnostics.
* **Auto-Updating UI:** Instantly reflects changes pushed via WebSockets without manual browser refreshes.

---

## 5. Data Flow
1. **Polling:** Background worker queries physical printers via SNMP or the simulation engine.
2. **AI Processing:** Raw metrics feed into the AI predictive engine to calculate health score, days-to-deplete, and failure probability.
3. **Database Recording:** Metric snapshots and alerts are logged to SQLite for historical reporting.
4. **Broadcasting:** Live telemetry packet emitted over WebSocket (`socketio.emit('printer_update', data)`).
5. **Rendering:** Browser dashboard updates gauges, status badges, alert list, and charts in real time.
6. **Auditing:** Identity changes and operator actions (login, registration, refill paper, clear jam, cool fuser, add/delete printer, resolve alert, test print job) are written to the MongoDB `activity_log` as `{ username, action, printer_id, timestamp }`.

---

## 6. Execution & Deployment Plan
1. Install dependencies (MongoDB driver included):
   ```bash
   pip install flask flask-socketio simple-websocket scikit-learn pysnmp pymongo
   ```
2. Ensure MongoDB is running on port `27018` - the default URI is
   `mongodb://localhost:27018/smartprint` and can be overridden with `MONGO_URI`:
   ```bash
   mongod --port 27018 --dbpath <your-data-folder>
   ```
3. Run the application server:
   ```bash
   python app.py
   ```
4. Open browser at `http://127.0.0.1:5000` and log in with default credentials (`admin` / `admin123`) or register a new account.
