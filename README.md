# BUFFETT+ Signaal Dashboard

Automatisch dagelijks technisch signaaloverzicht voor 10 aandelen.

## Inhoud repository

```
buffett-dashboard/
├── analyze.py                        # Hoofdscript: data ophalen + signalen berekenen
├── requirements.txt                  # Python dependencies (yfinance, pandas, numpy)
├── signals.json                      # Huidige staat (dagelijks overschreven)
├── timeline.json                     # Doorlopende kernmetrieken per aandeel (voor 'wat veranderde')
├── weekly.json                       # Wekelijkse changelog (wat veranderde sinds vorige week)
├── track_record.json                 # Trackrecord: aanbevelingen + forward return vs benchmark
│                                    #   (signals.json bevat ook 'baggers' sectie)
├── history/                          # Dagsnapshots YYYY-MM-DD.json (voor wekelijkse vergelijking)
├── .github/
│   └── workflows/
│       └── daily.yml                 # GitHub Actions: dagelijkse automatisering
└── README.md
```

## Versie 2 — fundament

Deze versie bevat de volgende verbeteringen t.o.v. de eerste opzet:
- **RSI exact volgens Wilder/TradingView** (gevalideerd tegen referentiewaarden)
- **NaN-guards overal**: aandelen met te weinig historie falen niet meer stil
- **Onvolledige weekcandle wordt weggegooid** (geen valse weekly signalen)
- **Batch-download** via yfinance (sneller, minder rate-limit risico)
- **Data sanity-checks** (negatieve prijzen, extreme bewegingen worden gedetecteerd)
- **Crossover-detectie matcht TradingView's crossover()** (tekenwissel op laatste candle)
- **Historische opslag**: timeline.json + dagsnapshots, fundament voor wekelijkse changelog
- **Geen pytz meer nodig** (gebruikt stdlib zoneinfo)

## Waarderingslaag

Beantwoordt de vraag: *"staat dit kwaliteitsaandeel nu goedkoop of duur?"* — zodat je niet te duur koopt.

Eerlijke meet-filosofie:
- **PEG-ratio** (P/E gedeeld door groei) is de primaire maatstaf. Lynch-stijl: PEG < 1 goedkoop, > 3.5 duur. Dit corrigeert voor groei en gebruikt echte huidige cijfers.
- **Echte historische P/E-percentiel** wordt alleen getoond met echte data via FMP. Het systeem fabriceert géén P/E-historie — dat zou groeiaandelen systematisch vals goedkoop laten lijken.
- **Prijspositie in 5-jaars range** als context, eerlijk gelabeld (een aandeel hoog in zijn prijsrange kan nog goedkoop zijn als de winst sneller groeide).

### Optioneel: echte historische P/E via FMP

Voor de echte historische P/E-percentiel heb je een gratis FMP-account nodig:
1. Maak een gratis account op financialmodelingprep.com
2. Kopieer je API-key
3. Voeg toe aan repository → Settings → Secrets → Actions: `FMP_API_KEY`

Zonder deze key werkt alles gewoon door op basis van PEG. De FMP-aanroep gebeurt in GitHub Actions (een echte server) en heeft dus geen last van de CORS-beperking die de browser-app wel had.

Let op: FMP dekt Euronext-tickers (zoals ASM.AS) vaak onbetrouwbaar — voor die aandelen valt het systeem automatisch terug op PEG.

## Allocatielaag — kwaliteit × timing

De kern van het systeem: het combineert alle lagen tot één maandelijkse aanbeveling.

**Vier lagen:**
1. **Kwaliteitspoort** — alleen aandelen met voldoende ROE, marge en houdbare schuld zijn koopkandidaat (harde filter).
2. **Waardering** — PEG + (indien FMP) historische P/E-percentiel bepalen of het niet te duur is.
3. **Multi-timeframe timing** — technische analyse op **daily, weekly én monthly**:
   - *Trend-score*: is dit een uptrend? (EMA 8/21, MACD, RSI-tilt) — hogere timeframes wegen zwaarder (monthly 45%, weekly 35%, daily 20%), zodat je geen vallend mes vangt.
   - *Entry-score*: is nú een goed instapmoment? (RSI oversold, Bollinger-positie, Fibonacci-steun) — vooral daily.
   - timingScore = 60% trend + 40% entry.
4. **Composiet** — kwaliteit (30%) + waardering (30%) + timing (40%) → één score per aandeel.

