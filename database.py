"""
Database module for AI-Based Printer Monitoring System.
Handles SQLite connection, schema initialization, and CRUD helpers.
"""

import sqlite3
import os
import json
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'printer_system.db')


def get_db():
    """Get a database connection with row factory configured."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Scripted demo fleet
# ---------------------------------------------------------------------------
# Every entry is tuned so that the dashboard shows exactly ONE obvious,
# plain-language problem per printer:
#   PRN-001 -> No Paper      (paper_level = 0)
#   PRN-002 -> No Ink        (toner_black = 0)
#   PRN-003 -> Paper Jam     (jammed = 1)
#   PRN-004 -> Overheating   (fuser_temp > 210)
#   PRN-005 -> No Issues     (all values healthy)
DEMO_FLEET = [
    {
        'id': 'PRN-001', 'name': 'HP LaserJet Enterprise M608', 'model': 'HP M608x',
        'ip_address': '192.168.1.101', 'mode': 'simulation', 'status': 'Warning',
        'toner_black': 78, 'toner_cyan': 0, 'toner_magenta': 0, 'toner_yellow': 0,
        'paper_level': 0, 'drum_wear': 24, 'fuser_temp': 185, 'total_pages': 45210,
        'jammed': 0,
    },
    {
        'id': 'PRN-002', 'name': 'Canon imageRUNNER ADVANCE C3530', 'model': 'Canon C3530i',
        'ip_address': '192.168.1.102', 'mode': 'simulation', 'status': 'Warning',
        'toner_black': 0, 'toner_cyan': 0, 'toner_magenta': 0, 'toner_yellow': 0,
        'paper_level': 62, 'drum_wear': 41, 'fuser_temp': 188, 'total_pages': 98400,
        'jammed': 0,
    },
    {
        'id': 'PRN-003', 'name': 'Epson WorkForce Pro WF-C879R', 'model': 'WF-C879R',
        'ip_address': '192.168.1.103', 'mode': 'simulation', 'status': 'Error',
        'toner_black': 82, 'toner_cyan': 88, 'toner_magenta': 91, 'toner_yellow': 84,
        'paper_level': 80, 'drum_wear': 18, 'fuser_temp': 175, 'total_pages': 12890,
        'jammed': 1,
    },
    {
        'id': 'PRN-004', 'name': 'Brother MFC-L8900CDW (Floor 3)', 'model': 'MFC-L8900CDW',
        'ip_address': '192.168.1.104', 'mode': 'simulation', 'status': 'Warning',
        'toner_black': 60, 'toner_cyan': 55, 'toner_magenta': 58, 'toner_yellow': 61,
        'paper_level': 55, 'drum_wear': 45, 'fuser_temp': 214, 'total_pages': 134200,
        'jammed': 0,
    },
    {
        'id': 'PRN-005', 'name': 'Xerox VersaLink C405 (Reception)', 'model': 'C405/DN',
        'ip_address': '192.168.1.105', 'mode': 'simulation', 'status': 'Idle',
        'toner_black': 95, 'toner_cyan': 96, 'toner_magenta': 94, 'toner_yellow': 97,
        'paper_level': 95, 'drum_wear': 8, 'fuser_temp': 182, 'total_pages': 7620,
        'jammed': 0,
    },
]


def ensure_demo_fleet(cursor):
    """
    Insert the scripted demo printers, or restore them to their documented
    starting state if they already exist (keeps the demo reproducible).
    Printers added by the operator are never touched.
    """
    for printer in DEMO_FLEET:
        columns = list(printer.keys())
        cursor.execute('SELECT id FROM printers WHERE id = ?', (printer['id'],))
        if cursor.fetchone():
            assignments = ', '.join(f'{column} = ?' for column in columns if column != 'id')
            cursor.execute(
                f'UPDATE printers SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                [printer[column] for column in columns if column != 'id'] + [printer['id']]
            )
        else:
            cursor.execute(
                f"INSERT INTO printers ({', '.join(columns)}) "
                f"VALUES ({', '.join(['?'] * len(columns))})",
                [printer[column] for column in columns]
            )


def init_db():
    """Initialize database tables and seed default admin user and sample printers."""
    conn = get_db()
    cursor = conn.cursor()

    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'admin',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Printers table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS printers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            model TEXT NOT NULL,
            ip_address TEXT DEFAULT '127.0.0.1',
            snmp_community TEXT DEFAULT 'public',
            snmp_port INTEGER DEFAULT 161,
            mode TEXT DEFAULT 'simulation',
            status TEXT DEFAULT 'Idle',
            toner_black INTEGER DEFAULT 85,
            toner_cyan INTEGER DEFAULT 90,
            toner_magenta INTEGER DEFAULT 75,
            toner_yellow INTEGER DEFAULT 80,
            paper_level INTEGER DEFAULT 95,
            drum_wear INTEGER DEFAULT 12,
            fuser_temp INTEGER DEFAULT 185,
            total_pages INTEGER DEFAULT 12450,
            jammed INTEGER DEFAULT 0,
            data_source TEXT DEFAULT 'simulation',
            last_scan_at REAL,
            scan_report TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Lightweight migration: databases created before the 'jammed' column was
    # introduced are upgraded in place so no manual reset is required.
    cursor.execute('PRAGMA table_info(printers)')
    existing_columns = [row['name'] for row in cursor.fetchall()]
    if 'jammed' not in existing_columns:
        cursor.execute('ALTER TABLE printers ADD COLUMN jammed INTEGER DEFAULT 0')
    if 'data_source' not in existing_columns:
        cursor.execute("ALTER TABLE printers ADD COLUMN data_source TEXT DEFAULT 'simulation'")
    if 'last_scan_at' not in existing_columns:
        cursor.execute('ALTER TABLE printers ADD COLUMN last_scan_at REAL')
    if 'scan_report' not in existing_columns:
        cursor.execute('ALTER TABLE printers ADD COLUMN scan_report TEXT')

    # Print jobs log
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS print_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id TEXT NOT NULL,
            job_name TEXT NOT NULL,
            pages INTEGER NOT NULL,
            user_name TEXT DEFAULT 'system',
            status TEXT DEFAULT 'Completed',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (printer_id) REFERENCES printers(id)
        )
    ''')

    # Telemetry history for AI trends & analytics
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS telemetry_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id TEXT NOT NULL,
            toner_black INTEGER,
            paper_level INTEGER,
            drum_wear INTEGER,
            fuser_temp INTEGER,
            total_pages INTEGER,
            failure_risk_score REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (printer_id) REFERENCES printers(id)
        )
    ''')

    # Predictive maintenance alerts
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id TEXT NOT NULL,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            is_resolved INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (printer_id) REFERENCES printers(id)
        )
    ''')

    conn.commit()

    # Seed default admin user if not exists
    cursor.execute('SELECT id FROM users WHERE username = ?', ('admin',))
    if not cursor.fetchone():
        hashed = generate_password_hash('admin123')
        cursor.execute(
            'INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)',
            ('admin', 'admin@printer.local', hashed, 'admin')
        )
    # Seed (or restore) the scripted demo fleet so the dashboard always shows
    # the five distinct, clearly visible printer issue states.
    ensure_demo_fleet(cursor)

    conn.commit()
    conn.close()


def authenticate_user(username_or_email, password):
    """Authenticate a user by username/email and password."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT * FROM users WHERE username = ? OR email = ?',
        (username_or_email, username_or_email)
    )
    user = cursor.fetchone()
    conn.close()

    if user and check_password_hash(user['password_hash'], password):
        return dict(user)
    return None


