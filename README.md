# SmartPrint AI: AI-Based Printer Monitoring & Tracking System

An end-to-end smart IoT printer telemetry and predictive maintenance web application built with **Flask**, **Flask-SocketIO**, **Scikit-Learn Machine Learning**, and **SNMP/Simulation Data Acquisition**.

---

## Features
- **User Authentication & Session Management**: Registration and login stored in **MongoDB** (`smartprint.users`) with Werkzeug password hashing, plus an automatic operator **activity log** (`smartprint.activity_log`).
- **Real-Time Telemetry Streaming**: WebSockets via Flask-SocketIO broadcast live metrics (toner levels, paper tray, fuser temperature, drum wear, printed page counts) without manual browser refreshes.
- **AI Predictive Maintenance Engine**:
  - Trained Machine Learning Classifier (Random Forest) predicting imminent hardware failure risk and anomaly detection.
  - Toner depletion forecasting (estimated remaining days and pages).
  - Component wear tracking (fuser heating thresholds, drum lifecycle, feed roller health).
  - Actionable smart recommendations (cartridge ordering, jam prevention, thermal cycle warnings).
- **Physical Dynamics Simulation & Real SNMP Support**:
  - Queries physical network printers over SNMP (Port 161 standard Printer MIB OIDs), with an automatic IPP fallback over TCP port 631 when SNMP is unavailable.
  - Produces a plain-language **diagnostic report** per printer: findings sorted by severity with "what to do" instructions, per-colour supply levels, paper trays, cover states, and an explicit "not reported by this printer" list - missing readings are shown as unknown, never fabricated.
  - High-fidelity physical simulation engine modeling realistic page throughput, consumable consumption, thermal heat curves, and stochastic jam triggers.
- **Interactive Modern UI / UX**:
  - Dark-mode glassmorphic dashboard with **one card per printer**, a large plain-language issue banner (No Paper / No Ink / Paper Jam / Overheating / No Issues) and a single **"Fix It"** button that resolves the reported problem.
  - Interactive maintenance controls (Test Print, Refill Paper & Ink, Clear Jam, Cool Down) plus a live connection indicator for the telemetry stream.
  - Real-printer cards show the scan findings and a **Diagnose** panel with the full SNMP report instead of simulated controls; printers that have not been scanned yet show an honest "Status Not Available" state.
  - Real-time Chart.js graphs displaying fleet health and toner consumption trends.
  - Maintenance alert resolutions and recent print job audit logs.

---

## Architecture
```
[Physical Network Printer (SNMP, IPP fallback)] / [Simulation Engine]
                  │
                  ▼
        [snmp_scanner.py Scan Engine]
                  │
                  ▼
         [monitor.py Poller]
                  │
                  ▼
      [ai_engine.py ML Model] ───> [SQLite Database]
                  │
                  ▼
           [Flask-SocketIO]
                  │ (WebSocket Broadcast)
                  ▼
       [Modern Frontend Dashboard]
```

Storage split:

```
  SQLite  (printer_system.db) -> printers, print_jobs, telemetry_history, alerts
  MongoDB (smartprint)        -> users (accounts), activity_log (operator audit trail)
```

---

## Data Storage: MongoDB + SQLite

The application deliberately uses two stores, each for what it does best:

| Store | Location | Collections / Tables | Purpose |
| --- | --- | --- | --- |
| **MongoDB** | `mongodb://localhost:27018/smartprint` | `users`, `activity_log` | Accounts (identity) and the operator audit trail |
| **SQLite** | `printer_system.db` | `printers`, `print_jobs`, `telemetry_history`, `alerts` | All printer telemetry - untouched by the MongoDB layer |

### MongoDB collections

`users` - one document per account (passwords are **always** Werkzeug hashes):

```json
{
  "_id": ObjectId("..."), "user_id": "...", "username": "admin",
  "email": "admin@printer.local", "hashed_password": "scrypt:32768:8:1$...",
  "role": "admin", "created_at": ISODate("...")
}
```

`activity_log` - one document per operator action:

```json
{ "username": "admin", "action": "Cleared jam on PRN-003",
  "printer_id": "PRN-003", "timestamp": ISODate("...") }
```

`printer_id` is stored as `null` for actions that are not printer specific
(login, registration). Logged actions: login, registration, refill paper,
clear jam, cool fuser, add printer, delete printer, resolve alert and test
print job.

### Automatic setup

On startup `mongo_db.init_mongo()` creates both collections with their indexes
(unique `username` + `email`, descending `timestamp`, `printer_id`) and seeds the
default `admin / admin123` account **only when the `users` collection is empty**.

### Connection settings (optional)

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MONGO_URI` | `mongodb://localhost:27018/smartprint` | Server + database |
| `MONGO_DB_NAME` | `smartprint` | Database name (must match the URI) |
| `MONGO_TIMEOUT_MS` | `2500` | Server-selection / connect timeout |
| `MONGO_RETRY_SECONDS` | `30` | Throttle for automatic reconnect attempts |

### Graceful degradation

If MongoDB is unreachable the app **still starts and the printer dashboard keeps
working**: a clear error is printed to the console, the dashboard, telemetry and
all Fix It actions continue normally, and activity logging is skipped with a
warning. Login and registration temporarily fall back to the legacy SQLite
`users` table so the system is never a dead end. The module retries the
connection every `MONGO_RETRY_SECONDS` seconds and switches back to MongoDB as
soon as it is available again.

### Verifying the data from the shell

```bash
mongosh "mongodb://localhost:27018/smartprint"
```
```javascript
db.users.find()                                          // accounts
db.activity_log.find().sort({ timestamp: -1 }).limit(10) // newest actions first
db.users.countDocuments({})
```

---

