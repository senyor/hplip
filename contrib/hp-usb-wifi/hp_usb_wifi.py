#!/usr/bin/env python3
"""Responsive USB Wi-Fi setup using Debian HPLIP's internal LEDM API."""
import multiprocessing as mp
import os
import time
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

def parse_usb_devices(output):
    import re
    from urllib.parse import unquote
    uris = list(dict.fromkeys(re.findall(r'hp:/usb/[^\s]+', output)))
    return [{'uri': uri, 'label': unquote(uri[len('hp:/usb/'):]).replace('_', ' ')} for uri in uris]


def http_response_complete(data):
    """Recognize a fully framed HTTP response without exposing its payload."""
    header, sep, body = data.partition(b'\r\n\r\n')
    if not sep:
        return False
    headers = {}
    for line in header.split(b'\r\n')[1:]:
        key, colon, value = line.partition(b':')
        if colon:
            headers[key.lower().strip()] = value.strip().lower()
    if b'chunked' in headers.get(b'transfer-encoding', b''):
        offset = 0
        try:
            while True:
                end = body.find(b'\r\n', offset)
                if end < 0:
                    return False
                size = int(body[offset:end].split(b';')[0], 16)
                offset = end + 2
                if size == 0:
                    return (body[offset:offset+2] == b'\r\n' or
                            b'\r\n\r\n' in body[offset:])
                if len(body) < offset + size + 2:
                    return False
                if body[offset+size:offset+size+2] != b'\r\n':
                    return False
                offset += size + 2
        except ValueError:
            return False
    if b'content-length' in headers:
        try:
            return len(body) >= int(headers[b'content-length'])
        except ValueError:
            return False
    return False


def extract_wifi_mac(params):
    """Accept only explicit adapter MAC fields, never an access-point BSSID."""
    import re
    for key, value in (params or {}).items():
        tag = re.sub(r'-\d+$', '', str(key).split(':')[-1]).lower()
        if tag not in ('macaddress', 'hardwareaddress', 'stationaddress', 'physicaladdress'):
            continue
        raw = re.sub(r'[:.\-\s]', '', str(value))
        if re.fullmatch(r'[0-9a-fA-F]{12}', raw) and raw != '000000000000':
            return ':'.join(raw[i:i+2] for i in range(0, 12, 2)).upper()
    return ''


