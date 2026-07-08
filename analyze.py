#!/usr/bin/env python3
"""
BUFFETT+ Signal Analyzer  —  v2 (fundament)
=============================================
Verbeteringen t.o.v. v1:
  • RSI exact volgens Wilder's RMA (matcht TradingView)
  • NaN-guards overal: aandelen met te weinig historie falen niet stil
  • Onvolledige (lopende) weekcandle wordt weggegooid
  • Batch-download via yfinance (sneller, minder rate-limit risico)
  • Data sanity-checks (geen negatieve prijzen, plausibele dag-op-dag bewegingen)
  • Historische opslag: snapshots in history/ + doorlopende timeline.json
  • Robuustere crossover-detectie (kijkt naar tekenwissel, niet enkel 1 candle)

Schrijft atomisch naar:
  signals.json          → huidige staat
  timeline.json         → doorlopende kernmetrieken per aandeel over tijd
  history/YYYY-MM-DD.json → dagsnapshot (voor 'wat is veranderd')
"""

import json
import os
import sys
import time
import traceback
import statistics
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta
from typing import Optional, Tuple

import pandas as pd
from zoneinfo import ZoneInfo  # stdlib (Python 3.9+), geen externe install nodig

try:
    import yfinance as yf
    import numpy as np
except ImportError as e:
    print(f"FOUT: Ontbrekende package: {e}")
    print("Installeer via: pip3 install yfinance pandas numpy")
    sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────
BRUSSELS_TZ = ZoneInfo("Europe/Brussels")
NOW         = datetime.now(BRUSSELS_TZ)
TODAY       = NOW.date()
IS_FRIDAY   = TODAY.weekday() == 4
IS_WEEKEND  = TODAY.weekday() >= 5

OUTPUT_FILE   = "signals.json"
TIMELINE_FILE = "timeline.json"
WEEKLY_FILE   = "weekly.json"
TRACK_FILE    = "track_record.json"
HISTORY_DIR   = "history"

# Benchmark voor relatieve return (beter dan de index?). yfinance ticker voor S&P 500.
BENCHMARK_TICKER = "^GSPC"
BENCHMARK_NAME   = "S&P 500"

# Horizons (weken) waarop we forward return meten. Passen bij een lange beleggingshorizon.
TRACK_HORIZONS_WEEKS = [1, 4, 13, 26]
# Minimum aantal AFGERONDE observaties voor we een accuraatheidscijfer tonen (anti-ruis).
TRACK_MIN_OBSERVATIONS = 20

# ── MARKTREGIME & CONTEXT ─────────────────────────────────────────────────────
# Extra reeksen voor het marktregime (SPX komt al binnen als benchmark).
MARKET_TICKERS = {
    "NDX":    "^IXIC",     # NASDAQ Composite
    "DXY":    "DX-Y.NYB",  # Dollar-index (context, géén score-invloed)
    "GOLD":   "GLD",       # SPDR Gold Shares (grootste goud-ETF; EU-koopbaar: SGLD/IGLN)
    "COPPER": "CPER",      # US Copper Index Fund ("Dr. Copper"; EU-koopbaar: COPA)
    "OIL":    "USO",       # US Oil Fund, WTI (EU-koopbaar: CRUD)
    "SPY":    "SPY",       # Voor sector-relatieve-sterkte
    "XLK":"XLK", "SMH":"SMH", "XLI":"XLI", "ITA":"ITA", "XLV":"XLV", "XLE":"XLE", "XLF":"XLF",
}
SECTOR_LABELS = {"XLK":"Technologie","SMH":"Halfgeleiders","XLI":"Industrie",
                 "ITA":"Defensie & Ruimtevaart","XLV":"Gezondheidszorg","XLE":"Energie","XLF":"Financials"}
# Maximale invloed van het marktregime op de timing-score (mild, begrensd, zichtbaar).
MARKET_ADJ_MAX = 8

# Hoeveel historische datapunten bewaren we per aandeel in timeline.json
TIMELINE_MAX_POINTS = 400  # ~1.5 jaar werkdagen

# FMP voor echte historische P/E (optioneel — werkt in GitHub Actions, niet in browser).
# Zet FMP_API_KEY als GitHub Secret. Zonder key valt het systeem terug op PEG.
FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()
FMP_BASE    = "https://financialmodelingprep.com/api/v3"

# Tickers: (dashboard_naam, primaire_ticker, fallback_ticker)
# Kern-watchlist (kwaliteit + waardering + timing)
WATCHLIST = [
    ("WM",    "WM",      None),
    ("PLTR",  "PLTR",    None),
    ("CAT",   "CAT",     None),
    ("ASML",  "ASML.AS", "ASML"),   # Euronext Amsterdam (€); Nasdaq als data-fallback
    ("ASMI",  "ASM.AS",  "ASMIY"),
    ("MU",    "MU",      None),
    ("GOOGL", "GOOGL",   None),
    ("AMZN",  "AMZN",    None),
    ("ORCL",  "ORCL",    None),
    ("KO",    "KO",      None),
    # ── Bagger-testkandidaten (hoog groei/risico; apart spoor) ──
    ("LHX",   "LHX",     None),      # L3Harris — volwassen defensie (kan kernpoort halen)
    ("MOGA",  "MOG-A",   "MOG.A"),   # Moog klasse A — puntnotatie onbetrouwbaar in yfinance
    ("TDG",   "TDG",     None),      # TransDigm — volwassen, hoge marge
    ("KTOS",  "KTOS",    None),      # Kratos — defensie-groei
    ("RKLB",  "RKLB",    None),      # Rocket Lab — ruimtevaart, verlieslatend
    ("OPEN",  "OPEN",    None),      # Opendoor — controletest buiten sector
    ("SDGR",  "SDGR",    None),      # Schrödinger — computational drug discovery
    ("BNGO",  "BNGO",    None),      # Bionano Genomics — hoog-risico microcap
    # ── Uitbreiding juli 2026: kern-kwaliteit wereldwijd ──
    ("GAW",      "GAW.L",   None),   # Games Workshop (LSE, pence!)
    ("MNST",     "MNST",    None),   # Monster Beverage
    ("V",        "V",       None),   # Visa
    ("KPG",      "KPG.AX",  None),   # Kelly Partners (ASX)
    ("ADM",      "ADM.L",   None),   # Admiral Group (LSE, pence!)
    ("AON",      "AON",     None),   # Aon plc
    ("MELI",     "MELI",    None),   # MercadoLibre
    ("III",      "III.L",   None),   # 3i Group (LSE, pence!)
    ("SHOP",     "SHOP",    None),   # Shopify
    ("NET",      "NET",     None),   # Cloudflare
    ("CRWV",     "CRWV",    None),   # CoreWeave (ook bagger-spoor)
    ("MSFT",     "MSFT",    None),   # Microsoft
    ("MTLS",     "MTLS",    None),   # Materialise
    ("SNAP",     "SNAP",    None),   # Snap
    ("NVDA",     "NVDA",    None),   # Nvidia
    ("NKE",      "NKE",     None),   # Nike
    ("DIE",      "DIE.BR",  None),   # D'Ieteren (Euronext Brussel)
    ("SOF",      "SOF.BR",  None),   # Sofina (Euronext Brussel)
    ("AIR",      "AIR.PA",  None),   # Airbus (Euronext Parijs)
    ("ALFEN",    "ALFEN.AS",None),   # Alfen (Euronext Amsterdam)
    ("LOTB",     "LOTB.BR", None),   # Lotus Bakeries (Euronext Brussel)
    ("MSTR",     "MSTR",    None),   # Strategy (bitcoin-proxy — poort zal falen, bewust)
    ("AAPL",     "AAPL",    None),   # Apple
    ("NFLX",     "NFLX",    None),   # Netflix
    ("DIS",      "DIS",     None),   # Disney
    ("BLK",      "BLK",     None),   # BlackRock
    ("BABA",     "BABA",    None),   # Alibaba (NYSE ADR)
    # ── Robotics: Buffett-moat namen ──
    ("NABTESCO", "6268.T",  None),   # Nabtesco (Tokio, ¥)
    ("HARMONIC", "6324.T",  "HSYDF"),# Harmonic Drive (Tokio, ¥)
    ("KEYENCE",  "6861.T",  "KYCCF"),# Keyence (Tokio, ¥)
    ("FANUC",    "6954.T",  "FANUY"),# Fanuc (Tokio, ¥)
    ("YASKAWA",  "6506.T",  "YASKY"),# Yaskawa (Tokio, ¥)
    ("ABB",      "ABB",     None),   # ABB (NYSE-notering, $)
    ("SOFTBANK", "9984.T",  "SFTBY"),# SoftBank Group (Tokio, ¥)
    ("ROK",      "ROK",     None),   # Rockwell Automation
    ("TER",      "TER",     None),   # Teradyne
    ("ISRG",     "ISRG",    None),   # Intuitive Surgical
    ("CGNX",     "CGNX",    None),   # Cognex
    ("NOVT",     "NOVT",    None),   # Novanta
    # ── Quantum: 100x-bagger testkandidaten ──
    ("IONQ",     "IONQ",    None),   # IonQ
    ("RGTI",     "RGTI",    None),   # Rigetti
    ("QBTS",     "QBTS",    None),   # D-Wave
    # ── Robotics: speculatieve bagger-kandidaten ──
    ("UBTECH",   "9880.HK", None),   # UBTECH (Hong Kong, HK$)
    ("SYM",      "SYM",     None),   # Symbotic
    ("SERV",     "SERV",    None),   # Serve Robotics
    ("RR",       "RR",      None),   # Richtech Robotics
    ("PL",       "PL",      None),   # Planet Labs — aardobservatie (bagger-kandidaat)
]

# Welke tickers worden (ook) in het bagger-spoor beoordeeld?
BAGGER_TICKERS = {"LHX", "MOGA", "TDG", "KTOS", "RKLB", "OPEN", "SDGR", "BNGO",
                  "CRWV", "IONQ", "RGTI", "QBTS", "UBTECH", "SYM", "SERV", "RR", "PL"}

# Valuta per aandeel (weergave). "p" = Britse pence (LSE noteert in pence!).
CURRENCY = {
    "ASML":"€", "ASMI":"€", "DIE":"€", "SOF":"€", "AIR":"€", "ALFEN":"€", "LOTB":"€",
    "GAW":"p", "ADM":"p", "III":"p",
    "NABTESCO":"¥", "HARMONIC":"¥", "KEYENCE":"¥", "FANUC":"¥", "YASKAWA":"¥", "SOFTBANK":"¥",
    "UBTECH":"HK$", "KPG":"A$",
}  # alles zonder vermelding: "$"

