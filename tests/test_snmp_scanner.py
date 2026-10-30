"""Unit tests for snmp_scanner pure parsers (no network required)."""

import pytest

import snmp_scanner as scanner


class TestErrorStateBitmask:
    def test_bit_zero_is_most_significant_bit_of_first_byte(self):
        # bit 0 (lowPaper) set alone -> 0x80 in the first octet.
        bits = scanner.parse_printer_error_state(b'\x80\x00')
        assert bits['lowPaper'] is True
        assert bits['noPaper'] is False
        assert bits['jammed'] is False

    def test_bit_five_jammed(self):
        # bit 5 of the first byte -> 0x04.
        bits = scanner.parse_printer_error_state(b'\x04')
        assert bits['jammed'] is True
        assert bits['lowToner'] is False

    def test_bit_seven_service_requested(self):
        # bit 7 -> 0x01 of first byte.
        bits = scanner.parse_printer_error_state(b'\x01')
        assert bits['serviceRequested'] is True
        assert bits['offline'] is False

    def test_bits_across_byte_boundary(self):
        # bit 12 (outputFull) -> first bit of the SECOND octet: 0x08.
        bits = scanner.parse_printer_error_state(b'\x00\x08')
        assert bits['outputFull'] is True
        assert bits['outputNearFull'] is False

    def test_hex_string_input(self):
        # 0x0080 -> second octet 0x80 -> bit 8 (inputTrayMissing).
        bits = scanner.parse_printer_error_state('0x0080')
        assert bits['inputTrayMissing'] is True
        assert bits['lowPaper'] is False
        # 0x0100 -> second octet 0x01 -> bit 7 (serviceRequested).
        bits = scanner.parse_printer_error_state('0x0100')
        assert bits['serviceRequested'] is True
        assert bits['inputTrayMissing'] is False

    def test_all_bits_set(self):
        bits = scanner.parse_printer_error_state(b'\xff\xfe')
        assert all(bits.values()) is True

    def test_empty_mask(self):
        bits = scanner.parse_printer_error_state(b'')
        assert all(value is False for value in bits.values())

    def test_active_error_bits(self):
        assert scanner.active_error_bits(b'\x84') == ['lowPaper', 'jammed']

    def test_invalid_hex_raises(self):
        with pytest.raises(ValueError):
            scanner.parse_printer_error_state('0xzz')

    def test_bit_count_covers_all_standard_bits(self):
        bits = scanner.parse_printer_error_state(b'\xff\xff\xff')
        assert len(bits) == 15
        assert set(bits) == {b['code'] for b in scanner.ERROR_STATE_BITS}


class TestSupplyPercent:
    def test_normal_percent(self):
        assert scanner.supply_percent(50, 100) == (50, 'ok')
        assert scanner.supply_percent(300, 1000) == (30, 'ok')

    def test_low_and_empty(self):
        assert scanner.supply_percent(15, 100) == (15, 'low')
        assert scanner.supply_percent(0, 100) == (0, 'empty')
        assert scanner.supply_percent(1, 100) == (1, 'low')

    def test_minus_one_is_other_not_zero(self):
        assert scanner.supply_percent(-1, 100) == (None, 'other')

    def test_minus_two_is_unknown_not_zero(self):
        assert scanner.supply_percent(-2, 100) == (None, 'unknown')

    def test_minus_three_is_some_remaining_not_zero(self):
        assert scanner.supply_percent(-3, 100) == (None, 'some-remaining')

    def test_unknown_max_capacity(self):
        assert scanner.supply_percent(50, -1) == (None, 'unknown')
        assert scanner.supply_percent(50, -2) == (None, 'unknown')
        assert scanner.supply_percent(50, 0) == (None, 'unknown')

    def test_none_inputs(self):
        assert scanner.supply_percent(None, 100) == (None, 'unknown')
        assert scanner.supply_percent(50, None) == (None, 'unknown')

    def test_percent_clamped(self):
        percent, _ = scanner.supply_percent(250, 100)
        assert percent == 100


