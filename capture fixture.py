#!/usr/bin/env python3
"""
Legt de ECHTE marktdata vast als bevroren testset ("fixture").

Werkwijze: dit script draait analyze.py precies zoals de dagelijkse run,
maar registreert elke yfinance-aanroep en schrijft de opgehaalde data weg
naar fixtures/. De selftest kan daarna een tweede testronde draaien op deze
echte data (replay) -- zonder netwerk, en elke keer identiek.

Draaien: via de GitHub Action "Capture fixture" (workflow_dispatch).
Opnieuw draaien = de fixture verversen (bijv. na nieuwe tickers).

De fixture is bewust BEVROREN: een test tegen bewegende data zou elke dag
andere uitkomsten geven, en dan weet je nooit of de code brak of de markt
bewoog. Echt en deterministisch tegelijk -- dat is het punt.
"""
import json, os, io, sys, contextlib

FIXDIR = "fixtures"

def main():
    import pandas as pd
    import yfinance  # de ECHTE -- dit script vereist netwerk (GitHub Actions)

    calls = {}
    orig_download = yfinance.download

    def _key(tickers, kwargs):
        t = tickers if isinstance(tickers, str) else sorted(list(tickers))
        return json.dumps({"t": t, "period": kwargs.get("period"),
                           "interval": kwargs.get("interval")}, sort_keys=True)

    def recorder(tickers, *a, **k):
        df = orig_download(tickers, *a, **k)
        calls[_key(tickers, k)] = df
        return df

    yfinance.download = recorder

    src = open("analyze.py", encoding="utf-8").read()
    print("analyze.py draaien met opname van alle yfinance-aanroepen...")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(src, {"__name__": "__main__"})
    except SystemExit as e:
        if (e.code or 0) != 0:
            print(buf.getvalue()[-3000:])
            print(f"FOUT: analyze.py eindigde met exitcode {e.code}")
            return 1

    if not calls:
        print("FOUT: geen enkele yfinance-aanroep geregistreerd")
        return 1

    os.makedirs(FIXDIR, exist_ok=True)
    index = {}
    for i, (key, df) in enumerate(calls.items()):
        fn = os.path.join(FIXDIR, f"call_{i:03d}.csv.gz")
        df.to_csv(fn, compression="gzip")
        index[key] = {"file": fn, "nlevels": int(df.columns.nlevels),
                      "rows": int(len(df))}
    with open(os.path.join(FIXDIR, "index.json"), "w") as f:
        json.dump(index, f, indent=1, sort_keys=True)

    totaal = sum(os.path.getsize(v["file"]) for v in index.values())
    print(f"Vastgelegd: {len(calls)} aanroepen, {totaal/1e6:.1f} MB in {FIXDIR}/")
    for key, v in sorted(index.items(), key=lambda x: x[1]["file"]):
        k = json.loads(key)
        t = k["t"] if isinstance(k["t"], str) else f"{len(k['t'])} tickers"
        print(f"  {v['file']}: {t} | period={k['period']} interval={k['interval']} | {v['rows']} rijen")
    return 0

if __name__ == "__main__":
    sys.exit(main())
