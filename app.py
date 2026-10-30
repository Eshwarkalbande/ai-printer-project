"""
Flask Real-Time Application Server for AI-Based Printer Monitoring System.
Handles Authentication, WebSockets (Flask-SocketIO), Telemetry Polling,
Predictive Insights, and Admin REST APIs.
"""

import os
import ipaddress
import json
import threading
import time
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit

import database
import mongo_db
from monitor import (init_monitor, poll_all_printers, trigger_print_job,
                     replenish_printer, clear_printer_jam, cool_printer_fuser,
                     poll_snmp_printer, diagnose_printer)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-only-change-me')
app.config['MONGO_URI'] = mongo_db.MONGO_URI
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

database.init_db()
init_monitor()

# MongoDB owns identity (users) and the audit trail (activity_log); printer
# telemetry stays in SQLite. A MongoDB outage must never stop the dashboard,
# so the outcome is only reported here and the application always continues.
mongo_ready, mongo_message = mongo_db.init_mongo()
if not mongo_ready:
    print(f"[Startup] MongoDB unavailable ({mongo_message}).")
    print("[Startup] Login/registration falls back to the local SQLite users "
          "table and activity logging is skipped.")

_worker_running = True


def telemetry_background_worker():
    """Continuously poll printer telemetry and broadcast over WebSocket."""
    global _worker_running
    while _worker_running:
        try:
            telemetry_data = poll_all_printers()
            socketio.emit('printer_update', {'printers': telemetry_data})
        except Exception as e:
            print(f"[Telemetry Worker Error]: {e}")
        time.sleep(3.5)


worker_thread = threading.Thread(target=telemetry_background_worker, daemon=True)
worker_thread.start()

# ---------------------- MONGODB ACTIVITY HELPERS ----------------------

def current_username():
    """Username of the signed-in operator ('system' when anonymous)."""
    return (session.get('user') or {}).get('username') or 'system'


def record_activity(action, printer_id=None, username=None):
    """
    Append one plain-language entry to MongoDB's `activity_log`.

    Called only after an action has actually succeeded. Never raises: when
    MongoDB is down a warning is printed and the request continues normally.
    """
    try:
        return mongo_db.log_activity(username or current_username(), action, printer_id)
    except Exception as exc:
        print(f"[activity_log] skipped '{action}': {exc}")
        return False


# ---------------------- AUTHENTICATION ROUTES ----------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        # Primary identity store: the MongoDB `users` collection.
        user = mongo_db.authenticate_user(username, password)
        auth_backend = 'mongodb'
        if user is None and not mongo_db.is_available():
            # MongoDB is unreachable: fall back to the legacy SQLite users
            # table so operators can still sign in.
            print("[MongoDB] login fallback: using SQLite users table")
            auth_backend = 'sqlite-fallback'
            user = database.authenticate_user(username, password)
        if user:
            session['user'] = {
                'id': user['id'],
                'username': user['username'],
                'email': user['email'],
                'role': user['role']
            }
            record_activity('Logged in')
            flash('Signed in successfully!', 'success')
            if auth_backend == 'sqlite-fallback':
                flash('MongoDB is offline - signed in through the local fallback '
                      'account store, so this action was not logged.', 'warning')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username/email or password', 'error')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()

        if not username or not email or not password:
            flash('All fields are required.', 'error')
            return render_template('register.html')

        # Primary identity store: the MongoDB `users` collection.
        success, msg = mongo_db.create_user(username, email, password)
        if not success and not mongo_db.is_available():
            # MongoDB is unreachable: keep registration usable via the legacy
            # SQLite users table so the app is never a dead end.
            print("[MongoDB] registration fallback: using SQLite users table")
            fallback_ok, fallback_msg = database.register_user(username, email, password)
            if fallback_ok:
                record_activity('Registered a new account', username=username)
                flash('MongoDB is offline - the account was created in the local '
                      'fallback store only.', 'warning')
                return redirect(url_for('login'))
            msg = fallback_msg
        if success:
            # The new account is not signed in yet, so log it explicitly.
            record_activity('Registered a new account', username=username)
            flash('Account created successfully! Please sign in.', 'success')
            return redirect(url_for('login'))
        else:
            flash(f'Registration error: {msg}', 'error')

    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# ---------------------- DASHBOARD ROUTE ----------------------

@app.route('/')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))
    printers = database.get_all_printers()
    return render_template('index.html', printers=printers)



# ---------------------- REST API ENDPOINTS ----------------------