# Fundamentals — handmatig bijgehouden per kwartaal. Laatste update: juni 2026.
FUNDAMENTALS = {
    "WM":    {"pe":29.2,  "roe":29.9, "fcfYield":3.0,  "debtEquity":2.28, "netMargin":11.0, "divYield":1.69, "revenueGrowth":6.1,   "eps":7.72,  "mktCap":"$90B",   "beta":0.46, "lastUpdated":"2026-06"},
    "PLTR":  {"pe":117.6, "roe":32.6, "fcfYield":1.0,  "debtEquity":0.02, "netMargin":43.7, "divYield":0,    "revenueGrowth":67.7,  "eps":0.96,  "mktCap":"$271B",  "beta":1.52, "lastUpdated":"2026-06"},
    "CAT":   {"pe":47.1,  "roe":51.3, "fcfYield":1.7,  "debtEquity":2.31, "netMargin":13.3, "divYield":1.1,  "revenueGrowth":11.9,  "eps":8.12,  "mktCap":"$176B",  "beta":1.60, "lastUpdated":"2026-06"},
    "ASML":  {"pe":58.8,  "roe":55.0, "fcfYield":1.9,  "debtEquity":0.12, "netMargin":31.0, "divYield":0.4,  "revenueGrowth":12.0,  "eps":27.80, "mktCap":"€640B",  "beta":1.37, "lastUpdated":"2026-07 (geverifieerd)"},
    "ASMI":  {"pe":44.9,  "roe":21.6, "fcfYield":2.9,  "debtEquity":0.01, "netMargin":23.9, "divYield":0.3,  "revenueGrowth":12.0,  "eps":23.75, "mktCap":"$52B",   "beta":1.50, "lastUpdated":"2026-06"},
    "MU":    {"pe":25.3,  "roe":66.6, "fcfYield":2.1,  "debtEquity":0.06, "netMargin":55.9, "divYield":0.05, "revenueGrowth":125.0, "eps":44.80, "mktCap":"$1280B", "beta":2.17, "lastUpdated":"2026-06"},
    "GOOGL": {"pe":19.8,  "roe":31.5, "fcfYield":4.2,  "debtEquity":0.07, "netMargin":28.6, "divYield":0.5,  "revenueGrowth":14.0,  "eps":9.15,  "mktCap":"$2200B", "beta":1.05, "lastUpdated":"2026-06"},
    "AMZN":  {"pe":27.2,  "roe":24.3, "fcfYield":2.8,  "debtEquity":0.53, "netMargin":12.2, "divYield":0,    "revenueGrowth":11.0,  "eps":8.36,  "mktCap":"$2440B", "beta":1.44, "lastUpdated":"2026-06"},
    "ORCL":  {"pe":31.6,  "roe":53.4, "fcfYield":4.2,  "debtEquity":3.63, "netMargin":25.2, "divYield":1.3,  "revenueGrowth":15.0,  "eps":5.83,  "mktCap":"$438B",  "beta":1.65, "lastUpdated":"2026-06"},
    "KO":    {"pe":25.5,  "roe":43.4, "fcfYield":3.3,  "debtEquity":1.25, "netMargin":27.8, "divYield":3.0,  "revenueGrowth":3.5,   "eps":2.91,  "mktCap":"$320B",  "beta":0.36, "lastUpdated":"2026-06"},
    # ── Bagger-kandidaten ── Extra velden: grossMargin, grossMarginTrend (pp YoY),
    #    revenueGrowthPrev (voor versnelling), cashRunwayMonths (None = winstgevend/n.v.t.).
    #    Cijfers indicatief per begin 2026 — VERIFIEER en werk per kwartaal bij.
    "LHX":   {"pe":24.5, "roe":11.2, "fcfYield":4.8, "debtEquity":0.70, "netMargin":9.8,  "divYield":1.9, "revenueGrowth":9.5,   "eps":13.10, "mktCap":"$47B",  "beta":0.90, "lastUpdated":"2026-01",
              "grossMargin":27.0, "grossMarginTrend":0.8, "revenueGrowthPrev":7.0,  "cashRunwayMonths":None},
    "MOGA":  {"pe":22.0, "roe":13.5, "fcfYield":3.5, "debtEquity":0.85, "netMargin":7.5,  "divYield":1.1, "revenueGrowth":11.0,  "eps":8.60,  "mktCap":"$6B",   "beta":1.15, "lastUpdated":"2026-01",
              "grossMargin":28.5, "grossMarginTrend":1.2, "revenueGrowthPrev":8.0,  "cashRunwayMonths":None},
    "TDG":   {"pe":38.0, "roe":45.0, "fcfYield":2.7, "debtEquity":5.20, "netMargin":18.5, "divYield":0,   "revenueGrowth":12.5,  "eps":33.00, "mktCap":"$78B",  "beta":1.20, "lastUpdated":"2026-01",
              "grossMargin":59.0, "grossMarginTrend":0.5, "revenueGrowthPrev":11.0, "cashRunwayMonths":None},
    "KTOS":  {"pe":95.0, "roe":3.5,  "fcfYield":0.4, "debtEquity":0.25, "netMargin":3.2,  "divYield":0,   "revenueGrowth":22.0,  "eps":0.55,  "mktCap":"$9B",   "beta":1.40, "lastUpdated":"2026-01",
              "grossMargin":25.0, "grossMarginTrend":1.5, "revenueGrowthPrev":12.0, "cashRunwayMonths":None},
    "RKLB":  {"pe":None, "roe":-18.0,"fcfYield":-3.0,"debtEquity":0.60, "netMargin":-28.0,"divYield":0,   "revenueGrowth":58.0,  "eps":-0.28, "mktCap":"$14B",  "beta":2.10, "lastUpdated":"2026-01",
              "grossMargin":28.0, "grossMarginTrend":4.0, "revenueGrowthPrev":40.0, "cashRunwayMonths":30},
    "OPEN":  {"pe":None, "roe":-22.0,"fcfYield":-5.0,"debtEquity":3.10, "netMargin":-6.5, "divYield":0,   "revenueGrowth":45.0,  "eps":-0.35, "mktCap":"$3B",   "beta":2.60, "lastUpdated":"2026-01",
              "grossMargin":8.5,  "grossMarginTrend":1.0, "revenueGrowthPrev":-30.0,"cashRunwayMonths":18},
    "SDGR":  {"pe":None, "roe":-15.0,"fcfYield":-4.0,"debtEquity":0.05, "netMargin":-32.0,"divYield":0,   "revenueGrowth":32.0,  "eps":-1.60, "mktCap":"$2B",   "beta":1.70, "lastUpdated":"2026-01",
              "grossMargin":52.0, "grossMarginTrend":2.5, "revenueGrowthPrev":18.0, "cashRunwayMonths":36},
    "BNGO":  {"pe":None, "roe":-85.0,"fcfYield":-40.0,"debtEquity":0.40,"netMargin":-180.0,"divYield":0,  "revenueGrowth":15.0,  "eps":-2.50, "mktCap":"$0.05B","beta":3.20, "lastUpdated":"2026-01",
              "grossMargin":32.0, "grossMarginTrend":-1.0,"revenueGrowthPrev":55.0, "cashRunwayMonths":9},
    # ── Uitbreiding juli 2026 — INDICATIEF per 2026-01 (CRWV: 2026-05), VERIFIEER per kwartaal ──
    # Let op eenheden: eps in noteringsvaluta (LSE in PENCE, Tokio in ¥, Brussel/Parijs/Adam in €, HK in HK$)
    "GAW":   {"pe":24.0, "roe":60.0, "fcfYield":4.0, "debtEquity":0.02, "netMargin":32.0, "divYield":4.2, "revenueGrowth":12.0, "eps":620,   "mktCap":"£5.2B",  "beta":0.50, "lastUpdated":"2026-01"},
    "MNST":  {"pe":34.0, "roe":23.0, "fcfYield":2.8, "debtEquity":0.03, "netMargin":21.5, "divYield":0,   "revenueGrowth":7.0,  "eps":1.68,  "mktCap":"$56B",   "beta":0.75, "lastUpdated":"2026-01"},
    "V":     {"pe":29.0, "roe":51.0, "fcfYield":3.4, "debtEquity":0.62, "netMargin":53.0, "divYield":0.75,"revenueGrowth":10.0, "eps":10.9,  "mktCap":"$640B",  "beta":0.95, "lastUpdated":"2026-01"},
    "KPG":   {"pe":55.0, "roe":35.0, "fcfYield":1.8, "debtEquity":2.40, "netMargin":9.0,  "divYield":1.0, "revenueGrowth":26.0, "eps":0.21,  "mktCap":"A$1.6B", "beta":0.90, "lastUpdated":"2026-01"},
    "ADM":   {"pe":19.0, "roe":45.0, "fcfYield":5.5, "debtEquity":0.40, "netMargin":17.0, "divYield":4.6, "revenueGrowth":12.0, "eps":160,   "mktCap":"£9B",    "beta":0.60, "lastUpdated":"2026-01"},
    "AON":   {"pe":25.0, "roe":70.0, "fcfYield":4.2, "debtEquity":3.40, "netMargin":19.0, "divYield":0.8, "revenueGrowth":9.0,  "eps":15.2,  "mktCap":"$77B",   "beta":0.90, "lastUpdated":"2026-01"},
    "MELI":  {"pe":52.0, "roe":38.0, "fcfYield":2.5, "debtEquity":1.10, "netMargin":9.5,  "divYield":0,   "revenueGrowth":34.0, "eps":42.0,  "mktCap":"$115B",  "beta":1.50, "lastUpdated":"2026-01"},
    "III":   {"pe":9.0,  "roe":22.0, "fcfYield":2.0, "debtEquity":0.30, "netMargin":60.0, "divYield":1.9, "revenueGrowth":16.0, "eps":450,   "mktCap":"£40B",   "beta":1.05, "lastUpdated":"2026-01"},
    "SHOP":  {"pe":85.0, "roe":13.0, "fcfYield":1.2, "debtEquity":0.08, "netMargin":13.0, "divYield":0,   "revenueGrowth":26.0, "eps":1.45,  "mktCap":"$155B",  "beta":2.20, "lastUpdated":"2026-01"},
    "NET":   {"pe":None, "roe":3.0,  "fcfYield":0.8, "debtEquity":0.90, "netMargin":1.5,  "divYield":0,   "revenueGrowth":28.0, "eps":0.08,  "mktCap":"$70B",   "beta":1.90, "lastUpdated":"2026-01"},
    "CRWV":  {"pe":None, "roe":-40.7,"fcfYield":-19.0,"debtEquity":5.20,"netMargin":-25.6,"divYield":0,   "revenueGrowth":105.0,"eps":-2.72, "mktCap":"$45B",   "beta":2.80, "lastUpdated":"2026-05",
              "grossMargin":73.0, "grossMarginTrend":-1.0, "revenueGrowthPrev":168.0, "cashRunwayMonths":None},
    "MSFT":  {"pe":34.0, "roe":34.0, "fcfYield":2.4, "debtEquity":0.30, "netMargin":36.0, "divYield":0.7, "revenueGrowth":15.0, "eps":13.4,  "mktCap":"$3.7T",  "beta":0.90, "lastUpdated":"2026-01"},
    "MTLS":  {"pe":48.0, "roe":6.0,  "fcfYield":1.0, "debtEquity":0.15, "netMargin":4.0,  "divYield":0,   "revenueGrowth":6.0,  "eps":0.13,  "mktCap":"$0.4B",  "beta":1.30, "lastUpdated":"2026-01"},
    "SNAP":  {"pe":None, "roe":-12.0,"fcfYield":1.5, "debtEquity":0.90, "netMargin":-8.0, "divYield":0,   "revenueGrowth":12.0, "eps":-0.30, "mktCap":"$16B",   "beta":1.90, "lastUpdated":"2026-01"},
    "NVDA":  {"pe":42.0, "roe":95.0, "fcfYield":2.2, "debtEquity":0.12, "netMargin":54.0, "divYield":0.03,"revenueGrowth":55.0, "eps":4.4,   "mktCap":"$4.5T",  "beta":1.70, "lastUpdated":"2026-01"},
    "NKE":   {"pe":32.0, "roe":28.0, "fcfYield":2.8, "debtEquity":0.65, "netMargin":8.5,  "divYield":2.1, "revenueGrowth":-3.0, "eps":2.2,   "mktCap":"$105B",  "beta":1.10, "lastUpdated":"2026-01"},
    "DIE":   {"pe":13.0, "roe":16.0, "fcfYield":5.0, "debtEquity":1.70, "netMargin":8.0,  "divYield":1.5, "revenueGrowth":7.0,  "eps":15.5,  "mktCap":"€11B",   "beta":1.00, "lastUpdated":"2026-01"},
    "SOF":   {"pe":13.0, "roe":8.0,  "fcfYield":1.0, "debtEquity":0.10, "netMargin":40.0, "divYield":1.4, "revenueGrowth":5.0,  "eps":19.0,  "mktCap":"€8B",    "beta":0.90, "lastUpdated":"2026-01"},
    "AIR":   {"pe":30.0, "roe":27.0, "fcfYield":3.0, "debtEquity":0.40, "netMargin":7.0,  "divYield":1.2, "revenueGrowth":12.0, "eps":6.4,   "mktCap":"€150B",  "beta":1.20, "lastUpdated":"2026-01"},
    "ALFEN": {"pe":20.0, "roe":8.0,  "fcfYield":2.0, "debtEquity":0.60, "netMargin":3.0,  "divYield":0,   "revenueGrowth":-5.0, "eps":0.55,  "mktCap":"€0.25B", "beta":1.80, "lastUpdated":"2026-01"},
    "LOTB":  {"pe":48.0, "roe":27.0, "fcfYield":1.5, "debtEquity":0.35, "netMargin":14.5, "divYield":0.9, "revenueGrowth":11.0, "eps":210.0, "mktCap":"€8.5B",  "beta":0.50, "lastUpdated":"2026-01"},
    "MSTR":  {"pe":None, "roe":-5.0, "fcfYield":-1.0,"debtEquity":0.90, "netMargin":-30.0,"divYield":0,   "revenueGrowth":2.0,  "eps":-1.0,  "mktCap":"$80B",   "beta":3.50, "lastUpdated":"2026-01"},
    "AAPL":  {"pe":36.0, "roe":150.0,"fcfYield":3.0, "debtEquity":1.60, "netMargin":25.0, "divYield":0.45,"revenueGrowth":7.0,  "eps":7.4,   "mktCap":"$4.0T",  "beta":1.10, "lastUpdated":"2026-01"},
    "NFLX":  {"pe":42.0, "roe":38.0, "fcfYield":2.0, "debtEquity":0.70, "netMargin":25.0, "divYield":0,   "revenueGrowth":14.0, "eps":26.0,  "mktCap":"$470B",  "beta":1.20, "lastUpdated":"2026-01"},
    "DIS":   {"pe":22.0, "roe":9.0,  "fcfYield":3.5, "debtEquity":0.45, "netMargin":9.0,  "divYield":1.0, "revenueGrowth":4.0,  "eps":5.4,   "mktCap":"$210B",  "beta":1.20, "lastUpdated":"2026-01"},
    "BLK":   {"pe":24.0, "roe":15.0, "fcfYield":3.8, "debtEquity":0.50, "netMargin":31.0, "divYield":2.0, "revenueGrowth":13.0, "eps":45.0,  "mktCap":"$170B",  "beta":1.20, "lastUpdated":"2026-01"},
    "BABA":  {"pe":18.0, "roe":11.0, "fcfYield":5.5, "debtEquity":0.35, "netMargin":13.0, "divYield":1.1, "revenueGrowth":7.0,  "eps":8.8,   "mktCap":"$280B",  "beta":1.30, "lastUpdated":"2026-01"},
    # Robotics moat (¥/$ — eps in noteringsvaluta)
    "NABTESCO":{"pe":21.0,"roe":8.5, "fcfYield":3.0, "debtEquity":0.25, "netMargin":7.5,  "divYield":3.0, "revenueGrowth":5.0,  "eps":135,   "mktCap":"¥350B",  "beta":0.80, "lastUpdated":"2026-01"},
    "HARMONIC":{"pe":55.0,"roe":6.0, "fcfYield":0.5, "debtEquity":0.30, "netMargin":9.0,  "divYield":0.8, "revenueGrowth":15.0, "eps":65,    "mktCap":"¥340B",  "beta":1.40, "lastUpdated":"2026-01"},
    "KEYENCE":{"pe":37.0, "roe":13.5,"fcfYield":2.0, "debtEquity":0.00, "netMargin":37.0, "divYield":0.7, "revenueGrowth":9.0,  "eps":1850,  "mktCap":"¥16.5T", "beta":0.95, "lastUpdated":"2026-01"},
    "FANUC": {"pe":27.0, "roe":8.0,  "fcfYield":2.5, "debtEquity":0.00, "netMargin":16.0, "divYield":2.2, "revenueGrowth":5.0,  "eps":160,   "mktCap":"¥4.3T",  "beta":0.90, "lastUpdated":"2026-01"},
    "YASKAWA":{"pe":26.0, "roe":11.0,"fcfYield":2.0, "debtEquity":0.20, "netMargin":8.5,  "divYield":1.6, "revenueGrowth":6.0,  "eps":165,   "mktCap":"¥1.1T",  "beta":1.20, "lastUpdated":"2026-01"},
    "ABB":   {"pe":29.0, "roe":29.0, "fcfYield":3.2, "debtEquity":0.50, "netMargin":14.5, "divYield":1.7, "revenueGrowth":6.0,  "eps":2.1,   "mktCap":"$115B",  "beta":1.10, "lastUpdated":"2026-01"},
    "SOFTBANK":{"pe":14.0,"roe":14.0,"fcfYield":0.5, "debtEquity":1.60, "netMargin":18.0, "divYield":0.4, "revenueGrowth":8.0,  "eps":1300,  "mktCap":"¥17T",   "beta":2.20, "lastUpdated":"2026-01"},
    "ROK":   {"pe":31.0, "roe":33.0, "fcfYield":3.0, "debtEquity":1.00, "netMargin":13.5, "divYield":1.5, "revenueGrowth":4.0,  "eps":10.5,  "mktCap":"$37B",   "beta":1.20, "lastUpdated":"2026-01"},
    "TER":   {"pe":36.0, "roe":21.0, "fcfYield":2.2, "debtEquity":0.05, "netMargin":19.0, "divYield":0.4, "revenueGrowth":12.0, "eps":3.6,   "mktCap":"$21B",   "beta":1.50, "lastUpdated":"2026-01"},
    "ISRG":  {"pe":66.0, "roe":17.5, "fcfYield":1.4, "debtEquity":0.00, "netMargin":28.5, "divYield":0,   "revenueGrowth":16.0, "eps":8.2,   "mktCap":"$190B",  "beta":1.30, "lastUpdated":"2026-01"},
    "CGNX":  {"pe":44.0, "roe":11.0, "fcfYield":2.0, "debtEquity":0.05, "netMargin":13.0, "divYield":0.8, "revenueGrowth":7.0,  "eps":0.95,  "mktCap":"$7B",    "beta":1.50, "lastUpdated":"2026-01"},
    "NOVT":  {"pe":42.0, "roe":13.0, "fcfYield":2.3, "debtEquity":0.50, "netMargin":11.0, "divYield":0,   "revenueGrowth":6.0,  "eps":3.3,   "mktCap":"$5B",    "beta":1.30, "lastUpdated":"2026-01"},
    # Quantum baggers (waardering irrelevant; bagger-velden leidend)
    "IONQ":  {"pe":None, "roe":-35.0,"fcfYield":-8.0, "debtEquity":0.10, "netMargin":-180.0,"divYield":0, "revenueGrowth":85.0, "eps":-1.4,  "mktCap":"$12B",   "beta":3.50, "lastUpdated":"2026-01",
              "grossMargin":55.0, "grossMarginTrend":3.0,  "revenueGrowthPrev":95.0,  "cashRunwayMonths":40},
    "RGTI":  {"pe":None, "roe":-25.0,"fcfYield":-15.0,"debtEquity":0.15, "netMargin":-350.0,"divYield":0, "revenueGrowth":20.0, "eps":-0.15, "mktCap":"$4B",    "beta":4.00, "lastUpdated":"2026-01",
              "grossMargin":50.0, "grossMarginTrend":-5.0, "revenueGrowthPrev":10.0,  "cashRunwayMonths":30},
    "QBTS":  {"pe":None, "roe":-40.0,"fcfYield":-12.0,"debtEquity":0.20, "netMargin":-400.0,"divYield":0, "revenueGrowth":110.0,"eps":-0.25, "mktCap":"$3.5B",  "beta":4.20, "lastUpdated":"2026-01",
              "grossMargin":62.0, "grossMarginTrend":5.0,  "revenueGrowthPrev":65.0,  "cashRunwayMonths":30},
    # Robotics speculatief (baggers)
    "UBTECH":{"pe":None, "roe":-20.0,"fcfYield":-10.0,"debtEquity":0.50, "netMargin":-30.0, "divYield":0, "revenueGrowth":32.0, "eps":-2.2,  "mktCap":"HK$55B", "beta":2.50, "lastUpdated":"2026-01",
              "grossMargin":30.0, "grossMarginTrend":1.5,  "revenueGrowthPrev":25.0,  "cashRunwayMonths":15},
    "SYM":   {"pe":None, "roe":2.0,  "fcfYield":1.0,  "debtEquity":0.10, "netMargin":0.5,   "divYield":0, "revenueGrowth":28.0, "eps":0.05,  "mktCap":"$23B",   "beta":2.30, "lastUpdated":"2026-01",
              "grossMargin":17.0, "grossMarginTrend":1.5,  "revenueGrowthPrev":35.0,  "cashRunwayMonths":None},
    "SERV":  {"pe":None, "roe":-60.0,"fcfYield":-20.0,"debtEquity":0.10, "netMargin":-900.0,"divYield":0, "revenueGrowth":150.0,"eps":-0.90, "mktCap":"$1.5B",  "beta":3.80, "lastUpdated":"2026-01",
              "grossMargin":35.0, "grossMarginTrend":4.0,  "revenueGrowthPrev":200.0, "cashRunwayMonths":24},
    "RR":    {"pe":None, "roe":-30.0,"fcfYield":-15.0,"debtEquity":0.05, "netMargin":-120.0,"divYield":0, "revenueGrowth":60.0, "eps":-0.10, "mktCap":"$0.4B",  "beta":3.50, "lastUpdated":"2026-01",
              "grossMargin":45.0, "grossMarginTrend":2.0,  "revenueGrowthPrev":90.0,  "cashRunwayMonths":20},
    "PL":    {"pe":None, "roe":-8.0, "fcfYield":-1.0, "debtEquity":0.05, "netMargin":-10.0, "divYield":0, "revenueGrowth":18.0, "eps":-0.08, "mktCap":"$3.8B",  "beta":2.30, "lastUpdated":"2026-01",
              "grossMargin":58.0, "grossMarginTrend":4.0,  "revenueGrowthPrev":11.0,  "cashRunwayMonths":None},
}

# ── TECHNISCHE INDICATOREN ────────────────────────────────────────────────────
def wilder_rma(values: pd.Series, period: int) -> pd.Series:
    """
    Wilder's RMA met correcte SMA-seed (eerste waarde = simpel gemiddelde van
    de eerste `period` punten, daarna recursief gladgestreken).
    Dit matcht TradingView's RSI/ATR exact, ook op kortere reeksen.
    """
    v = values.values
    out = np.full(len(v), np.nan)
    if len(v) < period:
        return pd.Series(out, index=values.index)
    out[period - 1] = np.nanmean(v[:period])  # SMA-seed
    alpha = 1.0 / period
    for i in range(period, len(v)):
        prev, cur = out[i - 1], v[i]
        out[i] = prev if np.isnan(cur) else prev * (1 - alpha) + cur * alpha
    return pd.Series(out, index=values.index)

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI met Wilder's RMA — matcht TradingView's standaard RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0).fillna(0.0).iloc[1:]
    loss = (-delta).clip(lower=0.0).fillna(0.0).iloc[1:]
    avg_gain = wilder_rma(gain, period)
    avg_loss = wilder_rma(loss, period)
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)  # enkel stijging → RSI 100
    return rsi.reindex(series.index)

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()

def calc_macd(series: pd.Series):
    ema12  = calc_ema(series, 12)
    ema26  = calc_ema(series, 26)
    line   = ema12 - ema26
    signal = line.ewm(span=9, adjust=False, min_periods=9).mean()
    hist   = line - signal
    return line, signal, hist

def calc_bollinger(series: pd.Series, period: int = 20, mult: float = 2.0):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)  # population std, zoals TradingView
    return mid + mult * std, mid, mid - mult * std

