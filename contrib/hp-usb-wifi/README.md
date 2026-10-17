# HP USB Wi-Fi setup utility

A small Tkinter application that configures an HP printer's Wi-Fi network through USB using HPLIP's internal LEDM API. Tested by a user with an HP LaserJet Professional P1102w and Debian 13's HPLIP 3.22.10.

## Run on Debian

```sh
sudo apt install hplip python3-tk python3-dbus
/usr/bin/python3 hp_usb_wifi.py
```

Use Debian's system Python: HPLIP's compiled cupsext and hpmudext modules are supplied by its Debian package, not pip. In VS Code, select `/usr/bin/python3` as the Python interpreter.

Close other HP setup windows and connect the printer by USB. Select it from the automatically populated USB dropdown, scan, select a Wi-Fi network, enter its password, and connect. Wait about 30 seconds and choose **Check IP / Info**. A successful settings request means the printer accepted the profile; its IP and the access point's client list confirm network connectivity.

## Features

- USB printer selection and refresh, Wi-Fi scanning, masked password with Show/Hide control.
- Wi-Fi MAC, hostname and IP fields with individual Copy buttons. Values remain blank when unavailable; an access point's BSSID is never treated as the printer's MAC.
- Responsive background processes, cancellation and a 90-second operation limit.
- Timestamped debug panel and Copy debug. Logs include method names, byte counts, Python path and security enums; raw USB payloads and passwords are suppressed.
- Window centered at 50% screen width and 90% screen height.

The application includes local workarounds for HPLIP's Marvell response-reading loop and the P1102w's WPA_PSK profile encoding, so it works without modifying the installed HPLIP sources. Cancelling an operation does not undo settings already accepted by the printer. Passwords are held transiently in memory and sent to the printer; the application does not save them.

## Scope and limitations

This is a Linux utility relying on HPLIP's internal API, which can change between releases. USB discovery uses `/usr/bin/hp-probe`. It does not add or modify a CUPS print queue. Other printer models have not been hardware-tested. The GUI remains responsive if the USB transport stalls, but cannot guarantee success on an unsupported model. Scan results are used directly; hidden networks are not supported by manual SSID entry.

## Development

Run syntax checks and the parser tests:

```sh
python3 -m py_compile hp_usb_wifi.py
python3 tests/test_utility_parsers.py
```

## License

MIT; see LICENSE.
