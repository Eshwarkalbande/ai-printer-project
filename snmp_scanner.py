"""
Standalone network printer scanner (SNMP v1/v2c with IPP fallback).

Reads standard Printer MIB / Host Resources MIB objects over SNMP using the
real pysnmp 7.x asyncio API (get_cmd / walk_cmd / UdpTransportTarget.create)
and, when SNMP is unavailable but TCP 631 answers, falls back to a hand-built
IPP Get-Printer-Attributes request.

The parser helpers (bitmask, supply percent, finding merge) are pure functions
so they can be unit-tested without any network access.
"""

import asyncio
import socket
import struct

from pysnmp.hlapi.v3arch.asyncio import (
    get_cmd, walk_cmd, CommunityData, ContextData, SnmpEngine,
    UdpTransportTarget, ObjectType, ObjectIdentity,
)

# ---------------------------------------------------------------------------
# OIDs (standard Printer MIB / Host Resources MIB)
# ---------------------------------------------------------------------------
OID_SYS_DESCR = '1.3.6.1.2.1.1.1.0'
OID_SYS_NAME = '1.3.6.1.2.1.1.5.0'
OID_HR_PRINTER_STATUS = '1.3.6.1.2.1.25.3.5.1.1.0'
OID_HR_PRINTER_DETECTED_ERROR_STATE = '1.3.6.1.2.1.25.3.5.1.2.0'

OID_PRT_MARKER_SUPPLIES_TABLE = '1.3.6.1.2.1.43.11.1.1'  # cols 6 desc, 8 max, 9 level
OID_PRT_INPUT_TABLE = '1.3.6.1.2.1.43.8.2.1'             # cols 9 max, 10 level, 11 status, 13 name
OID_PRT_ALERT_TABLE = '1.3.6.1.2.1.43.18.1.1'            # cols 2 severity, 7 code, 8 description
OID_PRT_COVER_TABLE = '1.3.6.1.2.1.43.6.1.1'             # cols 2 description, 3 status
OID_PRT_MARKER_LIFE_COUNT = '1.3.6.1.2.1.43.10.2.1.4'    # total pages (per marker)

# hrPrinterStatus (Host Resources MIB)
HR_PRINTER_STATUS_NAMES = {1: 'Other', 2: 'Unknown', 3: 'Idle', 4: 'Printing', 5: 'Warmup'}

# prtAlertSeverity (Printer MIB): other(1), critical(3), warning(4), info(5)
ALERT_SEVERITY_NAMES = {1: 'info', 3: 'critical', 4: 'warning', 5: 'info'}

# prtInputStatus (Printer MIB)
INPUT_STATUS_NAMES = {
    1: 'other', 2: 'unknown', 3: 'available',
    4: 'low', 5: 'empty', 6: 'not-present',
}

# prtCoverStatus (Printer MIB)
COVER_STATUS_NAMES = {
    1: 'other', 2: 'open', 3: 'closed', 4: 'interlock-open', 5: 'interlock-closed',
}

SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}