def calc_fibonacci(swing_low: float, swing_high: float,
                   ext_low: float = None, ext_high: float = None) -> dict:
    """Twee onafhankelijke fib-sets met eigen swings:

    RETRACEMENTS (entry-zoeker) — swing bodem→recente top. We nemen aan dat de top
      net gezet is; niveaus 0.236–0.886 zijn steunzones ONDER de top waar je een
      instap zoekt, met de golden pocket (0.618–0.705) als premium entry.
      Hoog niveau = ondiepe pullback = prijs vlak onder de top.

    EXTENSIES (winstnemer) — aparte swing (bij voorkeur weekly top→bodem), omhoog
      geprojecteerd naar take-profit-zones BOVEN de huidige prijs: 1.272–2.618.

    Als ext_low/ext_high niet gegeven zijn, valt de extensieset terug op dezelfde swing.
    """
    if swing_high <= swing_low or swing_low <= 0:
        return {"retracements": {}, "extensions": {}, "goldenPocket": None,
                "swingHigh": swing_high, "swingLow": swing_low,
                "extSwingHigh": ext_high, "extSwingLow": ext_low, "logScale": True}

    # ── LOGARITMISCHE fib-berekening ──────────────────────────────────────────
    # Charts worden op log-schaal gelezen (zoals TradingView in log-mode): procentuele
    # bewegingen wegen gelijk. Een fib-niveau op fractie f ligt op:
    #     prijs = exp( log(low) + f * (log(high) - log(low)) )
    # Dit is cruciaal bij aandelen met grote koersrange (bv. PL $1.67 → $12.37 → $50+),
    # waar lineaire projectie de TP-zones veel te laag zou zetten.
    import math as _m
    def _logfib(lo, hi, f):
        return round(_m.exp(_m.log(lo) + f * (_m.log(hi) - _m.log(lo))), 2)

    # ── Retracementset: 0.000 = top, 1.000 = bodem (hoog pct = diepe pullback) ──
    # Op log-schaal: prijs = exp( log(high) - pct*(log(high)-log(low)) )
    log_hi, log_lo = _m.log(swing_high), _m.log(swing_low)
    log_rng = log_hi - log_lo
    def _logretr(pct):
        return round(_m.exp(log_hi - pct * log_rng), 2)
    retr = {lbl: _logretr(pct) for lbl, pct in
            [("0.000",0.000),("0.236",0.236),("0.382",0.382),("0.500",0.500),
             ("0.618",0.618),("0.705",0.705),("0.786",0.786),("0.886",0.886),("1.000",1.000),
             ("1.272",1.272),("1.618",1.618)]}
    gp_low  = _logretr(0.705)   # dieper (lagere prijs)
    gp_high = _logretr(0.618)   # ondieper (hogere prijs)

    # ── Extensieset: eigen swing bodem→top, log-geprojecteerd BOVEN de top ──
    e_lo = ext_low  if ext_low  is not None else swing_low
    e_hi = ext_high if ext_high is not None else swing_high
    if e_hi <= e_lo or e_lo <= 0:
        e_lo, e_hi = swing_low, swing_high
    ext = {lbl: _logfib(e_lo, e_hi, pct) for lbl, pct in
           [("0.000",0.000),("1.000",1.000),("1.272",1.272),("1.414",1.414),
            ("1.618",1.618),("1.818",1.818),("2.000",2.000),("2.618",2.618)]}

    return {"retracements": retr, "extensions": ext,
            "goldenPocket": {"low": gp_low, "high": gp_high},
            "swingHigh": round(swing_high, 2), "swingLow": round(swing_low, 2),
            "extSwingHigh": round(e_hi, 2), "extSwingLow": round(e_lo, 2),
            "logScale": True}

def safe_last(series: pd.Series, default=None):
    """Laatste niet-NaN waarde, of default. Voorkomt stille NaN-fouten."""
    if series is None or len(series) == 0:
        return default
    s = series.dropna()
    if len(s) == 0:
        return default
    return float(s.iloc[-1])

def crossed_up(line: pd.Series, ref: pd.Series) -> bool:
    """
    True als 'line' boven 'ref' kruiste op de LAATSTE candle (tekenwissel).
    Matcht TradingView's crossover(): vuurt enkel op de candle waar de wissel gebeurt.
    Gaten (feestdagen/weekends) worden opgevangen door de laatste twee GELDIGE
    vergelijkingspunten te nemen na uitlijning en dropna.
    """
    l = line.dropna()
    r = ref.reindex(l.index).dropna()
    common = l.index.intersection(r.index)
    if len(common) < 2:
        return False
    diff = (l.loc[common] - r.loc[common])
    return bool(diff.iloc[-1] > 0 and diff.iloc[-2] <= 0)

def crossed_down(line: pd.Series, ref: pd.Series) -> bool:
    """True als 'line' onder 'ref' kruiste op de laatste candle (tekenwissel)."""
    l = line.dropna()
    r = ref.reindex(l.index).dropna()
    common = l.index.intersection(r.index)
    if len(common) < 2:
        return False
    diff = (l.loc[common] - r.loc[common])
    return bool(diff.iloc[-1] < 0 and diff.iloc[-2] >= 0)

def prox_pct(price: float, level: float) -> float:
    if price == 0:
        return 999.0
    return abs((price - level) / price * 100)

# ── DATA OPHALEN (batch) ──────────────────────────────────────────────────────
def sanity_check(df: pd.DataFrame, name: str) -> Tuple[bool, str]:
    """Controleer of de prijsdata plausibel is."""
    if df is None or df.empty:
        return False, "lege dataset"
    if len(df) < 60:
        return False, f"te weinig candles ({len(df)})"
    closes = df["Close"].dropna()
    if (closes <= 0).any():
        return False, "negatieve of nul-prijzen gevonden"
    # Dag-op-dag beweging > 60% is verdacht (behalve bekende splits, maar auto_adjust vangt die)
    pct_change = closes.pct_change().abs()
    if (pct_change > 0.60).sum() > 0:
        n = int((pct_change > 0.60).sum())
        # Niet hard falen — waarschuwen, kan legitiem zijn bij extreme volatiliteit
        return True, f"⚠ {n} dag(en) met >60% beweging (mogelijk data-artefact)"
    return True, "ok"

def fetch_all(watchlist) -> dict:
    """
    Batch-download alle tickers in één yfinance-call.
    Geeft dict terug: dashboard_naam -> {"daily": df, "weekly": df, "ticker": used}
    """
    # Bouw mapping van alle te proberen tickers
    primary_map = {name: prim for (name, prim, _fb) in watchlist}
    fallback_map = {name: fb for (name, _p, fb) in watchlist if fb}

    # Benchmark meebestellen in dezelfde batch (efficiënt)
    all_tickers = list(primary_map.values()) + [BENCHMARK_TICKER] + list(MARKET_TICKERS.values())
    result = {}

    print(f"Batch-download van {len(all_tickers)} tickers (incl. benchmark {BENCHMARK_TICKER})...")
    # Download in blokken: één probleemticker of netwerk-hik kost hooguit zijn eigen blok,
    # en dat blok krijgt daarna nog een individuele herkansing per ticker.
    CHUNK = 20
    data_parts = []
    for i in range(0, len(all_tickers), CHUNK):
        chunk = all_tickers[i:i+CHUNK]
        try:
            part = yf.download(chunk, period="5y", interval="1d", auto_adjust=True,
                               group_by="ticker", progress=False, threads=True, timeout=60)
            if part is not None and not part.empty:
                if not isinstance(part.columns, pd.MultiIndex):
                    part = pd.concat({chunk[0]: part}, axis=1)
                data_parts.append(part)
                print(f"  ✓ blok {i//CHUNK+1} ({len(chunk)} tickers) binnen")
            else:
                raise ValueError("leeg resultaat")
        except Exception as e:
            print(f"  ⚠ blok {i//CHUNK+1} faalde ({type(e).__name__}: {e}) — tickers individueel...")
            for tk in chunk:
                try:
                    p1 = yf.download(tk, period="5y", interval="1d", auto_adjust=True,
                                     progress=False, timeout=30)
                    if p1 is not None and not p1.empty:
                        if isinstance(p1.columns, pd.MultiIndex):
                            p1.columns = p1.columns.get_level_values(-1)
                        data_parts.append(pd.concat({tk: p1}, axis=1))
                except Exception as e2:
                    print(f"    ✗ {tk}: {e2}")
    try:
        data = pd.concat(data_parts, axis=1) if data_parts else None
    except Exception as e:
        print(f"  ✗ Samenvoegen van blokken faalde: {e}")
        data = None
    if data is not None:
        # Dubbele kolommen (zelfde ticker 2×) veilig verwijderen
        data = data.loc[:, ~data.columns.duplicated()]

    for (name, primary, fallback) in watchlist:
        df = None
        used = None

        # Probeer primaire ticker uit batch
        if data is not None:
            try:
                if len(all_tickers) == 1:
                    candidate = data.copy()
                else:
                    candidate = data[primary].copy()
                candidate = candidate.dropna(how="all")
                ok, msg = sanity_check(candidate, name)
                if ok:
                    df, used = candidate, primary
                    if "⚠" in msg:
                        print(f"  {name} ({primary}): {msg}")
            except (KeyError, Exception):
                pass

        # Fallback: aparte download
        if df is None and fallback:
            print(f"  {name}: primaire ticker faalde, probeer fallback {fallback}...")
            try:
                candidate = yf.download(fallback, period="5y", interval="1d",
                                        auto_adjust=True, progress=False, timeout=30)
                if isinstance(candidate.columns, pd.MultiIndex):
                    candidate.columns = candidate.columns.get_level_values(0)
                candidate = candidate.dropna(how="all")
                ok, msg = sanity_check(candidate, name)
                if ok:
                    df, used = candidate, fallback
            except Exception as e:
                print(f"  ✗ {name} fallback faalde: {e}")

        if df is None:
            print(f"  ✗ {name}: geen bruikbare data")
            result[name] = None
            continue

        # Normaliseer kolommen
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()

        # Weekly resample, en gooi de LAATSTE (lopende, onvolledige) week weg
        weekly = df.resample("W-FRI").agg({
            "Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"
        }).dropna()
        if len(weekly) > 0 and weekly.index[-1].date() >= TODAY:
            weekly = weekly.iloc[:-1]  # lopende week is onvolledig

        # Monthly resample (ME = month-end), gooi de lopende onvolledige maand weg.
        monthly = df.resample("ME").agg({
            "Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"
        }).dropna()
        if len(monthly) > 0:
            # De laatste maandcandle is onvolledig tenzij we de laatste handelsdag
            # van de maand voorbij zijn. Veilig: vergelijk maand van laatste candle met huidige maand.
            last_month = (monthly.index[-1].year, monthly.index[-1].month)
            if last_month == (TODAY.year, TODAY.month):
                monthly = monthly.iloc[:-1]

        print(f"  ✓ {name} ({used}): {len(df)} dag, {len(weekly)} week, {len(monthly)} maand candles")
        result[name] = {"daily": df, "weekly": weekly, "monthly": monthly, "ticker": used}

    # Benchmark-serie apart bewaren (alleen slotkoersen nodig)
    bench = None
    if data is not None:
        try:
            bench_df = data[BENCHMARK_TICKER].copy() if len(all_tickers) > 1 else data.copy()
            bench_df = bench_df.dropna(how="all")
            if not bench_df.empty and "Close" in bench_df.columns:
                bench = bench_df["Close"].copy()
                bench.index = pd.to_datetime(bench.index)
                bench = bench.sort_index()
                print(f"  ✓ Benchmark {BENCHMARK_TICKER}: {len(bench)} slotkoersen")
        except (KeyError, Exception) as e:
            print(f"  ⚠ Benchmark ophalen faalde: {e}")
    result["__benchmark__"] = bench

    # Markt-reeksen (alleen slotkoersen) apart bewaren
    market = {}
    if data is not None:
        for key, tk in MARKET_TICKERS.items():
            try:
                mdf = data[tk].dropna(how="all")
                if not mdf.empty and "Close" in mdf.columns:
                    s = mdf["Close"].copy()
                    s.index = pd.to_datetime(s.index)
                    market[key] = s.sort_index()
            except (KeyError, Exception):
                pass
    print(f"  ✓ Markt-reeksen: {len(market)}/{len(MARKET_TICKERS)} beschikbaar")
    result["__market__"] = market

    return result

# ── SIGNAAL ENGINE ────────────────────────────────────────────────────────────
def _monthly_state(monthly):
    """Drie-toestanden-oordeel over de monthly (richting-anker).
    Returns: ("strong_bear" | "light_bear" | "neutral" | "bull", detail-dict).

    strong_bear = EMA bearish gekruist + MACD bearish & dalend  -> VETO op koop
    light_bear  = reversal-tekenen: RSI oversold, MACD keert & dicht bij kruising,
                  verkoopvolume neemt week na week af           -> koop mits confluence
    """
    d = {}
    if monthly is None or len(monthly) < 35:
        return "neutral", d
    cm = monthly["Close"]
    ema8  = calc_ema(cm, 8);  ema21 = calc_ema(cm, 21)
    e8, e21 = safe_last(ema8), safe_last(ema21)
    macd_l, macd_s, macd_h = calc_macd(cm)
    ml, ms = safe_last(macd_l), safe_last(macd_s)
    hist = (macd_l - macd_s).dropna()
    rsi_m = safe_last(calc_rsi(cm, 14), 50.0)
    d["ema8"], d["ema21"], d["rsi"] = e8, e21, round(rsi_m,1)
    d["macdLine"], d["macdSignal"] = (round(ml,3) if ml else None), (round(ms,3) if ms else None)

    ema_bear = (e8 is not None and e21 is not None and e8 < e21)
    macd_bear = (ml is not None and ms is not None and ml < ms)
    macd_falling = len(hist) >= 2 and hist.iloc[-1] < hist.iloc[-2]
    macd_rising  = len(hist) >= 2 and hist.iloc[-1] > hist.iloc[-2]
    # MACD "dicht bij kruising": lijn en signaal binnen kleine marge
    macd_near_cross = (ml is not None and ms is not None and
                       abs(ml - ms) < (abs(ms) * 0.15 + 1e-9))
    # Verkoopvolume neemt af: laatste 3 maandvolumes dalend
    vol_declining = False
    if "Volume" in monthly.columns and len(monthly) >= 4:
        v = monthly["Volume"].dropna()
        if len(v) >= 3:
            vol_declining = v.iloc[-1] < v.iloc[-2] < v.iloc[-3]
    # Prijs vlakt af of draait: de recente 3 closes dalen niet meer gestaag.
    # ZONDER dit tellen RSI-oversold en MACD-nabijheid ten onrechte als reversal
    # tijdens een vrije val (RSI is dan permanent laag). Reversal vereist stabilisatie.
    price_stabilizing = False
    if len(cm) >= 4:
        c1, c2, c3 = cm.iloc[-1], cm.iloc[-2], cm.iloc[-3]
        price_stabilizing = not (c1 < c2 < c3)   # niet drie op rij lager = afvlakking/kering
    d["emaBear"], d["macdBear"], d["rsiOversold"] = ema_bear, macd_bear, rsi_m < 35
    d["volDeclining"], d["macdNearCross"], d["macdRising"] = vol_declining, macd_near_cross, macd_rising

    # Reversal-tekenen tellen ALLEEN als de prijs stabiliseert (niet in vrije val).
    reversal_signs = 0
    if price_stabilizing:
        reversal_signs = sum([rsi_m < 40, macd_near_cross or macd_rising, vol_declining])
    d["priceStabilizing"] = price_stabilizing

    # STRONG BEAR: EMA bearish gekruist én MACD bearish (lijn onder signaal, beide negatief).
    # Een diep-negatieve, gevestigde MACD IS het sterkste bear-signaal — geen 'dalend' vereist.
    # UITZONDERING: als er ≥2 duidelijke reversal-tekenen zijn, degradeer naar light_bear
    # (de bodem lijkt nabij → koop mag weer, mits confluence).
    macd_deep_bear = macd_bear and (ml is not None and ml < 0)
    if ema_bear and macd_deep_bear:
        if reversal_signs >= 2:
            return "light_bear", d   # bearish maar met keer-tekenen → vroege instap mogelijk
        return "strong_bear", d
    # Eén van beide bearish, of ondiepe MACD → licht bearish (onder druk, niet gebroken)
    if ema_bear or macd_bear:
        return "light_bear", d
    # Beide bullish → bull; anders neutraal
    return ("bull" if (ml is not None and ms is not None and ml > ms) else "neutral"), d


def _weekly_turn(weekly):
    """Weekly timing-oordeel (de scherprechter). Returns dict met bullish/bearish draai."""
    d = {}
    if weekly is None or len(weekly) < 30:
        return d
    cw = weekly["Close"]
    ema8 = calc_ema(cw, 8); ema21 = calc_ema(cw, 21)
    e8, e21 = safe_last(ema8), safe_last(ema21)
    macd_l, macd_s, _ = calc_macd(cw)
    ml, ms = safe_last(macd_l), safe_last(macd_s)
    rsi_w = safe_last(calc_rsi(cw, 14), 50.0)
    d["emaBullish"] = (e8 is not None and e21 is not None and e8 > e21)
    d["emaBearish"] = (e8 is not None and e21 is not None and e8 < e21)
    d["emaCrossUp"] = crossed_up(ema8, ema21)
    d["emaCrossDown"] = crossed_down(ema8, ema21)
    d["macdBullish"] = (ml is not None and ms is not None and ml > ms)
    d["rsi"] = round(rsi_w, 1)
    d["oversold"] = rsi_w < 40
    d["overbought"] = rsi_w > 70
    # "Bullish draai": EMA bullish (of net gekruist) én momentum mee
    d["bullTurn"] = (d["emaBullish"] or d["emaCrossUp"]) and d["macdBullish"]
    d["bearTurn"] = (d["emaBearish"] or d["emaCrossDown"]) and not d["macdBullish"]
    return d


def _fib_buy_depth(last, fib):
    """Hoe diep in de koop-retracement zit de prijs? 0 = niet, 1..N = toenemend interessant.
    Koopzone = golden pocket (0.618/0.705) t/m 1.818 (dieper = interessanter).
    """
    if not fib:
        return 0, None
    retr = fib.get("retracements", {})
    # Levels van ondiep->diep die als koopzone tellen (vanaf 0.618 dieper)
    order = ["0.618","0.705","0.786","0.886","1.000"]
    # 1.0 -> swing low; nog dieper (richting 1.272..1.818 onder de bodem) ook koop
    deeper = ["1.272","1.414","1.618","1.818"]  # deze staan in extensions bij een DALING onder de low
    zone_level = None
    depth = 0
    for i, lbl in enumerate(order, start=1):
        lvl = retr.get(lbl)
        if lvl is not None and last <= lvl * 1.02:  # prijs op of onder dit level
            depth = i
            zone_level = lbl
    # Nog dieper dan de swing low? (prijs onder 1.000) -> extra diepte via extensies
    lvl_100 = retr.get("1.000")
    if lvl_100 is not None and last < lvl_100:
        exts = fib.get("extensions", {})
        for j, lbl in enumerate(deeper, start=len(order)+1):
            lvl = exts.get(lbl)
            if lvl is not None and last <= lvl:
                depth = j; zone_level = lbl
    return depth, zone_level


