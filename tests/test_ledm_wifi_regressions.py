"""Hardware-independent regression checks for the upstream LEDM Wi-Fi patch."""
import ast
import binascii
import io
from pathlib import Path
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'base' / 'LedmWifi.py'


def load_functions():
    # HPLIP normally imports compiled USB/CUPS modules. Load the actual patched
    # functions with fake transport dependencies so no printer is needed.
    names = {'_http_response_complete', '_read_marvell_response', 'associate',
             'passPhraseXml', 'keyInfoXml', 'LEDM_WIFI_BASE_URI', 'HTTP_OK',
             'HTTP_NOCONTENT', 'HTTP_ERROR'}
    nodes = []
    for node in ast.parse(SOURCE.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            nodes.append(node)
    ns = dict(time=time, binascii=binascii,
              to_bytes_utf8=lambda s: s.encode('utf-8'), Error=RuntimeError,
              ERROR_DEVICE_IO_ERROR='USB response timed out')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns


class FramingTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_functions()
        self.complete = self.ns['_http_response_complete']

    def test_content_length(self):
        prefix = b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n'
        self.assertFalse(self.complete(prefix + b'ab'))
        self.assertTrue(self.complete(prefix + b'abc'))

    def test_empty_response(self):
        self.assertTrue(self.complete(b'HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n'))

    def test_chunked_split_terminator(self):
        data = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n'
        self.assertFalse(self.complete(data[:-1]))
        self.assertTrue(self.complete(data))

    def test_chunk_extensions_and_trailers(self):
        self.assertTrue(self.complete(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3;x=y\r\nabc\r\n0\r\nX-Test: yes\r\n\r\n'))

    def test_malformed_and_unframed(self):
        for data in (b'', b'HTTP/1.1 200 OK\r\n\r\nabc',
                     b'HTTP/1.1 200 OK\r\nContent-Length: bad\r\n\r\n',
                     b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n'):
            self.assertFalse(self.complete(data))

    def test_no_read_after_complete_response(self):
        class Device:
            calls = 0
            def readMarvell_EWS(self, size, stream, timeout):
                self.calls += 1
                if self.calls > 2:
                    raise AssertionError('Extra read would block on the real printer')
                data = (b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\na', b'bc')[self.calls - 1]
                stream.write(data)
                return len(data)
        dev, stream = Device(), io.BytesIO()
        self.ns['_read_marvell_response'](dev, stream)
        self.assertEqual(dev.calls, 2)

    def test_zero_read_ends_unframed_response(self):
        class Device:
            def readMarvell_EWS(self, *args):
                return 0
        self.ns['_read_marvell_response'](Device(), io.BytesIO())

    def test_overall_deadline(self):
        class Clock:
            def __init__(self):
                self.t = 0
            def monotonic(self):
                self.t += 2
                return self.t
        self.ns['time'] = Clock()
        with self.assertRaises(RuntimeError):
            self.ns['_read_marvell_response'](object(), io.BytesIO(), timeout=1)


class AssociationTests(unittest.TestCase):
    def attempt(self, codes, security):
        ns = load_functions()
        payloads, errors = [], []
        ns['log'] = type('Log', (), {'error': lambda _, text: errors.append(text)})()
        def write(dev, uri, xml, timeout):
            payloads.append(xml)
            return codes[len(payloads) - 1]
        ns['writeXmlDataToURI'] = write
        result = ns['associate'](object(), 'wifi0', 'test-network',
                                 'infrastructure', security, 'test-password')
        return result, payloads, errors

    def test_wpa2_400_retries_personal_profile(self):
        result, payloads, _ = self.attempt([400, 200], 'WPA2')
        self.assertEqual(result['errorreturn'], 200)
        self.assertEqual(len(payloads), 2)
        self.assertIn('<wifi:EncryptionType>WPA_PSK</wifi:EncryptionType>', payloads[1])
        self.assertIn('<wifi:RsnAuthorization>autoWPA</wifi:RsnAuthorization>', payloads[1])
        self.assertIn('<wifi:RsnEncryption>AESOrTKIP</wifi:RsnEncryption>', payloads[1])
        self.assertNotIn('test-password', payloads[1])

    def test_success_does_not_retry(self):
        _, payloads, errors = self.attempt([204], 'WPA2')
        self.assertEqual(len(payloads), 1)
        self.assertEqual(errors, [])

    def test_other_errors_do_not_retry(self):
        result, payloads, errors = self.attempt([500], 'WPA2')
        self.assertEqual(result['errorreturn'], 500)
        self.assertEqual(len(payloads), 1)
        self.assertTrue(errors)

    def test_fallback_does_not_recurse_indefinitely(self):
        result, payloads, errors = self.attempt([400, 400], 'WPA2')
        self.assertEqual(result['errorreturn'], 400)
        self.assertEqual(len(payloads), 2)
        self.assertTrue(errors)

    def test_open_network_does_not_retry(self):
        _, payloads, _ = self.attempt([400], 'none')
        self.assertEqual(len(payloads), 1)


if __name__ == '__main__':
    unittest.main()
