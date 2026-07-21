#!/usr/bin/env python3
"""
BUFFETT+ selftest — draait analyze.py tegen gesimuleerde marktdata en
controleert de invarianten. Bedoeld om VOOR de echte dagelijkse run te
bewijzen dat een code-wijziging niets breekt.

Gebruik:  python selftest.py            (vanuit de repo-root)
Exitcode: 0 = alle checks groen, 1 = minstens een check rood.

De test raakt de echte signals.json / track_record.json NIET: hij draait
in een tijdelijke map. yfinance wordt gemockt met 7 koersvormen zodat er
zowel koop- als verkoopsituaties ontstaan; er is dus geen netwerk nodig.
"""
import sys, os, io, json, types, zlib, tempfile, contextlib

REPO = os.path.dirname(os.path.abspath(__file__))
FOUT = []

def check(naam, conditie, extra=""):
    status = "PASS" if conditie else "FAIL"
    print(f"  [{status}] {naam}" + (f" -- {extra}" if extra and not conditie else ""))
    if not conditie:
        FOUT.append(naam)

def main():
    try:
        import pandas as pd
        import numpy as np
    except ImportError as e:
        print(f"FAIL: pandas/numpy niet beschikbaar ({e}). Installeer: pip install pandas numpy")
        return 1

    src = open(os.path.join(REPO, "analyze.py"), encoding="utf-8").read()

    # ── yfinance-mock: 7 koersvormen, deterministisch per ticker ─────────────
    def _seed(x): return zlib.crc32(x.encode()) % 99991
    def mk(a, s, n, freq):
        np.random.seed(s)
        idx = (pd.bdate_range(end="2026-07-14", periods=n) if freq == "B"
               else pd.date_range(end="2026-07-14", periods=n, freq="W-FRI"))
        L = len(idx); m = s % 7
        if m == 0:
            c = np.linspace(a*0.5, a, L) + (np.linspace(0, 1, L)**3)*a*0.4
        elif m == 1:
            c = np.concatenate([np.linspace(a*0.6, a*1.4, L//2), np.linspace(a*1.4, a*0.75, L-L//2)])
        elif m == 2:
            c = np.concatenate([np.linspace(a*1.3, a*0.7, int(L*.7)), np.linspace(a*0.7, a*0.9, L-int(L*.7))])
        elif m == 3:
            p2 = int(L*.45); pk = int(L*.90)
            c = np.concatenate([np.linspace(a*.95, a, int(L*.12)), np.linspace(a, a*.55, p2-int(L*.12)),
                                np.linspace(a*.55, a*2.9, pk-p2), np.linspace(a*2.9, a*2.5, L-pk)])
        elif m == 4:
            c = np.concatenate([np.linspace(a*.8, a, int(L*.12)), np.linspace(a, a*.3, int(L*.55)-int(L*.12)),
                                np.linspace(a*.3, a*1.05, L-int(L*.55))])
        elif m == 5:
            c = np.concatenate([np.linspace(a*1.6, a*.5, int(L*.7)), np.linspace(a*.5, a*.62, L-int(L*.7))])
        else:
            c = np.linspace(a*.85, a, L)
        c = np.maximum(c + np.random.randn(L)*a*.008, a*.05)
        return pd.DataFrame({"Open": c, "High": c*1.01, "Low": c*.99, "Close": c,
                             "Volume": np.abs(np.random.randn(L)*1e6 + 5e6)}, index=idx)
    def fake(t, *a, **k):
        per = k.get("period", "5y"); iv = k.get("interval", "1d")
        N = 1400 if per == "max" else 1250; F = "W" if iv == "1wk" else "B"
        if isinstance(t, str):
            return mk(80 + _seed(t) % 1500, _seed(t), N, F)
        return pd.concat({x: mk(50 + _seed(x) % 2000, _seed(x), N, F) for x in t}, axis=1)
    yf = types.ModuleType("yfinance"); yf.download = fake; yf.__version__ = "0.2.65"
    sys.modules["yfinance"] = yf
    os.environ["GITHUB_EVENT_NAME"] = "workflow_dispatch"

    werkmap = tempfile.mkdtemp(prefix="buffett-selftest-")
    os.chdir(werkmap)
    json.dump({"_meta": {}, "records": {}, "_outcomeFormulaVersion": 2}, open("track_record.json", "w"))
    print(f"Selftest in {werkmap}\n")

    # ── Run 1 ────────────────────────────────────────────────────────────────
    ec = 0
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            exec(src, {"__name__": "__main__"})
    except SystemExit as e:
        ec = e.code or 0
    except Exception as e:
        print(f"  [FAIL] analyze.py crasht: {type(e).__name__}: {e}")
        return 1

    check("exitcode 0", ec == 0, f"exitcode={ec}")
    if not os.path.exists("signals.json"):
        print("  [FAIL] signals.json niet geschreven"); return 1
    raw1 = open("signals.json", "rb").read()
    sig = json.loads(raw1.decode("ascii"))          # faalt op non-ASCII -> in de except
    stocks = sig.get("stocks", {})

    check("84 aandelen", len(stocks) == 84, f"{len(stocks)}")
    check("0 errors", len(sig.get("errors", [])) == 0, str(sig.get("errors", []))[:120])
    check("geen NaN in JSON", raw1.count(b"NaN") == 0)
    check("10 sectoren", len(set(s["sector"] for s in stocks.values())) == 10)
    check("17 bagger-kandidaten", len(sig.get("baggers", {}).get("candidates", [])) == 17)
    alloc = sig.get("allocation") or {}
    check("maandpick aanwezig", alloc.get("primaryPick") is not None)
    check("pickTop3 = 3", len(alloc.get("pickTop3") or []) == 3)
    from collections import Counter
    oo = Counter(s["overall"] for s in stocks.values())
    check("KOOP-oordelen aanwezig", sum(v for k, v in oo.items() if "KOOP" in k) > 0)
    check("VERKOOP-oordelen aanwezig", sum(v for k, v in oo.items() if "VERKOOP" in k) > 0)
    r = (stocks.get("V", {}).get("indicators") or {}).get("fib", {}).get("retracements", {})
    check("VISA retracement 1.414", "1.414" in r)
    e = (stocks.get("CAT", {}).get("indicators") or {}).get("fib", {}).get("extensions", {})
    check("CAT extensie 4.236", "4.236" in e)
    check("verdictChanges-veld aanwezig", "verdictChanges" in sig)

    # ── Run 2 + 3: idempotentie (zelfde dag => inhoudelijk identieke output) ─
    # generatedAt verschilt per run (kloktijd), dus die wordt genormaliseerd;
    # al het overige moet exact gelijk blijven.
    def _norm(b):
        d = json.loads(b.decode("ascii"))
        (d.get("meta") or {}).pop("generatedAt", None)
        (d.get("meta") or {}).pop("generatedAtHuman", None)
        (d.get("weekly") or {}).pop("generatedAt", None)
        (d.get("weekly") or {}).pop("generatedAtHuman", None)
        return json.dumps(d, sort_keys=True)
    tr1 = len(json.load(open("track_record.json"))["records"])
    for _ in range(2):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exec(src, {"__name__": "__main__"})
        except SystemExit:
            pass
    raw3 = open("signals.json", "rb").read()
    tr3 = len(json.load(open("track_record.json"))["records"])
    check("track_record groeit niet bij herrun", tr1 == tr3, f"{tr1} -> {tr3}")
    check("signals.json inhoudelijk identiek bij herrun", _norm(raw1) == _norm(raw3),
          f"{len(raw1)} vs {len(raw3)} bytes")

    # ── Ronde 2 (optioneel): replay van ECHTE marktdata uit fixtures/ ────────
    # De synthetische ronde hierboven test extremen die in de echte 84 toevallig
    # niet voorkomen; deze ronde test tegen de rommeligheid van echte data. De
    # fixture is bevroren (capture_fixture.py via de "Capture fixture"-Action),
    # dus de uitkomsten zijn deterministisch.
    fixdir = os.path.join(REPO, "fixtures")
    fixidx = os.path.join(fixdir, "index.json")
    if os.path.exists(fixidx):
        print("\n-- Ronde 2: replay van echte marktdata (fixtures/) --")
        index = json.load(open(fixidx))

        def _key(tickers, kwargs):
            t = tickers if isinstance(tickers, str) else sorted(list(tickers))
            return json.dumps({"t": t, "period": kwargs.get("period"),
                               "interval": kwargs.get("interval")}, sort_keys=True)
        _cache = {}
        def replay(tickers, *a, **k):
            key = _key(tickers, k)
            if key not in index:
                raise KeyError("Fixture mist deze aanroep -- analyze.py vraagt nieuwe data. "
                               "Draai de 'Capture fixture'-Action opnieuw. Aanroep: " + key)
            if key not in _cache:
                meta = index[key]
                hdr = [0, 1] if meta["nlevels"] == 2 else 0
                _cache[key] = pd.read_csv(os.path.join(REPO, meta["file"]),
                                          header=hdr, index_col=0,
                                          parse_dates=True, compression="gzip")
            return _cache[key].copy()
        yf.download = replay

        os.chdir(tempfile.mkdtemp(prefix="buffett-fixture-"))
        json.dump({"_meta": {}, "records": {}, "_outcomeFormulaVersion": 2},
                  open("track_record.json", "w"))
        ec2 = 0
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exec(src, {"__name__": "__main__"})
        except SystemExit as e:
            ec2 = e.code or 0
        except KeyError as e:
            print(f"  [FAIL] {e}")
            FOUT.append("fixture dekt de aanroepen")
            ec2 = None
        if ec2 is not None:
            check("fixture: exitcode 0", ec2 == 0, f"exitcode={ec2}")
            fraw1 = open("signals.json", "rb").read()
            fsig = json.loads(fraw1.decode("ascii"))
            fst = fsig.get("stocks", {})
            check("fixture: 84 aandelen", len(fst) == 84, f"{len(fst)}")
            check("fixture: 0 errors", len(fsig.get("errors", [])) == 0,
                  str(fsig.get("errors", []))[:120])
            check("fixture: alle prijzen > 0",
                  all(((s.get("indicators") or {}).get("last") or 0) > 0
                      for s in fst.values()))
            # Door Ruben op de chart geverifieerde niveaus -- de overrides maken
            # deze exact voorspelbaar, onafhankelijk van de koers van de dag:
            fe = (fst.get("V", {}).get("indicators") or {}).get("fib", {}).get("extensions", {})
            check("fixture: VISA 1.618 = ~$316", 300 < (fe.get("1.618") or 0) < 330,
                  f"{fe.get('1.618')}")
            ce = (fst.get("CAT", {}).get("indicators") or {}).get("fib", {}).get("extensions", {})
            check("fixture: CAT 4.236 = ~$987", 950 < (ce.get("4.236") or 0) < 1030,
                  f"{ce.get('4.236')}")
            check("fixture: CAT 3.618 = ~$757", 720 < (ce.get("3.618") or 0) < 800,
                  f"{ce.get('3.618')}")
            lg = fst.get("LOTB", {}).get("earningsGrowth") or {}
            check("fixture: LOTB winsthistorie (mediaan 16%)",
                  lg.get("medianGrowth") == 16.0, f"{lg.get('medianGrowth')}")
            # Idempotentie ook op echte data
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    exec(src, {"__name__": "__main__"})
            except SystemExit:
                pass
            fraw2 = open("signals.json", "rb").read()
            check("fixture: herrun inhoudelijk identiek", _norm(fraw1) == _norm(fraw2))
    else:
        print("\n(fixtures/ niet gevonden -- ronde 2 overgeslagen; draai eenmalig de "
              "'Capture fixture'-Action om ook tegen echte data te testen)")

    print()
    if FOUT:
        print(f"RESULTAAT: {len(FOUT)} check(s) ROOD: {', '.join(FOUT)}")
        return 1
    print("RESULTAAT: alle checks GROEN")
    return 0

if __name__ == "__main__":
    sys.exit(main())
