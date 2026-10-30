import random
import time
import json
from concurrent.futures import ThreadPoolExecutor
from database import get_all_printers, get_printer, update_printer_telemetry, save_telemetry_snapshot, add_alert
from ai_engine import ai_engine
from snmp_scanner import scan_printer

# SNMP scans take seconds (timeouts on unreachable printers), so they run in
# worker threads on a slower cadence than the 3.5 s simulation loop and their
# results are picked up on a later tick - the loop itself never blocks on them.
SNMP_POLL_INTERVAL = 12
SNMP_SCAN_TIMEOUT = 2
SNMP_SCAN_RETRIES = 1

_printer_telemetry_cache = {}

# SNMP credentials live here and are NEVER copied into the telemetry cache,
# because cache entries are broadcast to every browser over the socket.
_snmp_config = {}   # printer_id -> {'community': str, 'port': int}

_snmp_pool = None
_snmp_pending = {}  # printer_id -> Future (scan in flight)

_SNMP_METRIC_KEYS = ('toner_black', 'toner_cyan', 'toner_magenta', 'toner_yellow',
                     'paper_level', 'drum_wear', 'fuser_temp', 'total_pages')


def _cache_from_row(row):
    """Cache entry for a DB row, with SNMP credentials stripped out and the
    persisted JSON scan report restored to a dict."""
    state = {key: value for key, value in row.items()
             if key not in ('snmp_community', 'snmp_port')}
    state['jammed'] = bool(state.get('jammed', 0))

    scan_report = state.get('scan_report')
    if isinstance(scan_report, str):
        try:
            scan_report = json.loads(scan_report)
        except (TypeError, ValueError):
            scan_report = None
    state['scan_report'] = scan_report if isinstance(scan_report, dict) else None
    if state['scan_report']:
        state['findings'] = state['scan_report'].get('findings') or []
    return state


def init_monitor():
    """(Re)build the in-memory telemetry cache from the database."""
    _printer_telemetry_cache.clear()
    _snmp_pending.clear()
    printers = get_all_printers()
    for p in printers:
        _printer_telemetry_cache[p['id']] = _cache_from_row(p)
        _printer_telemetry_cache[p['id']]['last_poll'] = time.time()
        _snmp_config[p['id']] = {
            'community': p.get('snmp_community') or 'public',
            'port': p.get('snmp_port') or 161,
        }


def poll_snmp_printer(ip, community='public', port=161):
    """Full structured scan of one network printer (SNMP with IPP fallback).

    Returns the scan report dict - never raises."""
    return scan_printer(ip, community=community, port=port,
                        timeout=SNMP_SCAN_TIMEOUT, retries=SNMP_SCAN_RETRIES)


def simulate_printer_step(printer_state):
    status = printer_state.get('status', 'Idle')
    toner = printer_state.get('toner_black', 50)
    paper = printer_state.get('paper_level', 50)
    drum = printer_state.get('drum_wear', 20)
    fuser_temp = printer_state.get('fuser_temp', 180)
    total_pages = printer_state.get('total_pages', 10000)
    jammed = printer_state.get('jammed', False)

    if jammed:
        status = 'Error'
        fuser_temp = max(160, fuser_temp - 2)
        printer_state['status'] = status
        printer_state['fuser_temp'] = fuser_temp
        printer_state['error_detail'] = 'Paper jam detected in duplex feeder path.'
        return printer_state

    if status == 'Printing':
        pages_printed = random.randint(1, 4)
        total_pages += pages_printed
        toner = max(0, round(toner - random.uniform(0.04, 0.12), 1))
        paper = max(0, round(paper - (pages_printed * 0.2), 1))
        fuser_temp = min(218, fuser_temp + random.randint(1, 3))
        drum = min(100, round(drum + 0.02, 2))

        if paper <= 0:
            status = 'Warning'
            paper = 0
        elif toner <= 0:
            status = 'Warning'
            toner = 0
        else:
            if random.random() < 0.35:
                status = 'Idle'

    elif status == 'Idle':
        if fuser_temp > 180:
            fuser_temp -= random.randint(1, 2)
        elif fuser_temp < 175:
            fuser_temp += 1

        if toner > 2 and paper > 2 and random.random() < 0.18:
            status = 'Printing'

    elif status == 'Warning':
        if toner > 10 and paper > 10 and fuser_temp < 210:
            status = 'Idle'

    elif status == 'Error':
        # No jam is latched in memory, so the error state is stale:
        # return the unit to service instead of leaving it stuck forever.
        status = 'Idle'
        jammed = False
        printer_state.pop('error_detail', None)

    if status == 'Printing' and random.random() < 0.015:
        jammed = True
        status = 'Error'

    printer_state['status'] = status
    printer_state['toner_black'] = toner
    printer_state['paper_level'] = paper
    printer_state['drum_wear'] = drum
    printer_state['fuser_temp'] = fuser_temp
    printer_state['total_pages'] = total_pages
    printer_state['jammed'] = jammed
    printer_state['last_poll'] = time.time()
    return printer_state