def worker(conn, action, args):
    # HPLIP debug output can contain credentials. Never forward it to the UI.
    import sys
    sys.stdout = sys.stderr = open(os.devnull, 'w')
    os.setsid()
    dev = None
    def debug(text):
        conn.send(('debug', text))
    debug(f'Python: {sys.executable} ({sys.version.split()[0]}); action: {action}')
    try:
        if action == 'discover':
            import subprocess
            conn.send(('progress', 'Finding USB printers…'))
            result = subprocess.run(
                ['/usr/bin/hp-probe', '-b', 'usb'], capture_output=True,
                text=True, timeout=25)
            if result.returncode:
                raise RuntimeError('USB discovery failed.')
            conn.send(('result', parse_usb_devices(result.stdout)))
            return
        sys.path.insert(0, '/usr/share/hplip')
        from base import device, LedmWifi
        debug('Loading HPLIP from /usr/share/hplip')
        debug('Creating USB device')
        dev = device.Device(args['uri'])
        # Trace only method names and timings, never arguments or USB payloads.
        def traced(name, original):
            def call(*a, **kw):
                stream = a[1] if len(a) > 1 else kw.get('stream')
                if name == 'readMarvell_EWS' and stream is not None:
                    if http_response_complete(stream.getvalue()):
                        debug('USB response is complete according to HTTP framing; ending read loop')
                        return 0
                began = time.monotonic()
                debug(f'USB {name}: starting')
                try:
                    result = original(*a, **kw)
                    detail = ''
                    if isinstance(result, int):
                        detail = f'; return={result}'
                    if name == 'readMarvell_EWS' and stream is not None:
                        detail += f'; buffered={len(stream.getvalue())} bytes'
                    debug(f'USB {name}: completed in {time.monotonic() - began:.2f}s{detail}')
                    return result
                except BaseException:
                    debug(f'USB {name}: failed in {time.monotonic() - began:.2f}s')
                    raise
            return call
        for name in ('openLEDM', 'closeLEDM', 'openEWS_LEDM', 'closeEWS_LEDM',
                     'openMarvell_EWS', 'closeMarvell_EWS', 'writeLEDM',
                     'writeEWS_LEDM', 'writeMarvell_EWS', 'readLEDMData',
                     'readLEDMAllData', 'readMarvell_EWS'):
            original = getattr(dev, name, None)
            if original is not None:
                setattr(dev, name, traced(name, original))
        if action == 'scan':
            conn.send(('progress', 'Reading wireless adapters…'))
            adapters = LedmWifi.getWifiAdaptorID(dev)
            if not adapters:
                raise RuntimeError('Printer returned no wireless adapters.')
            conn.send(('progress', 'Enabling wireless radio and scanning…'))
            _, name, _, _ = LedmWifi.setAdaptorPower(dev, adapters)
            if not name:
                raise RuntimeError('Could not enable a wireless adapter.')
            found = LedmWifi.performScan(dev, name)
            networks = []
            for i in range(int(found.get('numberofscanentries', 0))):
                row = {k: found.get(f'{k}-{i}', '') for k in
                       ('ssid', 'communicationmode', 'encryptiontype', 'dbm')}
                if all(row[k] for k in ('ssid', 'communicationmode', 'encryptiontype')):
                    networks.append(row)
            conn.send(('result', {'adapter': name, 'networks': networks}))
        elif action == 'connect':
            conn.send(('progress', 'Sending wireless settings…'))
            n = args['network']
            debug(f"Selected security: {n['encryptiontype']}; communication mode: {n['communicationmode']}")
            debug('Reading active profile for supported security field values…')
            profile, profile_code, _ = LedmWifi.readXmlDataFromURI(
                dev, LedmWifi.LEDM_WIFI_BASE_URI + args['adapter'] + '/Profiles/Active',
                '<io:Profile', '<io:Profile', timeout=10)
            debug(f'Active profile response: HTTP {profile_code}')
            # Strict whitelist: do not expose keys, passphrases, or SSIDs.
            allowed = {'communicationmode', 'encryptiontype', 'authenticationmode',
                       'rsnencryption', 'rsnauthorization'}
            import re
            for field, value in (profile or {}).items():
                tag = re.sub(r'-\d+$', '', field.split(':')[-1]).lower()
                if tag in allowed:
                    debug(f'Active profile {tag}: {value}')
            result = LedmWifi.associate(dev, args['adapter'], n['ssid'],
                                       n['communicationmode'], n['encryptiontype'], args['password'])
            code = result.get('errorreturn')
            # Check explicitly: HPLIP 3.22.10 has an incorrect boolean check here.
            debug(f'Wi-Fi settings response: HTTP {code}')
            if code == 400 and n['encryptiontype'].upper() in ('WPA', 'WPA2'):
                debug('Trying legacy personal-security profile value WPA_PSK (AESOrTKIP / autoWPA).')
                conn.send(('progress', 'Retrying with legacy WPA personal profile encoding…'))
                result = LedmWifi.associate(dev, args['adapter'], n['ssid'],
                                           n['communicationmode'], 'WPA_PSK', args['password'])
                code = result.get('errorreturn')
                debug(f'Legacy personal-security response: HTTP {code}')
            if code not in (200, 201, 202, 204):
                conn.send(('error', f'Printer rejected Wi-Fi settings: HTTP {code}.'))
                return
            conn.send(('result', 'Settings accepted. Wait about 30 seconds, then click Check IP.'))
        elif action == 'status':
            conn.send(('progress', 'Reading wireless adapter information…'))
            adapter = args['adapter']
            entries, code = LedmWifi.readXmlTagDataFromURI(
                dev, '/IoMgmt/Adapters', '<io:Adapters', '<io:Adapter>', timeout=10)
            params = {}
            for entry in entries:
                name = entry.get('io:adapter-io:hardwareconfig-dd:name', '')
                kind = entry.get('io:adapter-io:hardwareconfig-dd:deviceconnectivityporttype', '').lower()
                if name == adapter or (not name and kind in ('wifiembedded', 'wifiaccessory')):
                    params = entry
                    break
            mac = extract_wifi_mac(params) if code == 200 else ''
            conn.send(('info', {'mac': mac}))
            if not mac:
                debug(f'Wi-Fi MAC not reported in adapter response (HTTP {code}).')
                # Log field names only, so unsupported models can be diagnosed safely.
                if params:
                    debug('Adapter response fields: ' + ', '.join(params.keys()))
            conn.send(('progress', 'Reading printer hostname…'))
            hostname = LedmWifi.getHostname(dev)
            if not hostname or not mac:
                dynamic, dynamic_code, _ = LedmWifi.readXmlDataFromURI(
                    dev, '/DevMgmt/IOConfigDyn.xml', '<iocfgdyn2:IOConfigDyn',
                    '<dd3:IOAdaptorConfig', timeout=10)
                if dynamic_code == 200:
                    import re
                    for key, value in dynamic.items():
                        tag = re.sub(r'-\d+$', '', key.split(':')[-1]).lower()
                        if tag in ('currenthostname', 'hostname') and value:
                            hostname = str(value)
                            break
                    current_key = next((k for k in dynamic if k.endswith(':currenthostname')), None)
                    if current_key and dynamic[current_key]:
                        hostname = str(dynamic[current_key])
                    if not mac:
                        mac = extract_wifi_mac(dynamic)
                        conn.send(('info', {'mac': mac}))
                    debug('Dynamic information read: hostname ' + ('available' if hostname else 'not reported') +
                          '; hardware address ' + ('available' if mac else 'not reported'))
            conn.send(('info', {'hostname': hostname or ''}))
            conn.send(('progress', 'Reading IP configuration…'))
            result = LedmWifi.getIPConfiguration(dev, adapter)
            ip = result[0] if result else ''
            # Some LaserJets expose their current address in the active profile.
            if not ip or ip == '0.0.0.0':
                profile, code, _ = LedmWifi.readXmlDataFromURI(
                    dev, LedmWifi.LEDM_WIFI_BASE_URI + adapter + '/Profiles/Active',
                    '<io:Profile', '<io:Profile', timeout=10)
                if code == 200:
                    ip = profile.get('io:profile-io:networkprofile-io:ipv4network-dd:ipaddress', ip)
            conn.send(('info', {'ip': ip if ip and ip != '0.0.0.0' else ''}))
            conn.send(('result', 'Printer information refreshed. A blank IP means no address was reported; retry after connecting.'))
    except ModuleNotFoundError as exc:
        name = exc.name or 'unknown'
        conn.send(('error', f"Missing Python module: {name}. Run this app with /usr/bin/python3. "
                   "Install Debian packages hplip, python3-tk and python3-dbus if needed."))
    except BaseException as exc:
        import traceback
        debug(f'Exception type: {type(exc).__name__}')
        for frame in traceback.extract_tb(exc.__traceback__):
            debug(f'  {frame.filename}:{frame.lineno} in {frame.name}')
        # Do not include exception text: transport errors may echo request payloads.
        conn.send(('error', f'{type(exc).__name__}: operation failed. No credentials were logged.'))
    finally:
        # Report results before cleanup; parent also bounds cleanup time.
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass
        conn.close()