def _support_confluence(last, daily, weekly, monthly_state):
    """Detecteert 200-MA-steun en onderste weekly-Bollinger-steun als KOOP-bewijs.

    Cruciaal (uit onderzoek): een bandaanraking/MA-tik is ALLEEN steun in een intacte
    of neutrale trend. In een sterke downtrend 'walkt' de prijs langs de band / stuitert
    van de MA als weerstand -> geen koopsignaal, maar vallend mes. Daarom telt steun
    NIET mee als de monthly strong_bear is.

    Returns (count, flags): aantal samenvallende steunen + beschrijvingen.
    """
    flags = []
    if monthly_state == "strong_bear":
        return 0, flags   # walking-the-band-regime: steun telt niet

    close_d = daily["Close"]
    rsi_d = safe_last(calc_rsi(close_d, 14), 50.0)
    oversold = rsi_d < 45   # bevestiging vereist (bron: nooit kale bandtouch)

    # 200-daagse MA: steun alleen als de MA STIJGT (bron: dalende MA = weerstand)
    ma200 = close_d.rolling(200).mean()
    m_last = safe_last(ma200)
    if m_last is not None and len(ma200.dropna()) > 20:
        ma_rising = m_last > safe_last(ma200.iloc[:-20], m_last)
        near_ma = prox_pct(last, m_last) < 3.0 and last >= m_last * 0.98
        if near_ma and ma_rising and oversold:
            flags.append("200-MA-steun (stijgende MA + oversold)")

    # Onderste weekly-Bollinger: mean-reversion-bounce, alleen met oversold-bevestiging
    if weekly is not None and len(weekly) >= 25:
        cw = weekly["Close"]
        _, _, bb_l_w = calc_bollinger(cw, 20)
        bl = safe_last(bb_l_w)
        rsi_w = safe_last(calc_rsi(cw, 14), 50.0)
        if bl is not None and last <= bl * 1.02 and rsi_w < 40:
            flags.append("onderste weekly-Bollinger (oversold bounce)")

    return len(flags), flags


