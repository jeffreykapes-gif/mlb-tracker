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

doc = db.collection('tracker').document('roster').get()
if not doc.exists:
    print("No MLB roster found in Firebase.")
    exit(0)

players = doc.to_dict().get('players', [])
print(f"Loaded {len(players)} MLB players from Firebase")

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

def parse_gamelog(data):
    names = [str(n) for n in (data.get('names') or [])]
    ab_idx = next((i for i, n in enumerate(names) if n == 'atBats'), -1)
    h_idx  = next((i for i, n in enumerate(names) if n == 'hits'), -1)
    hr_idx = next((i for i, n in enumerate(names) if n == 'homeRuns'), -1)
    if ab_idx < 0 or h_idx < 0 or hr_idx < 0:
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
                    'date': meta.get('gameDate', ''),
                    'ab': int(stats[ab_idx] or 0),
                    'h':  int(stats[h_idx]  or 0),
                    'hr': int(stats[hr_idx] or 0),
                })

    games.sort(key=lambda g: g['date'])
    total_ab = sum(g['ab'] for g in games)
    total_h  = sum(g['h']  for g in games)
    total_hr = sum(g['hr'] for g in games)

    g_drought = 0
    ab_drought = 0
    for g in reversed(games):
        if g['hr'] > 0:
            break
        g_drought += 1
        ab_drought += g['ab']

    avg = round(total_h / total_ab, 3) if total_ab else 0.0
    return {
        'G': len(games),
        'AB': total_ab,
        'H': total_h,
        'AVG': f"{avg:.3f}".lstrip('0') or '.000',
        'HR': total_hr,
        'G Drought': g_drought,
        'AB Drought': ab_drought,
    }

# ── Fetch stats ───────────────────────────────────────────────────────────────
today = date.today().isoformat()
rows = []

for p in players:
    pid = p.get('id')
    name = p.get('name', 'Unknown')
    if not pid:
        continue
    print(f"  Fetching {name}...")
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/baseball/mlb/athletes/{pid}/gamelog?season={SEASON}"
    data = fetch(url)
    if not data:
        print(f"    Skipped (no data)")
        continue
    stats = parse_gamelog(data)
    if not stats:
        print(f"    Skipped (parse failed)")
        continue
    rows.append({'Player': name, **stats, 'As Of': today})
    time.sleep(0.15)

print(f"Got stats for {len(rows)} players")

# ── Build CSV ─────────────────────────────────────────────────────────────────
fieldnames = ['Player', 'G', 'AB', 'H', 'AVG', 'HR', 'G Drought', 'AB Drought', 'As Of']
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=fieldnames)
writer.writeheader()
# Sort by G Drought descending
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
msg['Subject'] = f"⚾ MLB Stats — {today}"

body = f"MLB stats as of {today}. {len(rows)} players tracked. Sorted by G Drought (longest first)."
msg.attach(MIMEText(body, 'plain'))

attachment = MIMEBase('application', 'octet-stream')
attachment.set_payload(csv_bytes)
encoders.encode_base64(attachment)
attachment.add_header('Content-Disposition', f'attachment; filename="MLB_Stats_{today}.csv"')
msg.attach(attachment)

with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())

print(f"MLB email sent to {TO_EMAIL}")
