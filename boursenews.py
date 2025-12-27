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
        (id, feed_name, title, link, published, summary, ai_summary, sentiment, score, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["id"], row["feed_name"], row["title"], row["link"],
        row.get("published"), row.get("summary"), row.get("ai_summary"),
        row.get("sentiment"), row.get("score"), row.get("created_at")
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
Tu es un expert analyste boursier. Donne un résultat concis en FRANÇAIS.

CONTEXTE:
Source: {item["feed_name"]}
Titre: {item["title"]}
Extrait: {item["summary"]}
Lien: {item["link"]}

TÂCHE:
1) Résume en 10-15 phrases (FR).
2) Classe l'impact potentiel "marché" pour l'actif concerné:
   - sentiment: positive / negative / neutral
   - score: -2, -1, 0, +1, +2
3) Donne 2 points clés sous forme de puces.
Réponds STRICTEMENT en JSON avec ces clés:
{{
  "ai_summary": "...",
  "sentiment": "positive|negative|neutral",
  "score": -2|-1|0|1|2,
  "bullets": ["...", "..."]
}}
""".strip()

    resp = client.responses.create(
        model=MODEL,
        input=prompt
    )

    text = resp.output_text.strip()

    # extraction JSON robuste
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {"ai_summary": text, "sentiment": "neutral", "score": 0, "bullets": []}

    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"ai_summary": text, "sentiment": "neutral", "score": 0, "bullets": []}

    # normalisation
    sentiment = str(data.get("sentiment", "neutral")).lower().strip()
    if sentiment not in ("positive", "negative", "neutral"):
        sentiment = "neutral"
    score = data.get("score", 0)
    try:
        score = int(score)
    except Exception:
        score = 0
    if score not in (-2, -1, 0, 1, 2):
        score = 0

    bullets = data.get("bullets", [])
    if not isinstance(bullets, list):
        bullets = []

    return {
        "ai_summary": norm_text(data.get("ai_summary", "")) or text,
        "sentiment": sentiment,
        "score": score,
        "bullets": bullets[:2],
    }

def load_all_items() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, feed_name, title, link, published, summary, ai_summary, sentiment, score, created_at
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

  {% for it in items %}
    {% set cls = "neu" %}
    {% if it.sentiment == "positive" %}{% set cls = "pos" %}{% endif %}
    {% if it.sentiment == "negative" %}{% set cls = "neg" %}{% endif %}

    <div class="card {{ cls }}">
      <div class="row">
        <span class="tag">{{ it.sentiment }} ({{ it.score }})</span>
        <span class="tag">{{ it.feed_name }}</span>
        <span class="small">{{ it.created_at }}</span>
      </div>
      <h3 style="margin:10px 0 6px 0;">
        <a href="{{ it.link }}" target="_blank" rel="noopener">{{ it.title }}</a>
      </h3>
      <div class="small"><b>Résumé IA:</b> {{ it.ai_summary }}</div>
      {% if it.summary %}
        <div class="small" style="margin-top:6px; opacity:0.75;"><b>Extrait:</b> {{ it.summary }}</div>
      {% endif %}
    </div>
  {% endfor %}
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
                analysis = {"ai_summary": "", "sentiment": "neutral", "score": 0, "bullets": []}

            row = {
                **it,
                "ai_summary": analysis.get("ai_summary", ""),
                "sentiment": analysis.get("sentiment", "neutral"),
                "score": analysis.get("score", 0),
                "created_at": datetime.now(timezone.utc).isoformat(),
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