def generate_signals(name: str, daily: pd.DataFrame, weekly: pd.DataFrame, monthly: pd.DataFrame = None) -> dict:
    signals, alerts = [], []

    close_d = daily["Close"]
    vol_d   = daily["Volume"]
    last    = safe_last(close_d)
    if last is None:
        return {"error": "geen geldige slotkoers", "signals": [], "alerts": []}

    # Daily indicatoren
    rsi_d   = calc_rsi(close_d, 14)
    ema8_d  = calc_ema(close_d, 8)
    ema21_d = calc_ema(close_d, 21)
    ma50_d  = close_d.rolling(50).mean()
    ma200_d = close_d.rolling(200).mean()
    bb_u, bb_m, bb_l = calc_bollinger(close_d, 20)
    macd_l, macd_s, macd_h = calc_macd(close_d)

    # Volume met NaN-guard
    vol_avg20 = vol_d.rolling(20).mean()
    last_vol_avg = safe_last(vol_avg20)
    last_vol = safe_last(vol_d)
    if last_vol_avg and last_vol_avg > 0 and last_vol is not None:
        vol_ratio = last_vol / last_vol_avg
        vol_known = True
    else:
        vol_ratio = 1.0
        vol_known = False
    high_volume = vol_known and vol_ratio > 1.5

    # Laatste waarden met guards
    last_rsi_d  = safe_last(rsi_d, 50.0)
    last_ema8d  = safe_last(ema8_d, last)
    last_ema21d = safe_last(ema21_d, last)
    last_ma50   = safe_last(ma50_d)   # kan None zijn
    last_ma200  = safe_last(ma200_d)  # kan None zijn
    last_bb_u   = safe_last(bb_u, last * 1.05)
    last_bb_l   = safe_last(bb_l, last * 0.95)
    last_macd_l = safe_last(macd_l, 0.0)
    last_macd_s = safe_last(macd_s, 0.0)

    # Weekly indicatoren met guards
    has_weekly = weekly is not None and len(weekly) >= 30
    if has_weekly:
        close_w = weekly["Close"]
        rsi_w   = calc_rsi(close_w, 14)
        ema8_w  = calc_ema(close_w, 8)
        ema21_w = calc_ema(close_w, 21)
        macd_wl, macd_ws, _ = calc_macd(close_w)
        last_rsi_w   = safe_last(rsi_w, 50.0)
        last_ema8w   = safe_last(ema8_w, last)
        last_ema21w  = safe_last(ema21_w, last)
        last_macd_wl = safe_last(macd_wl, 0.0)
        last_macd_ws = safe_last(macd_ws, 0.0)
    else:
        close_w = None
        last_rsi_w = last_ema8w = last_ema21w = None
        last_macd_wl = last_macd_ws = None

    # ── Fib-swings volgens de meerjarige structuur (log-schaal) ──
    # KERN: de diepste bodem in ~5 jaar is het draaipunt. Daaromheen liggen twee tops:
    #   - de HISTORISCHE TOP VÓÓR de bodem (waar de grote daling begon)
    #   - de HERSTEL-TOP NÁ de bodem (de nieuwe stijging sindsdien)
    #
    # TP-METING (extensie): van de historische top VÓÓR de bodem, omlaag naar de bodem,
    #   omhoog geprojecteerd. Bij PL: top $12.37 (2021) → bodem $1.67 (2024) → 1.618 ≈ $50.
    #   Dit geeft de take-profit-zones voor de huidige uptrend.
    # ENTRY-METING (retracement): van de bodem naar de herstel-top erná. Bij een
    #   sterk hersteld aandeel is de golden pocket daarvan de interessante instapzone.
    lo_idx   = daily["Low"].idxmin()                     # diepste punt in ~5j = draaipunt
    swing_lo = float(daily["Low"].min())

    # Historische top VÓÓR de bodem (chronologisch eerder dan de bodem)
    before_lo = daily[daily.index < lo_idx]
    if len(before_lo) >= 10:
        hist_top = float(before_lo["High"].max())        # bv. PL $12.37 van 2021
    else:
        # Geen geschiedenis vóór de bodem (bodem ligt helemaal aan het begin) →
        # gebruik de herstel-top als terugval voor de TP-meting.
        hist_top = None

    # Herstel-top NÁ de bodem (de nieuwe stijging)
    since_lo = daily[daily.index >= lo_idx]
    recovery_top = float(since_lo["High"].max())         # bv. PL $51.76 van 2026

    # ENTRY-swing (retracement): bodem → herstel-top
    swing_hi = recovery_top

    # TP-swing (extensie): historische top → bodem, omhoog geprojecteerd.
    # calc_fibonacci projecteert ext van ext_low omhoog; we willen de 1.618 boven de
    # HISTORISCHE top uitkomen. Dus ext_low = bodem, ext_high = historische top.
    if hist_top is not None and hist_top > swing_lo:
        ext_lo, ext_hi = swing_lo, hist_top
    else:
        # Terugval: geen historische top → gebruik de herstel-swing
        ext_lo, ext_hi = swing_lo, recovery_top
    fib      = calc_fibonacci(swing_lo, swing_hi, ext_low=ext_lo, ext_high=ext_hi)

    vol_note = " ✓ hoog volume" if high_volume else (" (laag volume)" if vol_known else "")

    # ── 1. RSI DAILY ──
    if last_rsi_d <= 30:
        signals.append({"type":"BUY","cat":"RSI","tf":"1D","weight":3,"icon":"📉",
            "title":f"RSI daily oversold ({last_rsi_d:.0f}){vol_note}",
            "detail":f"RSI {last_rsi_d:.1f} — historisch koopniveau."})
    elif last_rsi_d <= 40:
        alerts.append({"type":"WATCH","cat":"RSI","tf":"1D","icon":"👀",
            "title":f"RSI daily nadert oversold ({last_rsi_d:.0f})"})
    elif last_rsi_d >= 70:
        signals.append({"type":"SELL","cat":"RSI","tf":"1D","weight":3,"icon":"📈",
            "title":f"RSI daily overbought ({last_rsi_d:.0f}){vol_note}",
            "detail":f"RSI {last_rsi_d:.1f} — overbought."})
    elif last_rsi_d >= 60:
        alerts.append({"type":"WATCH","cat":"RSI","tf":"1D","icon":"⚠️",
            "title":f"RSI daily nadert overbought ({last_rsi_d:.0f})"})

    # ── 2. RSI WEEKLY ──
    if has_weekly:
        if last_rsi_w <= 30:
            signals.append({"type":"BUY","cat":"RSI","tf":"1W","weight":4,"icon":"📉",
                "title":f"RSI WEEKLY oversold ({last_rsi_w:.0f})",
                "detail":"Sterk koopsignaal op hogere timeframe."})
        elif last_rsi_w <= 40:
            alerts.append({"type":"WATCH","cat":"RSI","tf":"1W","icon":"👀",
                "title":f"RSI weekly nadert oversold ({last_rsi_w:.0f})"})
        elif last_rsi_w >= 70:
            signals.append({"type":"SELL","cat":"RSI","tf":"1W","weight":4,"icon":"📈",
                "title":f"RSI WEEKLY overbought ({last_rsi_w:.0f})",
                "detail":"Sterk verkoopsignaal op hogere timeframe."})
        elif last_rsi_w >= 60:
            alerts.append({"type":"WATCH","cat":"RSI","tf":"1W","icon":"⚠️",
                "title":f"RSI weekly nadert overbought ({last_rsi_w:.0f})"})

    # ── 3. MACD DAILY (robuuste crossover) ──
    if crossed_up(macd_l, macd_s):
        signals.append({"type":"BUY","cat":"MACD","tf":"1D","weight":2,"icon":"🟢",
            "title":f"MACD bullish crossover (daily){vol_note}",
            "detail":f"MACD {last_macd_l:.3f} kruist boven signaal {last_macd_s:.3f}."})
    elif crossed_down(macd_l, macd_s):
        signals.append({"type":"SELL","cat":"MACD","tf":"1D","weight":2,"icon":"🔴",
            "title":f"MACD bearish crossover (daily){vol_note}",
            "detail":f"MACD {last_macd_l:.3f} kruist onder signaal {last_macd_s:.3f}."})

    # ── 4. MACD WEEKLY — alleen vrijdag, volledige candle ──
    if has_weekly and IS_FRIDAY:
        if crossed_up(macd_wl, macd_ws):
            signals.append({"type":"BUY","cat":"MACD","tf":"1W","weight":4,"icon":"🟢",
                "title":"MACD bullish crossover (WEEKLY) ⭐",
                "detail":f"Weekly MACD {last_macd_wl:.3f} kruist boven {last_macd_ws:.3f}. Krachtig."})
        elif crossed_down(macd_wl, macd_ws):
            signals.append({"type":"SELL","cat":"MACD","tf":"1W","weight":4,"icon":"🔴",
                "title":"MACD bearish crossover (WEEKLY) ⭐",
                "detail":f"Weekly MACD {last_macd_wl:.3f} kruist onder {last_macd_ws:.3f}. Krachtig."})
    elif has_weekly and not IS_FRIDAY and last_macd_wl is not None:
        direction = "bullish" if last_macd_wl > last_macd_ws else "bearish"
        alerts.append({"type":"INFO","cat":"MACD","tf":"1W","icon":"ℹ️",
            "title":f"MACD weekly momenteel {direction} (crossover enkel vrijdag geëvalueerd)",
            "detail":f"MACD: {last_macd_wl:.3f} | Signaal: {last_macd_ws:.3f}"})

    # ── 5. EMA 8/21 DAILY ──
    if crossed_up(ema8_d, ema21_d):
        signals.append({"type":"BUY","cat":"EMA","tf":"1D","weight":3,"icon":"🔀",
            "title":f"8 EMA kruist boven 21 EMA (daily){vol_note}",
            "detail":f"EMA8 ${last_ema8d:.2f} | EMA21 ${last_ema21d:.2f} — bullish momentum."})
    elif crossed_down(ema8_d, ema21_d):
        signals.append({"type":"SELL","cat":"EMA","tf":"1D","weight":3,"icon":"🔀",
            "title":f"8 EMA kruist onder 21 EMA (daily){vol_note}",
            "detail":f"EMA8 ${last_ema8d:.2f} | EMA21 ${last_ema21d:.2f} — bearish momentum."})

    # ── 6. EMA 8/21 WEEKLY — alleen vrijdag ──
    if has_weekly and IS_FRIDAY:
        if crossed_up(ema8_w, ema21_w):
            signals.append({"type":"BUY","cat":"EMA","tf":"1W","weight":4,"icon":"🔀",
                "title":"8 EMA kruist boven 21 EMA (WEEKLY) ⭐",
                "detail":f"EMA8 ${last_ema8w:.2f} | EMA21 ${last_ema21w:.2f} — krachtig bullish."})
        elif crossed_down(ema8_w, ema21_w):
            signals.append({"type":"SELL","cat":"EMA","tf":"1W","weight":4,"icon":"🔀",
                "title":"8 EMA kruist onder 21 EMA (WEEKLY) ⭐",
                "detail":f"EMA8 ${last_ema8w:.2f} | EMA21 ${last_ema21w:.2f} — krachtig bearish."})

    # ── 7. GOLDEN / DEATH CROSS — alleen als MA200 bestaat ──
    if last_ma50 is not None and last_ma200 is not None:
        if crossed_up(ma50_d, ma200_d):
            signals.append({"type":"BUY","cat":"MA","tf":"1D","weight":4,"icon":"✨",
                "title":"Golden Cross (MA50 boven MA200)",
                "detail":"Klassiek bull-marktsignaal. Historisch betrouwbaar."})
        elif crossed_down(ma50_d, ma200_d):
            signals.append({"type":"SELL","cat":"MA","tf":"1D","weight":4,"icon":"💀",
                "title":"Death Cross (MA50 onder MA200)",
                "detail":"Klassiek bear-marktsignaal."})
    else:
        alerts.append({"type":"INFO","cat":"MA","tf":"1D","icon":"ℹ️",
            "title":"MA50/MA200 niet beschikbaar (te weinig historie)",
            "detail":"Golden/Death cross vereist 200+ dagen data."})

    # ── 8. BOLLINGER BANDS ──
    bb_range = last_bb_u - last_bb_l
    if bb_range > 0:
        bb_pos = (last - last_bb_l) / bb_range
        if last <= last_bb_l:
            signals.append({"type":"BUY","cat":"BB","tf":"1D","weight":2,"icon":"🎯",
                "title":f"Prijs raakt Bollinger onderband (${last_bb_l:.2f}){vol_note}",
                "detail":"Statistische oversold conditie."})
        elif last >= last_bb_u:
            signals.append({"type":"SELL","cat":"BB","tf":"1D","weight":2,"icon":"🎯",
                "title":f"Prijs raakt Bollinger bovenband (${last_bb_u:.2f}){vol_note}",
                "detail":"Statistische overbought conditie."})
        elif bb_pos < 0.15:
            alerts.append({"type":"WATCH","cat":"BB","tf":"1D","icon":"📊",
                "title":f"Nadert Bollinger onderband (${last_bb_l:.2f})"})
        elif bb_pos > 0.85:
            alerts.append({"type":"WATCH","cat":"BB","tf":"1D","icon":"📊",
                "title":f"Nadert Bollinger bovenband (${last_bb_u:.2f})"})

    # ── 9. FIBONACCI ──
    PROX, NEAR = 1.5, 3.0
    all_fib = list(fib["retracements"].items()) + list(fib["extensions"].items())
    ext_keys = set(fib["extensions"].keys())
    for label, level in all_fib:
        dist = prox_pct(last, level)
        is_ext = label in ext_keys
        styp = "SELL" if is_ext else "BUY"
        zone = "take-profit zone" if is_ext else "steunzone"
        if dist <= PROX:
            signals.append({"type":styp,"cat":"FIB","tf":"1D","weight":3,"icon":"📐",
                "title":f"Fib {label} — {zone}: ${level:.2f} ({dist:.1f}% weg)",
                "detail":f"Prijs ${last:.2f} raakt Fibonacci {label}. "
                         f"{'Overweeg winstneming.' if is_ext else 'Potentiële koopzone.'}"})
        elif dist <= NEAR:
            alerts.append({"type":"WATCH","cat":"FIB","tf":"1D","icon":"📐",
                "title":f"Nadert Fib {label} {zone}: ${level:.2f} ({dist:.1f}% weg)"})

    # ── 10. MONTHLY MACD (zwaarste momentum-signaal) ──
    # Monthly weegt het zwaarst: een bearish/bullish MACD-stand op maandbasis is
    # een krachtig trendsignaal. We tonen de STAND (niet enkel de crossover-candle),
    # want maandcandles zijn zeldzaam en de stand is het bruikbare signaal.
    if monthly is not None and len(monthly) >= 35:
        close_m = monthly["Close"]
        macd_ml, macd_ms, _ = calc_macd(close_m)
        mm_l, mm_s = safe_last(macd_ml), safe_last(macd_ms)
        if mm_l is not None and mm_s is not None:
            if mm_l < mm_s:
                signals.append({"type":"SELL","cat":"MACD","tf":"1M","weight":4,"icon":"🔴",
                    "title":"MACD bearish (MONTHLY) ⭐⭐",
                    "detail":f"MACD {mm_l:.2f} onder signaal {mm_s:.2f} op maandbasis — "
                             "zwaarste momentum-tegenwind. Hoogste timeframe."})
            else:
                signals.append({"type":"BUY","cat":"MACD","tf":"1M","weight":4,"icon":"🟢",
                    "title":"MACD bullish (MONTHLY) ⭐⭐",
                    "detail":f"MACD {mm_l:.2f} boven signaal {mm_s:.2f} op maandbasis — "
                             "zwaarste momentum-rugwind. Hoogste timeframe."})

    # ── 11. TRENDRICHTING als expliciet signaal (multi-timeframe) ──
    # De trend zat tot nu alleen in de timing-SCORE; nu ook als zichtbaar koop/verkoopsignaal.
    # Downtrend op hogere timeframes = zwaar verkoopsignaal (weegt zwaarder dan daily-ruis).
    tr_w = _tf_trend_score(weekly["Close"]) if weekly is not None and len(weekly) >= 25 else None
    tr_m = _tf_trend_score(monthly["Close"]) if monthly is not None and len(monthly) >= 25 else None
    if tr_w is not None:
        if tr_w <= 35:
            signals.append({"type":"SELL","cat":"TREND","tf":"1W","weight":3,"icon":"📉",
                "title":"Downtrend (WEEKLY)",
                "detail":f"Weekly trendscore {tr_w}/100 — structureel dalend. "
                         "Weegt zwaarder dan daily koopsignalen."})
        elif tr_w >= 65:
            signals.append({"type":"BUY","cat":"TREND","tf":"1W","weight":3,"icon":"📈",
                "title":"Uptrend (WEEKLY)",
                "detail":f"Weekly trendscore {tr_w}/100 — structureel stijgend."})
    if tr_m is not None:
        if tr_m <= 35:
            signals.append({"type":"SELL","cat":"TREND","tf":"1M","weight":4,"icon":"📉",
                "title":"Downtrend (MONTHLY) ⭐",
                "detail":f"Monthly trendscore {tr_m}/100 — dalend op de hoogste timeframe. "
                         "Zwaarste trendsignaal."})
        elif tr_m >= 65:
            signals.append({"type":"BUY","cat":"TREND","tf":"1M","weight":4,"icon":"📈",
                "title":"Uptrend (MONTHLY) ⭐",
                "detail":f"Monthly trendscore {tr_m}/100 — stijgend op de hoogste timeframe."})

    # ── 12. TAKE-PROFIT-ZONE SIGNALEN (fib-extensies) ──────────────────────────
    # Twee mechanismen, zoals besproken:
    #  A) NABIJ een TP-extensie komen → verkoopsignaal. De 1.618 is het hoofdsignaal
    #     (zwaar, voor élk aandeel). De lagere zones (1.272/1.414) wegen zwaarder bij
    #     baggers (volatiel, lagere prijzen komen snel) dan bij kwaliteitsaandelen
    #     (die hou je langer vast; een gemiste lagere-TP-verkoop is minder erg).
    #  B) TERUGVAL uit een TP-zone met verzwakkend momentum (MACD-kruising op meerdere
    #     timeframes + dalend volume) → verkoopsignaal. Dit is het "de draai is al
    #     begonnen"-geval (PL terug van 1.618; IONQ/QBTS onder resistance 1.272).
    is_bagger = name in BAGGER_TICKERS
    exts = fib.get("extensions", {}) if fib else {}

    # TWEE tijdshorizonnen voor de twee fases:
    #  - VERKOOP (fase 1): verse terugval → kort venster (~3 maanden / 65 dagen).
    #    Na 3 maanden is een terugval geen actueel verkoopmoment meer.
    #  - KOOP (fase 2): uitgebodemd na TP → lang venster. De TP-aanraking mag lang
    #    geleden zijn; de instap hangt af van de HUIDIGE reversal, niet van recentheid.
    sell_win = min(len(daily), 65)     # ~3 maanden voor de verkoop-terugval
    sell_high = float(daily["High"].iloc[-sell_win:].max()) if sell_win >= 20 else last
    # Voor fase 2: de grote TP-piek over de hele dataperiode (kan >1 jaar geleden zijn)
    long_high = float(daily["High"].max())

    def _ext_val(lbl):
        try: return exts.get(lbl)
        except: return None

    # ── A) Prijs NABIJ een TP-extensie (binnen 3%) ──
    e1618 = _ext_val("1.618")
    if e1618 and prox_pct(last, e1618) < 3.0:
        signals.append({"type":"SELL","cat":"FIB","tf":"TP","weight":6,"icon":"🎯",
            "title":"Bij 1.618 TP-zone ⭐⭐ (winst nemen)",
            "detail":f"Prijs ${last:.2f} bij de 1.618-extensie (${e1618:.2f}) — de kern-"
                     "winstnemingszone. Historisch een sterk verkooppunt, geen instap."})
    else:
        # Lagere TP-zones: gewicht hangt af van bedrijfstype
        for lbl in ("1.414", "1.272"):
            lvl = _ext_val(lbl)
            if lvl and prox_pct(last, lvl) < 3.0:
                w = 4 if is_bagger else 2   # baggers: zwaarder; kwaliteit: lichter
                extra = " (bagger — lagere prijzen komen snel)" if is_bagger else ""
                signals.append({"type":"SELL","cat":"FIB","tf":"TP","weight":w,"icon":"🎯",
                    "title":f"Bij {lbl} TP-zone{' ⭐' if is_bagger else ''} (winst nemen)",
                    "detail":f"Prijs ${last:.2f} bij de {lbl}-extensie (${lvl:.2f}) — "
                             f"winstnemingszone{extra}."})
                break

    # ── B) TERUGVAL uit een TP-zone met verzwakkend momentum ──
    # Voorwaarden: (1) recente high raakte een TP-extensie ≥1.272, (2) prijs is nu
    # merkbaar teruggevallen van die high, (3) MACD kruist op ≥2 timeframes, (4) dalend
    # volume of onder de high. Dit vangt PL/IONQ/QBTS: de draai vanaf de TP is bezig.
    # Fase 1: raakte de prijs in de LAATSTE 3 MAANDEN een TP-zone? (verse terugval)
    tp_recent = None
    for lbl in ("1.618", "1.414", "1.272"):
        lvl = _ext_val(lbl)
        if lvl and sell_high >= lvl * 0.98:
            tp_recent = (lbl, lvl); break
    # Fase 2: raakte de prijs OOIT (hele periode) een TP-zone? (voor uitbodem-instap)
    tp_ever = None
    for lbl in ("1.618", "1.414", "1.272"):
        lvl = _ext_val(lbl)
        if lvl and long_high >= lvl * 0.98:
            tp_ever = (lbl, lvl); break

    # ── FASE 1: VERSE TERUGVAL uit TP → verkoopsignaal ──
    if tp_recent:
        lbl, lvl = tp_recent
        pulled_back = last < sell_high * 0.92   # ≥8% terug van de recente (3mnd) high
        macd_turning = _macd_rolling_over(daily["Close"], close_w, close_m)
        # Weekly/monthly MACD bearish gekruist telt zwaar mee
        wk_bear = False; mo_bear = False
        if close_w is not None and len(close_w) >= 35:
            mwl, mws, _ = calc_macd(close_w)
            wk_bear = safe_last(mwl) is not None and safe_last(mws) is not None and safe_last(mwl) < safe_last(mws)
        if close_m is not None and len(close_m) >= 35:
            mml, mms, _ = calc_macd(close_m)
            mo_bear = safe_last(mml) is not None and safe_last(mms) is not None and safe_last(mml) < safe_last(mms)
        vol_declining = False
        if "Volume" in daily.columns and len(daily) >= 15:
            v = daily["Volume"].dropna()
            if len(v) >= 10:
                vol_declining = v.iloc[-10:].mean() < v.iloc[-30:-10].mean() if len(v) >= 30 else False
        # Momentum verzwakt als: MACD draait op ≥1 TF, OF weekly/monthly al bearish gekruist
        momentum_weak = len(macd_turning) >= 1 or wk_bear or mo_bear

        # ── KANTELPUNT: nog vallend (verkoop) vs uitgebodemd (instapkans) ──
        # Zodra de reversal-tekenen verschijnen — RSI oversold, weekly MACD keert,
        # verkoopvolume daalt — is de daling waarschijnlijk uitgewerkt. Dan vervalt
        # het TP-verkoopsignaal en wordt dit een goede langetermijn-instap (niet per
        # se de bodem, wel gunstig risico). Zelfde reversal-logica als monthly licht-bearish.
        rsi_d_now = safe_last(calc_rsi(daily["Close"], 14), 50.0)
        rsi_w_now = safe_last(calc_rsi(close_w, 14), 50.0) if close_w is not None and len(close_w) >= 15 else 50.0
        oversold = rsi_d_now < 40 or rsi_w_now < 40
        # Weekly MACD keert: histogram draait omhoog (niet meer dalend)
        wk_macd_turning_up = False
        if close_w is not None and len(close_w) >= 35:
            mwl, mws, _ = calc_macd(close_w)
            wh = (mwl - mws).dropna()
            if len(wh) >= 2:
                wk_macd_turning_up = wh.iloc[-1] > wh.iloc[-2]
        # Prijs stabiliseert (niet meer in vrije val): laatste 3 weken niet gestaag lager
        price_stabilizing = False
        if close_w is not None and len(close_w) >= 4:
            c1, c2, c3 = close_w.iloc[-1], close_w.iloc[-2], close_w.iloc[-3]
            price_stabilizing = not (c1 < c2 < c3)
        # Reversal bevestigd: prijs stabiel + minstens 2 van (oversold, weekly MACD keert, dalend volume)
        reversal_confirmed = price_stabilizing and (
            sum([oversold, wk_macd_turning_up, vol_declining]) >= 2)

        if pulled_back and momentum_weak:
            # Nog vallend → verkoopsignaal (fase 1)
            w = 6 if is_bagger else 5
            redenen = []
            if wk_bear: redenen.append("weekly MACD bearish")
            if mo_bear: redenen.append("monthly MACD bearish")
            if macd_turning: redenen.append(f"MACD draait op {len(macd_turning)} TF")
            if vol_declining: redenen.append("dalend volume")
            signals.append({"type":"SELL","cat":"FIB","tf":"TP","weight":w,"icon":"📉",
                "title":f"Terugval uit {lbl} TP-zone " + ("⭐⭐" if is_bagger else "⭐"),
                "detail":f"Prijs zakte {(1-last/sell_high)*100:.0f}% van de recente top "
                         f"(${sell_high:.2f}, bij de {lbl}-TP-zone). "
                         + ", ".join(redenen) +
                         " — de draai vanaf de TP-zone is bezig, lagere prijzen in beeld."})

    # ── FASE 2: UITGEBODEMD na een (mogelijk lang geleden) TP-aanraking → instapkans ──
    # Onafhankelijk van fase 1: het aandeel raakte ooit een TP-zone, viel diep terug,
    # en bodemt nu uit met bevestigde reversal-tekenen. Alleen als het NIET meer in een
    # verse terugval zit (dat zou fase 1 = verkoop zijn) en flink onder de TP-piek staat.
    if tp_ever and not (tp_recent and last < sell_high * 0.92):
        lbl_e, lvl_e = tp_ever
        deep_below_tp = last < lvl_e * 0.75   # minstens 25% onder de TP-piek = echt teruggevallen
        rsi_d2 = safe_last(calc_rsi(daily["Close"], 14), 50.0)
        rsi_w2 = safe_last(calc_rsi(close_w, 14), 50.0) if close_w is not None and len(close_w) >= 15 else 50.0
        oversold2 = rsi_d2 < 45 or rsi_w2 < 45
        wk_up2 = False
        if close_w is not None and len(close_w) >= 35:
            mwl2, mws2, _ = calc_macd(close_w)
            wh2 = (mwl2 - mws2).dropna()
            if len(wh2) >= 2: wk_up2 = wh2.iloc[-1] > wh2.iloc[-2]
        stab2 = False
        if close_w is not None and len(close_w) >= 4:
            stab2 = not (close_w.iloc[-1] < close_w.iloc[-2] < close_w.iloc[-3])
        vol_decl2 = False
        if "Volume" in daily.columns and len(daily) >= 30:
            v2 = daily["Volume"].dropna()
            if len(v2) >= 30: vol_decl2 = v2.iloc[-10:].mean() < v2.iloc[-30:-10].mean()
        reversal2 = stab2 and (sum([oversold2, wk_up2, vol_decl2]) >= 2)
        if deep_below_tp and reversal2:
            w = 3 if is_bagger else 5
            tekenen = []
            if oversold2: tekenen.append("oversold")
            if wk_up2: tekenen.append("weekly MACD keert")
            if vol_decl2: tekenen.append("verkoopvolume daalt")
            voorzichtig = " (bagger - klein instappen, dieper dal mogelijk)" if is_bagger else ""
            signals.append({"type":"BUY","cat":"FIB","tf":"TP","weight":w,"icon":"🟢",
                "title":"Uitgebodemd na TP-terugval " + ("" if is_bagger else "⭐"),
                "detail":f"Na een diepe terugval van de {lbl_e}-TP-zone (${lvl_e:.2f}) "
                         f"stabiliseert de prijs op ${last:.2f}: " + ", ".join(tekenen) +
                         f". Niet per se de bodem, wel een gunstige langetermijn-instap{voorzichtig}."})

    # ── Conflict + score ──
    buy_sigs  = [s for s in signals if s["type"] == "BUY"]
    sell_sigs = [s for s in signals if s["type"] == "SELL"]
    conflict  = len(buy_sigs) > 0 and len(sell_sigs) > 0
    conflict_note = ""
    if conflict:
        conflict_note = (f"⚠️ CONFLICT: {len(buy_sigs)} koop vs {len(sell_sigs)} verkoop. "
                         "Gebruik hogere timeframe als beslissend of wacht op bevestiging.")

    buy_w  = sum(s.get("weight",1) for s in buy_sigs)
    sell_w = sum(s.get("weight",1) for s in sell_sigs)
    # Overall op NETTO gewicht: de dominante kant wint, met de sterkte van het
    # verschil. Zo kan een aandeel met zwaar verkoop-overwicht nooit "KOOP" tonen
    # omdat er toevallig één koopsignaal met gewicht ≥4 tussen zit.
    net = buy_w - sell_w

    # ── OVEREXTENSIE-VETO ──
    # Trend geeft richting, MAAR een aandeel in de take-profit-zone of ver boven de
    # 8-EMA is rijp voor terugval — dan mag "STERK KOOP" niet blijven staan, hoe
    # sterk de trend ook is. Dit vangt precies de overextended-markt-situatie.
    overext_flags = []
    # a) Prijs in/nabij een echte take-profit-extensie (≥1.272)?
    tp_zone = False
    for lbl, lvl in fib.get("extensions", {}).items():
        try:
            if float(lbl) >= 1.272 and prox_pct(last, lvl) < 2.5:
                tp_zone = True; break
        except (ValueError, TypeError):
            pass
    # b) Ver boven 8-EMA op weekly/monthly?
    wk_close = weekly["Close"] if (weekly is not None and len(weekly) >= 10) else None
    mo_close = monthly["Close"] if (monthly is not None and len(monthly) >= 10) else None
    overext_pen, overext_detail = _overextension_penalty(wk_close, mo_close)
    far_above_ema = overext_pen >= 12   # substantieel boven de 8-EMA
    if tp_zone:        overext_flags.append("take-profit-zone")
    if far_above_ema:  overext_flags.append(f"ver boven 8-EMA {overext_detail}")

    if   net >=  8: base_overall = "STERK KOOP"
    elif net >=  4: base_overall = "KOOP"
    elif net >=  1: base_overall = "LICHT KOOP"
    elif net <= -8: base_overall = "STERK VERKOOP"
    elif net <= -4: base_overall = "VERKOOP"
    elif net <= -1: base_overall = "LICHT VERKOOP"
    else:           base_overall = "NEUTRAAL"

    # ══ CONFLUENCE-ENGINE ══════════════════════════════════════════════════════
    # Vervangt "tel de signalen op" door "wijzen richting (monthly), moment (weekly)
    # en zone (fib) samen dezelfde kant op?". Zo leest een trader een chart.
    m_state, m_det = _monthly_state(monthly)
    w_turn = _weekly_turn(weekly)
    fib_depth, fib_zone = _fib_buy_depth(last, fib)
    support_count, support_flags = _support_confluence(last, daily, weekly, m_state)
    confl = {"monthlyState": m_state, "weeklyBullTurn": w_turn.get("bullTurn"),
             "weeklyBearTurn": w_turn.get("bearTurn"), "fibBuyDepth": fib_depth, "fibZone": fib_zone,
             "supportCount": support_count, "supportFlags": support_flags}
    reasons_c = []
    overall = base_overall

    # ── 1. MONTHLY STRONG-BEAR VETO: geen koop mogelijk ──
    if m_state == "strong_bear":
        if "KOOP" in overall or overall == "NEUTRAAL":
            overall = "VERKOOP" if (w_turn.get("bearTurn") or w_turn.get("emaBearish")) else "LICHT VERKOOP"
        reasons_c.append("Monthly sterk bearish (EMA-kruising + MACD bearish & dalend) - koop geblokkeerd")
    else:
        # ── 2. KOOP-CONFLUENCE: weekly bullish draai + steunbewijs (fib-zone en/of MA/Bollinger) ──
        #    Monthly mag licht-bearish of neutraal zijn (niet sterk-bearish).
        #    Steunbewijzen: fib-koopzone, 200-MA-steun, onderste weekly-Bollinger.
        #    Hoe meer samenvallen, hoe sterker (bron: confluence verhoogt betrouwbaarheid).
        total_support = (1 if fib_depth >= 1 else 0) + support_count
        if w_turn.get("bullTurn") and total_support >= 1:
            # Sterk bij: diepe fib OF meerdere samenvallende steunen OF gezonde trend + oversold
            strong = (fib_depth >= 2) or (total_support >= 2) or \
                     (m_state in ("bull", "neutral") and w_turn.get("oversold"))
            overall = "STERK KOOP" if strong else "KOOP"
            bewijs = []
            if fib_depth >= 1: bewijs.append("fib " + (fib_zone or "koopzone"))
            bewijs.extend(support_flags)
            reasons_c.append("Confluence KOOP: weekly bullish draai + " + " + ".join(bewijs))
            if m_state == "light_bear":
                reasons_c.append("Monthly licht bearish met keer-tekenen - vroege instap")
        # ── 3. VERKOOP-CONFLUENCE: weekly bearish draai + daily downtrend ──
        elif w_turn.get("bearTurn") and net < 0:
            overall = "STERK VERKOOP" if (m_state in ("light_bear", "strong_bear") and net <= -4) else "VERKOOP"
            reasons_c.append("Confluence VERKOOP: weekly bearish draai + daily downtrend")
        # ── 4. Geen confluence -> geen 'sterk', laat netto meespelen maar getemperd ──
        else:
            if "STERK" in base_overall:
                overall = base_overall.replace("STERK ", "")
            reasons_c.append("Geen duidelijke confluence - signaal getemperd")

    # ── 5. OVEREXTENSIE-VETO: TP-zone of ver boven 8-EMA kapt koop af ──
    if overext_flags:
        if "KOOP" in overall:
            overall = "CAUTION (overextended)"
        elif overall == "NEUTRAAL":
            overall = "LICHT VERKOOP"
        conflict_note = (conflict_note + " " if conflict_note else "") + ("Overextensie: " + ", ".join(overext_flags) + " - rijp voor terugval.")

    if reasons_c:
        conflict_note = (conflict_note + " " if conflict_note else "") + " | ".join(reasons_c)

    return {
        "signals": signals, "alerts": alerts, "overall": overall,
        "buyWeight": buy_w, "sellWeight": sell_w, "baseOverall": base_overall,
        "confluence": confl,
        "conflict": conflict, "conflictNote": conflict_note,
        "indicators": {
            "last": round(last, 2),
            "rsiDaily": round(last_rsi_d, 1),
            "rsiWeekly": round(last_rsi_w, 1) if last_rsi_w is not None else None,
            "ema8d": round(last_ema8d, 2), "ema21d": round(last_ema21d, 2),
            "ema8w": round(last_ema8w, 2) if last_ema8w is not None else None,
            "ema21w": round(last_ema21w, 2) if last_ema21w is not None else None,
            "ma50": round(last_ma50, 2) if last_ma50 is not None else None,
            "ma200": round(last_ma200, 2) if last_ma200 is not None else None,
            "macdLine": round(last_macd_l, 4), "macdSignal": round(last_macd_s, 4),
            "macdLineW": round(last_macd_wl, 4) if last_macd_wl is not None else None,
            "macdSigW": round(last_macd_ws, 4) if last_macd_ws is not None else None,
            "bollUpper": round(last_bb_u, 2), "bollLower": round(last_bb_l, 2),
            "volRatio": round(vol_ratio, 2), "volKnown": vol_known, "highVolume": high_volume, "volNote": vol_note.strip(),
            "fib": fib, "isFriday": IS_FRIDAY, "hasWeekly": has_weekly,
        },
    }

