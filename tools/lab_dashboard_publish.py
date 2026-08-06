#!/usr/bin/env python3
"""Splice the live tools/lab_e2e_snapshot.json into the dashboard template's
inline <script id="lab-data"> blob and write tools/lab_dashboard_live.html.

This is the 'publish cycle': the e2e monitor writes lab_e2e_snapshot.json every
interval; run this to refresh the publishable HTML, then (re)publish the SAME
file path as an Artifact so it redeploys to the same URL.

  python tools/lab_dashboard_publish.py
"""
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
tpl = (HERE / "lab_dashboard.html").read_text(encoding="utf-8")
snap_txt = (HERE / "lab_e2e_snapshot.json").read_text(encoding="utf-8")
json.loads(snap_txt)  # validate — never splice non-JSON into the page

pat = re.compile(
    r'(<script id="lab-data" type="application/json">)(.*?)(</script>)', re.S)
if not pat.search(tpl):
    raise SystemExit("could not find the lab-data blob in lab_dashboard.html")

out = pat.sub(lambda m: m.group(1) + "\n" + snap_txt.strip() + "\n" + m.group(3),
              tpl, count=1)
dst = HERE / "lab_dashboard_live.html"
dst.write_text(out, encoding="utf-8")
print(f"wrote {dst.name} ({len(out)} bytes) with live snapshot")
