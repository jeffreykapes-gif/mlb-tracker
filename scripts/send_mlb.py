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

    season_types = data.get('seasonTypes') or []
    team = ''
    if season_types:
        team = season_types[0].get('displayTeam', '')
    if not team:
        team = (data.get('athlete') or {}).get('team', {}).get('abbreviation', '')

    return {
        'G': len(games),
        'AB': total_ab,
        'H': total_h,
        'AVG': f"{avg:.3f}".lstrip('0') or '.000',
        'HR': total_hr,
        'G Drought': g_drought,
        'AB Drought': ab_drought,
        'Team': team,
    }

# ── Yesterday's scores & homers ───────────────────────────────────────────────
yesterday = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
yesterday_display = (date.today() - timedelta(days=1)).strftime('%B %d, %Y')

print(f"Fetching yesterday's scores for {yesterday}...")
scores_data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={yesterday}")

game_summaries = []
all_homers = []

if scores_data:
    for event in (scores_data.get('events') or []):
        comps = event.get('competitions', [{}])[0]
        competitors = comps.get('competitors', [])
        if len(competitors) < 2:
            continue

        # Get teams and scores
        home = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0])
        away = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1])

        home_team  = home.get('team', {}).get('abbreviation', '?')
        away_team  = away.get('team', {}).get('abbreviation', '?')
        home_score = home.get('score', '?')
        away_score = away.get('score', '?')
        status     = event.get('status', {}).get('type', {}).get('description', '')

        if 'Final' in status or 'final' in status.lower():
            game_summaries.append(f"{away_team} {away_score}, {home_team} {home_score}")

        # Get home runs from linescores / leaders
        event_id = event.get('id')
        if event_id:
            box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event={event_id}")
            if box:
                for leader_cat in (box.get('leaders') or []):
                    if 'home' in leader_cat.get('name', '').lower() or 'HR' in leader_cat.get('abbreviation', ''):
                        for leader in (leader_cat.get('leaders') or []):
                            athlete = leader.get('athlete', {})
                            player_name = athlete.get('displayName', '')
                            team_abbr = athlete.get('team', {}).get('abbreviation', '')
                            hr_count = leader.get('value', 0)
                            if player_name:
                                all_homers.append(f"{player_name} ({team_abbr}): {int(hr_count)} HR")
        time.sleep(0.2)

# ── Fetch tracked player stats ────────────────────────────────────────────────
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
        continue
    stats = parse_gamelog(data)
    if not stats:
        continue
    if not stats.get('Team'):
        stats['Team'] = p.get('team', '')
    rows.append({'Player': name, **stats, 'As Of': today})
    time.sleep(0.15)

print(f"Got stats for {len(rows)} players")

# ── AI Summary ────────────────────────────────────────────────────────────────
AI_TOKEN = os.environ.get('AI_TOKEN', '')
ai_summary = ''

if AI_TOKEN and (game_summaries or all_homers):
    scores_text  = '\n'.join(game_summaries) if game_summaries else 'No completed games found.'
    homers_text  = '\n'.join(all_homers) if all_homers else 'No home run data found.'

    prompt = f"""You are a baseball analyst. Based on yesterday's MLB results ({yesterday_display}), write a short, engaging 3-4 sentence summary of the day. Mention notable scores, standout performances, and any interesting storylines. Keep it conversational and exciting.

Yesterday's Final Scores:
{scores_text}

Home Runs Hit:
{homers_text}"""

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
fieldnames = ['Player', 'Team', 'G', 'AB', 'H', 'AVG', 'HR', 'G Drought', 'AB Drought', 'As Of']
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=fieldnames)
writer.writeheader()
rows.sort(key=lambda r: r['G Drought'], reverse=True)
writer.writerows(rows)
csv_bytes = buf.getvalue().encode('utf-8')

# ── Build email body ──────────────────────────────────────────────────────────
body_parts = [f"⚾ MLB Stats — {today}", f"{len(rows)} players tracked. CSV attached, sorted by G Drought (longest first).", ""]

if ai_summary:
    body_parts += ["📰 Yesterday's Recap", "─" * 40, ai_summary, ""]

if game_summaries:
    body_parts += [f"📊 Final Scores — {yesterday_display}", "─" * 40]
    body_parts += game_summaries
    body_parts.append("")

if all_homers:
    body_parts += [f"💥 Home Runs Hit — {yesterday_display}", "─" * 40]
    body_parts += all_homers
    body_parts.append("")

email_body = '\n'.join(body_parts)

# ── Send email ────────────────────────────────────────────────────────────────
GMAIL_USER = os.environ['GMAIL_USER']
GMAIL_PASS = os.environ['GMAIL_PASS']
TO_EMAIL   = os.environ['TO_EMAIL']

msg = MIMEMultipart()
msg['From']    = GMAIL_USER
msg['To']      = TO_EMAIL
msg['Subject'] = f"⚾ MLB Stats — {today}"

msg.attach(MIMEText(email_body, 'plain'))

attachment = MIMEBase('application', 'octet-stream')
attachment.set_payload(csv_bytes)
encoders.encode_base64(attachment)
attachment.add_header('Content-Disposition', f'attachment; filename="MLB_Stats_{today}.csv"')
msg.attach(attachment)

with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())

print(f"MLB email sent to {TO_EMAIL}")
