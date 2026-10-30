"""End-to-end scan test against the in-process fake SNMP agent."""

import pytest
from pysnmp.proto import rfc1902

from fake_snmp_agent import start_fake_agent
import snmp_scanner as scanner


def _octets(value):
    return rfc1902.OctetString(value)


def _tree():
    """
    A broken printer: low toner + paper jam error bits, magenta empty,
    cyan "some remaining", tray 2 empty, a printer-reported fuser
    over-temperature alert and a low-paper alert.
    """
    return {
        '1.3.6.1.2.1.1.1.0': _octets('HP LaserJet Enterprise M608 (fake agent)'),
        '1.3.6.1.2.1.1.5.0': _octets('PRN-FAKE'),
        '1.3.6.1.2.1.25.3.5.1.1.0': rfc1902.Integer32(4),   # printing
        '1.3.6.1.2.1.25.3.5.1.2.0': _octets(b'\x24'),       # lowToner + jammed
        # Supplies: black 120/300, cyan -3, magenta 0/100, yellow 45/100
        '1.3.6.1.2.1.43.11.1.1.6.1': _octets('Black Toner Cartridge'),
        '1.3.6.1.2.1.43.11.1.1.8.1': rfc1902.Integer32(300),
        '1.3.6.1.2.1.43.11.1.1.9.1': rfc1902.Integer32(120),
        '1.3.6.1.2.1.43.11.1.1.6.2': _octets('Cyan Toner Cartridge'),
        '1.3.6.1.2.1.43.11.1.1.8.2': rfc1902.Integer32(100),
        '1.3.6.1.2.1.43.11.1.1.9.2': rfc1902.Integer32(-3),
        '1.3.6.1.2.1.43.11.1.1.6.3': _octets('Magenta Toner Cartridge'),
        '1.3.6.1.2.1.43.11.1.1.8.3': rfc1902.Integer32(100),
        '1.3.6.1.2.1.43.11.1.1.9.3': rfc1902.Integer32(0),
        '1.3.6.1.2.1.43.11.1.1.6.4': _octets('Yellow Toner Cartridge'),
        '1.3.6.1.2.1.43.11.1.1.8.4': rfc1902.Integer32(100),
        '1.3.6.1.2.1.43.11.1.1.9.4': rfc1902.Integer32(45),
        # Input trays: Tray 1 62/250 available, Tray 2 0/250 empty
        '1.3.6.1.2.1.43.8.2.1.9.1': rfc1902.Integer32(250),
        '1.3.6.1.2.1.43.8.2.1.10.1': rfc1902.Integer32(62),
        '1.3.6.1.2.1.43.8.2.1.11.1': rfc1902.Integer32(3),
        '1.3.6.1.2.1.43.8.2.1.13.1': _octets('Tray 1'),
        '1.3.6.1.2.1.43.8.2.1.9.2': rfc1902.Integer32(250),
        '1.3.6.1.2.1.43.8.2.1.10.2': rfc1902.Integer32(0),
        '1.3.6.1.2.1.43.8.2.1.11.2': rfc1902.Integer32(5),
        '1.3.6.1.2.1.43.8.2.1.13.2': _octets('Tray 2'),
        # Alerts: fuser over-temperature (critical), input media supply low (warning)
        '1.3.6.1.2.1.43.18.1.1.2.1': rfc1902.Integer32(3),
        '1.3.6.1.2.1.43.18.1.1.7.1': rfc1902.Integer32(1002),
        '1.3.6.1.2.1.43.18.1.1.8.1': _octets('Fuser over temperature detected'),
        '1.3.6.1.2.1.43.18.1.1.2.2': rfc1902.Integer32(4),
        '1.3.6.1.2.1.43.18.1.1.7.2': rfc1902.Integer32(807),
        '1.3.6.1.2.1.43.18.1.1.8.2': _octets('Input media supply low'),
        # Cover: front cover closed
        '1.3.6.1.2.1.43.6.1.1.2.1': _octets('Front Cover'),
        '1.3.6.1.2.1.43.6.1.1.3.1': rfc1902.Integer32(3),
        # Total page count
        '1.3.6.1.2.1.43.10.2.1.4.1': rfc1902.Counter32(45210),
    }


