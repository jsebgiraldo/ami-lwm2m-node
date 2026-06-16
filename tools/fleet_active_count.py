"""Print active count for our v0.6.67 cohort vs the full fleet."""
import csv
import json
import sys
import urllib.request

BASE = "http://192.168.8.111:8090"


def api(t, p, m="GET", d=None):
    h = {"Content-Type": "application/json"}
    if t:
        h["X-Authorization"] = f"Bearer {t}"
    body = json.dumps(d).encode() if d else None
    r = urllib.request.Request(BASE + p, data=body, headers=h, method=m)
    return json.loads(urllib.request.urlopen(r, timeout=8).read())


def main():
    targets = set(sys.argv[1:]) if len(sys.argv) > 1 else {"3", "6", "7", "9", "11", "12", "16"}
    tok = api(None, "/api/auth/login", "POST",
              {"username": "tenant@thingsboard.org", "password": "tenant"})["token"]
    devs = api(tok, "/api/tenant/devices?pageSize=100&page=0")["data"]
    fmap = {}
    with open("tools/fleet_map.csv") as f:
        for r in csv.DictReader(f):
            fmap[r["endpoint"]] = r["label"]

    cohort_active = 0
    total_active = 0
    for d in devs:
        if not d["name"].startswith("ami-esp32c6-"):
            continue
        lbl = fmap.get(d["name"], "?")
        if lbl in ("31", "32", "?"):
            continue
        attrs_raw = api(tok, f"/api/plugins/telemetry/DEVICE/{d['id']['id']}/values/attributes/SERVER_SCOPE")
        attrs = {a["key"]: a["value"] for a in attrs_raw}
        if attrs.get("active"):
            total_active += 1
            if lbl in targets:
                cohort_active += 1
    print(f"v0.6.67 active={cohort_active}/{len(targets)} fleet_active={total_active}/30")


if __name__ == "__main__":
    main()
