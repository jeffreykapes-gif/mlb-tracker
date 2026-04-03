import os, csv, io, json, time, requests, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import date, timedelta
import firebase_admin
from firebase_admin import credentials, firestore

print("=== MLB EMAIL SCRIPT STARTING ===")

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
print(f"Loaded {len(players)} players from Firebase")

SEASON = 2026
HEADERS = {'User-Agent': 'Mozilla/5.0'}

def fetch(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code}: {url}")
        except Exception as e:
            print(f"  Error (attempt {i+1}): {e}")
            time.sleep(3)
    return None

# ── Build roster index ────────────────────────────────────────────────────────
print("Building roster index...")
roster_index = {}
for tid in range(1, 31):
    d = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{tid}/roster?season={SEASON}")
    if not d:
        continue
    team_abbr = (d.get('team') or {}).get('abbreviation', '')
    for group in (d.get('athletes') or []):
        for p in (group.get('items') or []):
            if p.get('fullName') and p.get('id'):
                roster_index[p['fullName'].lower()] = {'id': str(p['id']), 'name': p['fullName'], 'team': team_abbr}
    time.sleep(0.1)
print(f"Roster index: {len(roster_index)} players")

def get_player_meta(entry):
    key = entry.get('name', '').lower()
    if key in roster_index:
        m = roster_index[key]
        return m['id'], m['team']
    for k, v in roster_index.items():
        if key in k or k in key:
            return v['id'], v['team']
    fb_id = entry.get('id')
    if fb_id:
        print(f"  No roster match for '{entry.get('name')}' — using Firebase ID {fb_id}")
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
                games.append({'date': meta.get('gameDate', ''), 'ab': int(stats[ab_idx] or 0), 'h': int(stats[h_idx] or 0), 'hr': int(stats[hr_idx] or 0)})
    games.sort(key=lambda g: g['date'])
    total_ab = sum(g['ab'] for g in games)
    total_h  = sum(g['h']  for g in games)
    total_hr = sum(g['hr'] for g in games)
    g_drought = ab_drought = 0
    for g in reversed(games):
        if g['hr'] > 0: break
        g_drought += 1
        ab_drought += g['ab']
    avg = round(total_h / total_ab, 3) if total_ab else 0.0
    team = (data.get('seasonTypes') or [{}])[0].get('displayTeam', '') or team_fallback
    return {'G': len(games), 'AB': total_ab, 'H': total_h, 'AVG': f"{avg:.3f}".lstrip('0') or '.000', 'HR': total_hr, 'G Drought': g_drought, 'AB Drought': ab_drought, 'Team': team}

# ── Yesterday's scores ────────────────────────────────────────────────────────
yesterday     = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
yesterday_iso = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
yesterday_display = (date.today() - timedelta(days=1)).strftime('%B %d, %Y')
today = date.today().isoformat()

print(f"Fetching MLB scores for {yesterday}...")
scores_data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={yesterday}")
print(f"Scoreboard API result: {'OK - ' + str(len(scores_data.get('events',[]))) + ' events' if scores_data else 'FAILED - None returned'}")

games_data = []

if scores_data:
    for event in (scores_data.get('events') or []):
        comps   = event.get('competitions', [{}])[0]
        competitors = comps.get('competitors', [])
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0])
        away = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1])

        status_obj  = event.get('status', {})
        status_type = status_obj.get('type', {})
        status_name = status_type.get('name', '')
        status_desc = status_type.get('description', '')
        print(f"  Event: {away.get('team',{}).get('abbreviation','?')} @ {home.get('team',{}).get('abbreviation','?')} | status.type.name='{status_name}' | status.type.description='{status_desc}'")

        is_final = 'STATUS_FINAL' in status_name or 'final' in status_desc.lower() or 'final' in status_name.lower()

        if is_final:
            home_name = home.get('team', {}).get('displayName', home.get('team', {}).get('abbreviation', '?'))
            away_name = away.get('team', {}).get('displayName', away.get('team', {}).get('abbreviation', '?'))
            home_rec  = home.get('records', [{}])[0].get('summary', '') if home.get('records') else ''
            away_rec  = away.get('records', [{}])[0].get('summary', '') if away.get('records') else ''
            if home_rec: home_name = f"{home_name} ({home_rec})"
            if away_rec: away_name = f"{away_name} ({away_rec})"
            score_line = f"{away_name} {away.get('score','?')}, {home_name} {home.get('score','?')}"
            games_data.append({'score_line': score_line, 'homers': [], 'away': away.get('team',{}).get('abbreviation',''), 'home': home.get('team',{}).get('abbreviation','')})
            print(f"  -> Added as final: {score_line}")

print(f"Total final games found: {len(games_data)}")

