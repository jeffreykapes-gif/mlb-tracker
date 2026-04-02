import os, csv, io, json, time, requests, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import date, timedelta
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

SEASON = 2026
HEADERS = {'User-Agent': 'Mozilla/5.0'}

def fetch(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code} for {url}")
        except Exception as e:
            print(f"  Retry {i+1}: {e}")
            time.sleep(2)
    return None

# ── Build roster index (name -> id, team) ─────────────────────────────────────
print("Building MLB roster index...")
roster_index = {}
ALL_TEAM_IDS = list(range(1, 31))
for tid in ALL_TEAM_IDS:
    d = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{tid}/roster?season={SEASON}")
    if not d:
        continue
    team_abbr = (d.get('team') or {}).get('abbreviation', '')
    for group in (d.get('athletes') or []):
        for p in (group.get('items') or []):
            if p.get('fullName') and p.get('id'):
                roster_index[p['fullName'].lower()] = {
                    'id': str(p['id']),
                    'name': p['fullName'],
                    'team': team_abbr
                }
    time.sleep(0.1)
print(f"Roster index built: {len(roster_index)} players")

def get_player_meta(entry):
    """Get fresh ID and team from roster index, fall back to Firebase entry."""
    key = entry.get('name', '').lower()
    if key in roster_index:
        return roster_index[key]['id'], roster_index[key]['team']
    # Try partial match
    matches = [v for k, v in roster_index.items() if entry.get('name', '').lower() in k]
    if matches:
        return matches[0]['id'], matches[0]['team']
    return entry.get('id'), entry.get('team', '')

def parse_gamelog(data, team_fallback=''):
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

    g_drought = ab_drought = 0
    for g in reversed(games):
        if g['hr'] > 0:
            break
        g_drought += 1
        ab_drought += g['ab']

    avg = round(total_h / total_ab, 3) if total_ab else 0.0
    team = (data.get('seasonTypes') or [{}])[0].get('displayTeam', '') or team_fallback

    return {
        'G': len(games), 'AB': total_ab, 'H': total_h,
        'AVG': f"{avg:.3f}".lstrip('0') or '.000',
        'HR': total_hr, 'G Drought': g_drought,
        'AB Drought': ab_drought, 'Team': team,
    }

# ── Yesterday's scores ────────────────────────────────────────────────────────
yesterday     = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
yesterday_iso = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
yesterday_display = (date.today() - timedelta(days=1)).strftime('%B %d, %Y')

print(f"Fetching yesterday's MLB scores ({yesterday})...")
scores_data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={yesterday}")

game_summaries = []
if scores_data:
    for event in (scores_data.get('events') or []):
        comps = event.get('competitions', [{}])[0]
        competitors = comps.get('competitors', [])
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0])
        away = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1])
        status = event.get('status', {}).get('type', {}).get('description', '')
        if 'final' in status.lower():
            game_summaries.append(
                f"{away.get('team',{}).get('abbreviation','?')} {away.get('score','?')}, "
                f"{home.get('team',{}).get('abbreviation','?')} {home.get('score','?')}"
            )

print(f"Found {len(game_summaries)} completed games")

# ── Yesterday's home runs via ESPN daily leaders ──────────────────────────────
print("Fetching yesterday's HR leaders...")
all_homers = []
# ESPN daily stats leaders endpoint
leaders_data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/stats/leaders?dates={yesterday_iso}&categories=batting")
if not leaders_data:
    leaders_data = fetch(f"https://site.web.api.espn.com/apis/common/v3/sports/baseball/mlb/statistics/byathlete?region=us&lang=en&contentorigin=espn&isrequired=true&type=total&dates={yesterday_iso}&groups=50")

# Alternative: pull from each game's box score plays
if not all_homers and scores_data:
    for event in (scores_data.get('events') or []):
        event_id = event.get('id')
        if not event_id:
            continue
        status = event.get('status', {}).get('type', {}).get('description', '')
        if 'final' not in status.lower():
            continue
        box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event={event_id}")
        if not box:
            continue
        # Check scoring plays for home runs
        for play in (box.get('scoringPlays') or []):
            text = play.get('text', '').lower()
            if 'home run' in text or 'homer' in text or 'homers' in text:
                team_abbr = play.get('team', {}).get('abbreviation', '')
                all_homers.append(f"{play.get('text', '')} ({team_abbr})")
        time.sleep(0.15)