def register_user(username, email, password, role='operator'):
    """Register a new user account."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        hashed = generate_password_hash(password)
        cursor.execute(
            'INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)',
            (username, email, hashed, role)
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        return True, user_id
    except sqlite3.IntegrityError:
        conn.close()
        return False, "Username or Email already registered"
    except Exception as e:
        conn.close()
        return False, str(e)


def get_all_printers():
    """Retrieve all printer records from the database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM printers ORDER BY id ASC')
    printers = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return printers


def get_printer(printer_id):
    """Retrieve a single printer by ID."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM printers WHERE id = ?', (printer_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def add_printer(p_id, name, model, ip_address='127.0.0.1', mode='simulation',
                snmp_community='public', snmp_port=161):
    """Insert a new printer into database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO printers (id, name, model, ip_address, mode, snmp_community, snmp_port,
                              data_source, status, toner_black, paper_level, drum_wear, fuser_temp, total_pages)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Idle', 100, 100, 0, 180, 0)
    ''', (p_id, name, model, ip_address, mode, snmp_community, snmp_port, mode))
    conn.commit()
    conn.close()


def delete_printer(printer_id):
    """Delete a printer and all of its associated telemetry, alerts and jobs."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM telemetry_history WHERE printer_id = ?', (printer_id,))
    cursor.execute('DELETE FROM alerts WHERE printer_id = ?', (printer_id,))
    cursor.execute('DELETE FROM print_jobs WHERE printer_id = ?', (printer_id,))
    cursor.execute('DELETE FROM printers WHERE id = ?', (printer_id,))
    conn.commit()
    conn.close()


def update_printer_telemetry(printer_id, telemetry):
    """Update printer metrics in database."""
    conn = get_db()
    cursor = conn.cursor()
    jammed = telemetry.get('jammed')
    jammed_value = None if jammed is None else (1 if jammed else 0)
    scan_report = telemetry.get('scan_report')
    cursor.execute('''
        UPDATE printers SET
            status = ?,
            toner_black = ?,
            toner_cyan = ?,
            toner_magenta = ?,
            toner_yellow = ?,
            paper_level = ?,
            drum_wear = ?,
            fuser_temp = ?,
            total_pages = ?,
            jammed = ?,
            data_source = ?,
            last_scan_at = ?,
            scan_report = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (
        telemetry.get('status', 'Idle'),
        telemetry.get('toner_black', 0),
        telemetry.get('toner_cyan', 0),
        telemetry.get('toner_magenta', 0),
        telemetry.get('toner_yellow', 0),
        telemetry.get('paper_level', 0),
        telemetry.get('drum_wear', 0),
        telemetry.get('fuser_temp', 180),
        telemetry.get('total_pages', 0),
        jammed_value,
        telemetry.get('data_source') or telemetry.get('mode', 'simulation'),
        telemetry.get('last_scan_at'),
        json.dumps(scan_report) if scan_report else None,
        printer_id
    ))
    conn.commit()
    conn.close()


