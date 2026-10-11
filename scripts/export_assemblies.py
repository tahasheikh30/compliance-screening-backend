"""
Read assembly member lists from this computer and write a CSV to upload on the Lists page ("Your own PEP list").

Some assembly websites do not answer the hosting server (a cloud address) but do answer a computer in Pakistan. Run
this there, with the same readers the server uses:

    pip install requests
    python scripts/export_assemblies.py [key ...] [-o assemblies.csv] [--ignore-robots]

Keys are those of the official sources (kp_official, punjab_official, ...). With none given, the sources that
the server could not read are tried: kp_official and punjab_official. A site whose robots.txt asks automated
readers to stay away is skipped unless you pass --ignore-robots, which is your decision and your risk.
The file is an upload: it replaces your previous uploaded PEP list, so include every assembly you need in one run.
"""
import argparse
import csv
import os
import sys
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests                                              # noqa: E402

from app.screening import pep_assemblies as pa              # noqa: E402

AGENT = "ComplianceScreening"
UA = "ComplianceScreeningBot/1.0 (+https://github.com/tahasheikh30/compliance-screening-backend) python-requests"


def allowed(url: str) -> bool:
    p = urlparse(url)
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", headers={"User-Agent": UA}, timeout=30)
    except requests.RequestException:
        return False
    if 400 <= r.status_code < 500:
        return True
    if r.status_code != 200:
        return False
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(AGENT, url)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keys", nargs="*")
    ap.add_argument("-o", "--out", default="assemblies.csv")
    ap.add_argument("--ignore-robots", action="store_true")
    args = ap.parse_args()

    sources, _pending = pa.official_sources()
    by_key = {s["key"]: s for s in sources}
    keys = args.keys or ["kp_official", "punjab_official"]
    rows = []
    for key in keys:
        src = by_key.get(key)
        if not src:
            print(f"{key}: unknown source (known: {', '.join(by_key)})")
            continue
        if not args.ignore_robots and not allowed(src["url"]):
            print(f"{key}: skipped, robots.txt does not allow automated reading (use --ignore-robots if you decide to)")
            continue
        try:
            resp = requests.get(src["url"], headers={"User-Agent": UA}, timeout=60)
            resp.raise_for_status()
            members = pa._PARSERS[src.get("parser", "table")](resp.text)
        except Exception as exc:                              # noqa: BLE001
            print(f"{key}: could not be read ({exc.__class__.__name__}: {str(exc)[:120]})")
            continue
        if len(members) < pa.MIN_MEMBERS:
            print(f"{key}: only {len(members)} rows were read, the page layout may have changed; not written")
            continue
        office = "Member of the " + src["label"].split(" website")[0]
        for m in members:
            seat = " ".join(x for x in [m.get("constituency", ""), m.get("party", "")] if x)
            rows.append({"Name": m["name"], "Position": f"{office}" + (f" ({seat})" if seat else ""),
                         "Level": src["level"], "Province": src["province"], "Party": m.get("party", "")})
        print(f"{key}: {len(members)} members")
        time.sleep(2)
    if not rows:
        print("Nothing to write.")
        return 1
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["Name", "Position", "Level", "Province", "Party"])
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {args.out}. Upload it on the Lists page under \"Your own PEP list\".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
