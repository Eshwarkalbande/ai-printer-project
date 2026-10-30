<!-- Steps, assuming the printer is on the same Wi-Fi or LAN as your PC. The code is built for this, but I haven’t seen it work on a real printer, so the first attempt is also your test.

Before touching the project

Find the printer’s IP. Check its display (Network or Wi-Fi settings), print a network configuration page, or look at your router’s connected-devices list.
Enable SNMP v1/v2c in the printer’s web admin page (type its IP in a browser). Note the community string, usually public.
Check reachability: ping <printer-ip> in PowerShell.

Test it from the terminal (optional but recommended)

In the project folder with the venv active:
   python scan_printer_cli.py <printer-ip> --community public

JSON with a model name means it works. An error or reachable: false means fix the printer side (steps 1 to 3) before going further.

Add it in the app

Run python app.py and log in at http://127.0.0.1:5000.
Click Add Printer and fill in:
Printer ID: any unique label, e.g. PRN-006
Display Name and Model: any text
Operation Mode: change it to SNMP Network Polling
IP Address, Community and Port (161)
Click Test Connection. It should show the model and findings. Then click Add Printer.

Check it in real time

The card says “Status Not Available” at first. Within about 12 seconds the first real scan arrives and fills in the status and findings.
Click Diagnose on the card for the full report: supplies per colour, trays, covers and alerts.
Prove it’s live. Open the printer’s paper tray or a cover, or pull out the paper. Within roughly 12 seconds the card should show the new issue (e.g. “No Paper” or “Cover open”) with no browser refresh. That is the moment to show an interviewer.

If it shows nothing

Confirm Operation Mode really is SNMP, not Simulation.
Check that SNMP is enabled and the community string matches.
Windows Firewall or a Wi-Fi network with client isolation (common on college and guest networks) can block the scan. -->