# ---------------------------------------------------------------------------
# hrPrinterDetectedErrorState bitmask
# ---------------------------------------------------------------------------
# Bit 0 is the MOST significant bit of the first octet of the OCTET STRING.
# Each entry: code, human label, severity, finding title / message / action.
ERROR_STATE_BITS = [
    {
        'code': 'lowPaper', 'label': 'Low paper', 'severity': 'warning',
        'title': 'Paper low',
        'plain_message': 'The printer reports that its paper supply is low.',
        'what_to_do': 'Load more paper soon to avoid interrupting print jobs.',
    },
    {
        'code': 'noPaper', 'label': 'No paper', 'severity': 'critical',
        'title': 'Out of paper',
        'plain_message': 'The printer has run out of paper and cannot print.',
        'what_to_do': 'Load paper into the indicated tray.',
    },
    {
        'code': 'lowToner', 'label': 'Low toner', 'severity': 'warning',
        'title': 'Toner/ink low',
        'plain_message': 'One or more toner/ink supplies are running low.',
        'what_to_do': 'Order a replacement cartridge before it runs out.',
    },
    {
        'code': 'noToner', 'label': 'No toner', 'severity': 'critical',
        'title': 'Toner/ink empty',
        'plain_message': 'One or more toner/ink supplies are empty.',
        'what_to_do': 'Replace the empty cartridge to resume printing.',
    },
    {
        'code': 'doorOpen', 'label': 'Door open', 'severity': 'warning',
        'title': 'Door or cover open',
        'plain_message': 'A door or cover on the printer is open.',
        'what_to_do': 'Close all doors and covers on the printer.',
    },
    {
        'code': 'jammed', 'label': 'Paper jam', 'severity': 'critical',
        'title': 'Paper jam',
        'plain_message': 'A sheet of paper is jammed inside the printer.',
        'what_to_do': 'Open the covers and carefully remove the jammed sheet.',
    },
    {
        'code': 'offline', 'label': 'Offline', 'severity': 'warning',
        'title': 'Printer reports offline',
        'plain_message': 'The printer answers on the network but reports its own status as offline.',
        'what_to_do': 'Check the printer panel and network settings to bring it back online.',
    },
    {
        'code': 'serviceRequested', 'label': 'Service requested', 'severity': 'critical',
        'title': 'Service requested',
        'plain_message': 'The printer has requested a service call.',
        'what_to_do': 'Check the printer display and contact your service provider if needed.',
    },
    {
        'code': 'inputTrayMissing', 'label': 'Input tray missing', 'severity': 'warning',
        'title': 'Input tray missing',
        'plain_message': 'An input paper tray is not installed.',
        'what_to_do': 'Reinstall the missing input tray.',
    },
    {
        'code': 'outputTrayMissing', 'label': 'Output tray missing', 'severity': 'warning',
        'title': 'Output tray missing',
        'plain_message': 'An output tray is not installed.',
        'what_to_do': 'Reinstall the missing output tray.',
    },
    {
        'code': 'markerSupplyMissing', 'label': 'Supply missing', 'severity': 'critical',
        'title': 'Toner/ink supply missing',
        'plain_message': 'A toner/ink supply is missing from the printer.',
        'what_to_do': 'Install the missing supply to resume printing.',
    },
    {
        'code': 'outputNearFull', 'label': 'Output near full', 'severity': 'warning',
        'title': 'Output tray nearly full',
        'plain_message': 'The output tray is nearly full.',
        'what_to_do': 'Empty the output tray soon.',
    },
    {
        'code': 'outputFull', 'label': 'Output full', 'severity': 'warning',
        'title': 'Output tray full',
        'plain_message': 'The output tray is full.',
        'what_to_do': 'Empty the output tray.',
    },
    {
        'code': 'inputTrayEmpty', 'label': 'Input tray empty', 'severity': 'critical',
        'title': 'Input tray empty',
        'plain_message': 'An input paper tray is empty.',
        'what_to_do': 'Load paper into the empty tray.',
    },
    {
        'code': 'overduePreventMaintenance', 'label': 'Maintenance overdue', 'severity': 'warning',
        'title': 'Preventive maintenance overdue',
        'plain_message': 'The printer reports that preventive maintenance is overdue.',
        'what_to_do': 'Schedule preventive maintenance for this printer.',
    },
]

# prtAlertCode (Printer MIB) -> standard finding code. The description text of
# the alert is always kept as the human-readable message.
KNOWN_ALERT_CODES = {
    3: 'doorOpen',        # coverOpen
    4: None,              # coverClosed
    5: 'doorOpen',        # interlockOpen
    6: None,              # interlockClosed
    8: 'jammed',          # jam
    501: 'doorOpen',       # doorOpen
    801: 'inputTrayMissing',
    807: 'lowPaper',       # inputMediaSupplyLow
    808: 'noPaper',        # inputMediaSupplyEmpty
    901: 'outputTrayMissing',
    1001: 'fuserUnderTemp',
    1002: 'overheat',      # markerFuserOverTemperature
    1101: 'noToner',       # markerTonerEmpty
    1102: 'noToner',       # markerInkEmpty
    1103: 'lowToner',      # markerTonerAlmostEmpty
    1104: 'lowToner',      # markerInkAlmostEmpty
    1501: 'inputTrayMissing',   # mediaPathMediaTrayMissing
    1502: 'outputNearFull',     # mediaPathMediaTrayAlmostFull
    1503: 'outputFull',         # mediaPathMediaTrayFull
    1506: 'jammed',             # mediaPathJam
}

OVERHEAT_ALERT_HINTS = ('over temperature', 'over-temperature', 'overtemp', 'overheat', 'fuser temp')

SUPPLY_COLOURS = ('black', 'cyan', 'magenta', 'yellow', 'red', 'blue', 'green')


# ---------------------------------------------------------------------------
# Pure parsers (unit-tested)
# ---------------------------------------------------------------------------