## Default Credentials
- **Username:** `admin`
- **Password:** `admin123`

---

## Demo Fleet (seeded automatically)

On startup the database is seeded (or restored) with five demonstration printers, so the
dashboard always shows exactly one obvious, plain-language problem per card:

| Printer | Device | Reported state | One-click fix |
| --- | --- | --- | --- |
| `PRN-001` | HP LaserJet Enterprise M608 | **No Paper** - paper tray empty | Fix It: Refill Paper |
| `PRN-002` | Canon imageRUNNER ADVANCE C3530 | **No Ink** - black toner empty | Fix It: Replace Ink |
| `PRN-003` | Epson WorkForce Pro WF-C879R | **Paper Jam** - jam latched | Fix It: Clear Jam |
| `PRN-004` | Brother MFC-L8900CDW (Floor 3) | **Overheating** - fuser above 210 °C | Fix It: Cool Down |
| `PRN-005` | Xerox VersaLink C405 (Reception) | **No Issues** | - |

Printers you register yourself are never modified. The five demo devices are reset to
these values on every server start, so the demonstration is always repeatable.

---

## Registering a Real Network Printer (SNMP)

1. **Enable SNMP on the printer** - most devices ship with it off. In the printer's
   web admin page (usually under *Network* or *Security* settings) enable **SNMP v1/v2c**
   and note the configured **community string** (commonly `public`). Put the printer on
   the same network as this server and note its IP.
2. **Add it in the dashboard** - open *Add Printer*, switch the mode to
   **SNMP Network Polling**, and fill in the IP address, community string and port
   (default 161). Press **Test Connection** to scan the printer once and preview its
   model, page count and findings before saving.
3. **Watch the card** - real printers are scanned in the background roughly every
   12 seconds (offline printers never stall the dashboard). Each card shows the most
   important finding and what to do about it; the **Diagnose** button opens the full
   report (supplies per colour, trays, covers, "not reported" metrics) and lets you
   re-scan on demand. For real printers the simulated *Fix It / Test Print / Refill*
   buttons are replaced by the report, because those actions cannot be performed
   remotely - the report tells the operator what to do at the physical device.

### What real scans can and cannot report

| Reported over standard SNMP | Not available |
| --- | --- |
| Model, system name, status, total page count | Fuser temperature |
| Error bits (jam, door open, service requested, ...) | Drum wear |
| Per-colour toner/ink supply levels | Feed-roller health |
| Paper tray levels, cover states, alert table | - |

Values the printer does not report are shown as `n/a` / *"not reported"*, the AI
health score is computed only from reported data, and the Random Forest failure
probability is skipped (shown as `-` with a "model excluded" note) instead of being
fed placeholder numbers. No vendor-specific OIDs are queried.

### Command-line scan

```bash
python scan_printer_cli.py 192.168.1.50 --community public --port 161
```

Prints the same diagnostic report as JSON. Exit code `0` means the printer
answered, `1` means it did not, `2` means the address was rejected (only private /
link-local IPv4 targets are allowed).

---

## REST API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/printers` | List printers with their stored metrics |
| `POST` | `/api/printers` | Register a new printer (simulation or SNMP mode) |
| `DELETE` | `/api/printers/<id>` | Remove a printer and its telemetry/jobs/alerts |
| `POST` | `/api/printers/test-connection` | Scan `{ip, community, port}` once and return the diagnostic report (used by the Add Printer dialog) |
| `POST` | `/api/printers/<id>/diagnose` | Force an immediate SNMP/IPP scan of one printer and apply the result |
| `GET` | `/api/printers/<id>/report` | The stored diagnostic report of one printer (last scan time, findings, supplies, trays, covers, troubleshooting) |
| `POST` | `/api/printers/<id>/job` | Send a simulated print job (simulation mode only) |
| `POST` | `/api/printers/<id>/replenish` | Refill paper and black toner (simulation mode only) |
| `POST` | `/api/printers/<id>/clear-jam` | Clear a latched paper jam (simulation mode only) |
| `POST` | `/api/printers/<id>/cool-fuser` | Cool the fuser back to 180 °C (simulation mode only) |
| `POST` | `/api/telemetry/poll` | Force an immediate telemetry poll |
| `GET` | `/api/jobs` | Recent print job log |
| `GET` | `/api/alerts` | Unresolved maintenance alerts |
| `POST` | `/api/alerts/<id>/resolve` | Resolve a single maintenance alert |
| `GET` | `/api/activity` | Recent MongoDB `activity_log` entries (newest first) |
| `GET` | `/api/mongo/status` | MongoDB connection state + user/activity counts |

All endpoints require a logged-in session. The `test-connection`, `diagnose` and `report`
endpoints validate the target IP and accept only private, link-local or loopback
IPv4 addresses (10.x, 172.16-31.x, 192.168.x, 169.254.x) - the server can never be
used to probe arbitrary hosts, and SNMP community strings are never logged or echoed back.

---

## Quick Start Guide

### 0. Start MongoDB (accounts + activity log)
The system expects **`mongodb://localhost:27018/smartprint`**. If the server is not
already running:

```bash
mongod --port 27018 --dbpath <your-data-folder>
```

Point the app at a different server with the `MONGO_URI` environment variable:

```powershell
set MONGO_URI=mongodb://host:port/smartprint
```

MongoDB is optional at runtime: if it cannot be reached the app still starts, the
printer dashboard keeps working and a clear error is printed (see
[Graceful degradation](#graceful-degradation)).

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```
This installs Flask, Flask-SocketIO, Scikit-Learn, `pysnmp` and the **`pymongo`**
MongoDB driver.

### 2. Start the Server
```bash
python app.py
```

### 3. Open the Dashboard
Navigate to `http://127.0.0.1:5000` in your web browser.
