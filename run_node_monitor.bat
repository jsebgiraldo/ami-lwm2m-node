@echo off
REM AMI pi4/Thread node e2e monitor — hourly wrapper (analog of r1000\run_research.bat).
REM Samples the connected XIAO node's full e2e chain + per-OSI-layer telemetry,
REM appends node_history_<suffix>.json and renders node_report_<suffix>.html.
cd /d "C:\Users\User\Documents\ESP32\zephyrproject\ami-lwm2m-node"
"C:\Users\User\Documents\ESP32\.venv\Scripts\python.exe" -X utf8 "tools\node_monitor.py" --suffix 25c0 >> "tools\node_monitor_cron.log" 2>&1
