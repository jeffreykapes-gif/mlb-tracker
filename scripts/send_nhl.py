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

doc = db.collection('nhl_tracker').document('roster').get()
if not doc.exists:
    print("No NHL roster found in Firebase.")
    exit(0)

players = doc.to_dict().get('players', [])
print(f"Loaded {len(players)} NHL players from Firebase")

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
# ── Build roster index using ESPN teams list (no hardcoded IDs) ───────────────
print("Building NHL roster index...")
roster_index = {}

# Fetch all NHL teams dynamically
teams_data = fetch("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams?limit=40")
team_list = []
if teams_data:
    for t in (teams_data.get('sports', [{}])[0].get('leagues', [{}])[0].get('teams', [])):
        team = t.get('team', {})
        tid = team.get('id')
        if tid:
            team_list.append(tid)

print(f"Found {len(team_list)} NHL teams")

for tid in team_list:
    d = fetch(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams/{tid}/roster")
    if not d:
        continue
    team_abbr = (d.get('team') or {}).get('abbreviation', '')
    for group in (d.get('athletes') or []):
        for p in (group.get('items') or []):
            if p.get('fullName') and p.get('id'):
                roster_index[p['fullName'].lower()] = {
                    'id': str(p['id']),
                    'name': p['fullName'],
                    'team': team_abbr,
                    'jersey': str(p.get('jersey', ''))
                }
    time.sleep(0.1)
print(f"Roster index built: {len(roster_index)} players")

def get_player_meta(entry):
    key = entry.get('name', '').lower()
    if key in roster_index:
        m = roster_index[key]
        return m['id'], m['team'], m.get('jersey', '')
    matches = [v for k, v in roster_index.items() if entry.get('name', '').lower() in k]
    if matches:
        return matches[0]['id'], matches[0]['team'], matches[0].get('jersey', '')
    return entry.get('id'), entry.get('team', ''), entry.get('jersey', '')

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

def parse_gamelog(data, team_fallback=''):
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

    g_drought = shot_drought = 0
    for g in reversed(games):
        if g['goals'] > 0:
            break
        g_drought += 1
        shot_drought += g['shots']

    last10  = [g for g in games[-10:] if g['toi'] > 0]
    avg_toi = round(sum(g['toi'] for g in last10) / len(last10)) if last10 else None
    team    = (data.get('seasonTypes') or [{}])[0].get('displayTeam', '') or team_fallback

    return {
        'G': len(games), 'Goals': total_goals, 'Shots': total_shots,
        'G Drought': g_drought, 'Shots Since Goal': shot_drought,
        'Avg TOI (L10)': fmt_toi(avg_toi), 'Team': team,
    }

# ── Yesterday's scores & goal scorers ────────────────────────────────────────
yesterday = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
yesterday_display = (date.today() - timedelta(days=1)).strftime('%B %d, %Y')

print(f"Fetching yesterday's NHL scores ({yesterday})...")
scores_data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={yesterday}")

game_summaries = []
all_goals = []

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

        # Get goal scorers from scoring summary
        event_id = event.get('id')
        if event_id and 'final' in status.lower():
            box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event={event_id}")
            if box:
                for play in (box.get('scoringPlays') or []):
                    text = play.get('text', '')
                    team_abbr = play.get('team', {}).get('abbreviation', '')
                    period = play.get('period', {}).get('displayValue', '')
                    if text:
                        all_goals.append(f"{team_abbr} — {text} ({period})")
            time.sleep(0.15)

print(f"Found {len(game_summaries)} games, {len(all_goals)} goals")

# ── Fetch tracked player stats ────────────────────────────────────────────────
today = date.today().isoformat()
rows = []

for p in players:
    name = p.get('name', 'Unknown')
    pid, team, jersey = get_player_meta(p)
    if not pid:
        print(f"  Skipping {name} (no ID)")
        continue
    print(f"  Fetching {name} (ID:{pid}, Team:{team})...")
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/hockey/nhl/athletes/{pid}/gamelog?season={SEASON}"
    data = fetch(url)
    if not data:
        continue
    stats = parse_gamelog(data, team_fallback=team)
    if not stats:
        continue
    if not stats.get('Team'):
        stats['Team'] = team
    rows.append({'Player': name, 'Jersey': f"#{jersey}" if jersey else '', **stats, 'As Of': today})
    time.sleep(0.15)

print(f"Got stats for {len(rows)} / {len(players)} players")

# ── AI Summary ────────────────────────────────────────────────────────────────
AI_TOKEN = os.environ.get('AI_TOKEN', '')
ai_summary = ''

if AI_TOKEN:
    scores_text = '\n'.join(game_summaries) if game_summaries else 'No completed games.'
    goals_text  = '\n'.join(all_goals[:30]) if all_goals else 'No goal data available.'

    prompt = f"""You are an NHL analyst. Write a short, engaging 3-4 sentence summary of yesterday's NHL action ({yesterday_display}). Mention notable scores, standout goal scorers, and interesting storylines. Keep it conversational and exciting.

Yesterday's Final Scores:
{scores_text}

Goals Scored:
{goals_text}"""

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
fieldnames = ['Player', 'Jersey', 'Team', 'G', 'Goals', 'Shots', 'G Drought', 'Shots Since Goal', 'Avg TOI (L10)', 'As Of']
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=fieldnames)
writer.writeheader()
rows.sort(key=lambda r: r.get('G Drought', 0), reverse=True)
writer.writerows(rows)
csv_bytes = buf.getvalue().encode('utf-8')

# ── Build email body ──────────────────────────────────────────────────────────
lines = [
    f"🏒 NHL Stats — {today}",
    f"{len(rows)} players tracked. CSV attached, sorted by G Drought (longest first).",
    ""
]

if ai_summary:
    lines += ["📰 Yesterday's Recap", "─" * 40, ai_summary, ""]

if game_summaries:
    lines += [f"📊 Final Scores — {yesterday_display}", "─" * 40] + game_summaries + [""]

if all_goals:
    lines += [f"🥅 Goals Scored — {yesterday_display}", "─" * 40] + all_goals + [""]

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
msg['Subject'] = f"🏒 NHL Stats — {today}"
msg.attach(MIMEText(email_body, 'plain'))

att = MIMEBase('application', 'octet-stream')
att.set_payload(csv_bytes)
encoders.encode_base64(att)
att.add_header('Content-Disposition', f'attachment; filename="NHL_Stats_{today}.csv"')
msg.attach(att)

with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())

print(f"✓ NHL email sent to {TO_EMAIL}")