**De maandelijkse aanbeveling** rangschikt alle kwaliteitspoort-passers op composietscore. Het hoogste aandeel is de aanbevolen bestemming voor je maandelijkse inleg — kwaliteit, niet te duur, én goed getimed.

De monthly-analyse vereist ~5 jaar historie (daarom haalt het script 5 jaar op) voor een betrouwbare monthly MACD.

> Geen financieel advies. De scores zijn hulpmiddelen; het systeem is transparant zodat je elke aanbeveling kunt narekenen en met eigen oordeel combineren.

## Wekelijkse changelog

Elke run vergelijkt de huidige staat met ~7 dagen geleden (uit `timeline.json`) en toont **alleen wat veranderde** — geen statisch overzicht. Gedetecteerd worden:
- Composietscore-verschuivingen (drempel ≥ 5 punten)
- Kwaliteitspoort in/uit bewogen
- Waarderingsoordeel gewijzigd (bv. van "duur" naar "redelijk")
- Overall signaal gewijzigd
- RSI weekly die oversold/overbought werd
- Koersbewegingen ≥ 8% in de week

Output in `weekly.json`, getoond bovenaan de Allocatie-pagina. Een stabiele week zonder wijzigingen toont dat expliciet. Nieuwe aandelen zonder weekhistorie en te oude vergelijkingspunten worden overgeslagen (geen misleidende vergelijking).

## Trackrecord (observatie-instrument)

Meet of de aanbevelingen werken — maar eerlijk, met ingebouwde waarborgen tegen zelfbedrog.

Bij elke run worden aanbevelingen vastgelegd met tijdstempel en de volledige staat op dat moment:
- **Maandaanbeveling** (de primaire pick) en **sterke signalen** (STERK KOOP / STERK VERKOOP), gescheiden gemeten.

Bij latere runs wordt de forward return berekend op **1, 4, 13 en 26 weken**, telkens **relatief tot de S&P 500** (`^GSPC`) — zodat je "beter dan de index?" meet in plaats van "ging de markt omhoog?". Een STERK VERKOOP telt een koersdaling als succes.

**Twee eerlijkheidswaarborgen:**
1. **Anti-ruis drempel:** zolang er minder dan 20 afgeronde observaties per horizon zijn, wordt géén accuraatheidspercentage getoond — enkel de telling en gemiddelde return. Zo ga je geen ruis voor signaal aanzien.
2. **Mens-in-de-lus:** het model past nooit zelf gewichten aan. Het toont data; jij trekt de conclusies.

Output in `track_record.json`, getoond op de Trackrecord-pagina. Realistisch duurt het maanden tot de eerste horizons genoeg observaties hebben voor betekenisvolle cijfers — dat is inherent aan een eerlijke meting, geen tekortkoming.

> Een accuraatheidscijfer op weinig data is misleidend. Het instrument is bewust ontworpen om die val te vermijden.

## Bagger-spoor (apart raamwerk)

Een tweede, gescheiden spoor dat zoekt naar vroege multibaggers — aandelen met asymmetrisch potentieel die er op klassieke maatstaven juist "duur" of "zwak" uitzien.

**Waardering telt hier bewust NIET.** In plaats daarvan een bagger-score (0-100) op basis van:
- **Omzetgroei** (>40% telt zwaar, >60% is explosief) — de kern van elke bagger
- **Groei-versnelling** — versnelt de omzetgroei t.o.v. vorig jaar, of vertraagt ze?
- **Brutomarge-trend** — operating leverage die zich ontvouwt (stijgende marge bij groei)
- **Relatieve sterkte vs de markt** — "stemt" de markt al voor het aandeel?

**Risico-flags** (korte cash runway, microcap) bepalen niet de score maar wél het positiegrootte-advies. Een aandeel kan een hoge bagger-score hebben én een "vermijden"-advies als het risico te groot is.

**Kleine positiegroottes** zijn ingebouwd in het advies: 100x-baggers vooraf spotten lukt zelfs topfondsen zelden. Dit verhoogt de kans, het is geen garantie — vandaar klein instappen.

Een aandeel kan in **beide sporen** staan: een kwaliteitsbedrijf dat óók hard groeit krijgt in het bagger-overzicht een "✓ ook kwaliteitspoort"-label.

### Bagger-tickers en hun data

