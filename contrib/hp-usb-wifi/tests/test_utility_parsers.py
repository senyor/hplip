import ast
from pathlib import Path
import unittest

source = Path(__file__).resolve().parents[1] / 'hp_usb_wifi.py'
names = {'parse_usb_devices', 'extract_wifi_mac', 'http_response_complete'}
nodes = [n for n in ast.parse(source.read_text()).body
         if isinstance(n, ast.FunctionDef) and n.name in names]
functions = {}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), functions)


class UtilityParserTests(unittest.TestCase):
    def test_usb_discovery_and_deduplication(self):
        uri = 'hp:/usb/HP_LaserJet?serial=ABC'
        result = functions['parse_usb_devices'](uri + ' HP LaserJet\n' + uri)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['uri'], uri)
        self.assertEqual(result[0]['label'], 'HP LaserJet?serial=ABC')

    def test_multiple_printers_and_empty_discovery(self):
        self.assertEqual(functions['parse_usb_devices']('Found 0 printers.'), [])
        self.assertEqual(len(functions['parse_usb_devices'](
            'hp:/usb/Printer_A?serial=1\nhp:/usb/Printer_B?serial=2')), 2)

    def test_mac_normalization(self):
        for value in ('aabbccddeeff', 'aa:bb:cc:dd:ee:ff', 'aa-bb-cc-dd-ee-ff'):
            self.assertEqual(functions['extract_wifi_mac']({'dd:hardwareaddress': value}), 'AA:BB:CC:DD:EE:FF')

    def test_access_point_and_invalid_addresses_excluded(self):
        for values in ({'wifi:bssid': 'aa:bb:cc:dd:ee:ff'},
                       {'dd:macaddress': '000000000000'},
                       {'dd:macaddress': 'invalid'}):
            self.assertEqual(functions['extract_wifi_mac'](values), '')

    def test_partial_and_complete_http_response(self):
        prefix = b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n'
        self.assertFalse(functions['http_response_complete'](prefix + b'ab'))
        self.assertTrue(functions['http_response_complete'](prefix + b'abc'))

    def test_split_chunked_ending(self):
        data = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\na\r\n0\r\n\r\n'
        self.assertFalse(functions['http_response_complete'](data[:-1]))
        self.assertTrue(functions['http_response_complete'](data))


if __name__ == '__main__':
    unittest.main()
