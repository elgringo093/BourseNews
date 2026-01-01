import os
import re
import json
import time
import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import feedparser
from jinja2 import Template

# OpenAI SDK
from openai import OpenAI

# ----------------------------
# CONFIG
# ----------------------------

MODEL = "gpt-5.2"  # ou "gpt-5.2-chat-latest" si tu préfères
MAX_ITEMS_PER_FEED = 10
REQUEST_DELAY_SEC = 0.3  # petit délai entre feeds

# Tes feeds RSS (tu peux en ajouter/enlever)
FEEDS: Dict[str, str] = {
    # ONDAS (IR officiel)
    "Ondas IR (official)": "https://ir.ondas.com/rss",

    # Bloomberg (gros flux)
    "Bloomberg Markets": "https://feeds.bloomberg.com/markets/news.rss",
    "Bloomberg Technology": "https://feeds.bloomberg.com/technology/news.rss",

    # Google News RSS (requêtes ciblées)
    # Astuce: quand tu veux cibler “Ondas Holdings” + ticker
    "GoogleNews ONDS": "https://news.google.com/rss/search?q=Ondas%20Holdings%20ONDS%20when%3A7d&hl=en-US&gl=US&ceid=US:en",
    "GoogleNews Micron MU": "https://news.google.com/rss/search?q=Micron%20Technology%20MU%20when%3A7d&hl=en-US&gl=US&ceid=US:en",

    # Tu peux faire une version Europe/FR :
    "GoogleNews Micron FR": "https://news.google.com/rss/search?q=Micron%20Technology%20when%3A7d&hl=fr&gl=FR&ceid=FR:fr",
    "Les Echos - Marchés": "https://www.lesechos.fr/rss/rss_finance.xml",
    "Boursorama - Bourse": "https://www.boursorama.com/rss/actualites/bourse/",
    "Zonebourse - News": "https://www.zonebourse.com/rss/news.xml",
    "Bloomberg - Europe": "https://news.google.com/rss/search?q=site%3Abloomberg.com%20Europe&hl=en-GB&gl=GB&ceid=GB:en",

    # Tu peux faire une version Etats-unis/US :
    "Reuters - Markets": "https://feeds.reuters.com/reuters/businessNews",
    "CNBC - Top News": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "Micron Technology": "https://news.google.com/rss/search?q=Micron%20Technology%20MU&hl=en-US&gl=US&ceid=US:en",
    "Ondas filings": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001646188&owner=exclude&count=40&output=atom",
    "Micron filings": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000723125&owner=exclude&count=40&output=atom",
    "Bloomberg - général via Google News": "https://news.google.com/rss/search?q=site%3Abloomberg.com&hl=en-US&gl=US&ceid=US:en",

}

OUT_DIR = "output"
DB_PATH = os.path.join(OUT_DIR, "boursenews.sqlite3")
DASHBOARD_PATH = os.path.join(OUT_DIR, "dashboard.html")
ITEMS_JSON_PATH = os.path.join(OUT_DIR, "items.json")
DAILY_SUMMARY_PATH = os.path.join(OUT_DIR, "daily_summary.txt")

# ----------------------------
# HELPERS
# ----------------------------

def ensure_out_dir() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