# ── Home runs via MLB Stats API ───────────────────────────────────────────────
print(f"Fetching home runs from MLB Stats API for {yesterday_iso}...")
schedule = fetch(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={yesterday_iso}")
print(f"MLB Stats API schedule: {'OK - ' + str(len((schedule.get('dates') or [{}])[0].get('games', []))) + ' games' if schedule else 'FAILED'}")

all_homers = []
if schedule:
    for date_entry in (schedule.get('dates') or []):
        for game in (date_entry.get('games') or []):
            game_pk = game.get('gamePk')
            status  = game.get('status', {}).get('detailedState', '')
            away_abbr = game.get('teams', {}).get('away', {}).get('team', {}).get('abbreviation', '')
            home_abbr = game.get('teams', {}).get('home', {}).get('team', {}).get('abbreviation', '')
            print(f"  MLB game {game_pk}: {away_abbr} @ {home_abbr} | {status}")
            if 'Final' not in status:
                continue
            pbp = fetch(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/playByPlay")
            if not pbp:
                print(f"    No PBP data")
                continue
            game_homers = []
            for play in (pbp.get('allPlays') or []):
                if play.get('result', {}).get('eventType') == 'home_run':
                    batter = play.get('matchup', {}).get('batter', {}).get('fullName', 'Unknown')
                    team   = play.get('offense', {}).get('team', {}).get('abbreviation', '')
                    desc   = play.get('result', {}).get('description', '')
                    game_homers.append(f"  💥 {batter} ({team}) — {desc}")
                    all_homers.append(f"{batter} ({team}) — {desc}")
            print(f"    Found {len(game_homers)} HRs")
            # Match to games_data
            for g in games_data:
                if g['away'] == away_abbr or g['home'] == home_abbr:
                    g['homers'] = game_homers
                    break
            time.sleep(0.1)

print(f"Total home runs: {len(all_homers)}")

# ── Fetch tracked player stats ────────────────────────────────────────────────
print("Fetching tracked player stats...")
rows = []
for p in players:
    name = p.get('name', 'Unknown')
    pid, team = get_player_meta(p)
    if not pid:
        continue
    url  = f"https://site.web.api.espn.com/apis/common/v3/sports/baseball/mlb/athletes/{pid}/gamelog?season={SEASON}"
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
AI_TOKEN  = os.environ.get('AI_TOKEN', '')
ai_summary = ''
if AI_TOKEN and (games_data or all_homers):
    scores_text = '\n'.join(g['score_line'] for g in games_data) if games_data else 'No completed games.'
    homers_text = '\n'.join(all_homers[:20]) if all_homers else 'No home runs.'
    prompt = f"You are a baseball analyst. Write a short exciting 3-4 sentence summary of yesterday's MLB action ({yesterday_display}). Mention notable scores and home runs.\n\nScores:\n{scores_text}\n\nHome Runs:\n{homers_text}"
    try:
        resp = requests.post(
            "https://models.inference.ai.azure.com/chat/completions",
            headers={"Authorization": f"Bearer {AI_TOKEN}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 300},
            timeout=30
        )
        if resp.status_code == 200:
            ai_summary = resp.json()['choices'][0]['message']['content'].strip()
            print("AI summary generated")
        else:
            print(f"AI error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"AI failed: {e}")
else:
    print(f"Skipping AI: token={'yes' if AI_TOKEN else 'NO'}, games={len(games_data)}, homers={len(all_homers)}")

# ── Build CSV ─────────────────────────────────────────────────────────────────
fieldnames = ['Player', 'Team', 'G', 'AB', 'H', 'AVG', 'HR', 'G Drought', 'AB Drought', 'As Of']
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=fieldnames)
writer.writeheader()
rows.sort(key=lambda r: r.get('G Drought', 0), reverse=True)
writer.writerows(rows)
csv_bytes = buf.getvalue().encode('utf-8')

# ── Build email body ──────────────────────────────────────────────────────────
lines = [f"⚾ MLB Stats — {today}", f"{len(rows)} players tracked. CSV attached, sorted by HR Drought.", ""]

if ai_summary:
    lines += ["📰 Yesterday's Recap", "─" * 40, ai_summary, ""]

if games_data:
    lines += [f"📊 Final Scores & Home Runs — {yesterday_display}", "─" * 40]
    for g in games_data:
        lines.append(g['score_line'])
        lines += g['homers'] if g['homers'] else ["  No home runs"]
        lines.append("")
else:
    lines += [f"No completed games found for {yesterday_display}.", ""]

email_body = '\n'.join(lines)
print(f"Email body length: {len(email_body)} chars")
print(f"First 300 chars of body:\n{email_body[:300]}")

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