def validate_probe_target(ip):
    """Only private/link-local/loopback IPv4 targets may ever be probed.

    Returns (ok, error_message). This server must not be usable to probe
    arbitrary hosts, so everything outside those ranges is rejected."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False, 'Not a valid IP address'
    if addr.version != 4:
        return False, 'Only IPv4 addresses are supported'
    if not (addr.is_private or addr.is_link_local or addr.is_loopback):
        return False, 'Only private or link-local IP addresses are allowed'
    return True, None


@app.route('/api/printers', methods=['GET'])
def api_get_printers():
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    printers = database.get_all_printers()
    return jsonify(printers)


@app.route('/api/printers', methods=['POST'])
def api_add_printer():
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    p_id = data.get('id')
    name = data.get('name')
    model = data.get('model')
    ip = (data.get('ip_address') or '127.0.0.1').strip()
    mode = data.get('mode', 'simulation')
    community = data.get('snmp_community') or 'public'
    port = data.get('snmp_port') or 161

    if not p_id or not name or not model:
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

    if mode == 'snmp':
        ok, msg = validate_probe_target(ip)
        if not ok:
            return jsonify({'status': 'error', 'message': msg}), 400
        try:
            port = int(port)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': 'SNMP port must be a number'}), 400
        if not 1 <= port <= 65535:
            return jsonify({'status': 'error', 'message': 'SNMP port must be between 1 and 65535'}), 400

    try:
        database.add_printer(p_id, name, model, ip, mode, community, port)
        init_monitor()
        telemetry = poll_all_printers()
        socketio.emit('printer_update', {'printers': telemetry})
        record_activity(f'Added printer {p_id}', p_id)
        return jsonify({'status': 'success', 'message': 'Printer registered successfully'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/printers/test-connection', methods=['POST'])
def api_test_connection():
    """Probe an IP before registering it. Community/port are never echoed."""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    ip = (data.get('ip') or '').strip()
    community = data.get('community') or 'public'
    port = data.get('port') or 161

    ok, msg = validate_probe_target(ip)
    if not ok:
        return jsonify({'status': 'error', 'message': msg}), 400
    try:
        port = int(port)
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Port must be a number'}), 400
    if not 1 <= port <= 65535:
        return jsonify({'status': 'error', 'message': 'Port must be between 1 and 65535'}), 400

    report = poll_snmp_printer(ip, community, port)
    return jsonify({'status': 'success', 'report': report})


@app.route('/api/printers/<printer_id>/diagnose', methods=['POST'])
def api_diagnose(printer_id):
    """Run an immediate full scan of one SNMP printer and broadcast the result."""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    printer = database.get_printer(printer_id)
    if not printer:
        return jsonify({'status': 'error', 'message': f'Printer {printer_id} not found'}), 404
    if printer.get('mode') != 'snmp':
        return jsonify({'status': 'error',
                        'message': 'Diagnose is only available for network (SNMP) printers'}), 400
    ok, msg = validate_probe_target(printer.get('ip_address') or '')
    if not ok:
        return jsonify({'status': 'error', 'message': msg}), 400

    report = diagnose_printer(printer_id)
    if report is None:
        return jsonify({'status': 'error', 'message': f'Printer {printer_id} not found'}), 404

    telemetry = poll_all_printers()
    socketio.emit('printer_update', {'printers': telemetry})
    record_activity(f'Ran diagnosis on {printer_id}', printer_id)
    return jsonify({'status': 'success', 'report': report})


@app.route('/api/printers/<printer_id>/report', methods=['GET'])
def api_printer_report(printer_id):
    """The stored scan report of one printer (no credentials included)."""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    printer = database.get_printer(printer_id)
    if not printer:
        return jsonify({'status': 'error', 'message': f'Printer {printer_id} not found'}), 404
    scan_report = None
    if printer.get('scan_report'):
        try:
            scan_report = json.loads(printer['scan_report'])
        except (TypeError, ValueError):
            scan_report = None
    return jsonify({
        'printer_id': printer_id,
        'mode': printer.get('mode'),
        'data_source': printer.get('data_source'),
        'last_scan_at': printer.get('last_scan_at'),
        'scan_report': scan_report,
    })


@app.route('/api/printers/<printer_id>', methods=['DELETE'])
def api_delete_printer(printer_id):
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    database.delete_printer(printer_id)
    init_monitor()
    record_activity(f'Deleted printer {printer_id}', printer_id)
    return jsonify({'status': 'success', 'message': f'Printer {printer_id} removed'})


@app.route('/api/printers/<printer_id>/job', methods=['POST'])
def api_trigger_job(printer_id):
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    printer = database.get_printer(printer_id)
    if not printer:
        return jsonify({'status': 'error', 'message': f'Printer {printer_id} not found'}), 404
    if printer.get('mode') != 'simulation':
        return jsonify({'status': 'error',
                        'message': 'Test print jobs are only available for simulated printers.'}), 400
    data = request.get_json() or {}
    job_name = data.get('job_name', 'Diagnostic Print')
    pages = int(data.get('pages', 3))

    success, msg = trigger_print_job(printer_id, job_name, pages)
    if success:
        database.log_print_job(printer_id, job_name, pages, session['user']['username'])
        telemetry = poll_all_printers()
        socketio.emit('printer_update', {'printers': telemetry})
        record_activity(f'Sent test print job to {printer_id} ({pages} pages)', printer_id)
        return jsonify({'status': 'success', 'message': msg})
    return jsonify({'status': 'error', 'message': msg}), 400


@app.route('/api/printers/<printer_id>/replenish', methods=['POST'])
def api_replenish(printer_id):
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    printer = database.get_printer(printer_id)
    if not printer:
        return jsonify({'status': 'error', 'message': f'Printer {printer_id} not found'}), 404
    if printer.get('mode') != 'simulation':
        return jsonify({'status': 'error',
                        'message': 'Supplies of real printers must be refilled physically at the device.'}), 400
    applied = replenish_printer(printer_id, toner=True, paper=True)
    telemetry = poll_all_printers()
    socketio.emit('printer_update', {'printers': telemetry})
    if applied:
        record_activity(f'Refilled paper on {printer_id}', printer_id)
    return jsonify({'status': 'success', 'message': 'Toner and paper refilled'})


@app.route('/api/printers/<printer_id>/clear-jam', methods=['POST'])
def api_clear_jam(printer_id):
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    printer = database.get_printer(printer_id)
    if not printer:
        return jsonify({'status': 'error', 'message': f'Printer {printer_id} not found'}), 404
    if printer.get('mode') != 'simulation':
        return jsonify({'status': 'error',
                        'message': 'A jam on a real printer must be cleared physically at the device.'}), 400
    applied = clear_printer_jam(printer_id)
    telemetry = poll_all_printers()
    socketio.emit('printer_update', {'printers': telemetry})
    if applied:
        record_activity(f'Cleared jam on {printer_id}', printer_id)
    return jsonify({'status': 'success', 'message': 'Paper jam resolved'})


@app.route('/api/printers/<printer_id>/cool-fuser', methods=['POST'])
def api_cool_fuser(printer_id):
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    printer = database.get_printer(printer_id)
    if not printer:
        return jsonify({'status': 'error', 'message': f'Printer {printer_id} not found'}), 404
    if printer.get('mode') != 'simulation':
        return jsonify({'status': 'error',
                        'message': 'A real printer must be allowed to cool down on its own - '
                                   'switch it off if it is overheating.'}), 400
    if not cool_printer_fuser(printer_id):
        return jsonify({'status': 'error', 'message': f'Printer {printer_id} not found'}), 404
    telemetry = poll_all_printers()
    socketio.emit('printer_update', {'printers': telemetry})
    record_activity(f'Cooled fuser on {printer_id}', printer_id)
    return jsonify({'status': 'success', 'message': 'Fuser temperature restored to nominal 180 C'})


@app.route('/api/telemetry/poll', methods=['POST'])
def api_force_poll():
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    telemetry = poll_all_printers()
    socketio.emit('printer_update', {'printers': telemetry})
    return jsonify({'status': 'success', 'printers': telemetry})


@app.route('/api/jobs', methods=['GET'])
def api_jobs():
    jobs = database.get_recent_print_jobs(15)
    return jsonify(jobs)


@app.route('/api/alerts', methods=['GET'])
def api_alerts():
    alerts = database.get_active_alerts()
    return jsonify(alerts)


@app.route('/api/alerts/<int:alert_id>/resolve', methods=['POST'])
def api_resolve_alert(alert_id):
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    # Look up the owning printer before the alert is marked resolved.
    printer_id = next((alert.get('printer_id') for alert in database.get_active_alerts()
                       if alert.get('id') == alert_id), None)
    database.resolve_alert(alert_id)
    record_activity(f'Resolved alert #{alert_id}', printer_id)
    return jsonify({'status': 'success'})


@app.route('/api/activity', methods=['GET'])
def api_activity():
    """Recent MongoDB activity_log entries (newest first)."""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'mongo_available': mongo_db.is_available(),
        'activities': mongo_db.get_recent_activities(50)
    })


@app.route('/api/mongo/status', methods=['GET'])
def api_mongo_status():
    """Diagnostic view of the MongoDB identity/audit store."""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(mongo_db.get_status())


if __name__ == '__main__':
    print("=" * 60)
    print("   AI-BASED SMART PRINTER MONITORING & TRACKING SYSTEM")
    print("   * Listening on: http://127.0.0.1:5000")
    print("   * Default Admin User: admin / admin123")
    mongo_state = ('connected' if mongo_db.is_available()
                   else 'UNAVAILABLE - login falls back to SQLite')
    print(f"   * MongoDB (users/activity_log): {mongo_db.MONGO_URI} [{mongo_state}]")
    print(f"   * SQLite (printer telemetry): {database.DB_PATH}")
    print("=" * 60)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)