# ── WAARDERINGSLAAG ───────────────────────────────────────────────────────────
# Doel: "staat dit kwaliteitsaandeel nu goedkoop of duur?" — beschermt tegen te duur kopen.
#
# Eerlijke meet-filosofie:
#   • PEG-ratio (P/E / groei) is de PRIMAIRE maatstaf — groei-gecorrigeerd, echte cijfers.
#   • Echte historische P/E-percentiel ALLEEN met echte data (FMP). We fabriceren GEEN
#     P/E-historie via groei-reconstructie — dat maakt groeiaandelen systematisch vals goedkoop.
#   • Prijspositie in 5-jaars range = context, eerlijk gelabeld (geen waardering).

def fetch_historical_pe_fmp(ticker: str, timeout: int = 15):
    """
    Haalt historische kwartaal-P/E via FMP. Werkt in GitHub Actions (server), niet in browser.
    Returnt lijst P/E-waarden of None bij geen key/fout/Euronext-ticker.
    """
    if not FMP_API_KEY:
        return None
    if "." in ticker:  # bv. ASM.AS — FMP dekt Euronext vaak onbetrouwbaar
        return None
    url = f"{FMP_BASE}/ratios/{ticker}?period=quarter&limit=20&apikey={FMP_API_KEY}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "buffett-dashboard"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list):
            return None
        pe = [float(r["priceEarningsRatio"]) for r in data
              if r.get("priceEarningsRatio") is not None and r["priceEarningsRatio"] > 0]
        return pe if len(pe) >= 8 else None
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError, TimeoutError, OSError) as e:
        print(f"    ⚠ FMP P/E ophalen faalde voor {ticker}: {e}")
        return None

def percentile_rank(values, target):
    """Welk percentiel neemt `target` in binnen `values`? 0-100 (midpoint bij ties)."""
    if not values:
        return None
    below = sum(1 for v in values if v < target)
    equal = sum(1 for v in values if v == target)
    return round((below + 0.5 * equal) / len(values) * 100, 1)

def compute_valuation(name: str, daily: pd.DataFrame, fund: dict, hist_pe_fmp=None) -> dict:
    """Bereken waarderingspositie met eerlijke, niet-circulaire methodes."""
    close = daily["Close"]
    current_price = float(close.iloc[-1])
    current_pe = fund.get("pe")
    current_eps = fund.get("eps")
    growth     = fund.get("revenueGrowth")

    # P/E live berekenen: koers (live) ÷ laatst bekende EPS. Zo beweegt de waardering
    # mee met de koers en veroudert alleen de EPS (kwartaal-update) — niet de hele P/E.
    pe_live = False
    if current_eps and current_eps > 0 and current_price > 0:
        current_pe = round(current_price / current_eps, 1)
        pe_live = True

    out = {
        "currentPE": current_pe, "peg": None,
        "pePercentile": None, "peMin": None, "peMedian": None, "peMax": None,
        "peSource": None, "priceRangePosition": None,
        "verdict": None, "verdictColor": None, "notes": [],
    }
    if pe_live:
        out["notes"].append("P/E live: koers ÷ laatst bekende EPS")

    # PEG — primaire groei-gecorrigeerde maatstaf (Lynch)
    if current_pe and growth and growth > 0:
        out["peg"] = round(current_pe / growth, 2)

    # Prijspositie in 5-jaars range (eerlijke context, géén waardering)
    lo, hi = float(close.min()), float(close.max())
    if hi > lo:
        out["priceRangePosition"] = round((current_price - lo) / (hi - lo) * 100, 1)

    # Echte historische P/E-percentiel — ALLEEN met echte FMP-data
    if hist_pe_fmp and len(hist_pe_fmp) >= 8 and current_pe:
        clean = [v for v in hist_pe_fmp if v > 0]
        if len(clean) >= 8:
            out["pePercentile"] = percentile_rank(clean, current_pe)
            out["peMin"]    = round(min(clean), 1)
            out["peMedian"] = round(statistics.median(clean), 1)
            out["peMax"]    = round(max(clean), 1)
            out["peSource"] = "fmp_historical"
            out["notes"].append("Echte historische P/E (FMP, 20 kwartalen)")

    # ── VERDICT ──  PEG leidend; verfijnd met echte P/E-percentiel indien beschikbaar.
    pct, peg = out["pePercentile"], out["peg"]
    verdict, color = "Onvoldoende data", "neutral"

    if peg is not None:
        if   peg < 1.0: verdict, color = "Aantrekkelijk (PEG < 1)", "green"
        elif peg < 1.5: verdict, color = "Redelijk (PEG 1–1.5)", "green"
        elif peg < 2.5: verdict, color = "Neutraal (PEG 1.5–2.5)", "neutral"
        elif peg < 3.5: verdict, color = "Aan de dure kant (PEG 2.5–3.5)", "orange"
        else:           verdict, color = "Duur (PEG > 3.5)", "red"
        if pct is not None:
            if pct < 25 and color in ("neutral", "orange"):
                verdict += f" · maar P/E in onderste {pct:.0f}% van eigen historie"
                color = "green" if color == "neutral" else "orange"
            elif pct > 80 and color in ("green", "neutral"):
                verdict += f" · let op: P/E in bovenste {100-pct:.0f}% van historie"
                color = "orange" if color == "green" else color
    elif pct is not None:
        if   pct < 30: verdict, color = f"Goedkoop vs eigen historie (P{pct:.0f})", "green"
        elif pct > 75: verdict, color = f"Duur vs eigen historie (P{pct:.0f})", "red"
        elif pct > 60: verdict, color = f"Aan de dure kant (P{pct:.0f})", "orange"
        else:          verdict, color = f"Redelijk vs eigen historie (P{pct:.0f})", "neutral"
    elif current_pe is not None:
        if   current_pe < 15: verdict, color = "Lage P/E (absoluut)", "green"
        elif current_pe < 25: verdict, color = "Gemiddelde P/E (absoluut)", "neutral"
        elif current_pe < 40: verdict, color = "Hoge P/E (absoluut)", "orange"
        else:                 verdict, color = "Zeer hoge P/E (absoluut)", "red"
        out["notes"].append("Geen groeicijfer — oordeel op absolute P/E")

    out["verdict"], out["verdictColor"] = verdict, color
    return out

# ── MULTI-TIMEFRAME TIMING ────────────────────────────────────────────────────
# Filosofie voor een maandelijkse kwaliteitsbelegger:
#   • TREND-score: is dit een uptrend? — zwaarder op monthly/weekly (geen vallend mes vangen)
#   • ENTRY-score: is NU een goed instapmoment? — zwaarder op daily (pullback naar steun/oversold)
#   • timingScore = 0.6·trend + 0.4·entry
# Zo wordt de ideale setup beloond: kwaliteit in uptrend die net is teruggevallen.

def _tf_trend_score(close):
    """0-100: hoe bullish is de trend op dit timeframe? None bij te weinig data."""
    if close is None or len(close) < 25:
        return None
    last = float(close.iloc[-1])
    ema8  = safe_last(calc_ema(close, 8), last)
    ema21 = safe_last(calc_ema(close, 21), last)
    macd_l, macd_s, _ = calc_macd(close)
    ml, ms = safe_last(macd_l, 0.0), safe_last(macd_s, 0.0)
    rsi = safe_last(calc_rsi(close, 14), 50.0)
    s = 50
    s += 12 if ema8 > ema21 else -12   # EMA-alignment = trendrichting
    s += 8  if last > ema21 else -8    # prijs boven/onder middellange EMA
    s += 10 if ml > ms else -10        # MACD-richting
    s += 5  if ml > 0 else -5          # MACD boven/onder nul
    s += 5  if rsi >= 55 else (-5 if rsi <= 45 else 0)  # momentum-tilt
    return max(0, min(100, s))

def _entry_score(close, fib_daily=None):
    """0-100: hoe goed is NU de instap (pullback/oversold)? None bij te weinig data."""
    if close is None or len(close) < 25:
        return None
    last = float(close.iloc[-1])
    rsi = safe_last(calc_rsi(close, 14), 50.0)
    bb_u, _, bb_l = calc_bollinger(close, 20)
    u, l = safe_last(bb_u, last*1.05), safe_last(bb_l, last*0.95)
    bb_pos = (last - l) / (u - l) if u > l else 0.5
    s = 50
    # RSI: lager = betere instap (oversold pullback); overbought = wachten
    if   rsi < 30: s += 25
    elif rsi < 40: s += 15
    elif rsi < 50: s += 5
    elif rsi > 70: s -= 25
    elif rsi > 60: s -= 10
    # Bollinger-positie: bij onderband = goede instap, bij bovenband = slecht
    if   bb_pos < 0.20: s += 15
    elif bb_pos < 0.40: s += 7
    elif bb_pos > 0.80: s -= 15
    elif bb_pos > 0.60: s -= 7
    # Fibonacci: golden pocket (0.618–0.705) = premium instapzone; extensies >1.0 = winstnemen.
    if fib_daily:
        gp = fib_daily.get("goldenPocket")
        if gp and gp["low"] <= last <= gp["high"]:
            s += 18   # in de golden pocket → sterke instap
        else:
            # nabij een retracement-steun onder 1.0 = mild positief
            for lbl, level in fib_daily.get("retracements", {}).items():
                if lbl not in ("0.000","1.000") and prox_pct(last, level) < 2.0:
                    s += 8; break
        # In of boven een extensie (>1.0) = winstnemingszone, geen instap → straf
        exts = fib_daily.get("extensions", {})
        if exts:
            ext_1618 = exts.get("1.618")
            if ext_1618 and last >= ext_1618:
                s -= 20   # ver in winstnemingsgebied (jouw MU-signaal)
            elif exts.get("1.272") and last >= exts["1.272"]:
                s -= 10
    return max(0, min(100, s))

def _overextension_penalty(close_w, close_m):
    """Straf voor prijs die te ver van de 8-EMA staat (mean-reversion-risico).
    Meet weekly én monthly: hoe verder boven de 8-EMA, hoe groter de kans op terugval.
    Retourneert (penalty 0..~25, detail-dict) — puur aftrek, nooit bonus."""
    pen, detail = 0, {}
    for lbl, close, cap in (("weekly", close_w, 12), ("monthly", close_m, 15)):
        if close is None or len(close) < 10:
            continue
        ema8 = calc_ema(close, 8)
        e = safe_last(ema8)
        last = float(close.iloc[-1])
        if e and e > 0:
            dist_pct = (last - e) / e * 100
            detail[lbl] = round(dist_pct, 1)
            if dist_pct > 0:  # alleen bóven de EMA = overextensie-risico
                # >20% boven 8-EMA telt vol; lineair opgebouwd, begrensd per timeframe
                pen += min(cap, dist_pct / 20 * cap)
    return round(pen), detail

def _macd_rolling_over(close_d, close_w, close_m):
    """True-telling van timeframes waar MACD-histogram naar beneden krult
    (momentum draait). Puur richting, geen niveau."""
    turning = []
    for lbl, close in (("daily", close_d), ("weekly", close_w), ("monthly", close_m)):
        if close is None or len(close) < 35:
            continue
        macd_line, signal_line, _ = calc_macd(close)
        hist = (macd_line - signal_line).dropna()
        if len(hist) >= 2:
            # histogram daalt en piek is voorbij = naar beneden krullen
            if hist.iloc[-1] < hist.iloc[-2]:
                turning.append(lbl)
    return turning

def compute_timing(daily, weekly, monthly, fib_daily) -> dict:
    """Combineer trend (multi-TF) en entry (vooral daily) tot één timing-score."""
    close_d = daily["Close"]
    close_w = weekly["Close"]  if weekly  is not None and len(weekly)  >= 25 else None
    close_m = monthly["Close"] if monthly is not None and len(monthly) >= 25 else None

    trend_d = _tf_trend_score(close_d)
    trend_w = _tf_trend_score(close_w)
    trend_m = _tf_trend_score(close_m)

    # Gewogen trend: hogere timeframes wegen zwaarder (geen vallend mes vangen)
    parts = []
    if trend_m is not None: parts.append((trend_m, 0.45))
    if trend_w is not None: parts.append((trend_w, 0.35))
    if trend_d is not None: parts.append((trend_d, 0.20))
    trend_score = round(sum(s*w for s, w in parts) / sum(w for _, w in parts)) if parts else 50

    # Entry: daily primair, weekly als bevestiging
    entry = _entry_score(close_d, fib_daily)
    entry_score = entry if entry is not None else 50
    if close_w is not None:
        rsi_w = safe_last(calc_rsi(close_w, 14), 50.0)
        if   rsi_w < 35: entry_score = min(100, entry_score + 10)
        elif rsi_w > 70: entry_score = max(0,   entry_score - 10)

    base_timing = round(0.6 * trend_score + 0.4 * entry_score)

    # Overextensie-rem: prijs ver boven 8-EMA (weekly/monthly) → mean-reversion-risico
    overext_pen, overext_detail = _overextension_penalty(close_w, close_m)
    # MACD-draai: momentum kantelt op meerdere timeframes → extra voorzichtigheid
    macd_turning = _macd_rolling_over(close_d, close_w, close_m)
    macd_pen = 5 * len(macd_turning)   # 5 per timeframe die naar beneden krult

    timing_score = max(0, min(100, base_timing - overext_pen - macd_pen))

    if   timing_score >= 70: label, color = "Sterke instap-setup", "green"
    elif timing_score >= 58: label, color = "Gunstige setup", "green"
    elif timing_score >= 45: label, color = "Neutrale setup", "neutral"
    elif timing_score >= 32: label, color = "Zwakke setup", "orange"
    else:                    label, color = "Slechte instap (wachten)", "red"

    return {
        "score": timing_score, "label": label, "color": color,
        "trendScore": trend_score, "entryScore": entry_score,
        "trendDaily": trend_d, "trendWeekly": trend_w, "trendMonthly": trend_m,
        "hasMonthly": close_m is not None,
        "overextPenalty": overext_pen, "overextDetail": overext_detail,
        "macdTurning": macd_turning, "baseTimin": base_timing,
    }

# ── KWALITEIT (Buffett-poort) ─────────────────────────────────────────────────
def compute_quality(fund: dict) -> dict:
    """
    Kwaliteitsscore 0-100 + harde poort. Alleen poort-passers zijn koopkandidaten
    in de kern-allocatie (baggers vormen een apart spoor — volgende stap).
    """
    roe    = fund.get("roe")
    margin = fund.get("netMargin")
    de     = fund.get("debtEquity")
    growth = fund.get("revenueGrowth")
    fcf    = fund.get("fcfYield")

    s, reasons = 0, []
    # ROE (max 25)
    if   roe is not None and roe >= 20: s += 25; reasons.append(f"ROE {roe:.0f}% (sterk)")
    elif roe is not None and roe >= 15: s += 18; reasons.append(f"ROE {roe:.0f}% (goed)")
    elif roe is not None and roe >= 10: s += 10
    # Netto marge (max 20)
    if   margin is not None and margin >= 20: s += 20; reasons.append(f"Marge {margin:.0f}% (sterk)")
    elif margin is not None and margin >= 12: s += 14
    elif margin is not None and margin >= 8:  s += 8
    # Debt/Equity (max 20, lager = beter)
    if   de is not None and de < 0.5: s += 20; reasons.append("Lage schuld")
    elif de is not None and de < 1.0: s += 14
    elif de is not None and de < 2.0: s += 8
    elif de is not None and de < 3.0: s += 3
    # Omzetgroei (max 20)
    if   growth is not None and growth >= 15: s += 20; reasons.append(f"Groei +{growth:.0f}%")
    elif growth is not None and growth >= 8:  s += 14
    elif growth is not None and growth >= 4:  s += 8
    # FCF yield (max 15)
    if   fcf is not None and fcf >= 4: s += 15
    elif fcf is not None and fcf >= 2: s += 10
    elif fcf is not None and fcf > 0:  s += 5

    score = min(100, max(0, s))

    # Harde poort: minimale kwaliteit om investeerbaar te zijn in de kern
    gate = True
    fails = []
    if roe is None or roe < 12:        gate = False; fails.append("ROE < 12%")
    if margin is None or margin < 8:   gate = False; fails.append("marge < 8%")
    if de is not None and de > 4:      gate = False; fails.append("schuld te hoog (D/E > 4)")

    return {"score": score, "gate": gate, "reasons": reasons, "gateFails": fails}

def valuation_to_score(valuation: dict) -> int:
    """Zet waardering om naar 0-100 (goedkoper = hoger). PEG primair, percentiel verfijnt."""
    peg = valuation.get("peg")
    pct = valuation.get("pePercentile")
    score = 50
    if peg is not None:
        if   peg < 1.0: score = 90
        elif peg < 1.5: score = 75
        elif peg < 2.0: score = 62
        elif peg < 2.5: score = 50
        elif peg < 3.0: score = 40
        elif peg < 3.5: score = 30
        elif peg < 4.5: score = 18
        else:           score = 8
    if pct is not None:
        pct_score = 100 - pct  # laag percentiel = goedkoop = hoge score
        score = round(0.6 * score + 0.4 * pct_score) if peg is not None else round(pct_score)
    return int(max(0, min(100, score)))

def compute_composite(quality_score, valuation_score, timing_score) -> int:
    """
    Composietscore voor de maandelijkse allocatie.
    Gewichten: timing 40% (Rubens nadruk), kwaliteit 30%, waardering 30%.
    """
    return round(0.30 * quality_score + 0.30 * valuation_score + 0.40 * timing_score)