class TestSupplyColour:
    def test_toner_cartridge(self):
        assert scanner.supply_colour('Black Toner Cartridge') == 'black'
        assert scanner.supply_colour('Cyan Toner Cartridge') == 'cyan'
        assert scanner.supply_colour('Magenta Toner') == 'magenta'
        assert scanner.supply_colour('Yellow toner') == 'yellow'

    def test_ink_cartridge(self):
        assert scanner.supply_colour('Black Ink Cartridge') == 'black'
        assert scanner.supply_colour('Photo Blue Ink') == 'blue'

    def test_unknown(self):
        assert scanner.supply_colour('Waste Toner Bin') is None
        assert scanner.supply_colour(None) is None
        assert scanner.supply_colour('') is None


class TestMergeFindings:
    def test_dedup_keeps_critical_over_warning(self):
        findings = [
            {'code': 'noPaper', 'severity': 'warning', 'title': 'Tray low',
             'plain_message': 'low', 'what_to_do': ''},
            {'code': 'noPaper', 'severity': 'critical', 'title': 'Out of paper',
             'plain_message': 'empty', 'what_to_do': 'Load paper'},
        ]
        merged = scanner.merge_findings(findings)
        assert len(merged) == 1
        assert merged[0]['severity'] == 'critical'

    def test_dedup_prefers_richer_message_at_equal_severity(self):
        findings = [
            {'code': 'jammed', 'severity': 'critical', 'title': 'Jam',
             'plain_message': 'jam', 'what_to_do': ''},
            {'code': 'jammed', 'severity': 'critical', 'title': 'Paper jam',
             'plain_message': 'A sheet is stuck in the duplex path.', 'what_to_do': 'Open rear cover.'},
        ]
        merged = scanner.merge_findings(findings)
        assert len(merged) == 1
        assert merged[0]['plain_message'] == 'A sheet is stuck in the duplex path.'

    def test_sort_by_severity(self):
        findings = [
            {'code': 'a', 'severity': 'info', 'title': 'i', 'plain_message': '', 'what_to_do': ''},
            {'code': 'b', 'severity': 'critical', 'title': 'c', 'plain_message': '', 'what_to_do': ''},
            {'code': 'c', 'severity': 'warning', 'title': 'w', 'plain_message': '', 'what_to_do': ''},
        ]
        merged = scanner.merge_findings(findings)
        assert [f['severity'] for f in merged] == ['critical', 'warning', 'info']

    def test_empty_and_none(self):
        assert scanner.merge_findings([]) == []
        assert scanner.merge_findings(None) == []

    def test_alert_codes_merge_with_error_bits(self):
        # Same underlying code ('noPaper') from two sources collapses to one finding.
        findings = [
            {'code': 'noPaper', 'severity': 'critical', 'title': 'Out of paper',
             'plain_message': 'bit', 'what_to_do': ''},
            {'code': 'noPaper', 'severity': 'critical', 'title': 'Out of paper',
             'plain_message': 'Input media supply empty (alert)', 'what_to_do': ''},
        ]
        assert len(scanner.merge_findings(findings)) == 1


class TestStatusFromSources:
    def test_idle_when_nothing_wrong(self):
        assert scanner.status_from_sources(3, set(), []) == 'Idle'

    def test_printing(self):
        assert scanner.status_from_sources(4, set(), []) == 'Printing'

    def test_unknown_stays_unknown(self):
        assert scanner.status_from_sources(2, set(), []) == 'Unknown'
        assert scanner.status_from_sources(None, set(), []) == 'Unknown'

    def test_offline_bit_wins(self):
        assert scanner.status_from_sources(3, {'offline'}, []) == 'Offline'

    def test_jam_is_error(self):
        assert scanner.status_from_sources(3, {'jammed'}, []) == 'Error'

    def test_warning_bits(self):
        assert scanner.status_from_sources(3, {'lowPaper'}, []) == 'Warning'

    def test_critical_finding_is_error(self):
        findings = [{'code': 'noToner', 'severity': 'critical', 'title': 'x',
                     'plain_message': '', 'what_to_do': ''}]
        assert scanner.status_from_sources(3, set(), findings) == 'Error'
