import os, csv, io, json, time, requests, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import date
import firebase_admin
from firebase_admin import credentials, firestore

# ── Firebase ──────────────────────────────────────────────────────────────────
firebase_key = json.loads(os.environ['FIREBASE_KEY'])
cred = credentials.Certificate(firebase_key)
firebase_admin.initialize_app(cred)
db = firestore.client()

doc = db.collection('nhl_tracker').document('roster').get()
if not doc.exists:
    print("No NHL roster found in Firebase.")
    exit(0)

players = doc.to_dict().get('players', [])
print(f"Loaded {len(players)} NHL players from Firebase")

# ── ESPN API ──────────────────────────────────────────────────────────────────
SEASON = 2026
HEADERS = {'User-Agent': 'Mozilla/5.0'}

def fetch(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"  Retry {i+1}: {e}")
            time.sleep(2)
    return None

def parse_toi(raw):
    if not raw:
        return 0
    s = str(raw)
    parts = s.split(':')
    if len(parts) == 2:
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except:
            pass
    try:
        return int(float(s))
    except:
        return 0

def fmt_toi(secs):
    if not secs:
        return '—'
    return f"{secs // 60}:{str(secs % 60).zfill(2)}"

def parse_gamelog(data):
    names = [str(n) for n in (data.get('names') or [])]

    g_idx   = next((i for i, n in enumerate(names) if n == 'goals'), -1)
    s_idx   = next((i for i, n in enumerate(names) if n == 'shotsTotal'), -1)
    toi_idx = next((i for i, n in enumerate(names) if n == 'timeOnIcePerGame'), -1)

    if g_idx < 0:
        return None

    events_meta = data.get('events', {})
    games = []
    for st in (data.get('seasonTypes') or []):
        for cat in (st.get('categories') or []):
            for ev in (cat.get('events') or []):
                stats = ev.get('stats', [])
                if not stats:
                    continue
                meta = events_meta.get(ev.get('eventId'), {})
                games.append({
                    'date':  meta.get('gameDate', ''),
                    'goals': int(stats[g_idx] or 0),
                    'shots': int(stats[s_idx] or 0) if s_idx >= 0 else 0,
                    'toi':   parse_toi(stats[toi_idx]) if toi_idx >= 0 else 0,
                })

    games.sort(key=lambda g: g['date'])
    total_goals = sum(g['goals'] for g in games)
    total_shots = sum(g['shots'] for g in games)

    g_drought = 0
    shot_drought = 0
    for g in reversed(games):
        if g['goals'] > 0:
            break
        g_drought += 1
        shot_drought += g['shots']

    last10 = [g for g in games[-10:] if g['toi'] > 0]
    avg_toi = round(sum(g['toi'] for g in last10) / len(last10)) if last10 else None

    team = (data.get('seasonTypes') or [{}])[0].get('displayTeam', '')
    return {
        'G': len(games),
        'Goals': total_goals,
        'Shots': total_shots,
        'G Drought': g_drought,
        'Shots Since Goal': shot_drought,
        'Avg TOI (L10)': fmt_toi(avg_toi),
        'Team': team,
    }

# ── Fetch stats ───────────────────────────────────────────────────────────────
today = date.today().isoformat()
rows = []

for p in players:
    pid = p.get('id')
    name = p.get('name', 'Unknown')
    jersey = p.get('jersey', '')
    if not pid:
        continue
    print(f"  Fetching {name}...")
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/hockey/nhl/athletes/{pid}/gamelog?season={SEASON}"
    data = fetch(url)
    if not data:
        print(f"    Skipped (no data)")
        continue
    stats = parse_gamelog(data)
    if not stats:
        print(f"    Skipped (parse failed)")
        continue
    rows.append({'Player': name, 'Jersey': f"#{jersey}" if jersey else '', **stats, 'As Of': today})
    time.sleep(0.15)

print(f"Got stats for {len(rows)} players")

# ── Build CSV ─────────────────────────────────────────────────────────────────
fieldnames = ['Player', 'Jersey', 'Team', 'G', 'Goals', 'Shots', 'G Drought', 'Shots Since Goal', 'Avg TOI (L10)', 'As Of']
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=fieldnames)
writer.writeheader()
rows.sort(key=lambda r: r['G Drought'], reverse=True)
writer.writerows(rows)
csv_bytes = buf.getvalue().encode('utf-8')

# ── Send email ────────────────────────────────────────────────────────────────
GMAIL_USER = os.environ['GMAIL_USER']
GMAIL_PASS = os.environ['GMAIL_PASS']
TO_EMAIL   = os.environ['TO_EMAIL']

msg = MIMEMultipart()
msg['From']    = GMAIL_USER
msg['To']      = TO_EMAIL
msg['Subject'] = f"🏒 NHL Stats — {today}"

body = f"NHL stats as of {today}. {len(rows)} players tracked. Sorted by G Drought (longest first)."
msg.attach(MIMEText(body, 'plain'))

attachment = MIMEBase('application', 'octet-stream')
attachment.set_payload(csv_bytes)
encoders.encode_base64(attachment)
attachment.add_header('Content-Disposition', f'attachment; filename="NHL_Stats_{today}.csv"')
msg.attach(attachment)

with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())

print(f"NHL email sent to {TO_EMAIL}")