def get_api_key() -> str:
    """
    Priorité:
    1) variable d'env OPENAI_API_KEY
    2) fallback: tente de lire .env en UTF-8 / UTF-8-sig si présent
    """
    k = os.getenv("OPENAI_API_KEY")
    if k:
        return k.strip()

    # fallback .env sans dépendre de python-dotenv (pour éviter les soucis d'encodage)
    if os.path.exists(".env"):
        for enc in ("utf-8", "utf-8-sig", "cp1252"):
            try:
                with open(".env", "r", encoding=enc) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("OPENAI_API_KEY="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass

    raise RuntimeError(
        "OPENAI_API_KEY introuvable. Mets-la via: $env:OPENAI_API_KEY=\"sk-...\" (PowerShell) "
        "ou crée un fichier .env avec OPENAI_API_KEY=sk-..."
    )

def norm_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s

def stable_id(feed_name: str, title: str, link: str) -> str:
    raw = f"{feed_name}||{title}||{link}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            feed_name TEXT,
            title TEXT,
            link TEXT,
            published TEXT,
            summary TEXT,
            ai_summary TEXT,
            sentiment TEXT,
            score INTEGER,
            created_at TEXT
        )
    """)

    # migrations légères (ajoute les colonnes si elles n'existent pas)
    cols_to_add = [
        ("priority", "TEXT"),
        ("publication_freshness", "TEXT"),
        ("market_bias", "TEXT"),
        ("time_horizon", "TEXT"),
        ("confidence_level", "TEXT"),
        ("key_links", "TEXT"),          # JSON string
        ("investor_takeaway", "TEXT"),
        ("publisher", "TEXT"),
        ("markets_impacted", "TEXT"),   # JSON string (liste)
]

    cur.execute("PRAGMA table_info(items)")
    existing = {row[1] for row in cur.fetchall()}

    for col, typ in cols_to_add:
        if col not in existing:
            cur.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")

    conn.commit()
    conn.close()

def seen(conn: sqlite3.Connection, item_id: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM items WHERE id=? LIMIT 1", (item_id,))
    return cur.fetchone() is not None

def save_item(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO items
        (id, feed_name, title, link, published, summary, ai_summary, sentiment, score, created_at,
         priority, publication_freshness, market_bias, time_horizon, confidence_level, key_links, investor_takeaway, publisher, markets_impacted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["id"], row["feed_name"], row["title"], row["link"],
        row.get("published"), row.get("summary"), row.get("ai_summary"),
        row.get("sentiment"), row.get("score"), row.get("created_at"),
        row.get("priority"), row.get("publication_freshness"), row.get("market_bias"),
        row.get("time_horizon"), row.get("confidence_level"),
        json.dumps(row.get("key_links", []), ensure_ascii=False),
        row.get("investor_takeaway"), row.get("publisher"),
        json.dumps(row.get("markets_impacted", []), ensure_ascii=False),
    ))
    conn.commit()

def fetch_feed(feed_name: str, url: str) -> List[Dict[str, Any]]:
    parsed = feedparser.parse(url)
    entries = parsed.entries[:MAX_ITEMS_PER_FEED]
    out = []
    for e in entries:
        title = norm_text(getattr(e, "title", ""))
        link = norm_text(getattr(e, "link", ""))
        summary = norm_text(getattr(e, "summary", "") or getattr(e, "description", ""))
        published = norm_text(getattr(e, "published", "") or getattr(e, "updated", ""))

        if not title or not link:
            continue

        out.append({
            "feed_name": feed_name,
            "title": title,
            "link": link,
            "summary": summary,
            "published": published,
            "id": stable_id(feed_name, title, link),
        })
    return out

def analyze_with_openai(client: OpenAI, item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Retourne:
    - ai_summary (FR)
    - sentiment: positive/negative/neutral
    - score: -2,-1,0,+1,+2 (impact marché)
    """
    prompt = f"""
Tu es un analyste macro-financier senior spécialisé en marchés financiers globaux
(actions, indices, taux, matières premières, devises, crypto, ETF).

OBJECTIF :
Transformer l’actualité brute en un signal exploitable pour un investisseur,
en tenant compte de la temporalité de l’information.

CONTEXTE DE L’ARTICLE :
Source : {item["feed_name"]}
Date de publication : {item["published"]}
Titre : {item["title"]}
Contenu / extrait : {item["summary"]}
Lien : {item["link"]}

INSTRUCTIONS CRITIQUES :

1️⃣ ANALYSE TEMPORELLE (PRIORITAIRE)
- Analyse la date de publication :
  - information très récente (impact immédiat possible)
  - information récente mais déjà partiellement intégrée par le marché
  - information ancienne servant de confirmation ou de rappel
- Évalue si le marché a probablement déjà “pricé” cette information ou non.

2️⃣ ANALYSE CONTEXTUELLE
- Interprète l’information dans un cadre macro-économique global.
- Identifie si la news est :
  - micro (entreprise spécifique)
  - sectorielle
  - macro (inflation, taux, géopolitique, liquidité, politique monétaire).
- Détermine si cette info est :
  - nouvelle
  - une confirmation
  - une contradiction d’une tendance existante.

3️⃣ LIENS ENTRE ACTUALITÉS (RENFORCEMENT DE SIGNAL)
- Mets cette information en relation avec :
  - d’autres articles récents
  - événements macro-économiques connus
  - narratifs dominants de marché (risk-on / risk-off, taux, croissance).
- Si plusieurs articles récents convergent vers la même conséquence,
  considère cela comme un SIGNAL un plus ou moins RENFORCÉ (logique).

4️⃣ IMPACT MARCHÉ
- Évalue l’impact probable sur :
  - sentiment global de marché
  - actifs concernés (actions, indices, obligations, devises, matières premières).
- Distingue clairement :
  - impact immédiat (heures / jours)
  - impact différé (semaines / mois).

5️⃣ PRIORISATION (ESSENTIEL)
Classe l’importance de cette information en tenant compte de :
- sa date
- sa nouveauté
- sa capacité à modifier un narratif existant

Niveaux :
- critical : événement structurant ou catalyseur
- high : information importante mais non décisive seule
- medium : confirmation utile
- low : bruit de marché ou info déjà intégrée

6️⃣ SORTIE OBLIGATOIRE (FORMAT STRICT JSON)
Réponds STRICTEMENT en JSON avec les champs suivants :

{{ 
  "priority": "critical|high|medium|low",
  "publication_freshness": "très_récent|récent|ancien",
  "ai_summary": "Analyse synthétique en français, orientée investisseur (10–15 phrases max)",
  "market_bias": "bullish|bearish|neutral|volatile",
  "sentiment": "positive|negative|neutral",
  "score": -2|-1|0|1|2,
  "time_horizon": "court_terme|moyen_terme|long_terme",
  "confidence_level": "faible|modérée|élevée",
  "publisher": "Nom court de l’éditeur (ex: Bloomberg, Reuters, FT...)",
  "markets_impacted": ["Liste des marchés/actifs impactés (ex: Nasdaq 100, EU steel, USD, Oil, Bunds... en français)"],
  "key_links": [
    "Lien logique avec d’autres événements ou articles récents",
    "Confirmation ou contradiction d’un narratif macro"
  ],
  "investor_takeaway": "Pourquoi cette information compte réellement pour un investisseur aujourd’hui"
}}

RÈGLES :
- Ton analyse doit être froide, factuelle et orientée décision.
- Prends explicitement en compte la date de publication dans ton jugement.
- Ne surestime pas une information ancienne sauf si elle renforce un signal récent.
- Privilégie la convergence d’informations et la temporalité plutôt que l’article isolé.

""".strip()

    resp = client.responses.create(
        model=MODEL,
        input=prompt
    )

    text = resp.output_text.strip()

    # extraction JSON robuste
    m = re.search(r"\{[\s\S]*?\}", text)
    if not m:
        return {"ai_summary": text, "sentiment": "neutral", "score": 0, "bullets": []}

    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"ai_summary": text, "sentiment": "neutral", "score": 0, "bullets": []}

        # normalisation
    def get_str(key, default=""):
        v = data.get(key, default)
        return norm_text(str(v)) if v is not None else default


    priority = get_str("priority", "low").lower()
    if priority not in ("critical", "high", "medium", "low"):
        priority = "low"

    publication_freshness = get_str("publication_freshness", "récent").lower()
    # laisse flexible (tes valeurs peuvent être "très_récent|récent|ancien" ou variantes)

    market_bias = get_str("market_bias", "neutral").lower()
    sentiment = get_str("sentiment", "neutral").lower()
    if sentiment not in ("positive", "negative", "neutral"):
        sentiment = "neutral"

    try:
        score = int(data.get("score", 0))
    except Exception:
        score = 0
    if score not in (-2, -1, 0, 1, 2):
        score = 0

    time_horizon = get_str("time_horizon", "")
    confidence_level = get_str("confidence_level", "")

    key_links = data.get("key_links", [])
    if not isinstance(key_links, list):
        key_links = []
    key_links = [norm_text(str(x)) for x in key_links][:3]

    investor_takeaway = get_str("investor_takeaway", "")

    ai_summary = get_str("ai_summary", "")
    if not ai_summary:
        ai_summary = investor_takeaway or item.get("summary", "")

    publisher = get_str("publisher", "") or item.get("feed_name", "")

    markets_impacted = data.get("markets_impacted", [])
    if not isinstance(markets_impacted, list):
        markets_impacted = []

    markets_impacted = [
        norm_text(str(x))
        for x in markets_impacted
        if str(x).strip()
    ][:5]

    return {
        "priority": priority,
        "publication_freshness": publication_freshness,
        "ai_summary": ai_summary,
        "market_bias": market_bias,
        "sentiment": sentiment,
        "score": score,
        "time_horizon": time_horizon,
        "confidence_level": confidence_level,
        "key_links": key_links,
        "investor_takeaway": investor_takeaway,
        "publisher": publisher,
        "markets_impacted": markets_impacted,
    }