def _to_octets(data):
    """Normalise bitmask input to bytes: accept bytes/bytearray, list of ints, hex string."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        text = data.strip()
        if text.lower().startswith('0x'):
            text = text[2:]
        if not text:
            return b''
        try:
            return bytes.fromhex(text)
        except ValueError:
            raise ValueError(f'Invalid hrPrinterDetectedErrorState hex string: {data!r}')
    if isinstance(data, (list, tuple)):
        return bytes(int(b) & 0xFF for b in data)
    raise TypeError(f'Unsupported bitmask input type: {type(data)!r}')


def parse_printer_error_state(data):
    """
    Parse hrPrinterDetectedErrorState (OCTET STRING bitmask).

    Bit 0 is the MOST significant bit of the first byte. Returns a dict of
    {code: bool} covering all standard bits; bits beyond the supplied octets
    are reported as False.
    """
    octets = _to_octets(data)
    bits = {}
    for index, bit in enumerate(ERROR_STATE_BITS):
        if index >= len(octets) * 8:
            bits[bit['code']] = False
            continue
        byte = octets[index // 8]
        mask = 1 << (7 - (index % 8))
        bits[bit['code']] = bool(byte & mask)
    return bits


def active_error_bits(data):
    """Return the list of set bit codes for an hrPrinterDetectedErrorState value."""
    return [code for code, is_set in parse_printer_error_state(data).items() if is_set]


def supply_percent(level, max_capacity):
    """
    Convert prtMarkerSuppliesLevel / prtMarkerSuppliesMaxCapacity into
    (percent_or_None, state).

    level -3  -> "some remaining"  (OK-unknown, never a number)
    level -2  -> unknown
    level -1  -> other
    """
    state = 'unknown'
    if level is None or max_capacity is None:
        return None, 'unknown'
    if level == -3:
        return None, 'some-remaining'
    if level in (-1, -2) or level < 0:
        return None, 'other' if level == -1 else 'unknown'
    if max_capacity in (-1, -2) or max_capacity <= 0:
        return None, 'unknown'
    percent = max(0, min(100, round(level / float(max_capacity) * 100)))
    if percent <= 0:
        state = 'empty'
    elif percent <= 15:
        state = 'low'
    else:
        state = 'ok'
    return percent, state


def tray_percent(level, max_capacity):
    """Like supply_percent but never flags anything (tray state comes from prtInputStatus)."""
    percent, _ = supply_percent(level, max_capacity)
    return percent


def supply_colour(description):
    """Derive the supply colour from its description text, or None when unknown."""
    if not description:
        return None
    text = description.lower()
    for colour in SUPPLY_COLOURS:
        if colour in text:
            return colour
    return None


def finding_for_code(code, extra=None):
    """Build the standard finding dict for an error-state bit code."""
    for bit in ERROR_STATE_BITS:
        if bit['code'] == code:
            finding = {
                'code': code,
                'severity': bit['severity'],
                'title': bit['title'],
                'plain_message': bit['plain_message'],
                'what_to_do': bit['what_to_do'],
            }
            if extra:
                finding['plain_message'] = extra
            return finding
    return None


def merge_findings(findings):
    """
    De-duplicate findings by code (keeping the most severe, then the most
    informative message) and sort by severity: critical, warning, info.
    """
    merged = {}
    for finding in findings or []:
        code = finding.get('code') or finding.get('title') or 'unknown'
        existing = merged.get(code)
        if existing is None:
            merged[code] = dict(finding)
            continue
        # Keep the more severe occurrence.
        cur_rank = SEVERITY_ORDER.get(finding.get('severity'), 2)
        old_rank = SEVERITY_ORDER.get(existing.get('severity'), 2)
        if cur_rank < old_rank:
            merged[code] = dict(finding)
        elif cur_rank == old_rank:
            # Prefer the occurrence with a richer message / instructions.
            if (len(finding.get('plain_message') or '') + len(finding.get('what_to_do') or '')
                    > len(existing.get('plain_message') or '') + len(existing.get('what_to_do') or '')):
                merged[code] = dict(finding)
    return sorted(merged.values(), key=lambda f: SEVERITY_ORDER.get(f.get('severity'), 2))


def status_from_sources(hr_status, bits, findings):
    """
    Compute the human-facing printer status from hrPrinterStatus, the error
    bitmask and the merged findings. Never fabricates: unknown stays unknown.
    """
    critical_codes = {f['code'] for f in findings if f.get('severity') == 'critical'}
    warning_codes = {f['code'] for f in findings if f.get('severity') == 'warning'}

    if 'offline' in bits or 'offline' in critical_codes or 'offline' in warning_codes:
        return 'Offline'
    blocking = {'jammed', 'serviceRequested', 'noPaper', 'noToner',
                'markerSupplyMissing', 'inputTrayEmpty', 'overheat'}
    if bits & blocking or critical_codes & blocking:
        return 'Error'
    if bits or warning_codes:
        return 'Warning'
    return HR_PRINTER_STATUS_NAMES.get(hr_status, 'Unknown')


# ---------------------------------------------------------------------------
# SNMP value helpers
# ---------------------------------------------------------------------------

def _is_null_value(val):
    if val is None:
        return True
    name = type(val).__name__
    return name in ('Null', 'NoSuchObject', 'NoSuchInstance', 'EndOfMibView')


def to_int(val):
    if _is_null_value(val):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def to_bytes(val):
    if _is_null_value(val):
        return None
    try:
        if isinstance(val, (bytes, bytearray)):
            return bytes(val)
        return bytes(val)
    except (TypeError, ValueError):
        return None


def to_str(val):
    if _is_null_value(val):
        return None
    raw = to_bytes(val)
    if raw is None:
        try:
            return str(val)
        except Exception:
            return None
    return raw.replace(b'\x00', b'').decode('utf-8', errors='replace').strip()


def _column_and_row(oid_text, base_oid):
    """Split '1.3.6.1.2.1.43.11.1.1.9.1' into (column, row) relative to the table base."""
    prefix = base_oid + '.'
    if not oid_text.startswith(prefix):
        return None, None
    parts = oid_text[len(prefix):].split('.')
    try:
        row = int(parts[-1])
        column = int(parts[-2]) if len(parts) >= 2 else None
    except (ValueError, IndexError):
        return None, None
    return column, row


class SnmpUnreachableError(Exception):
    """Raised when the initial SNMP GET gets no response."""


def _error_status_is_failure(error_status):
    """pysnmp 7 returns 0 (int) from get_cmd but a pyasn1 Integer named
    'noError' from next_cmd/walk_cmd; both are truthy objects."""
    if not error_status:
        return False
    try:
        return int(error_status) != 0
    except (TypeError, ValueError):
        return str(error_status) not in ('noError', '0', '')


# ---------------------------------------------------------------------------
# SNMP scan (pysnmp 7.x asyncio API)
# ---------------------------------------------------------------------------

async def _walk_table(engine, auth, target, ctx, base_oid):
    """Walk a table subtree; returns [(oid_text, value), ...]."""
    rows = []
    try:
        async for error_indication, error_status, _error_index, var_binds in walk_cmd(
                engine, auth, target, ctx,
                ObjectType(ObjectIdentity(base_oid)),
                lookupMib=False, lexicographicMode=False):
            if error_indication or _error_status_is_failure(error_status):
                break
            for name, val in var_binds:
                rows.append((str(name), val))
    except Exception:
        pass
    return rows


async def _snmp_scan(ip, community, port, timeout, retries):
    """
    Perform a full SNMP scan. Raises SnmpUnreachableError when the printer
    does not answer the initial GET at all.
    """
    engine = SnmpEngine()
    target = await UdpTransportTarget.create((ip, port), timeout=timeout, retries=retries)
    auth = CommunityData(community)
    ctx = ContextData()

    # -- Core GET: any failure here means the printer cannot be reached ----
    error_indication, error_status, _error_index, var_binds = await get_cmd(
        engine, auth, target, ctx,
        ObjectType(ObjectIdentity(OID_SYS_DESCR)),
        ObjectType(ObjectIdentity(OID_SYS_NAME)),
        ObjectType(ObjectIdentity(OID_HR_PRINTER_STATUS)),
        ObjectType(ObjectIdentity(OID_HR_PRINTER_DETECTED_ERROR_STATE)),
        lookupMib=False,
    )
    if error_indication:
        raise SnmpUnreachableError(str(error_indication))
    if _error_status_is_failure(error_status):
        raise SnmpUnreachableError(f'SNMP error status {error_status}')

    core = {}
    for name, val in var_binds:
        core[str(name)] = val

    sys_descr = to_str(core.get(OID_SYS_DESCR))
    sys_name = to_str(core.get(OID_SYS_NAME))
    hr_status = to_int(core.get(OID_HR_PRINTER_STATUS))
    error_octets = to_bytes(core.get(OID_HR_PRINTER_DETECTED_ERROR_STATE))
    bits = active_error_bits(error_octets) if error_octets else []

    findings = [finding_for_code(code) for code in bits]
    findings = [f for f in findings if f]

    # -- Supplies table ------------------------------------------------------
    supplies = []
    supply_rows = {}
    for oid_text, val in await _walk_table(engine, auth, target, ctx, OID_PRT_MARKER_SUPPLIES_TABLE):
        column, row = _column_and_row(oid_text, OID_PRT_MARKER_SUPPLIES_TABLE)
        if column is None or row is None:
            continue
        entry = supply_rows.setdefault(row, {})
        if column == 6:
            entry['description'] = to_str(val)
        elif column == 8:
            entry['max'] = to_int(val)
        elif column == 9:
            entry['level'] = to_int(val)

    for row in sorted(supply_rows):
        entry = supply_rows[row]
        description = entry.get('description') or f'Supply {row}'
        colour = supply_colour(description)
        colour_known = colour is not None
        kind = 'ink' if 'ink' in description.lower() else 'toner'
        percent, state = supply_percent(entry.get('level'), entry.get('max'))
        supplies.append({
            'name': description,
            'colour': colour,
            'kind': kind,
            'percent': percent,
            'state': state,
        })
        if state == 'empty':
            label = f'{colour.title()} {kind}' if colour_known else f'Supply "{description}"'
            findings.append({
                'code': 'noToner', 'severity': 'critical',
                'title': f'{label} empty',
                'plain_message': f'The {label} supply is empty ({description}).',
                'what_to_do': f'Replace the {label} supply to resume printing.',
            })
        elif state == 'low':
            label = f'{colour.title()} {kind}' if colour_known else f'Supply "{description}"'
            findings.append({
                'code': 'lowToner', 'severity': 'warning',
                'title': f'{label} low',
                'plain_message': f'The {label} supply is low ({description}).',
                'what_to_do': f'Order a replacement {label} supply.',
            })

    # -- Input trays ----------------------------------------------------------
    trays = []
    tray_rows = {}
    for oid_text, val in await _walk_table(engine, auth, target, ctx, OID_PRT_INPUT_TABLE):
        column, row = _column_and_row(oid_text, OID_PRT_INPUT_TABLE)
        if column is None or row is None:
            continue
        entry = tray_rows.setdefault(row, {})
        if column == 9:
            entry['max'] = to_int(val)
        elif column == 10:
            entry['level'] = to_int(val)
        elif column == 11:
            entry['status'] = to_int(val)
        elif column == 13:
            entry['name'] = to_str(val)

    for row in sorted(tray_rows):
        entry = tray_rows[row]
        name = entry.get('name') or f'Tray {row}'
        status_code = entry.get('status')
        state = INPUT_STATUS_NAMES.get(status_code, 'unknown')
        percent = tray_percent(entry.get('level'), entry.get('max'))
        trays.append({'name': name, 'percent': percent, 'state': state})
        if state == 'empty':
            findings.append({
                'code': 'noPaper', 'severity': 'critical',
                'title': f'{name} empty',
                'plain_message': f'The paper tray "{name}" is empty.',
                'what_to_do': f'Load paper into {name}.',
            })
        elif state == 'low':
            findings.append({
                'code': 'lowPaper', 'severity': 'warning',
                'title': f'{name} low',
                'plain_message': f'The paper tray "{name}" is low on paper.',
                'what_to_do': f'Load more paper into {name} soon.',
            })

    # -- Alerts ----------------------------------------------------------------
    raw_alerts = {}
    for oid_text, val in await _walk_table(engine, auth, target, ctx, OID_PRT_ALERT_TABLE):
        column, row = _column_and_row(oid_text, OID_PRT_ALERT_TABLE)
        if column is None or row is None:
            continue
        alert = raw_alerts.setdefault(row, {})
        if column == 2:
            severity_code = to_int(val)
            alert['severity'] = ALERT_SEVERITY_NAMES.get(severity_code, 'info')
        elif column == 7:
            alert['code'] = to_int(val)
        elif column == 8:
            alert['description'] = to_str(val)

    for alert in raw_alerts.values():
        description = alert.get('description') or 'Printer reported an alert.'
        numeric_code = alert.get('code')
        mapped = KNOWN_ALERT_CODES.get(numeric_code)
        severity = alert.get('severity', 'info')

        # Overheating is real ONLY when the printer itself reports it.
        if numeric_code == 1002 or any(hint in description.lower() for hint in OVERHEAT_ALERT_HINTS):
            mapped = 'overheat'
            severity = 'critical' if severity == 'info' else severity

        if mapped and mapped != 'overheat':
            std = finding_for_code(mapped, extra=description)
            if std:
                if severity == 'critical':
                    std['severity'] = 'critical'
                findings.append(std)
                continue

        if mapped == 'overheat':
            findings.append({
                'code': 'overheat', 'severity': 'critical',
                'title': 'Fuser overheating (printer-reported)',
                'plain_message': description,
                'what_to_do': 'Power the printer off, check the cooling airflow and the fuser unit, '
                              'then contact service if the condition persists.',
            })
            continue

        findings.append({
            'code': f'alert-{numeric_code}' if numeric_code is not None else 'alert',
            'severity': severity,
            'title': 'Printer alert',
            'plain_message': description,
            'what_to_do': '',
        })

    # -- Covers -----------------------------------------------------------------
    covers = []
    for oid_text, val in await _walk_table(engine, auth, target, ctx, OID_PRT_COVER_TABLE):
        column, row = _column_and_row(oid_text, OID_PRT_COVER_TABLE)
        if column is None or row is None:
            continue
        cover = covers[-1] if covers and covers[-1].get('_row') == row else None
        if cover is None:
            cover = {'_row': row}
            covers.append(cover)
        if column == 2:
            cover['name'] = to_str(val)
        elif column == 3:
            status_code = to_int(val)
            cover['state'] = COVER_STATUS_NAMES.get(status_code, 'unknown')

    for cover in covers:
        cover.pop('_row', None)
        cover.setdefault('name', 'Cover')
        if cover.get('state') in ('open', 'interlock-open'):
            findings.append({
                'code': 'doorOpen', 'severity': 'warning',
                'title': f'{cover["name"]} open',
                'plain_message': f'The {cover["name"]} is open.',
                'what_to_do': f'Close the {cover["name"]}.',
            })

    # -- Total page count --------------------------------------------------------
    total_pages = None
    life_rows = await _walk_table(engine, auth, target, ctx, OID_PRT_MARKER_LIFE_COUNT)
    life_values = [to_int(val) for _oid, val in life_rows]
    life_values = [v for v in life_values if v is not None and v >= 0]
    if life_values:
        total_pages = sum(life_values)

    findings = merge_findings(findings)

    not_reported = []
    # These are simply not present in the standard Printer MIB.
    not_reported.append('fuser temperature')
    not_reported.append('drum wear')
    if not supplies:
        not_reported.append('toner/ink levels per colour')
    if not trays or all(t['percent'] is None for t in trays):
        not_reported.append('paper level per tray')
    if total_pages is None:
        not_reported.append('total page count')
    if not covers:
        not_reported.append('door/cover sensors')

    return {
        'reachable': True,
        'method': 'snmp',
        'model': sys_descr,
        'sysName': sys_name,
        'total_pages': total_pages,
        'status': status_from_sources(hr_status, set(bits), findings),
        'findings': findings,
        'supplies': supplies,
        'trays': trays,
        'covers': covers,
        'raw_alerts': raw_alerts,
        'not_reported': not_reported,
        'error': None,
        # True when hrPrinterDetectedErrorState was readable, so "no jam bit"
        # is an actual reading rather than an unknown.
        'error_state_read': error_octets is not None,
    }


# ---------------------------------------------------------------------------
# IPP fallback (hand-built Get-Printer-Attributes, no extra dependency)
# ---------------------------------------------------------------------------

IPP_REASON_MAP = [
    ('media-empty', 'noPaper', 'critical'),
    ('media-needed', 'noPaper', 'critical'),
    ('media-low', 'lowPaper', 'warning'),
    ('media-jam', 'jammed', 'critical'),
    ('toner-low', 'lowToner', 'warning'),
    ('ink-low', 'lowToner', 'warning'),
    ('marker-supply-low', 'lowToner', 'warning'),
    ('toner-empty', 'noToner', 'critical'),
    ('ink-empty', 'noToner', 'critical'),
    ('marker-supply-empty', 'noToner', 'critical'),
    ('marker-supply-missing', 'markerSupplyMissing', 'critical'),
    ('door-open', 'doorOpen', 'warning'),
    ('cover-open', 'doorOpen', 'warning'),
    ('interlock-open', 'doorOpen', 'warning'),
    ('offline', 'offline', 'warning'),
    ('service-requested', 'serviceRequested', 'critical'),
    ('input-tray-missing', 'inputTrayMissing', 'warning'),
    ('output-tray-missing', 'outputTrayMissing', 'warning'),
    ('output-area-almost-full', 'outputNearFull', 'warning'),
    ('output-area-full', 'outputFull', 'warning'),
    ('fuser-over-temp', 'overheat', 'critical'),
    ('over-temp', 'overheat', 'critical'),
]

IPP_STATE_NAMES = {3: 'Idle', 4: 'Printing', 5: 'Stopped'}


def _build_ipp_request(printer_uri):
    """Encode a minimal IPP 2.0 Get-Printer-Attributes request."""
    body = struct.pack('>BBHIB', 2, 0, 0x000B, 1, 0x01)  # version, op, request-id, group tag
    attributes = [
        (0x47, 'attributes-charset', 'utf-8'),
        (0x48, 'attributes-natural-language', 'en'),
        (0x45, 'printer-uri', printer_uri),
        (0x44, 'requested-attributes', 'all'),
    ]
    for tag, name, value in attributes:
        encoded_value = value.encode('utf-8')
        body += struct.pack('>BH', tag, len(name)) + name.encode('utf-8')
        body += struct.pack('>H', len(encoded_value)) + encoded_value
    body += struct.pack('>B', 0x03)  # end-of-attributes tag, empty data
    return body


def _decode_ipp_value(tag, value):
    if tag == 0x21:  # integer / enum
        return struct.unpack('>i', value)[0] if len(value) >= 4 else None
    if tag in (0x41, 0x44, 0x45, 0x47, 0x48):  # text / keyword / uri / charset / language
        return value.decode('utf-8', errors='replace')
    if tag == 0x4A:  # nameWithLanguage
        try:
            lang_len = struct.unpack('>H', value[:2])[0]
            return value[2 + lang_len:].decode('utf-8', errors='replace')
        except struct.error:
            return value.decode('utf-8', errors='replace')
    if tag == 0x22:  # boolean
        return bool(value)
    if tag == 0x30:  # octetString
        return value
    return value


def _parse_ipp_response(data):
    """Return (status_code, {attribute_name: [values]}) from an IPP response."""
    if len(data) < 8:
        raise ValueError('IPP response too short')
    status_code = struct.unpack('>H', data[2:4])[0]
    attributes = {}
    index = 8
    last_name = None
    while index < len(data):
        tag = data[index]
        index += 1
        if tag == 0x03:  # end-of-attributes
            break
        if tag in (0x01, 0x02, 0x04, 0x05):  # group tags
            last_name = None
            continue
        if tag >= 0x10:  # member attribute (additional 1setOf value)
            name = last_name
        else:
            name_len = struct.unpack('>H', data[index:index + 2])[0]
            index += 2
            name = data[index:index + name_len].decode('utf-8', errors='replace')
            index += name_len
            last_name = name
        value_len = struct.unpack('>H', data[index:index + 2])[0]
        index += 2
        value = data[index:index + value_len]
        index += value_len
        if name is None:
            continue
        attributes.setdefault(name, []).append(_decode_ipp_value(tag, value))
    return status_code, attributes


def _scan_via_ipp(ip, timeout):
    """Try TCP 631 and IPP Get-Printer-Attributes. Returns report dict or None."""
    try:
        sock = socket.create_connection((ip, 631), timeout=timeout)
    except OSError:
        return None

    with sock:
        sock.settimeout(timeout)
        request = _build_ipp_request(f'ipp://{ip}:631/ipp/print')
        http_body = (
            'POST /ipp/print HTTP/1.1\r\n'
            'Host: %s:631\r\n'
            'Content-Type: application/ipp\r\n'
            'Content-Length: %d\r\n'
            'Connection: close\r\n\r\n'
        ) % (ip, len(request))
        try:
            sock.sendall(http_body.encode('latin-1') + request)
            response = b''
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response += chunk
        except OSError:
            return None

    if not response:
        return None
    header_end = response.find(b'\r\n\r\n')
    if header_end < 0:
        return None
    status_line = response[:header_end].split(b'\r\n', 1)[0].decode('latin-1', 'replace')
    if ' 200 ' not in status_line:
        return None
    payload = response[header_end + 4:]
    try:
        _status_code, attributes = _parse_ipp_response(payload)
    except (ValueError, struct.error):
        return None

    # printer-state
    printer_state = None
    if attributes.get('printer-state'):
        try:
            printer_state = int(attributes['printer-state'][0])
        except (TypeError, ValueError):
            printer_state = None

    reasons = [str(r).lower() for r in attributes.get('printer-state-reasons', [])]
    findings = []
    for keyword, code, severity in IPP_REASON_MAP:
        if any(keyword == r or r.endswith('-' + keyword) for r in reasons):
            std = finding_for_code(code, extra=f'IPP reports: {keyword}')
            if std:
                std['severity'] = severity
                if code == 'overheat':
                    std['title'] = 'Fuser overheating (printer-reported)'
                    std['what_to_do'] = ('Power the printer off, check the cooling airflow and the '
                                         'fuser unit, then contact service if the condition persists.')
                findings.append(std)
    known = {keyword for keyword, _code, _sev in IPP_REASON_MAP}
    for reason in reasons:
        if reason and reason not in known and not any(reason.endswith('-' + k) for k in known):
            findings.append({
                'code': f'ipp-{reason}', 'severity': 'info',
                'title': 'Printer reports a condition',
                'plain_message': f'IPP printer-state-reason: {reason}',
                'what_to_do': '',
            })

    # supplies
    marker_levels = attributes.get('marker-levels', [])
    marker_names = attributes.get('marker-names', [])
    marker_colours = attributes.get('marker-colors', [])
    marker_types = attributes.get('marker-types', [])
    supplies = []
    for i in range(max(len(marker_levels), len(marker_names), len(marker_colours))):
        level = marker_levels[i] if i < len(marker_levels) else None
        try:
            percent = int(level) if level is not None else None
        except (TypeError, ValueError):
            percent = None
        if percent is not None and (percent < 0 or percent > 100):
            percent = None
        colour = str(marker_colours[i]) if i < len(marker_colours) and marker_colours[i] else None
        marker_type = str(marker_types[i]) if i < len(marker_types) and marker_types[i] else None
        kind = 'ink' if marker_type and marker_type.lower() == 'ink' else 'toner'
        name = str(marker_names[i]) if i < len(marker_names) and marker_names[i] else None
        supplies.append({
            'name': name or f'Supply {i + 1}',
            'colour': colour,
            'kind': kind,
            'percent': percent,
            'state': 'unknown' if percent is None else ('empty' if percent <= 0
                                                        else 'low' if percent <= 15 else 'ok'),
        })

    media_ready = [str(m) for m in attributes.get('media-ready', [])]
    trays = [{'name': m, 'percent': None, 'state': 'unknown'} for m in media_ready]

    total_pages = None
    if attributes.get('pages-printed'):
        try:
            total_pages = int(attributes['pages-printed'][0])
        except (TypeError, ValueError):
            total_pages = None

    findings = merge_findings(findings)

    not_reported = ['fuser temperature', 'drum wear']
    if not supplies:
        not_reported.append('toner/ink levels per colour')
    if not trays:
        not_reported.append('paper level per tray')
    if total_pages is None:
        not_reported.append('total page count')
    not_reported.append('door/cover sensors')

    status = IPP_STATE_NAMES.get(printer_state, 'Unknown')
    codes = {f['code'] for f in findings}
    if 'offline' in codes or 'offline' in reasons:
        status = 'Offline'
    elif codes & {'jammed', 'serviceRequested', 'noPaper', 'noToner',
                  'markerSupplyMissing', 'overheat'}:
        status = 'Error'
    elif codes:
        status = 'Warning'
    elif status == 'Unknown' and not reasons:
        status = 'Idle' if printer_state is None else status

    model = None
    if attributes.get('printer-make-and-model'):
        model = str(attributes['printer-make-and-model'][0])
    elif attributes.get('printer-name'):
        model = str(attributes['printer-name'][0])

    return {
        'reachable': True,
        'method': 'ipp',
        'model': model,
        'sysName': None,
        'total_pages': total_pages,
        'status': status,
        'findings': findings,
        'supplies': supplies,
        'trays': trays,
        'covers': [],
        'raw_alerts': [],
        'not_reported': not_reported,
        'error': None,
        'error_state_read': 'printer-state-reasons' in attributes,
    }


NO_SNMP_TROUBLESHOOTING = [
    'SNMP may be disabled on the printer - enable it in the printer\'s web admin page '
    '(usually under Network or Security settings).',
    'The community string may not be "public" - check the community configured on the printer.',
    'The printer may be on a different subnet, on guest Wi-Fi, or the access point may '
    'have client isolation enabled.',
    'The IP address may be wrong - verify it on the printer panel or its network configuration page.',
    'A firewall on the printer, the computer, or the router may be blocking UDP port 161.',
]


def scan_printer(ip, community='public', port=161, timeout=2, retries=1):
    """
    Scan one printer. Returns the structured report dict:

    { reachable, method, model, sysName, total_pages, status,
      findings: [{code, severity, title, plain_message, what_to_do}],
      supplies: [{name, colour, percent|null, state}],
      trays: [{name, percent|null, state}], covers: [...], raw_alerts: [...],
      not_reported: [...], error: null | {type, message, troubleshooting} }
    """
    try:
        report = asyncio.run(_snmp_scan(ip, community, port, timeout, retries))
    except SnmpUnreachableError:
        # Phase 2: IPP fallback over TCP 631.
        ipp_report = _scan_via_ipp(ip, timeout)
        if ipp_report is not None:
            return ipp_report
        return {
            'reachable': False,
            'method': None,
            'model': None,
            'sysName': None,
            'total_pages': None,
            'status': 'Offline',
            'findings': [],
            'supplies': [],
            'trays': [],
            'covers': [],
            'raw_alerts': [],
            'not_reported': [],
            'error_state_read': False,
            'error': {
                'type': 'no_snmp_response',
                'message': 'The printer did not answer SNMP queries on UDP port %d '
                           'and did not answer IPP on TCP port 631.' % port,
                'troubleshooting': NO_SNMP_TROUBLESHOOTING,
            },
        }
    except Exception as exc:
        return {
            'reachable': False,
            'method': None,
            'model': None,
            'sysName': None,
            'total_pages': None,
            'status': 'Offline',
            'findings': [],
            'supplies': [],
            'trays': [],
            'covers': [],
            'raw_alerts': [],
            'not_reported': [],
            'error_state_read': False,
            'error': {
                'type': 'scan_error',
                'message': f'Unexpected scan error: {exc}',
                'troubleshooting': NO_SNMP_TROUBLESHOOTING,
            },
        }
    return report
