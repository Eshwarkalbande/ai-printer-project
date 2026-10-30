"""Command-line SNMP/IPP scan of a single printer.

Usage:
    python scan_printer_cli.py <ip> [--community public] [--port 161]
                                  [--timeout 2] [--retries 1]

Prints the full scan report as indented JSON. Exit code 0 when the printer
answered, 1 otherwise.
"""

import argparse
import json
import sys

from snmp_scanner import scan_printer


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Scan a printer over SNMP (with IPP fallback) and print the report.')
    parser.add_argument('ip', help='Printer IP address (private/link-local only)')
    parser.add_argument('--community', default='public',
                        help='SNMP community string (default: public)')
    parser.add_argument('--port', type=int, default=161,
                        help='SNMP UDP port (default: 161)')
    parser.add_argument('--timeout', type=float, default=2.0,
                        help='SNMP timeout in seconds (default: 2)')
    parser.add_argument('--retries', type=int, default=1,
                        help='SNMP retries (default: 1)')
    args = parser.parse_args(argv)

    import ipaddress
    try:
        addr = ipaddress.ip_address(args.ip)
    except ValueError:
        print('error: invalid IP address', file=sys.stderr)
        return 2
    if addr.version != 4 or not (addr.is_private or addr.is_link_local or addr.is_loopback):
        print('error: only private or link-local IPv4 addresses may be scanned',
              file=sys.stderr)
        return 2

    report = scan_printer(args.ip, community=args.community, port=args.port,
                          timeout=args.timeout, retries=args.retries)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get('reachable') else 1


if __name__ == '__main__':
    sys.exit(main())