print(f"Found {len(all_homers)} HR events")

# ── Fetch tracked player stats ────────────────────────────────────────────────
today = date.today().isoformat()
rows = []

for p in players:
    name = p.get('name', 'Unknown')
    pid, team = get_player_meta(p)
    if not pid:
        print(f"  Skipping {name} (no ID)")
        continue
    print(f"  Fetching {name} (ID:{pid}, Team:{team})...")
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/baseball/mlb/athletes/{pid}/gamelog?season={SEASON}"
    data = fetch(url)
    if not data:
        print(f"    No data returned")
        continue
    stats = parse_gamelog(data, team_fallback=team)
    if not stats:
        print(f"    Parse failed")
        continue
    if not stats.get('Team'):
        stats['Team'] = team
    rows.append({'Player': name, **stats, 'As Of': today})
    time.sleep(0.15)

print(f"Got stats for {len(rows)} / {len(players)} players")

# ── AI Summary ────────────────────────────────────────────────────────────────
AI_TOKEN = os.environ.get('AI_TOKEN', '')
ai_summary = ''

if AI_TOKEN:
    scores_text = '\n'.join(game_summaries) if game_summaries else 'No completed games.'
    homers_text = '\n'.join(all_homers) if all_homers else 'No home run data available.'

    prompt = f"""You are a baseball analyst. Write a short, engaging 3-4 sentence summary of yesterday's MLB action ({yesterday_display}). Mention notable scores, standout performances, and interesting storylines. Keep it conversational and exciting.

Yesterday's Final Scores:
{scores_text}

Home Run Plays:
{homers_text}"""

    try:
        resp = requests.post(
            "https://models.inference.ai.azure.com/chat/completions",
            headers={
                "Authorization": f"Bearer {AI_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "temperature": 0.7
            },
            timeout=30
        )
        if resp.status_code == 200:
            ai_summary = resp.json()['choices'][0]['message']['content'].strip()
            print("AI summary generated successfully")
        else:
            print(f"AI error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"AI request failed: {e}")

# ── Build CSV ─────────────────────────────────────────────────────────────────
fieldnames = ['Player', 'Team', 'G', 'AB', 'H', 'AVG', 'HR', 'G Drought', 'AB Drought', 'As Of']
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=fieldnames)
writer.writeheader()
rows.sort(key=lambda r: r.get('G Drought', 0), reverse=True)
writer.writerows(rows)
csv_bytes = buf.getvalue().encode('utf-8')

# ── Build email body ──────────────────────────────────────────────────────────
lines = [
    f"⚾ MLB Stats — {today}",
    f"{len(rows)} players tracked. CSV attached, sorted by HR Drought (longest first).",
    ""
]

if ai_summary:
    lines += ["📰 Yesterday's Recap", "─" * 40, ai_summary, ""]

if game_summaries:
    lines += [f"📊 Final Scores — {yesterday_display}", "─" * 40] + game_summaries + [""]

if all_homers:
    lines += [f"💥 Home Runs — {yesterday_display}", "─" * 40] + all_homers + [""]

if not game_summaries:
    lines += [f"No games played on {yesterday_display}."]

email_body = '\n'.join(lines)

# ── Send email ────────────────────────────────────────────────────────────────
GMAIL_USER = os.environ['GMAIL_USER']
GMAIL_PASS = os.environ['GMAIL_PASS']
TO_EMAIL   = os.environ['TO_EMAIL']

msg = MIMEMultipart()
msg['From']    = GMAIL_USER
msg['To']      = TO_EMAIL
msg['Subject'] = f"⚾ MLB Stats — {today}"
msg.attach(MIMEText(email_body, 'plain'))

att = MIMEBase('application', 'octet-stream')
att.set_payload(csv_bytes)
encoders.encode_base64(att)
att.add_header('Content-Disposition', f'attachment; filename="MLB_Stats_{today}.csv"')
msg.attach(att)

with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())

print(f"✓ MLB email sent to {TO_EMAIL}")