De bagger-kandidaten (LHX, MOG.A, TDG, KTOS, RKLB, OPEN, SDGR, BNGO) hebben extra fundamentele velden nodig: `grossMargin`, `grossMarginTrend` (pp YoY), `revenueGrowthPrev` (voor versnelling) en `cashRunwayMonths`. Deze staan in de `FUNDAMENTALS`-dict, indicatief per begin 2026.

> **Belangrijk:** voor verlieslatende microcaps (vooral BNGO) veranderen deze cijfers snel. Verifieer en werk ze per kwartaal bij. Let ook op: MOG.A gebruikt de puntnotatie die yfinance onbetrouwbaar dekt — er is een fallback (MOG-A), maar controleer of de data binnenkomt.

## Signaaltypen

| Signaal | Timeframe | Gewicht |
|---|---|---|
| RSI oversold/overbought | Daily + Weekly | 3 / 4 |
| MACD crossover | Daily / Weekly* | 2 / 4 |
| EMA 8/21 crossover | Daily / Weekly* | 3 / 4 |
| Golden/Death cross MA50/MA200 | Daily | 4 |
| Bollinger Bands touch | Daily | 2 |
| Fibonacci retracements | Daily (52W swing) | 3 |
| Fibonacci extensies (TP zones) | Daily (52W swing) | 3 |

*Weekly signalen worden uitsluitend op vrijdag geëvalueerd (volledige weekcandle).

## Eenmalige setup (±20 minuten)

### Stap 1 — GitHub repository aanmaken
1. Ga naar github.com en log in
2. Klik op "New repository"
3. Naam: `buffett-dashboard`
4. Zet op **Public** (nodig voor GitHub Pages)
5. Klik "Create repository"

### Stap 2 — Bestanden uploaden
Upload via de GitHub website (drag & drop):
- `analyze.py`
- `requirements.txt`
- `signals.json`

Voor de workflow: maak de mapstructuur `.github/workflows/` aan en upload `daily.yml`.

### Stap 3 — GitHub Pages activeren
1. Ga naar repository → Settings → Pages
2. Source: "Deploy from a branch"
3. Branch: `main` / `(root)`
4. Klik Save
5. Wacht 2 minuten → je krijgt een URL: `https://JOUW-NAAM.github.io/buffett-dashboard/`

### Stap 4 — E-mailnotificaties instellen (optioneel)
Voeg toe aan repository → Settings → Secrets → Actions:
- `GMAIL_USER`: jouw Gmail-adres (optioneel, voor foutmeldingen)
- `GMAIL_PASS`: Gmail App Password (optioneel — genereer via Google Account → Beveiliging → App-wachtwoorden)
- `NOTIFY_EMAIL`: e-mailadres voor notificaties (optioneel)
- `FMP_API_KEY`: Financial Modeling Prep key (optioneel, voor echte historische P/E)

Alle secrets zijn optioneel. Zonder Gmail-secrets krijg je geen foutmail; zonder FMP-key gebruikt de waardering PEG in plaats van historische P/E.

### Stap 5 — Dashboard app configureren
In de dashboard app (stock-dashboard.jsx), vervang op regel 4:
```
const SIGNALS_URL = "https://YOUR-USERNAME.github.io/buffett-dashboard/signals.json";
```
door:
```
const SIGNALS_URL = "https://JOUW-GITHUB-NAAM.github.io/buffett-dashboard/signals.json";
```

### Stap 6 — Eerste run handmatig starten
1. Ga naar repository → Actions
2. Klik "Dagelijkse Signaalanalyse"
3. Klik "Run workflow" → "Run workflow"
4. Wacht ±2 minuten
5. Ververs de dashboard app → data verschijnt

## Automatische werking

Elke werkdag om ±08:00 Brussels tijd:
1. GitHub Actions start automatisch
2. Python haalt 3 jaar dagdata op via yfinance
3. Alle 7 signaaltypen worden berekend
4. signals.json wordt atomisch overschreven
5. Wijziging wordt gecommit naar repository
6. Dashboard app leest automatisch de nieuwe data bij volgende opening

## Onderhoud

**Fundamentals bijwerken**: Open `analyze.py` en pas de `FUNDAMENTALS` dictionary aan na elk kwartaalrapport.

**Ticker toevoegen**: Voeg toe aan `WATCHLIST` in `analyze.py` en herlaad de workflow.

**Bij fouten**: Ga naar repository → Actions → bekijk de logs van de laatste run.