@pytest.fixture(scope='module')
def agent_port():
    return start_fake_agent(_tree())


def test_full_scan_via_snmp(agent_port):
    report = scanner.scan_printer('127.0.0.1', port=agent_port, timeout=2, retries=1)

    assert report['error'] is None
    assert report['reachable'] is True
    assert report['method'] == 'snmp'
    assert report['model'] == 'HP LaserJet Enterprise M608 (fake agent)'
    assert report['sysName'] == 'PRN-FAKE'
    assert report['total_pages'] == 45210
    assert report['status'] == 'Error'

    codes = {f['code'] for f in report['findings']}
    assert 'jammed' in codes
    assert 'lowToner' in codes
    assert 'noToner' in codes          # magenta empty
    assert 'noPaper' in codes          # tray 2 empty
    assert 'overheat' in codes         # only because the printer reported it
    assert 'lowPaper' in codes         # from alert 807
    assert 'fuserUnderTemp' not in codes

    # Overheat must be printer-reported, not invented.
    overheat = next(f for f in report['findings'] if f['code'] == 'overheat')
    assert overheat['severity'] == 'critical'
    assert 'Fuser over temperature detected' in overheat['plain_message']

    # Findings are sorted critical-first and de-duplicated.
    severities = [f['severity'] for f in report['findings']]
    assert severities == sorted(severities, key=lambda s: {'critical': 0, 'warning': 1, 'info': 2}[s])
    assert len(report['findings']) == len(codes)

    supplies = {s['colour']: s for s in report['supplies']}
    assert supplies['black']['percent'] == 40
    assert supplies['black']['state'] == 'ok'
    assert supplies['cyan']['percent'] is None
    assert supplies['cyan']['state'] == 'some-remaining'
    assert supplies['magenta']['percent'] == 0
    assert supplies['magenta']['state'] == 'empty'
    assert supplies['yellow']['percent'] == 45

    trays = {t['name']: t for t in report['trays']}
    assert trays['Tray 1']['percent'] == 25
    assert trays['Tray 1']['state'] == 'available'
    assert trays['Tray 2']['state'] == 'empty'

    assert len(report['covers']) == 1
    assert report['covers'][0]['state'] == 'closed'
    assert len(report['raw_alerts']) == 2

    assert 'fuser temperature' in report['not_reported']
    assert 'drum wear' in report['not_reported']
    assert 'total page count' not in report['not_reported']


def test_unreachable_printer_reports_no_snmp_response():
    # Nothing listens on this local port -> SNMP timeout, no IPP either.
    report = scanner.scan_printer('127.0.0.1', port=16123, timeout=1, retries=0)
    assert report['reachable'] is False
    assert report['status'] == 'Offline'
    assert report['method'] is None
    assert report['error'] is not None
    assert report['error']['type'] == 'no_snmp_response'
    assert isinstance(report['error']['troubleshooting'], list)
    assert len(report['error']['troubleshooting']) >= 4
    # Troubleshooting must cover the known causes without guessing one.
    text = ' '.join(report['error']['troubleshooting'])
    for hint in ('SNMP may be disabled', 'community', 'subnet', 'IP address', 'firewall'):
        assert hint in text


def test_inkjet_supplies_label(agent_port):
    # The same agent but with an inkjet description: colour + kind derivation
    # must still work (covered by unit tests; here we check the walk columns
    # produce an ink supply entry).
    report = scanner.scan_printer('127.0.0.1', port=agent_port, timeout=2, retries=1)
    assert all(s['kind'] in ('ink', 'toner') for s in report['supplies'])
    assert report['supplies'][0]['kind'] == 'toner'