# ── BAGGER-SPOOR (apart raamwerk; waardering speelt GEEN rol) ─────────────────
# Zoekt kenmerken van vroege multibaggers: hoge groei, groei-versnelling, operating
# leverage (stijgende brutomarge), relatieve sterkte. Kleine positie want het
# faillissementsrisico is reëel. Een aandeel kan zowel hier als in de kern staan.

def compute_relative_strength(stock_close, bench_close, lookback_days=126):
    """Relatieve sterkte: aandeelrendement min benchmarkrendement over ~6 mnd (%)."""
    if stock_close is None or bench_close is None:
        return None
    if len(stock_close) < lookback_days or len(bench_close) < lookback_days:
        return None
    s_ret = (float(stock_close.iloc[-1]) / float(stock_close.iloc[-lookback_days]) - 1) * 100
    b_ret = (float(bench_close.iloc[-1]) / float(bench_close.iloc[-lookback_days]) - 1) * 100
    return round(s_ret - b_ret, 1)

def compute_bagger_score(fund: dict, rel_strength) -> dict:
    """Bagger-potentieelscore 0-100 + risico-flags + positiegrootte-advies. Geen waardering."""
    growth      = fund.get("revenueGrowth")
    growth_prev = fund.get("revenueGrowthPrev")
    gm          = fund.get("grossMargin")
    gm_trend    = fund.get("grossMarginTrend")
    runway      = fund.get("cashRunwayMonths")
    mktcap_str  = fund.get("mktCap", "")

    s, reasons, flags = 0, [], []

    # 1. Omzetgroei (max 30) — kern van elke bagger
    if   growth is not None and growth >= 60: s += 30; reasons.append(f"Omzetgroei +{growth:.0f}% (explosief)")
    elif growth is not None and growth >= 40: s += 24; reasons.append(f"Omzetgroei +{growth:.0f}% (hoog)")
    elif growth is not None and growth >= 25: s += 16; reasons.append(f"Omzetgroei +{growth:.0f}%")
    elif growth is not None and growth >= 15: s += 8

    # 2. Groei-versnelling (max 20)
    if growth is not None and growth_prev is not None:
        accel = growth - growth_prev
        if   accel >= 15: s += 20; reasons.append(f"Groei versnelt sterk (+{accel:.0f}pp)")
        elif accel >= 5:  s += 13; reasons.append(f"Groei versnelt (+{accel:.0f}pp)")
        elif accel >= 0:  s += 7
        else: reasons.append(f"Groei vertraagt ({accel:.0f}pp)")

    # 3. Brutomarge-trend (max 20) — operating leverage
    if gm_trend is not None:
        if   gm_trend >= 3: s += 20; reasons.append(f"Brutomarge stijgt sterk (+{gm_trend:.1f}pp)")
        elif gm_trend >= 1: s += 13; reasons.append(f"Brutomarge stijgt (+{gm_trend:.1f}pp)")
        elif gm_trend >= 0: s += 6
        else: flags.append(f"Brutomarge daalt ({gm_trend:.1f}pp)")

    # 4. Brutomarge-niveau (max 10) — schaalbaarheid
    if gm is not None:
        if   gm >= 60: s += 10
        elif gm >= 40: s += 7
        elif gm >= 25: s += 4

    # 5. Relatieve sterkte vs markt (max 20)
    if rel_strength is not None:
        if   rel_strength >= 40: s += 20; reasons.append(f"Sterk boven markt (+{rel_strength:.0f}%)")
        elif rel_strength >= 15: s += 13; reasons.append(f"Boven markt (+{rel_strength:.0f}%)")
        elif rel_strength >= 0:  s += 6
        else: flags.append(f"Onder markt ({rel_strength:.0f}%)")

    score = min(100, max(0, s))

    # Risico-flags → bepalen positiegrootte-advies, niet de score
    risk = "gemiddeld"
    if runway is not None:
        if runway <= 12:
            flags.append(f"Cash runway kort (~{runway} mnd) — verwateringsrisico"); risk = "zeer hoog"
        elif runway <= 24:
            flags.append(f"Cash runway ~{runway} mnd"); risk = "hoog"
    if "$0.0" in mktcap_str or "$0." in mktcap_str:
        flags.append("Microcap — hoog faillissements-/volatiliteitsrisico"); risk = "zeer hoog"

    # 100x-realisme: de wiskunde van marktkap. Een 100x vanaf $10B = $1 biljoen.
    # Echte 100-baggers starten vrijwel altijd klein (<$1B) en onopgemerkt.
    cap_usd = None
    s = (mktcap_str or "").strip()
    if s.startswith("$") and (s.endswith("B") or s.endswith("T")):
        try:
            v = float(s[1:-1]); cap_usd = v * 1000 if s.endswith("T") else v
        except ValueError:
            pass
    if cap_usd is not None:
        if cap_usd >= 40:
            flags.append(f"Marktkap ~${cap_usd:.0f}B — 100x wiskundig uitgesloten; dit is een momentum-positie, geen bagger-lot")
        elif cap_usd >= 10:
            flags.append(f"Marktkap ~${cap_usd:.0f}B — 100x vergt biljoenen-waardering; realistisch plafond eerder 5–10x")

    if   score >= 70 and risk in ("gemiddeld", "hoog"): pos = "klein-tot-gemiddeld"
    elif score >= 55: pos = "klein"
    elif score >= 40: pos = "zeer klein (speculatief)"
    else:             pos = "vermijden / afwachten"

    if   score >= 70: label, color = "Sterk bagger-profiel", "green"
    elif score >= 55: label, color = "Interessant bagger-profiel", "green"
    elif score >= 40: label, color = "Zwak bagger-profiel", "orange"
    else:             label, color = "Geen bagger-profiel nu", "red"

    return {
        "score": score, "label": label, "color": color,
        "reasons": reasons, "flags": flags, "risk": risk,
        "positionSizing": pos, "relStrength": rel_strength,
    }

# ── MARKTREGIME (SPX/NDX) + CONTEXT (DXY, grondstoffen) + SECTOR-ROTATIE ──────
# Filosofie: het regime is een BESCHRIJVER van de brede markt, geen top-voorspeller.
# Invloed op timing is mild (±MARKET_ADJ_MAX), begrensd en volledig zichtbaar in de UI.
# DXY/goud/koper/olie zijn pure context (instabiele correlaties → géén score-invloed).

def _index_regime_score(close):
    """Gewogen multi-timeframe trendscore voor een index (zelfde toolkit als aandelen)."""
    if close is None or len(close) < 300:
        return None, {}
    d = close
    w = close.resample("W-FRI").last().dropna()
    if len(w) and w.index[-1].date() >= TODAY: w = w.iloc[:-1]
    m = close.resample("ME").last().dropna()
    if len(m) and (m.index[-1].year, m.index[-1].month) == (TODAY.year, TODAY.month): m = m.iloc[:-1]
    parts, detail = [], {}
    for lbl, ser, wt in (("monthly", m, 0.45), ("weekly", w, 0.35), ("daily", d, 0.20)):
        sc = _tf_trend_score(ser)
        detail[lbl] = sc
        if sc is not None: parts.append((sc, wt))
    if not parts: return None, detail
    score = round(sum(s*t for s, t in parts) / sum(t for _, t in parts))
    ma200 = safe_last(d.rolling(200).mean())
    detail["vsMA200"] = round((float(d.iloc[-1])/ma200 - 1)*100, 1) if ma200 else None
    return score, detail

def _pct_change(close, days):
    if close is None or len(close) <= days: return None
    return round((float(close.iloc[-1]) / float(close.iloc[-days]) - 1) * 100, 1)

def compute_market_context(spx_close, market: dict) -> dict:
    """Bouw het regime + context + sector-rotatie. Faalt zacht (adj=0) zonder data."""
    spx_score, spx_d = _index_regime_score(spx_close)
    ndx_score, ndx_d = _index_regime_score(market.get("NDX"))
    scores = [s for s in (spx_score, ndx_score) if s is not None]
    if spx_score is not None and ndx_score is not None:
        regime = round(0.6*spx_score + 0.4*ndx_score)
    elif scores:
        regime = scores[0]
    else:
        regime = None

    if   regime is None: label, color = "Onbekend (geen data)", "neutral"
    elif regime >= 65:   label, color = "Risk-on — brede uptrend", "green"
    elif regime >= 52:   label, color = "Licht positief", "green"
    elif regime >= 45:   label, color = "Neutraal", "neutral"
    elif regime >= 35:   label, color = "Voorzichtig — trend verzwakt", "orange"
    else:                label, color = "Risk-off — brede neerwaartse druk", "red"

    adj = 0
    if regime is not None:
        adj = round(max(-1.0, min(1.0, (regime - 50) / 50.0)) * MARKET_ADJ_MAX)

    # Context: 3-maands beweging (63 handelsdagen)
    context = {}
    for key, naam in (("DXY","Dollar-index"),("GOLD","Goud (GLD)"),("COPPER","Koper (CPER)"),("OIL","Olie (USO)")):
        ch = _pct_change(market.get(key), 63)
        if ch is not None:
            context[key] = {"name": naam, "change3m": ch}

    # Sector-rotatie: relatieve sterkte vs SPY (63d en 126d)
    sectors = []
    spy = market.get("SPY")
    if spy is not None:
        for etf, naam in SECTOR_LABELS.items():
            s = market.get(etf)
            r63  = _pct_change(s, 63);  b63  = _pct_change(spy, 63)
            r126 = _pct_change(s, 126); b126 = _pct_change(spy, 126)
            if r63 is not None and b63 is not None:
                sectors.append({"etf": etf, "name": naam,
                                "rs63": round(r63 - b63, 1),
                                "rs126": round(r126 - b126, 1) if (r126 is not None and b126 is not None) else None})
        sectors.sort(key=lambda x: x["rs63"], reverse=True)

    return {
        "regimeScore": regime, "regimeLabel": label, "regimeColor": color,
        "timingAdjustment": adj, "adjMax": MARKET_ADJ_MAX,
        "spx": {"score": spx_score, **spx_d}, "ndx": {"score": ndx_score, **ndx_d},
        "context": context, "sectors": sectors,
        "note": ("Regime beschrijft de brede markt (geen top-voorspelling). Invloed op timing is "
                 f"begrensd tot ±{MARKET_ADJ_MAX} punten en apart zichtbaar. DXY en grondstoffen zijn "
                 "pure context zonder score-invloed."),
    }

