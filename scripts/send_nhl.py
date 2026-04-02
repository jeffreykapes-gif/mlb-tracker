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

    season_types = data.get('seasonTypes') or []
    team = ''
    if season_types:
        team = season_types[0].get('displayTeam', '')
    if not team:
        team = (data.get('athlete') or {}).get('team', {}).get('abbreviation', '')

    return {
        'G': len(games),
        'Goals': total_goals,
        'Shots': total_shots,
        'G Drought': g_drought,
        'Shots Since Goal': shot_drought,
        'Avg TOI (L10)': fmt_toi(avg_toi),
        'Team': team,
    }

# ── Yesterday's scores & goals ────────────────────────────────────────────────
yesterday = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
yesterday_display = (date.today() - timedelta(days=1)).strftime('%B %d, %Y')

print(f"Fetching yesterday's NHL scores for {yesterday}...")
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

        home_team  = home.get('team', {}).get('abbreviation', '?')
        away_team  = away.get('team', {}).get('abbreviation', '?')
        home_score = home.get('score', '?')
        away_score = away.get('score', '?')
        status     = event.get('status', {}).get('type', {}).get('description', '')

        if 'Final' in status or 'final' in status.lower():
            game_summaries.append(f"{away_team} {away_score}, {home_team} {home_score}")

        # Get goal scorers from box score
        event_id = event.get('id')
        if event_id:
            box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event={event_id}")
            if box:
                for leader_cat in (box.get('leaders') or []):
                    abbr = leader_cat.get('abbreviation', '')
                    if abbr in ('G', 'Goals', 'goals'):
                        for leader in (leader_cat.get('leaders') or []):
                            athlete = leader.get('athlete', {})
                            player_name = athlete.get('displayName', '')
                            team_abbr = athlete.get('team', {}).get('abbreviation', '')
                            g_count = leader.get('value', 0)
                            if player_name:
                                all_goals.append(f"{player_name} ({team_abbr}): {int(g_count)} G")
        time.sleep(0.2)

# ── Fetch tracked player stats ────────────────────────────────────────────────
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
        continue
    stats = parse_gamelog(data)
    if not stats:
        continue
    if not stats.get('Team'):
        stats['Team'] = p.get('team', '')
    rows.append({'Player': name, 'Jersey': f"#{jersey}" if jersey else '', **stats, 'As Of': today})
    time.sleep(0.15)

print(f"Got stats for {len(rows)} players")

# ── AI Summary ────────────────────────────────────────────────────────────────
AI_TOKEN = os.environ.get('AI_TOKEN', '')
ai_summary = ''

if AI_TOKEN and (game_summaries or all_goals):
    scores_text = '\n'.join(game_summaries) if game_summaries else 'No completed games found.'
    goals_text  = '\n'.join(all_goals) if all_goals else 'No goal data found.'

    prompt = f"""You are an NHL analyst. Based on yesterday's NHL results ({yesterday_display}), write a short, engaging 3-4 sentence summary of the day. Mention notable scores, standout goal scorers, and any interesting storylines. Keep it conversational and exciting.

Yesterday's Final Scores:
{scores_text}

Goals Scored:
{goals_text}"""

    try:
        resp = requests.post(
            "https://models.inference.ai.azure.com/chat/completions",
            headers={"Authorization": f"Bearer {AI_TOKEN}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300
            },
            timeout=30
        )
        if resp.status_code == 200:
            ai_summary = resp.json()['choices'][0]['message']['content']
            print("AI summary generated")
        else:
            print(f"AI error: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"AI request failed: {e}")

# ── Build CSV ─────────────────────────────────────────────────────────────────
fieldnames = ['Player', 'Jersey', 'Team', 'G', 'Goals', 'Shots', 'G Drought', 'Shots Since Goal', 'Avg TOI (L10)', 'As Of']
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=fieldnames)
writer.writeheader()
rows.sort(key=lambda r: r['G Drought'], reverse=True)
writer.writerows(rows)
csv_bytes = buf.getvalue().encode('utf-8')

# ── Build email body ──────────────────────────────────────────────────────────
body_parts = [f"🏒 NHL Stats — {today}", f"{len(rows)} players tracked. CSV attached, sorted by G Drought (longest first).", ""]

if ai_summary:
    body_parts += ["📰 Yesterday's Recap", "─" * 40, ai_summary, ""]

if game_summaries:
    body_parts += [f"📊 Final Scores — {yesterday_display}", "─" * 40]
    body_parts += game_summaries
    body_parts.append("")

if all_goals:
    body_parts += [f"🥅 Goals Scored — {yesterday_display}", "─" * 40]
    body_parts += all_goals
    body_parts.append("")

email_body = '\n'.join(body_parts)

# ── Send email ────────────────────────────────────────────────────────────────
GMAIL_USER = os.environ['GMAIL_USER']
GMAIL_PASS = os.environ['GMAIL_PASS']
TO_EMAIL   = os.environ['TO_EMAIL']

msg = MIMEMultipart()
msg['From']    = GMAIL_USER
msg['To']      = TO_EMAIL
msg['Subject'] = f"🏒 NHL Stats — {today}"

msg.attach(MIMEText(email_body, 'plain'))

attachment = MIMEBase('application', 'octet-stream')
attachment.set_payload(csv_bytes)
encoders.encode_base64(attachment)
attachment.add_header('Content-Disposition', f'attachment; filename="NHL_Stats_{today}.csv"')
msg.attach(attachment)

with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())

print(f"NHL email sent to {TO_EMAIL}")
