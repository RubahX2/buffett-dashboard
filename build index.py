#!/usr/bin/env python3
"""Bouwt index.html uit ../stock-dashboard.jsx.
Gebruikt EXPLICIETE Babel.transform (niet de auto-detectie via type=text/babel,
die op sommige Safari-versies faalt met 'string did not match expected pattern').
Bevat noindex en een foutscherm dat bij een Babel-fout de volledige melding toont."""

HEAD = '''<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="robots" content="noindex, nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0a0d12">
<title>BUFFETT+ - Kwaliteit x Timing</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>chart</text></svg>">
<style>
  html, body { margin:0; padding:0; background:#0a0d12; }
  #root { min-height:100vh; }
  .bplus-loading { color:#8b949e; font-family:-apple-system,BlinkMacSystemFont,sans-serif;
    font-size:14px; text-align:center; padding-top:42vh; letter-spacing:1px; }
</style>
<script src="https://unpkg.com/react@18.2.0/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18.2.0/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone@7.23.5/babel.min.js"></script>
</head>
<body>
<div id="root"><div class="bplus-loading">BUFFETT+ laden...</div></div>
<script type="text/plain" id="jsx-source">
'''

TAIL = '''</script>
<script>
(function(){
  try {
    var src = document.getElementById("jsx-source").textContent;
    var out = Babel.transform(src, { presets: ["react"] }).code;
    var s = document.createElement("script");
    s.textContent = out;
    document.body.appendChild(s);
  } catch (e) {
    var msg = (e && e.message) ? e.message : String(e);
    var stack = (e && e.stack) ? e.stack : "";
    document.body.innerHTML = "<pre style=\\"color:#f88;padding:20px;font-family:monospace;white-space:pre-wrap;font-size:12px;\\">BABEL FOUT:\\n" + msg + "\\n\\n" + stack + "</pre>";
    console.error("Babel transform faalde:", e);
  }
})();
</script>
</body>
</html>
'''

jsx = open('../stock-dashboard.jsx').read()

# De React-import omzetten naar een destructuring van het globale React-object.
# BELANGRIJK: dit gebeurt met een REGEX, niet met een letterlijke string-replace.
# Eerder stond hier de exacte tekst 'import { useState, useEffect } from "react";'.
# Zodra er een hook bijkwam (useRef), matchte die replace niet meer: de import-regel
# bleef ongewijzigd staan als ongeldige JS, de hook was nooit gedefinieerd, en de
# pagina bleef hangen op "BUFFETT+ laden...". De regex vangt elke hook-combinatie.
import re as _re
jsx, _n = _re.subn(
    r'import\s*\{([^}]*)\}\s*from\s*["\']react["\']\s*;',
    lambda m: 'const {%s} = React;' % m.group(1),
    jsx, count=1)
if _n != 1:
    raise SystemExit("FOUT: de React-import is niet gevonden of niet omgezet. "
                     "Controleer de eerste regel van stock-dashboard.jsx.")
# Vangnet: er mag geen enkele import-regel overblijven in de bundel.
if _re.search(r'^\s*import\s', jsx, _re.M):
    raise SystemExit("FOUT: er staat nog een import-regel in de bundel; "
                     "die is ongeldig in een browser-script.")
old_url = '''// -- CONFIG -- pas deze URL aan na GitHub Pages setup ---------------------------
// Formaat: https://JOUW-GITHUB-NAAM.github.io/REPO-NAAM/signals.json
const SIGNALS_URL = "https://RubahX2.github.io/buffett-dashboard/signals.json";'''
# de bron kan al het relatieve pad hebben; probeer beide
ROBUST_URL = '''const SIGNALS_URL = (function(){
  var base = window.location.href.split("?")[0].split("#")[0];
  base = base.substring(0, base.lastIndexOf("/") + 1);
  return base + "signals.json";
})();'''
if old_url in jsx:
    jsx = jsx.replace(old_url, ROBUST_URL)
elif 'const SIGNALS_URL = "./signals.json";' in jsx:
    jsx = jsx.replace('const SIGNALS_URL = "./signals.json";', ROBUST_URL)
jsx = jsx.replace('export default function App() {', 'function App() {')
jsx = jsx.rstrip() + '\n\nReactDOM.createRoot(document.getElementById("root")).render(<App/>);\n'

# Verwijder onzichtbare tekens die parsers kunnen breken
for ch in ['\uFE0F','\uFE0E','\u200B','\u200C','\u200D','\u2060','\uFEFF']:
    jsx = jsx.replace(ch, '')

assert 'export' not in jsx, "export-statement niet verwijderd uit jsx"
assert 'signals.json' in jsx, "signals.json-referentie ontbreekt in jsx"
assert 'ReactDOM.createRoot' in jsx, "render-call ontbreekt"

# VEILIGHEIDSCHECK: geen parser-brekende onzichtbare tekens (Safari struikelt hierover).
# Variation selectors (U+FE0F etc.) en zero-width tekens breken de JS-parser stil.
import unicodedata as _ud
_bad = []
for _i, _ch in enumerate(jsx):
    _o = ord(_ch)
    if _o in (0xFE0F,0xFE0E,0x200B,0x200C,0x200D,0x2060,0xFEFF):
        _bad.append((_i, _o))
    elif _ud.category(_ch) in ('Cf','Cc') and _ch not in '\n\r\t':
        _bad.append((_i, _o))
if _bad:
    _ctx = jsx[max(0,_bad[0][0]-30):_bad[0][0]+10]
    raise SystemExit(f"BUILD GESTOPT: parser-brekend teken U+{_bad[0][1]:04X} in jsx "
                     f"(en {len(_bad)-1} andere). Context: {_ctx!r}. "
                     "Dit zou Safari breken — verwijder het teken in de bron.")

html = HEAD + jsx + TAIL
open('index.html','w',encoding='utf-8').write(html)
print(f"index.html gebouwd (robuuste versie): {len(html):,} tekens")