# ── HISTORISCHE OPSLAG ────────────────────────────────────────────────────────
def load_timeline() -> dict:
    if os.path.exists(TIMELINE_FILE):
        try:
            with open(TIMELINE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            print("  ⚠ timeline.json onleesbaar, start opnieuw")
    return {"_meta": {"created": NOW.isoformat()}, "stocks": {}}

def update_timeline(timeline: dict, name: str, ind: dict):
    """Voeg de kernmetrieken van vandaag toe aan de tijdreeks van dit aandeel."""
    stocks = timeline.setdefault("stocks", {})
    series = stocks.setdefault(name, [])
    point = {
        "date":      TODAY.isoformat(),
        "price":     ind.get("last"),
        "rsiDaily":  ind.get("rsiDaily"),
        "rsiWeekly": ind.get("rsiWeekly"),
        "overall":   None,       # ingevuld door caller
        "composite": None,       # ingevuld door caller
        "timing":    None,       # ingevuld door caller
        "qualityGate": None,     # ingevuld door caller
        "valuationVerdict": None,# ingevuld door caller
    }
    # Vervang als er al een punt voor vandaag is (idempotent bij dubbele run)
    series = [p for p in series if p.get("date") != TODAY.isoformat()]
    series.append(point)
    # Begrens lengte
    if len(series) > TIMELINE_MAX_POINTS:
        series = series[-TIMELINE_MAX_POINTS:]
    stocks[name] = series
    return point

def atomic_write(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=True, default=str)
    os.replace(tmp, path)

# ── WEKELIJKSE CHANGELOG ──────────────────────────────────────────────────────
# Vergelijkt vandaag met ~1 week geleden (uit timeline.json) en toont wat VERANDERDE.
def _find_prior_point(series, today_iso, target_days=7, tolerance=4):
    """Vind het datapunt het dichtst bij `target_days` geleden (binnen marge)."""
    if not series or len(series) < 2:
        return None
    today = date.fromisoformat(today_iso)
    target = today - timedelta(days=target_days)
    best, best_diff = None, None
    for p in series:
        if p.get("date") == today_iso:
            continue
        try:
            d = date.fromisoformat(p["date"])
        except (ValueError, KeyError, TypeError):
            continue
        diff = abs((d - target).days)
        if best_diff is None or diff < best_diff:
            best, best_diff = p, diff
    if best is not None and best_diff is not None and best_diff <= target_days + tolerance:
        return best
    return None

def build_changelog(timeline: dict, today_iso: str) -> list:
    """Produceer per aandeel de veranderingen sinds ~1 week geleden."""
    changes = []
    for name, series in timeline.get("stocks", {}).items():
        if not series:
            continue
        today_pt = next((p for p in series if p.get("date") == today_iso), series[-1])
        prior = _find_prior_point(series, today_pt.get("date", today_iso))
        if prior is None:
            continue

        sc = []
        c_now, c_old = today_pt.get("composite"), prior.get("composite")
        if c_now is not None and c_old is not None and abs(c_now - c_old) >= 5:
            up = c_now > c_old
            sc.append({"type":"composite","dir":"up" if up else "down",
                       "text":f"Composiet {c_old} {'↑' if up else '↓'} {c_now}"})

        g_now, g_old = today_pt.get("qualityGate"), prior.get("qualityGate")
        if g_now is not None and g_old is not None and g_now != g_old:
            sc.append({"type":"gate","dir":"up" if g_now else "down",
                       "text":"In kwaliteitspoort ✓" if g_now else "Uit kwaliteitspoort ✗"})

        v_now, v_old = today_pt.get("valuationVerdict"), prior.get("valuationVerdict")
        if v_now and v_old and v_now != v_old:
            sc.append({"type":"valuation","dir":"neutral","text":f"Waardering: {v_old} → {v_now}"})

        o_now, o_old = today_pt.get("overall"), prior.get("overall")
        if o_now and o_old and o_now != o_old:
            up = ("KOOP" in o_now) and ("KOOP" not in o_old)
            down = ("VERKOOP" in o_now) and ("VERKOOP" not in o_old)
            sc.append({"type":"signal","dir":"up" if up else ("down" if down else "neutral"),
                       "text":f"Signaal: {o_old} → {o_now}"})

        r_now, r_old = today_pt.get("rsiWeekly"), prior.get("rsiWeekly")
        if r_now is not None and r_old is not None:
            if r_now <= 30 and r_old > 30:
                sc.append({"type":"rsi","dir":"up","text":f"RSI weekly oversold ({r_now:.0f})"})
            elif r_now >= 70 and r_old < 70:
                sc.append({"type":"rsi","dir":"down","text":f"RSI weekly overbought ({r_now:.0f})"})

        p_now, p_old = today_pt.get("price"), prior.get("price")
        if p_now and p_old and p_old > 0:
            pct = (p_now - p_old) / p_old * 100
            if abs(pct) >= 8:
                sc.append({"type":"price","dir":"up" if pct > 0 else "down",
                           "text":f"Koers {pct:+.1f}% deze week (${p_old:.0f}→${p_now:.0f})"})

        if sc:
            changes.append({
                "ticker": name,
                "daysAgo": (date.fromisoformat(today_pt["date"]) - date.fromisoformat(prior["date"])).days,
                "changes": sc,
            })
    # Sorteer: meeste veranderingen eerst
    changes.sort(key=lambda x: len(x["changes"]), reverse=True)
    return changes

# ── TRACKRECORD (observatie, geen auto-optimalisatie) ─────────────────────────
# Legt elke aanbeveling vast met tijdstempel en meet forward return op meerdere
# horizons, telkens RELATIEF tot de benchmark (beter dan de index?). Bewust
# mens-in-de-lus: toont data, past nooit zelf gewichten aan.

def _price_on_or_after(close_series, target_date, max_gap_days=7):
    """Slotkoers op of net na target_date. None als voorbij data of te groot gat."""
    if close_series is None or len(close_series) == 0:
        return None, None
    idx = pd.to_datetime(close_series.index)
    mask = idx.date >= target_date
    candidates = close_series[mask]
    if len(candidates) == 0:
        return None, None
    first_date = pd.to_datetime(candidates.index[0]).date()
    if (first_date - target_date).days > max_gap_days:
        return None, None
    return float(candidates.iloc[0]), first_date.isoformat()

def _record_key(ticker, date_iso, rec_type):
    return f"{rec_type}:{ticker}:{date_iso}"

def record_recommendations(track, today_iso, allocation, stocks, prices, bench_close):
    """Leg vandaag's aanbevelingen vast (idempotent): maandpick + sterke signalen."""
    records = track.setdefault("records", {})
    bench_entry = float(bench_close.iloc[-1]) if bench_close is not None and len(bench_close) else None

    def add(ticker, rec_type, direction, entry_price, snapshot):
        if entry_price is None:
            return
        key = _record_key(ticker, today_iso, rec_type)
        if key in records:
            return  # idempotent
        records[key] = {
            "ticker": ticker, "type": rec_type, "direction": direction,
            "date": today_iso, "entryPrice": entry_price, "benchEntry": bench_entry,
            "currency": CURRENCY.get(ticker, "$"),
            "snapshot": snapshot, "outcomes": {},
        }

    if allocation and allocation.get("primaryPick"):
        p = allocation["primaryPick"]
        add(p["ticker"], "monthly_pick", "BUY", prices.get(p["ticker"]), {
            "composite": p.get("composite"), "quality": p.get("quality"),
            "valuation": p.get("valuation"), "timing": p.get("timing"),
        })

    for t, s in stocks.items():
        overall = s.get("overall")
        sc = s.get("scores", {})
        if overall == "STERK KOOP":
            add(t, "strong_signal", "BUY", prices.get(t),
                {"overall": overall, "composite": sc.get("composite"), "timing": sc.get("timing")})
        elif overall == "STERK VERKOOP":
            add(t, "strong_signal", "SELL", prices.get(t),
                {"overall": overall, "composite": sc.get("composite"), "timing": sc.get("timing")})

def evaluate_outcomes(track, today, price_data, bench_close, horizons_weeks):
    """Vul verstreken horizons in met forward return (absoluut + relatief vs benchmark)."""
    for rec in track.get("records", {}).values():
        entry_date = date.fromisoformat(rec["date"])
        entry_price = rec.get("entryPrice")
        bench_entry = rec.get("benchEntry")
        if not entry_price or entry_price <= 0:
            continue
        close_series = price_data.get(rec["ticker"])
        dir_mult = 1 if rec["direction"] == "BUY" else -1
        for wk in horizons_weeks:
            hkey = f"{wk}w"
            if hkey in rec["outcomes"]:
                continue
            target = entry_date + timedelta(weeks=wk)
            if target > today:
                continue
            exit_price, exit_date = _price_on_or_after(close_series, target)
            if exit_price is None:
                continue
            eff_ret = (exit_price - entry_price) / entry_price * 100 * dir_mult
            rel = None
            if bench_entry and bench_close is not None:
                bexit, _ = _price_on_or_after(bench_close, target)
                if bexit:
                    rel = eff_ret - ((bexit - bench_entry) / bench_entry * 100)
            rec["outcomes"][hkey] = {
                "exitDate": exit_date, "exitPrice": round(exit_price, 2),
                "return": round(eff_ret, 2),
                "relativeReturn": round(rel, 2) if rel is not None else None,
                "success": (rel > 0) if rel is not None else (eff_ret > 0),
            }

def compute_accuracy_stats(track, min_observations, horizons_weeks):
    """Aggregeer afgeronde observaties — verberg percentage bij te weinig data (anti-ruis)."""
    records = track.get("records", {})
    stats = {}
    for rec_type in ["monthly_pick", "strong_signal"]:
        type_recs = [r for r in records.values() if r["type"] == rec_type]
        per_horizon = {}
        for wk in horizons_weeks:
            hkey = f"{wk}w"
            outcomes = [r["outcomes"][hkey] for r in type_recs if hkey in r["outcomes"]]
            n = len(outcomes)
            if n == 0:
                per_horizon[hkey] = {"n": 0, "status": "geen data", "accuracyShown": False}
                continue
            wins = sum(1 for o in outcomes if o["success"])
            rels = [o["relativeReturn"] for o in outcomes if o["relativeReturn"] is not None]
            entry = {
                "n": n,
                "avgReturn": round(statistics.mean([o["return"] for o in outcomes]), 2),
                "avgRelativeReturn": round(statistics.mean(rels), 2) if rels else None,
            }
            if n < min_observations:
                entry["status"] = f"te weinig data ({n}/{min_observations})"
                entry["accuracyShown"] = False
            else:
                entry["accuracy"] = round(wins / n * 100, 1)
                entry["status"] = "ok"
                entry["accuracyShown"] = True
            per_horizon[hkey] = entry
        stats[rec_type] = per_horizon
    return stats

def load_track_record():
    if os.path.exists(TRACK_FILE):
        try:
            with open(TRACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            print("  ⚠ track_record.json onleesbaar, start opnieuw")
    return {"_meta": {"created": NOW.isoformat()}, "records": {}}

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"BUFFETT+ v2 — {NOW.strftime('%A %d %B %Y %H:%M')} Brussels")
    print(f"Vrijdag (weekly signals actief): {IS_FRIDAY} | Weekend: {IS_WEEKEND}")
    print(f"{'='*60}\n")

    if IS_WEEKEND:
        # Handmatige runs (Run workflow-knop) mogen wél in het weekend: analyse op vrijdagdata.
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            print("Weekend, maar handmatige run — analyse draait op de laatste handelsdag (vrijdag).")
        else:
            print("Weekend — beurs gesloten. Bestaande data blijft geldig. Analyse overgeslagen.")
            sys.exit(0)

    os.makedirs(HISTORY_DIR, exist_ok=True)
    timeline = load_timeline()

    results = {
        "meta": {
            "generatedAt": NOW.isoformat(),
            "generatedAtHuman": NOW.strftime("%A %d %B %Y om %H:%M"),
            "isFriday": IS_FRIDAY, "isWeekend": IS_WEEKEND,
            "version": "5.0",
            "fundamentalsNote": "Fundamentals handmatig bijgehouden — controleer bij elk kwartaalrapport.",
        },
        "stocks": {}, "errors": [],
    }

    # Batch ophalen
    fetched = fetch_all(WATCHLIST)
    bench_close = fetched.get("__benchmark__")
    market_series = fetched.get("__market__", {}) or {}

    # ── MARKTREGIME ────────────────────────────────────────────────────────────
    print(f"\n{'='*60}\nMarktregime bepalen...")
    market_ctx = compute_market_context(bench_close, market_series)
    market_adj = market_ctx.get("timingAdjustment", 0) or 0
    results["market"] = market_ctx
    print(f"  {market_ctx['regimeLabel']} (regime {market_ctx['regimeScore']}) → timing-aanpassing {market_adj:+d}")
    if market_ctx.get("sectors"):
        top = market_ctx["sectors"][0]; bot = market_ctx["sectors"][-1]
        print(f"  Sector-rotatie 3m: sterkst {top['name']} ({top['rs63']:+.1f}%), zwakst {bot['name']} ({bot['rs63']:+.1f}%)")

    # Prijsreeksen per ticker verzamelen (voor trackrecord-evaluatie)
    price_data = {}
    for (nm, _p, _fb) in WATCHLIST:
        entry_x = fetched.get(nm)
        if entry_x and entry_x.get("daily") is not None:
            price_data[nm] = entry_x["daily"]["Close"]

    print(f"\n{'='*60}\nSignalen berekenen...\n{'='*60}")
    for (name, primary, fallback) in WATCHLIST:
        print(f"\n[{name}]")
        entry = fetched.get(name)
        if entry is None:
            msg = f"{name}: data ophalen mislukt"
            print(f"  ✗ {msg}")
            results["errors"].append(msg)
            results["stocks"][name] = {"error": msg, "fund": FUNDAMENTALS.get(name, {})}
            continue
        try:
            analysis = generate_signals(name, entry["daily"], entry["weekly"], entry.get("monthly"))
            if "error" in analysis:
                results["errors"].append(f"{name}: {analysis['error']}")

            # Waardering: echte historische P/E via FMP (indien key), anders PEG-gebaseerd
            fund = FUNDAMENTALS.get(name, {})
            hist_pe = fetch_historical_pe_fmp(entry["ticker"])
            valuation = compute_valuation(name, entry["daily"], fund, hist_pe)

            # Multi-timeframe timing (daily/weekly/monthly) + kwaliteit + composiet
            fib_daily = analysis.get("indicators", {}).get("fib")
            timing = compute_timing(entry["daily"], entry["weekly"], entry["monthly"], fib_daily)
            quality = compute_quality(fund)
            val_score = valuation_to_score(valuation)
            # Marktregime past de timing mild aan (begrensd, apart zichtbaar)
            timing_eff = max(0, min(100, timing["score"] + market_adj))
            composite = compute_composite(quality["score"], val_score, timing_eff)

            scores = {
                "quality": quality["score"], "qualityGate": quality["gate"],
                "qualityReasons": quality["reasons"], "qualityFails": quality["gateFails"],
                "valuation": val_score, "timing": timing["score"],
                "marketAdj": market_adj, "timingEffective": timing_eff,
                "composite": composite,
            }

            # Bagger-spoor (apart): alleen voor aangewezen tickers
            bagger = None
            if name in BAGGER_TICKERS:
                rel_str = compute_relative_strength(entry["daily"]["Close"], bench_close)
                bagger = compute_bagger_score(fund, rel_str)

            results["stocks"][name] = {
                "name": name, "ticker": entry["ticker"],
                "fund": fund, "valuation": valuation,
                "timing": timing, "scores": scores, "bagger": bagger,
                "isBagger": name in BAGGER_TICKERS,
                "currency": CURRENCY.get(name, "$"), **analysis,
            }
            # Timeline bijwerken
            if "indicators" in analysis:
                pt = update_timeline(timeline, name, analysis["indicators"])
                pt["overall"] = analysis.get("overall")
                pt["valuationColor"] = valuation.get("verdictColor")
                pt["composite"] = composite
                pt["timing"] = timing["score"]
                pt["qualityGate"] = quality["gate"]
                pt["valuationVerdict"] = valuation.get("verdict")
                ind = analysis["indicators"]
                rw = f"{ind['rsiWeekly']:.0f}" if ind.get("rsiWeekly") is not None else "n/b"
                gate_str = "✓poort" if quality["gate"] else "✗poort"
                print(f"  ${ind['last']:.2f} | RSI-D {ind['rsiDaily']:.0f} | RSI-W {rw} | {analysis['overall']}")
                print(f"  Kwaliteit {quality['score']} ({gate_str}) | Waardering {val_score} | "
                      f"Timing {timing['score']} ({timing['label']}) | COMPOSIET {composite}")
                if analysis.get("conflict"):
                    print(f"  ⚠️ {analysis['conflictNote'][:70]}...")
        except Exception as e:
            msg = f"{name}: onverwachte fout — {e}"
            print(f"  ✗ {msg}")
            print(traceback.format_exc())
            results["errors"].append(msg)

    # ── MAANDELIJKSE ALLOCATIE-AANBEVELING ─────────────────────────────────────
    # Alleen kwaliteitspoort-passers zijn kandidaten. Gerangschikt op composietscore.
    print(f"\n{'='*60}\nAllocatie-aanbeveling opbouwen...\n{'='*60}")
    candidates = []
    gate_failed = []
    for name, s in results["stocks"].items():
        sc = s.get("scores")
        if not sc:
            continue
        row = {
            "ticker": name, "name": name,
            "composite": sc["composite"], "quality": sc["quality"],
            "valuation": sc["valuation"], "timing": sc["timing"],
            "valuationVerdict": s.get("valuation", {}).get("verdict"),
            "timingLabel": s.get("timing", {}).get("label"),
            "price": s.get("indicators", {}).get("last"),
            "currency": CURRENCY.get(name, "$"),
            "marketAdj": sc.get("marketAdj", 0),
        }
        if sc["qualityGate"]:
            candidates.append(row)
        else:
            row["fails"] = sc.get("qualityFails", [])
            gate_failed.append(row)

    candidates.sort(key=lambda x: x["composite"], reverse=True)

    primary = candidates[0] if candidates else None
    reasoning = None
    if primary:
        reasoning = (
            f"{primary['ticker']} combineert kwaliteit ({primary['quality']}/100), "
            f"waardering ({primary['valuation']}/100: {primary['valuationVerdict']}) en "
            f"timing ({primary['timing']}/100: {primary['timingLabel']}) tot de hoogste "
            f"composietscore ({primary['composite']}/100) van de kwaliteitsaandelen deze maand."
        )
        if market_adj:
            reasoning += f" Het marktregime telt {market_adj:+d} mee in de timing van alle kandidaten."

    results["allocation"] = {
        "generatedForMonth": NOW.strftime("%B %Y"),
        "primaryPick": primary,
        "reasoning": reasoning,
        "candidates": candidates,
        "gateFailed": gate_failed,
        "weights": {"quality": 0.30, "valuation": 0.30, "timing": 0.40},
        "note": ("Kwaliteitspoort-passers gerangschikt op composietscore (kwaliteit + waardering "
                 "+ multi-timeframe timing). Geen financieel advies — combineer met eigen oordeel."),
    }
    if primary:
        print(f"  🎯 Deze maand: {primary['ticker']} (composiet {primary['composite']})")
        print(f"     Top 3: " + ", ".join(f"{c['ticker']}({c['composite']})" for c in candidates[:3]))
    print(f"     Poort gefaald: " + (", ".join(c['ticker'] for c in gate_failed) or "geen"))

    # ── BAGGER-SPOOR RANGSCHIKKING ─────────────────────────────────────────────
    print(f"\n{'='*60}\nBagger-spoor opbouwen...")
    bagger_list = []
    for name, s in results["stocks"].items():
        b = s.get("bagger")
        if not b:
            continue
        bagger_list.append({
            "ticker": name, "name": name,
            "score": b["score"], "label": b["label"], "color": b["color"],
            "risk": b["risk"], "positionSizing": b["positionSizing"],
            "relStrength": b["relStrength"], "reasons": b["reasons"], "flags": b["flags"],
            "price": s.get("indicators", {}).get("last"),
            "currency": CURRENCY.get(name, "$"),
            "revenueGrowth": s.get("fund", {}).get("revenueGrowth"),
            "grossMarginTrend": s.get("fund", {}).get("grossMarginTrend"),
            "passesQualityGate": s.get("scores", {}).get("qualityGate", False),
        })
    bagger_list.sort(key=lambda x: x["score"], reverse=True)

    results["baggers"] = {
        "generatedForMonth": NOW.strftime("%B %Y"),
        "candidates": bagger_list,
        "note": ("Apart spoor voor asymmetrisch potentieel — waardering telt hier NIET. "
                 "Kleine positiegroottes: het faillissementsrisico is reëel. Geen financieel advies."),
        "methodNote": ("Score op omzetgroei, groei-versnelling, brutomarge-trend (operating leverage) "
                       "en relatieve sterkte vs markt. Risico-flags bepalen positiegrootte-advies."),
    }
    if bagger_list:
        top = bagger_list[0]
        print(f"  Sterkste profiel: {top['ticker']} (score {top['score']}, {top['positionSizing']})")
        print(f"  Rangschikking: " + ", ".join(f"{b['ticker']}({b['score']})" for b in bagger_list))
        overlap = [b['ticker'] for b in bagger_list if b['passesQualityGate']]
        if overlap:
            print(f"  Ook in kwaliteitspoort: {', '.join(overlap)}")

    # ── WEKELIJKSE CHANGELOG ───────────────────────────────────────────────────
    # Timeline is nu bijgewerkt met de punten van vandaag; bouw de vergelijking.
    print(f"\n{'='*60}\nWekelijkse changelog opbouwen...")
    changelog = build_changelog(timeline, TODAY.isoformat())
    weekly = {
        "generatedAt": NOW.isoformat(),
        "generatedAtHuman": NOW.strftime("%A %d %B %Y om %H:%M"),
        "periodLabel": "sinds ~1 week geleden",
        "changes": changelog,
        "changedCount": len(changelog),
        "note": "Toont enkel wat veranderde t.o.v. ~7 dagen geleden. Geen wijzigingen = stabiele week.",
    }
    results["weekly"] = weekly
    total_changes = sum(len(c["changes"]) for c in changelog)
    print(f"  {len(changelog)} aandelen met wijzigingen, {total_changes} veranderingen totaal")
    for c in changelog[:5]:
        print(f"    {c['ticker']}: " + "; ".join(ch["text"] for ch in c["changes"]))

    # ── TRACKRECORD ────────────────────────────────────────────────────────────
    # Leg aanbevelingen vast + evalueer verstreken horizons vs benchmark.
    print(f"\n{'='*60}\nTrackrecord bijwerken...")
    track = load_track_record()
    prices_today = {nm: results["stocks"][nm].get("indicators", {}).get("last")
                    for nm in results["stocks"] if "indicators" in results["stocks"][nm]}
    # 1. Vandaag's aanbevelingen vastleggen (idempotent)
    record_recommendations(track, TODAY.isoformat(), results["allocation"],
                           results["stocks"], prices_today, bench_close)
    # 2. Verstreken horizons invullen
    evaluate_outcomes(track, TODAY, price_data, bench_close, TRACK_HORIZONS_WEEKS)
    # 3. Accuraatheid aggregeren (met anti-ruis drempel)
    accuracy = compute_accuracy_stats(track, TRACK_MIN_OBSERVATIONS, TRACK_HORIZONS_WEEKS)
    track["_meta"]["lastUpdate"] = NOW.isoformat()
    track["accuracy"] = accuracy
    track["benchmarkName"] = BENCHMARK_NAME
    track["minObservations"] = TRACK_MIN_OBSERVATIONS
    track["horizonsWeeks"] = TRACK_HORIZONS_WEEKS

    n_records = len(track.get("records", {}))
    n_evaluated = sum(1 for r in track["records"].values() if r["outcomes"])
    print(f"  {n_records} vastgelegde aanbevelingen, {n_evaluated} met ≥1 afgeronde horizon")
    mp_4w = accuracy.get("monthly_pick", {}).get("4w", {})
    if mp_4w.get("accuracyShown"):
        print(f"  Maandpick 4w accuraatheid: {mp_4w['accuracy']}% (n={mp_4w['n']})")
    elif mp_4w.get("n", 0) > 0:
        print(f"  Maandpick 4w: {mp_4w['status']} (gem. relatief {mp_4w.get('avgRelativeReturn')}%)")

    # signals.json krijgt een compacte samenvatting mee (dashboard leest track_record.json apart)
    results["trackSummary"] = {
        "totalRecords": n_records, "evaluated": n_evaluated,
        "benchmarkName": BENCHMARK_NAME, "accuracy": accuracy,
    }

    # Wegschrijven
    print(f"\n{'='*60}\nWegschrijven...")
    try:
        atomic_write(OUTPUT_FILE, results)
        print(f"  ✓ {OUTPUT_FILE}")
        timeline["_meta"]["lastUpdate"] = NOW.isoformat()
        atomic_write(TIMELINE_FILE, timeline)
        print(f"  ✓ {TIMELINE_FILE}")
        atomic_write(WEEKLY_FILE, weekly)
        print(f"  ✓ {WEEKLY_FILE}")
        atomic_write(TRACK_FILE, track)
        print(f"  ✓ {TRACK_FILE}")
        snapshot_path = os.path.join(HISTORY_DIR, f"{TODAY.isoformat()}.json")
        atomic_write(snapshot_path, results)
        print(f"  ✓ {snapshot_path}")
    except Exception as e:
        print(f"  ✗ KRITIEKE FOUT bij wegschrijven: {e}")
        sys.exit(1)

    print(f"\nFouten: {len(results['errors'])}")
    for e in results["errors"]:
        print(f"  ✗ {e}")
    print(f"{'='*60}\n")
    # Exit-beleid: alleen falen als vrijwel niets lukte. Gedeeltelijke data is
    # waardevol en moet gecommit worden — een enkele kapotte exoot mag de run
    # niet rood kleuren en de commit-stap blokkeren.
    ok_count = sum(1 for s in results["stocks"].values() if "indicators" in s)
    if ok_count < max(1, round(len(WATCHLIST) * 0.3)):
        print(f"✗ Slechts {ok_count}/{len(WATCHLIST)} aandelen gelukt — run faalt.")
        sys.exit(1)
    if results["errors"]:
        print(f"⚠ {ok_count}/{len(WATCHLIST)} aandelen gelukt; {len(results['errors'])} fouten (zie boven) — run slaagt met waarschuwingen.")

if __name__ == "__main__":
    main()