def load_all_items() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, feed_name, title, link, published, summary, ai_summary, sentiment, score, created_at, priority, publication_freshness, market_bias,
        time_horizon, confidence_level, key_links, investor_takeaway, publisher, markets_impacted

        FROM items
        ORDER BY created_at DESC
        LIMIT 200
    """)
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        out.append({
    "id": r[0],
    "feed_name": r[1],
    "title": r[2],
    "link": r[3],
    "published": r[4],
    "summary": r[5],
    "ai_summary": r[6],
    "sentiment": r[7],
    "score": r[8],
    "created_at": r[9],

    # ✅ nouveaux champs
    "priority": r[10],
    "publication_freshness": r[11],
    "market_bias": r[12],
    "time_horizon": r[13],
    "confidence_level": r[14],
    "key_links": json.loads(r[15]) if r[15] else [],
    "investor_takeaway": r[16],
    "publisher": r[17] or "",
    "markets_impacted": json.loads(r[18]) if r[18] else [],
})

    return out

def build_dashboard(items: List[Dict[str, Any]]) -> None:
    tpl = Template("""
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>BourseNews Dashboard</title>
<style>
  body { font-family: Arial, sans-serif; margin: 20px; background: #0b0f14; color: #e8eef6; }
  h1 { margin-bottom: 6px; }
  .meta { opacity: 0.8; margin-bottom: 18px; }
  .card { background: #121826; border: 1px solid #1f2a3a; border-radius: 10px; padding: 14px; margin: 10px 0; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .tag { font-size: 12px; padding: 4px 8px; border-radius: 999px; background:#1f2a3a; }
  .pos { background: #0f2a1a; border-color:#1f6f3a; }
  .neg { background: #2a1212; border-color:#7a1f1f; }
  .neu { background: #121826; border-color:#1f2a3a; }
  a { color: #7dd3fc; text-decoration:none; }
  a:hover { text-decoration:underline; }
  .small { font-size: 12px; opacity: 0.85; }
  ul { margin: 8px 0 0 18px; }
</style>
</head>
<body>
  <h1>📈 BourseNews</h1>
  <div class="meta">Généré le {{ now }} — Modèle: {{ model }} — Items: {{ items|length }}</div>
  <div class="controls" style="margin: 12px 0; display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
  <label class="small">Tri :</label>
  <select id="sortMode">
    <option value="default">Par défaut</option>
    <option value="score">Score (desc)</option>
    <option value="score_x_priority">Score × Priority (desc)</option>
  </select>

  <button id="top10Today" type="button">Top 10 aujourd’hui</button>
  <button id="resetView" type="button">Reset</button>

  <span id="viewInfo" class="small" style="opacity:.85;"></span>
</div>

  <div id="filters" class="card" style="position:sticky; top:10px; z-index:20;">
  <div class="row" style="gap:12px;">
    <span class="tag">Filtres</span>

    <label class="small">Tri</label>
    <select id="sortBy" class="tag">
      <option value="created_desc">Date (récent → ancien)</option>
      <option value="priority_desc">Priorité (critical → low)</option>
      <option value="score_desc">Score (haut → bas)</option>
      <option value="publisher_asc">Éditeur (A → Z)</option>
    </select>

    <label class="small">Priorité</label>
    <select id="priorityFilter" class="tag">
      <option value="">Toutes</option>
      <option value="critical">critical</option>
      <option value="high">high</option>
      <option value="medium">medium</option>
      <option value="low">low</option>
    </select>

    <label class="small">Éditeur</label>
    <select id="publisherFilter" class="tag">
      <option value="">Tous</option>
    </select>

    <label class="small">Marché impacté</label>
    <select id="marketFilter" class="tag">
      <option value="">Tous</option>
    </select>

    <label class="small">Recherche</label>
    <input id="q" class="tag" style="min-width:220px;" placeholder="mot-clé (titre/résumé)…" />

    <button id="resetBtn" class="tag" type="button">Reset</button>

    <span class="small" id="countInfo"></span>
  </div>
</div>

  {% for it in items %}
    {% set cls = "neu" %}
    {% if it.sentiment == "positive" %}{% set cls = "pos" %}{% endif %}
    {% if it.sentiment == "negative" %}{% set cls = "neg" %}{% endif %}

<div class="card {{ cls }}"
     data-priority="{{ it.priority }}"
     data-score="{{ it.score if it.score is not none else 0 }}"
     data-created-at="{{ it.created_at if it.created_at else '' }}"
     data-publisher="{{ it.publisher if it.publisher else it.feed_name }}"
     data-markets="{{ (it.markets_impacted | join(',')) if it.markets_impacted else '' }}">

      <div class="row">
        <span class="tag">{{ it.sentiment }} ({{ it.score }})</span>
        <span class="tag">{{ it.feed_name }}</span>
        <span class="tag">prio: {{ it.priority }}</span>
        <span class="tag">fresh: {{ it.publication_freshness }}</span>
        {% if it.published %}
        <span class="tag">pub: {{ it.published }}</span>
        {% endif %}

        <span class="small">{{ it.created_at }}</span>
      </div>
      <h3 style="margin:10px 0 6px 0;">
        <a href="{{ it.link }}" target="_blank" rel="noopener">{{ it.title }}
        {% if it.markets_impacted %} ({{ it.markets_impacted | join(', ') }}){% endif %}</a>
      </h3>
      <div class="small"><b>Résumé IA:</b> {{ it.ai_summary }}</div>
      {% if it.summary %}
        <div class="small" style="margin-top:6px; opacity:0.75;"><b>Extrait:</b> {{ it.summary }}</div>
      {% endif %}
    </div>
  {% endfor %}
  <script>
(function(){
  // Ne prend que les vraies cartes articles (pas le bloc #filters)
  const getCards = () =>
    Array.from(document.querySelectorAll(".card"))
      .filter(c => c.id !== "filters" && !c.closest("#filters"));

  const sortBy = document.getElementById("sortBy");               // ton select "Tri" du bloc filtres
  const sortMode = document.getElementById("sortMode");           // ton select "Tri" du haut (si tu le gardes)
  const priorityFilter = document.getElementById("priorityFilter");
  const publisherFilter = document.getElementById("publisherFilter");
  const marketFilter = document.getElementById("marketFilter");
  const q = document.getElementById("q");
  const resetBtn = document.getElementById("resetBtn");
  const countInfo = document.getElementById("countInfo");

  const top10Btn = document.getElementById("top10Today");         // bouton "Top 10 aujourd’hui"
  const resetViewBtn = document.getElementById("resetView");      // bouton reset du haut (si présent)
  const viewInfo = document.getElementById("viewInfo");           // petit texte info (si présent)

  // Priorité -> poids (accepte aussi strings type "critical"/"high"... ou nombres)
  function priorityWeight(p) {
    if (p === null || p === undefined) return 1;

    // Numérique ("1","2","3","4")
    const n = Number(p);
    if (!Number.isNaN(n) && n > 0) return n;

    const s = String(p).trim().toLowerCase();
    if (s === "critical" || s === "prio: critical") return 4;
    if (s === "high" || s === "prio: high" || s === "urgent") return 3;
    if (s === "medium" || s === "med" || s === "prio: medium") return 2;
    if (s === "low" || s === "prio: low") return 1;
    return 1;
  }

  function parseScore(card){
    const v = parseFloat(card.dataset.score || "0");
    return Number.isFinite(v) ? v : 0;
  }

  function parseCreatedAt(card){
    const raw = card.dataset.createdAt || "";
    const d = raw ? new Date(raw) : null;
    return (d && !Number.isNaN(d.getTime())) ? d : null;
  }

  function sameLocalDay(a, b){
    return a.getFullYear() === b.getFullYear()
        && a.getMonth() === b.getMonth()
        && a.getDate() === b.getDate();
  }

  // Sauvegarde ordre initial (pour "Par défaut")
  const initialOrder = getCards().slice();

  // Remplit les dropdowns (publisher/markets) depuis les cartes
  const publishers = new Set();
  const markets = new Set();
  getCards().forEach(c => {
    const p = (c.dataset.publisher || "").trim();
    if (p) publishers.add(p);

    const m = (c.dataset.markets || "")
      .split(",").map(x => x.trim()).filter(Boolean);
    m.forEach(x => markets.add(x));
  });

  if (publisherFilter){
    Array.from(publishers).sort((a,b)=>a.localeCompare(b)).forEach(p => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      publisherFilter.appendChild(opt);
    });
  }

  if (marketFilter){
    Array.from(markets).sort((a,b)=>a.localeCompare(b)).forEach(m => {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      marketFilter.appendChild(opt);
    });
  }

  function renderInOrder(cards){
    // Réinsère les cartes après le bloc filtres (ou après controls/meta si besoin)
    const anchor =
      document.querySelector("#filters") ||
      document.querySelector(".controls") ||
      document.querySelector(".meta");

    cards.forEach(card => anchor.insertAdjacentElement("afterend", card));
  }

  function setViewInfo(txt){
    if (viewInfo) viewInfo.textContent = txt || "";
  }

  function applySort(){
    // Source du mode de tri :
    // - priorité au select du haut (sortMode) si présent
    // - sinon on utilise sortBy (bloc filtres)
    const mode = (sortMode && sortMode.value) ? sortMode.value : (sortBy ? sortBy.value : "created_desc");

    // "Par défaut"
    if (mode === "default"){
      renderInOrder(initialOrder);
      setViewInfo("");
      return;
    }

    const cards = getCards();

    cards.sort((a,b) => {
      // Dates
      if (mode === "created_desc"){
        const da = parseCreatedAt(a);
        const db = parseCreatedAt(b);
        return (db ? db.getTime() : 0) - (da ? da.getTime() : 0);
      }

      // Priorité
      if (mode === "priority_desc"){
        const pa = priorityWeight(a.dataset.priority);
        const pb = priorityWeight(b.dataset.priority);
        return pb - pa;
      }

      // Score
      if (mode === "score"){
        return parseScore(b) - parseScore(a);
      }

      // Score × Priority
      if (mode === "score_x_priority"){
        const sa = parseScore(a);
        const sb = parseScore(b);
        const pa = priorityWeight(a.dataset.priority);
        const pb = priorityWeight(b.dataset.priority);
        return (sb * pb) - (sa * pa);
      }

      return 0;
    });

    renderInOrder(cards);

    if (mode === "score") setViewInfo("Tri: Score (desc)");
    if (mode === "score_x_priority") setViewInfo("Tri: Score × Priority (desc)");
  }

  function applyFilters(){
    const cards = getCards();

    const prio = priorityFilter ? (priorityFilter.value || "") : "";
    const pub = publisherFilter ? (publisherFilter.value || "") : "";
    const mar = marketFilter ? (marketFilter.value || "") : "";
    const needle = q ? (q.value || "").trim().toLowerCase() : "";

    let shown = 0;

    cards.forEach(c => {
      let ok = true;

      if (prio){
        ok = ok && (String(c.dataset.priority || "").toLowerCase() === prio.toLowerCase());
      }

      if (pub){
        ok = ok && ((c.dataset.publisher || "").trim() === pub);
      }

      if (mar){
        const mm = (c.dataset.markets || "")
          .split(",").map(x => x.trim());
        ok = ok && mm.includes(mar);
      }

      if (needle){
        const text = (c.textContent || "").toLowerCase();
        ok = ok && text.includes(needle);
      }

      c.style.display = ok ? "" : "none";
      if (ok) shown += 1;
    });

    if (countInfo) countInfo.textContent = `${shown} / ${cards.length}`;
  }

  function refresh(){
    applySort();
    applyFilters();
  }

  // --- Top 10 aujourd’hui (Score×Priority) ---
  function showTop10Today(){
    const cards = getCards();
    const now = new Date();

    const today = cards.filter(c => {
      const d = parseCreatedAt(c);
      return d ? sameLocalDay(d, now) : false;
    });

    today.sort((a,b) => {
      const sa = parseScore(a);
      const sb = parseScore(b);
      const pa = priorityWeight(a.dataset.priority);
      const pb = priorityWeight(b.dataset.priority);
      return (sb * pb) - (sa * pa);
    });

    const top10 = today.slice(0, 10);

    // Affiche seulement le top10, cache le reste
    cards.forEach(c => c.style.display = "none");
    top10.forEach(c => c.style.display = "");

    // Réordonne pour afficher top10 au-dessus
    renderInOrder(top10);

    setViewInfo(`Top 10 aujourd’hui (${top10.length}) — Score×Priority`);
    if (countInfo) countInfo.textContent = `${top10.length} / ${cards.length}`;
  }

  // Events
  if (sortBy) sortBy.addEventListener("change", refresh);
  if (sortMode) sortMode.addEventListener("change", refresh);

  if (priorityFilter) priorityFilter.addEventListener("change", refresh);
  if (publisherFilter) publisherFilter.addEventListener("change", refresh);
  if (marketFilter) marketFilter.addEventListener("change", refresh);
  if (q) q.addEventListener("input", () => applyFilters());

  if (resetBtn){
    resetBtn.addEventListener("click", () => {
      if (sortBy) sortBy.value = "created_desc";
      if (priorityFilter) priorityFilter.value = "";
      if (publisherFilter) publisherFilter.value = "";
      if (marketFilter) marketFilter.value = "";
      if (q) q.value = "";
      // Réaffiche tout
      getCards().forEach(c => c.style.display = "");
      refresh();
      setViewInfo("");
    });
  }

  if (top10Btn){
    top10Btn.addEventListener("click", () => {
      // Optionnel : remet le mode tri en "default" pour éviter un double tri
      if (sortMode) sortMode.value = "default";
      showTop10Today();
    });
  }

  if (resetViewBtn){
    resetViewBtn.addEventListener("click", () => {
      if (sortMode) sortMode.value = "default";
      // Réaffiche tout + ordre initial
      getCards().forEach(c => c.style.display = "");
      renderInOrder(initialOrder);
      refresh();
      setViewInfo("");
    });
  }

  // init
  refresh();
})();
</script>
</body>
</html>
""".strip())

    html = tpl.render(
        items=items,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        model=MODEL
    )

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)

def build_daily_summary(items: List[Dict[str, Any]]) -> None:
    # résumé simple des 10 derniers items
    top = items[:10]
    lines = []
    lines.append(f"BOURSENEWS — Résumé du {datetime.now().strftime('%Y-%m-%d')}")
    lines.append("")
    for it in top:
        emo = "🟢" if it["sentiment"] == "positive" else ("🔴" if it["sentiment"] == "negative" else "⚪")
        lines.append(f"{emo} [{it['feed_name']}] {it['title']}")
        lines.append(f"    {it.get('ai_summary','')}")
        lines.append(f"    {it['link']}")
        lines.append("")
    with open(DAILY_SUMMARY_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def main() -> None:
    ensure_out_dir()
    init_db()

    api_key = get_api_key()
    client = OpenAI(api_key=api_key)

    conn = sqlite3.connect(DB_PATH)

    new_count = 0
    for feed_name, url in FEEDS.items():
        print(f"==> Fetch: {feed_name}")
        try:
            items = fetch_feed(feed_name, url)
        except Exception as e:
            print(f"   ! Erreur feed: {e}")
            continue

        for it in items:
            if seen(conn, it["id"]):
                continue

            try:
                analysis = analyze_with_openai(client, it)
            except Exception as e:
                print(f"   ! Erreur OpenAI: {e}")
                analysis = {}

            row = {
                **it,

                "ai_summary": analysis.get("ai_summary", ""),
                "sentiment": analysis.get("sentiment", "neutral"),
                "score": analysis.get("score", 0),

                "priority": analysis.get("priority", "low"),
                "publication_freshness": analysis.get("publication_freshness", "ancien"),
                "market_bias": analysis.get("market_bias", "neutral"),
                "time_horizon": analysis.get("time_horizon", ""),
                "confidence_level": analysis.get("confidence_level", ""),
                "key_links": analysis.get("key_links", []),
                "investor_takeaway": analysis.get("investor_takeaway", ""),

                "created_at": datetime.now(timezone.utc).isoformat(),
                "publisher": analysis.get("publisher", it["feed_name"]),
                "markets_impacted": analysis.get("markets_impacted", []),
            }

            save_item(conn, row)
            new_count += 1

        time.sleep(REQUEST_DELAY_SEC)

    conn.close()

    all_items = load_all_items()

    # sauve JSON
    with open(ITEMS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    build_dashboard(all_items)
    build_daily_summary(all_items)

    print("")
    print(f"✅ Terminé. Nouveaux items: {new_count}")
    print(f"📄 Dashboard: {os.path.abspath(DASHBOARD_PATH)}")
    print(f"🧾 Daily summary: {os.path.abspath(DAILY_SUMMARY_PATH)}")
    print(f"🗃️ JSON: {os.path.abspath(ITEMS_JSON_PATH)}")
    print(f"🗄️ DB: {os.path.abspath(DB_PATH)}")

if __name__ == "__main__":
    main()