class App:
    def __init__(self, root):
        self.root = root
        self.proc = self.pipe = None
        self.adapter = None
        self.networks = []
        self.scanned_uri = None
        root.title('HP USB Wi-Fi Setup')
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        width, height = screen_width // 2, int(screen_height * 0.9)
        x, y = (screen_width - width) // 2, (screen_height - height) // 2
        root.geometry(f'{width}x{height}+{x}+{y}')
        box = ttk.Frame(root, padding=18)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='HP printer Wi-Fi setup', font=('', 17, 'bold')).pack(anchor='w')
        ttk.Label(box, text='Keep USB connected. Close hp-setup and other HP windows.').pack(anchor='w', pady=(4, 14))
        ttk.Label(box, text='USB printer').pack(anchor='w')
        usb_row = ttk.Frame(box)
        usb_row.pack(fill='x', pady=(3, 10))
        self.usb_devices = []
        self.uri = tk.StringVar()
        self.printers = ttk.Combobox(usb_row, state='readonly')
        self.printers.pack(side='left', fill='x', expand=True)
        self.printers.bind('<<ComboboxSelected>>', self.select_printer)
        self.refresh = ttk.Button(usb_row, text='Refresh USB', command=self.discover)
        self.refresh.pack(side='right', padx=(8, 0))
        self.scan = ttk.Button(box, text='Scan Wi-Fi networks', command=self.do_scan)
        self.scan.pack(anchor='w')
        self.listbox = tk.Listbox(box, height=7, exportselection=False)
        self.listbox.pack(fill='both', expand=True, pady=10)
        ttk.Label(box, text='Wi-Fi password').pack(anchor='w')
        self.password = tk.StringVar()
        password_row = ttk.Frame(box)
        password_row.pack(fill='x', pady=(3, 10))
        self.password_entry = ttk.Entry(password_row, textvariable=self.password, show='•')
        self.password_entry.pack(side='left', fill='x', expand=True)
        self.password_visible = False
        self.password_toggle = ttk.Button(
            password_row, text='◉ Show', width=9, command=self.toggle_password)
        self.password_toggle.pack(side='right', padx=(8, 0))
        row = ttk.Frame(box)
        row.pack(fill='x')
        self.connect = ttk.Button(row, text='Connect selected network', command=self.do_connect)
        self.connect.pack(side='left')
        self.check = ttk.Button(row, text='Check IP / Info', command=self.do_status)
        self.check.pack(side='left', padx=8)
        self.cancel = ttk.Button(row, text='Cancel operation', command=self.stop, state='disabled')
        self.cancel.pack(side='right')
        info = ttk.LabelFrame(box, text='Printer information', padding=8)
        info.pack(fill='x', pady=(12, 0))
        info.columnconfigure(1, weight=1)
        self.info_vars = {}
        for index, (key, label) in enumerate((('mac', 'Wi-Fi MAC'), ('hostname', 'Hostname'), ('ip', 'IP address'))):
            variable = tk.StringVar()
            self.info_vars[key] = variable
            ttk.Label(info, text=label).grid(row=index, column=0, sticky='w', padx=(0, 10), pady=2)
            ttk.Entry(info, textvariable=variable, state='readonly').grid(row=index, column=1, sticky='ew', pady=2)
            ttk.Button(info, text='Copy', command=lambda k=key: self.copy_info(k)).grid(row=index, column=2, padx=(8, 0), pady=2)
        self.status = tk.StringVar(value='Ready. Scan to discover the actual adapter and security settings.')
        ttk.Label(box, textvariable=self.status, wraplength=650).pack(anchor='w', pady=(14, 0))
        ttk.Button(box, text='Copy message', command=self.copy_message).pack(anchor='w', pady=(5, 0))
        ttk.Label(box, text='A timeout stops the request; it does not undo settings already sent.', wraplength=650).pack(anchor='w', pady=(5, 0))
        debug_row = ttk.Frame(box)
        debug_row.pack(fill='x', pady=(12, 4))
        ttk.Label(debug_row, text='Debug log').pack(side='left')
        ttk.Button(debug_row, text='Copy debug', command=self.copy_debug).pack(side='right')
        self.debug_text = ScrolledText(box, height=12, wrap='word', state='disabled')
        self.debug_text.pack(fill='both', expand=True)
        self.status.trace_add('write', lambda *_: self.log_debug(self.status.get()))
        self.log_debug('Debug enabled. USB payloads and passwords are excluded.')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(200, self.discover)

    def start(self, action, **args):
        if self.proc:
            return
        self.action = action
        self.pipe, child = mp.get_context('spawn').Pipe(duplex=False)
        self.proc = mp.get_context('spawn').Process(target=worker, args=(child, action, args))
        self.proc.start()
        child.close()
        self.deadline = time.monotonic() + 90
        for b in (self.scan, self.connect, self.check, self.refresh):
            b.configure(state='disabled')
        self.printers.configure(state='disabled')
        self.cancel.configure(state='normal')
        self.status.set('Starting…')
        self.root.after(100, self.poll)

    def poll(self):
        if not self.proc:
            return
        try:
            while self.pipe.poll():
                kind, value = self.pipe.recv()
                if kind == 'info':
                    for key, text in value.items():
                        self.info_vars[key].set(text)
                elif kind == 'debug':
                    self.log_debug(value)
                elif kind == 'progress':
                    self.status.set(value)
                else:
                    if kind == 'error':
                        self.status.set(value)
                    elif self.action == 'discover':
                        self.usb_devices = value
                        self.printers.configure(values=[d['label'] for d in value])
                        if value:
                            self.printers.current(0)
                            self.select_printer()
                        self.status.set(f'Found {len(value)} USB printer(s). Select a printer, then scan Wi-Fi.' if value else 'No HP USB printers found. Check USB and click Refresh USB.')
                    elif self.action == 'scan':
                        self.adapter = value['adapter']
                        self.scanned_uri = self.pending_uri
                        self.networks = value['networks']
                        self.listbox.delete(0, 'end')
                        for n in self.networks:
                            self.listbox.insert('end', f"{n['ssid']}   [{n['encryptiontype']}]   {n['dbm']} dBm")
                        self.status.set(f'Adapter: {self.adapter}. Found {len(self.networks)} networks.')
                        self.root.after(300, self.do_status)
                    elif self.action == 'status':
                        self.status.set(value)
                    else:
                        self.status.set(value)
                    self.stop(keep_status=True)
                    return
        except (EOFError, OSError):
            self.status.set('Worker exited without a result.')
            self.stop(keep_status=True)
            return
        if time.monotonic() > self.deadline:
            self.status.set('Timed out after 90 seconds. HPLIP USB transport did not finish. Window remains usable.')
            self.stop(keep_status=True)
            return
        self.root.after(100, self.poll)

    def stop(self, keep_status=False):
        if self.proc:
            if self.proc.is_alive():
                import signal
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    self.proc.kill()
            self.proc.join(timeout=0.2)
            self.proc = None
            self.pipe.close()
            self.pipe = None
        for b in (self.scan, self.connect, self.check, self.refresh):
            b.configure(state='normal')
        self.printers.configure(state='readonly')
        self.cancel.configure(state='disabled')
        if not keep_status:
            self.status.set('Cancelled. Settings already accepted by the printer may still apply.')

    def select_printer(self, event=None):
        for variable in self.info_vars.values():
            variable.set('')
        index = self.printers.current()
        self.uri.set(self.usb_devices[index]['uri'] if index >= 0 else '')
        self.adapter = self.scanned_uri = None
        self.networks = []
        self.listbox.delete(0, 'end')
        self.password.set('')

    def discover(self):
        for variable in self.info_vars.values():
            variable.set('')
        self.uri.set('')
        self.adapter = self.scanned_uri = None
        self.networks = []
        self.usb_devices = []
        self.printers.set('')
        self.printers.configure(values=[])
        self.listbox.delete(0, 'end')
        self.start('discover')

    def do_scan(self):
        if not self.uri.get():
            messagebox.showinfo('Select USB printer', 'Choose a USB printer first. Use Refresh USB to discover printers.')
            return
        self.adapter = None
        self.networks = []
        self.listbox.delete(0, 'end')
        self.pending_uri = self.uri.get().strip()
        self.start('scan', uri=self.pending_uri)

    def valid_adapter(self):
        if not self.adapter or self.uri.get().strip() != self.scanned_uri:
            messagebox.showinfo('Scan first', 'Scan the printer to discover its wireless adapter first.')
            return False
        return True

    def toggle_password(self):
        self.password_visible = not self.password_visible
        self.password_entry.configure(show='' if self.password_visible else '•')
        self.password_toggle.configure(text='◉ Hide' if self.password_visible else '◉ Show')

    def do_connect(self):
        if not self.valid_adapter():
            return
        selected = self.listbox.curselection()
        if not selected:
            messagebox.showinfo('Choose a network', 'Select a Wi-Fi network from the scan results.')
            return
        n = self.networks[selected[0]]
        password = self.password.get()
        if n['encryptiontype'].lower() != 'none' and not password:
            messagebox.showinfo('Password required', 'Enter the selected network’s password.')
            return
        self.password.set('')
        self.start('connect', uri=self.scanned_uri, adapter=self.adapter, network=n, password=password)

    def do_status(self):
        if self.valid_adapter():
            self.start('status', uri=self.scanned_uri, adapter=self.adapter)

    def log_debug(self, text):
        stamp = time.strftime('%H:%M:%S')
        self.debug_text.configure(state='normal')
        self.debug_text.insert('end', f'[{stamp}] {text}\n')
        # Keep the most recent 2,000 lines to bound memory usage.
        lines = int(self.debug_text.index('end-1c').split('.')[0])
        if lines > 2000:
            self.debug_text.delete('1.0', f'{lines - 2000 + 1}.0')
        self.debug_text.see('end')
        self.debug_text.configure(state='disabled')

    def copy_debug(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.debug_text.get('1.0', 'end-1c'))

    def copy_info(self, key):
        value = self.info_vars[key].get()
        if value:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)

    def copy_message(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.status.get())

    def close(self):
        self.stop(keep_status=True)
        self.root.destroy()


if __name__ == '__main__':
    mp.freeze_support()
    root = tk.Tk()
    App(root)
    root.mainloop()