def trigger_print_job(printer_id, job_name='Manual Print Test', pages=5):
    if printer_id not in _printer_telemetry_cache:
        p = get_printer(printer_id)
        if not p:
            return False, "Printer not found"
        _printer_telemetry_cache[printer_id] = p

    state = _printer_telemetry_cache[printer_id]
    if state.get('status') == 'Error':
        return False, "Printer is currently in Error state (Clear Jam first)"

    state['status'] = 'Printing'
    state['total_pages'] = state.get('total_pages', 0) + pages
    state['toner_black'] = max(0, round(state.get('toner_black', 50) - (pages * 0.15), 1))
    state['paper_level'] = max(0, round(state.get('paper_level', 50) - (pages * 0.25), 1))
    state['fuser_temp'] = min(215, state.get('fuser_temp', 180) + 6)
    
    eval_res = ai_engine.evaluate_printer_health(state)
    state['ai_analysis'] = eval_res
    update_printer_telemetry(printer_id, state)
    save_telemetry_snapshot(printer_id, state, eval_res['failure_probability'])
    return True, f"Job '{job_name}' queued and executed ({pages} pages)."


def replenish_printer(printer_id, toner=True, paper=True):
    if printer_id in _printer_telemetry_cache:
        if toner:
            _printer_telemetry_cache[printer_id]['toner_black'] = 100
        if paper:
            _printer_telemetry_cache[printer_id]['paper_level'] = 100
        _printer_telemetry_cache[printer_id]['status'] = 'Idle'
        _printer_telemetry_cache[printer_id]['jammed'] = False
        update_printer_telemetry(printer_id, _printer_telemetry_cache[printer_id])
        return True
    return False


def clear_printer_jam(printer_id):
    if printer_id in _printer_telemetry_cache:
        _printer_telemetry_cache[printer_id]['jammed'] = False
        _printer_telemetry_cache[printer_id]['status'] = 'Idle'
        _printer_telemetry_cache[printer_id].pop('error_detail', None)
        update_printer_telemetry(printer_id, _printer_telemetry_cache[printer_id])
        return True
    return False


def cool_printer_fuser(printer_id):
    """Force the fuser assembly back to its nominal operating temperature."""
    if printer_id in _printer_telemetry_cache:
        state = _printer_telemetry_cache[printer_id]
        state['fuser_temp'] = 180
        if state.get('status') == 'Warning':
            state['status'] = 'Idle'
        update_printer_telemetry(printer_id, state)
        return True
    return False


def apply_snmp_report(state, report):
    """Map a scan_printer() report onto the cached telemetry state.

    Anything the printer did not report becomes None (unknown) - never zero
    and never a healthy default."""
    state['scan_report'] = report
    state['last_scan_at'] = time.time()
    state['data_source'] = (report or {}).get('method') or 'snmp'

    if not report or report.get('error'):
        state['status'] = 'Offline'
        state['jammed'] = None
        state['findings'] = []
        for key in _SNMP_METRIC_KEYS:
            state[key] = None
        state.pop('error_detail', None)
        return

    findings = report.get('findings') or []
    state['findings'] = findings
    state['status'] = report.get('status') or 'Unknown'

    toner = {}
    for supply in report.get('supplies') or []:
        colour = supply.get('colour')
        if colour in ('black', 'cyan', 'magenta', 'yellow'):
            toner[colour] = supply.get('percent')
    state['toner_black'] = toner.get('black')
    state['toner_cyan'] = toner.get('cyan')
    state['toner_magenta'] = toner.get('magenta')
    state['toner_yellow'] = toner.get('yellow')

    tray_percents = [t.get('percent') for t in report.get('trays') or []
                     if t.get('percent') is not None]
    state['paper_level'] = min(tray_percents) if tray_percents else None

    state['total_pages'] = report.get('total_pages')

    # The standard Printer MIB cannot report these; they stay unknown so the
    # dashboard shows "not available" instead of an invented number.
    state['fuser_temp'] = None
    state['drum_wear'] = None

    jam_codes = {f.get('code') for f in findings}
    if 'jammed' in jam_codes:
        state['jammed'] = True
    elif report.get('error_state_read'):
        state['jammed'] = False
    else:
        state['jammed'] = None


_ALERT_SEVERITY = {'critical': 'Critical', 'warning': 'Warning'}


def _feed_findings_to_alerts(p_id, findings):
    """Turn scan findings into maintenance alerts (de-duplicated by code)."""
    for finding in findings or []:
        message = finding.get('plain_message') or finding.get('title') or 'Printer reported a problem'
        what_to_do = finding.get('what_to_do')
        if what_to_do:
            message = f'{message} {what_to_do}'
        # add_alert de-dupes by printer + category while unresolved, so the
        # finding code doubles as the alert key and re-polls do not spam.
        add_alert(p_id,
                  _ALERT_SEVERITY.get(finding.get('severity'), 'Info'),
                  finding.get('code') or 'finding',
                  message)