def save_telemetry_snapshot(printer_id, telemetry, failure_risk=0.0):
    """Append a snapshot for historical trend analytics."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO telemetry_history (
            printer_id, toner_black, paper_level, drum_wear, fuser_temp, total_pages, failure_risk_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        printer_id,
        telemetry.get('toner_black', 0),
        telemetry.get('paper_level', 0),
        telemetry.get('drum_wear', 0),
        telemetry.get('fuser_temp', 180),
        telemetry.get('total_pages', 0),
        failure_risk
    ))
    conn.commit()
    conn.close()


def add_alert(printer_id, severity, category, message):
    """Log an alert if not already logged recently."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id FROM alerts
        WHERE printer_id = ? AND category = ? AND is_resolved = 0
    ''', (printer_id, category))
    existing = cursor.fetchone()

    if not existing:
        cursor.execute('''
            INSERT INTO alerts (printer_id, severity, category, message)
            VALUES (?, ?, ?, ?)
        ''', (printer_id, severity, category, message))
        conn.commit()
    conn.close()


def resolve_alert(alert_id):
    """Mark an alert as resolved."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('UPDATE alerts SET is_resolved = 1 WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()


def get_active_alerts():
    """Retrieve unresolved alerts ordered by severity and recency."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT a.*, p.name as printer_name, p.model
        FROM alerts a
        JOIN printers p ON a.printer_id = p.id
        WHERE a.is_resolved = 0
        ORDER BY
            CASE a.severity
                WHEN 'Critical' THEN 1
                WHEN 'Warning' THEN 2
                ELSE 3
            END,
            a.created_at DESC
        LIMIT 25
    ''')
    alerts = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return alerts


def log_print_job(printer_id, job_name, pages, user_name='System'):
    """Record a print job execution."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO print_jobs (printer_id, job_name, pages, user_name, status)
        VALUES (?, ?, ?, ?, 'Completed')
    ''', (printer_id, job_name, pages, user_name))
    conn.commit()
    conn.close()


def get_recent_print_jobs(limit=10):
    """Get recent print jobs."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT pj.*, p.name as printer_name
        FROM print_jobs pj
        JOIN printers p ON pj.printer_id = p.id
        ORDER BY pj.timestamp DESC
        LIMIT ?
    ''', (limit,))
    jobs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jobs


def get_telemetry_trends(printer_id=None, limit=20):
    """Get recent telemetry points for trend graphs."""
    conn = get_db()
    cursor = conn.cursor()
    if printer_id:
        cursor.execute('''
            SELECT * FROM telemetry_history
            WHERE printer_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (printer_id, limit))
    else:
        cursor.execute('''
            SELECT * FROM telemetry_history
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (limit,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return list(reversed(rows))
