"""Weekly check of every official regulation link in data/regions/rules.json.

    python -m fishershour_pipeline.links [--report links.md]

A link is OK on HTTP < 400 (redirects followed; GET with a browser-like UA; HEAD is not trusted).
Some official sites block bots (Cloudflare 403/429/503): those count as "blocked", not broken, unless
they were verified before and now return 404/410 or DNS fails. The JSON is never rewritten automatically:
it keeps the last good URL and a human fixes it from the issue (data-schema §1).
Exit 1 when at least one link is broken.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .spots import REGIONS_RULES

UA = "Mozilla/5.0 (compatible; FishersHourLinkCheck/1; +https://github.com/dprados/fishershour-data)"
BLOCKED = {401, 403, 405, 429, 503}


def check(url: str) -> tuple[str, str]:
    """('ok' | 'blocked' | 'broken', detail)."""
    last = ""
    for verify in (True, False):          # some regional sites have incomplete certificate chains
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "es,fr,pt,en"}, timeout=30,
                             allow_redirects=True, verify=verify, stream=True)
            r.close()
            if r.status_code < 400:
                return "ok", f"{r.status_code}" + ("" if verify else " (cert chain)")
            if r.status_code in BLOCKED:
                return "blocked", f"{r.status_code}"
            return "broken", f"HTTP {r.status_code}"
        except requests.exceptions.SSLError as e:
            last = f"ssl: {e}"[:120]
            continue
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"[:160]
            break
    return "broken", last


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", default=REGIONS_RULES)
    ap.add_argument("--report", default="")
    args = ap.parse_args(argv)
    rules = json.load(open(args.rules))["rules"]
    urls = sorted({v["url"] for v in rules.values() if v.get("url")})
    with ThreadPoolExecutor(8) as ex:
        res = dict(zip(urls, ex.map(check, urls)))
    broken = {u: d for u, (s, d) in res.items() if s == "broken"}
    blocked = {u: d for u, (s, d) in res.items() if s == "blocked"}
    lines = [f"Checked {len(urls)} regulation links: {len(urls) - len(broken) - len(blocked)} ok, "
             f"{len(blocked)} blocked by bot protection, {len(broken)} broken.", ""]
    for u, d in sorted(broken.items()):
        regions = sorted(k for k, v in rules.items() if v.get("url") == u)
        lines.append(f"- **broken** {u} ({d}) — regions: {', '.join(regions)}")
    for u, d in sorted(blocked.items()):
        lines.append(f"- blocked {u} ({d})")
    text = "\n".join(lines)
    print(text)
    if args.report:
        with open(args.report, "w") as f:
            f.write(text + "\n")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