def _submit_due_snmp_scans(now):
    """Start scans for SNMP printers whose interval has elapsed (non-blocking)."""
    global _snmp_pool
    for p_id, state in _printer_telemetry_cache.items():
        if state.get('mode') != 'snmp':
            continue
        if p_id in _snmp_pending:
            continue
        if now - (state.get('last_snmp_poll') or 0) < SNMP_POLL_INTERVAL:
            continue
        config = _snmp_config.get(p_id, {})
        if _snmp_pool is None:
            _snmp_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='snmp-scan')
        future = _snmp_pool.submit(
            scan_printer,
            state.get('ip_address', ''),
            config.get('community', 'public'),
            config.get('port', 161),
            SNMP_SCAN_TIMEOUT,
            SNMP_SCAN_RETRIES,
        )
        _snmp_pending[p_id] = future


def _collect_done_snmp_scans():
    """Apply the results of any scans that finished since the last tick."""
    for p_id, future in list(_snmp_pending.items()):
        if not future.done():
            continue
        _snmp_pending.pop(p_id, None)
        state = _printer_telemetry_cache.get(p_id)
        if state is None:
            continue
        state['last_snmp_poll'] = time.time()
        try:
            report = future.result()
        except Exception:
            report = None
        apply_snmp_report(state, report)
        _feed_findings_to_alerts(p_id, report.get('findings') if report else [])


def diagnose_printer(printer_id):
    """Force an immediate scan of one SNMP printer and apply the result.

    Runs synchronously (a manual button press tolerates the wait) and
    returns the scan report, or None when the printer is not an SNMP node."""
    state = _printer_telemetry_cache.get(printer_id)
    if state is None or state.get('mode') != 'snmp':
        return None
    config = _snmp_config.get(printer_id, {})
    report = scan_printer(state.get('ip_address', ''),
                          config.get('community', 'public'),
                          config.get('port', 161),
                          SNMP_SCAN_TIMEOUT, SNMP_SCAN_RETRIES)
    state['last_snmp_poll'] = time.time()
    apply_snmp_report(state, report)
    _feed_findings_to_alerts(printer_id, report.get('findings') or [])
    state['ai_analysis'] = ai_engine.evaluate_printer_health(state)
    update_printer_telemetry(printer_id, state)
    return report


def poll_all_printers():
    db_printers = get_all_printers()
    now = time.time()
    results = []

    for p in db_printers:
        p_id = p['id']
        if p_id not in _printer_telemetry_cache:
            _printer_telemetry_cache[p_id] = _cache_from_row(p)
        _snmp_config[p_id] = {
            'community': p.get('snmp_community') or 'public',
            'port': p.get('snmp_port') or 161,
        }

    # Simulation printers advance on every tick, exactly as before.
    for p in db_printers:
        p_id = p['id']
        state = _printer_telemetry_cache[p_id]
        if state.get('mode') == 'snmp':
            continue
        state = simulate_printer_step(state)
        _printer_telemetry_cache[p_id] = state

        ai_res = ai_engine.evaluate_printer_health(state)
        state['ai_analysis'] = ai_res

        for rec in ai_res.get('recommendations', []):
            if rec['priority'] == 'High':
                add_alert(p_id, 'Critical', 'Hardware/Consumable', rec['action'])
            elif rec['priority'] == 'Medium':
                add_alert(p_id, 'Warning', 'Predictive Alert', rec['action'])

        update_printer_telemetry(p_id, state)
        if random.random() < 0.2:
            save_telemetry_snapshot(p_id, state, ai_res['failure_probability'])

        results.append(state)

    # SNMP printers: submit due scans and collect finished ones. This never
    # blocks the loop - an offline printer's timeouts happen in a worker thread.
    _submit_due_snmp_scans(now)
    _collect_done_snmp_scans()

    for p in db_printers:
        p_id = p['id']
        state = _printer_telemetry_cache[p_id]
        if state.get('mode') != 'snmp':
            continue

        if not state.get('scan_report'):
            # Nothing is known yet: the placeholder database defaults are not
            # real readings, so report unknown instead of fabricating health.
            for key in _SNMP_METRIC_KEYS:
                state[key] = None
            state['status'] = 'Unknown'
            state['jammed'] = None
            state['findings'] = []
            state['ai_analysis'] = ai_engine.evaluate_printer_health(state)
        else:
            ai_res = ai_engine.evaluate_printer_health(state)
            state['ai_analysis'] = ai_res
            if not state['scan_report'].get('error') and random.random() < 0.2:
                save_telemetry_snapshot(p_id, state, ai_res['failure_probability'])

        update_printer_telemetry(p_id, state)
        results.append(state)

    return results

