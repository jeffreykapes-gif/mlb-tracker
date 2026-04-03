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

# ── Build roster index ────────────────────────────────────────────────────────
print("Building MLB roster index...")
roster_index = {}
for tid in range(1, 31):
    d = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{tid}/roster?season={SEASON}")
    if not d:
        continue
    team_abbr = (d.get('team') or {}).get('abbreviation', '')
    for group in (d.get('athletes') or []):
        for p in (group.get('items') or []):
            if p.get('fullName') and p.get('id'):
                roster_index[p['fullName'].lower()] = {
                    'id': str(p['id']), 'name': p['fullName'], 'team': team_abbr
                }
    time.sleep(0.1)
print(f"Roster index: {len(roster_index)} players")

def get_player_meta(entry):
    key = entry.get('name', '').lower()
    if key in roster_index:
        m = roster_index[key]
        return m['id'], m['team']
    # Try partial match both ways
    for k, v in roster_index.items():
        if key in k or k in key:
            print(f"  Partial match: '{entry.get('name')}' -> '{v['name']}'")
            return v['id'], v['team']
    # Fall back to Firebase ID — player may be on DL or listed differently
    fb_id = entry.get('id')
    if fb_id:
        print(f"  Falling back to Firebase ID {fb_id} for '{entry.get('name')}'")
    else:
        print(f"  ERROR: No ID at all for '{entry.get('name')}' — skipping")
    return fb_id, entry.get('team', '')

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

# ── Yesterday's scores with full names and records ───────────────────────────
yesterday     = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
yesterday_iso = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
yesterday_display = (date.today() - timedelta(days=1)).strftime('%B %d, %Y')

print(f"Fetching scores for {yesterday}...")
scores_data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={yesterday}")

game_summaries = []
game_ids = []

# games_data stores each completed game with its score line and homers
games_data = []  # list of {'score_line': str, 'homers': [str]}

if scores_data:
    for event in (scores_data.get('events') or []):
        comps = event.get('competitions', [{}])[0]
        competitors = comps.get('competitors', [])
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0])
        away = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1])
        status = event.get('status', {}).get('type', {}).get('description', '')

        home_name  = home.get('team', {}).get('displayName', home.get('team', {}).get('abbreviation', '?'))
        away_name  = away.get('team', {}).get('displayName', away.get('team', {}).get('abbreviation', '?'))
        home_score = home.get('score', '?')
        away_score = away.get('score', '?')

        home_rec = home.get('records', [{}])[0].get('summary', '') if home.get('records') else ''
        away_rec = away.get('records', [{}])[0].get('summary', '') if away.get('records') else ''
        if home_rec: home_name = f"{home_name} ({home_rec})"
        if away_rec: away_name = f"{away_name} ({away_rec})"

        # ESPN uses various status values - check all
        is_final = any(x in status.lower() for x in ['final', 'completed', 'complete'])
        print(f"  Game: {away.get('team',{}).get('abbreviation','?')} vs {home.get('team',{}).get('abbreviation','?')} | Status: '{status}' | Final: {is_final}")
        if is_final:
            score_line = f"{away_name} {away_score}, {home_name} {home_score}"
            game_ids.append(event.get('id'))
            games_data.append({'score_line': score_line, 'homers': [], 'mlb_away': away.get('team',{}).get('abbreviation',''), 'mlb_home': home.get('team',{}).get('abbreviation','')})

game_summaries = [g['score_line'] for g in games_data]
print(f"Found {len(game_summaries)} completed games out of {len(scores_data.get('events', []))} total events")
if not games_data and scores_data:
    # Show raw status values to diagnose
    for ev in (scores_data.get('events') or [])[:3]:
        raw_status = ev.get('status', {})
        print(f"  Raw status sample: {raw_status}")

# ── Get home runs using MLB Stats API, grouped by game ───────────────────────
print("Fetching home runs via MLB Stats API...")
all_homers = []

schedule = fetch(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={yesterday_iso}&hydrate=decisions")
if schedule:
    for date_entry in (schedule.get('dates') or []):
        for game in (date_entry.get('games') or []):
            game_pk = game.get('gamePk')
            status = game.get('status', {}).get('detailedState', '')
            if not game_pk or 'Final' not in status:
                continue
            away_abbr = game.get('teams', {}).get('away', {}).get('team', {}).get('abbreviation', '')
            home_abbr = game.get('teams', {}).get('home', {}).get('team', {}).get('abbreviation', '')

            pbp = fetch(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/playByPlay")
            if not pbp:
                continue

            game_homers = []
            for play in (pbp.get('allPlays') or []):
                result = play.get('result', {})
                if result.get('eventType') == 'home_run':
                    batter_name = play.get('matchup', {}).get('batter', {}).get('fullName', 'Unknown')
                    team = play.get('offense', {}).get('team', {}).get('abbreviation', '')
                    desc = result.get('description', '')
                    game_homers.append(f"  💥 {batter_name} ({team}) — {desc}")
                    all_homers.append(f"{batter_name} ({team}) — {desc}")

            # Match to games_data entry
            for g in games_data:
                if g['mlb_away'] == away_abbr or g['mlb_home'] == home_abbr:
                    g['homers'] = game_homers
                    break
            time.sleep(0.1)

print(f"Found {len(all_homers)} home runs")
for g in games_data:
    print(f"  Game: {g['score_line']} | HRs: {len(g['homers'])}")

# ── Fetch tracked player stats ────────────────────────────────────────────────
today = date.today().isoformat()
rows = []

for p in players:
    name = p.get('name', 'Unknown')
    pid, team = get_player_meta(p)
    if not pid:
        print(f"  Skipping {name} (no ID)")
        continue
    print(f"  Fetching {name}...")
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/baseball/mlb/athletes/{pid}/gamelog?season={SEASON}"
    data = fetch(url)
    if not data:
        continue
    stats = parse_gamelog(data, team_fallback=team)
    if not stats:
        continue
    if not stats.get('Team'):
        stats['Team'] = team
    rows.append({'Player': name, **stats, 'As Of': today})
    time.sleep(0.15)

print(f"Got stats for {len(rows)} players")

# ── AI Summary ────────────────────────────────────────────────────────────────
AI_TOKEN = os.environ.get('AI_TOKEN', '')
ai_summary = ''

if AI_TOKEN and (game_summaries or all_homers):
    scores_text = '\n'.join(game_summaries) if game_summaries else 'No completed games.'
    homers_text = '\n'.join(all_homers[:20]) if all_homers else 'No home run data.'

    prompt = f"""You are a baseball analyst. Write a short, engaging 3-4 sentence summary of yesterday's MLB action ({yesterday_display}). Mention notable scores, standout home run hitters, and interesting storylines. Keep it conversational and exciting.

Yesterday's Final Scores:
{scores_text}

Home Runs Hit:
{homers_text}"""

    try:
        resp = requests.post(
            "https://models.inference.ai.azure.com/chat/completions",
            headers={"Authorization": f"Bearer {AI_TOKEN}", "Content-Type": "application/json"},
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
            print("AI summary generated")
        else:
            print(f"AI error {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        print(f"AI failed: {e}")

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

if games_data:
    lines += [f"📊 Final Scores & Home Runs — {yesterday_display}", "─" * 40]
    for g in games_data:
        lines.append(g['score_line'])
        if g['homers']:
            lines += g['homers']
        else:
            lines.append("  No home runs")
        lines.append("")
else:
    lines += [f"No games played on {yesterday_display}.", ""]

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

print(f"Email body preview (first 500 chars):")
print(email_body[:500])
print(f"✓ MLB email sent to {TO_EMAIL}")
