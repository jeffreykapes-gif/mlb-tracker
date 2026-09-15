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
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}

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
# Skip ESPN roster index — ESPN blocks roster endpoint from GitHub Actions
# All players use Firebase IDs directly; special cases handled in KNOWN_IDS
roster_index = {}
print("Skipping ESPN roster index (blocked) — using Firebase IDs directly")

# Known ESPN IDs — used for two-way players and anyone whose Firebase ID is wrong
KNOWN_IDS = {
    'shohei ohtani':  ('39832',  'LAD'),  # two-way player
    'brent rooker':   ('39858',  'OAK'),
    'yainer diaz':    ('42474',  'HOU'),
    'mickey moniak':  ('36157',  'LAA'),
    'addison barger': ('4872690','TOR'),
}

def search_espn_player(name):
    """Search ESPN for a player — finds IL players not in active roster."""
    try:
        url = f"https://site.web.api.espn.com/apis/search/v2?limit=5&query={requests.utils.quote(name)}&sport=baseball&league=mlb&type=player"
        data = fetch(url)
        if data:
            for result in (data.get('results') or []):
                for item in (result.get('contents') or []):
                    pid   = item.get('id') or item.get('athleteId')
                    pname = item.get('displayName') or item.get('name')
                    team  = item.get('teamShortName') or ''
                    if pid and pname:
                        print(f"  ESPN search found: {pname} ({team}) ID:{pid}")
                        return str(pid), team
    except Exception as e:
        print(f"  ESPN search error: {e}")
    return None, ''

def get_player_meta(entry):
    key = entry.get('name', '').lower()
    if key in KNOWN_IDS:
        known_id, known_team = KNOWN_IDS[key]
        print(f"  Using known ID for '{entry.get('name')}': {known_id} ({known_team})")
        return known_id, known_team
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
    # Try ESPN search for IL players
    sid, steam = search_espn_player(entry.get('name', ''))
    if sid:
        return sid, steam
    print(f"  ERROR: No ID for '{entry.get('name')}' — skipping")
    return None, ''

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

# Use MLB Stats API for scores (ESPN scoreboard is blocked from GitHub Actions)
print(f"Fetching MLB scores via MLB Stats API for {yesterday_iso}...")
games_data = []
scores_schedule = fetch(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={yesterday_iso}&hydrate=linescore,team")
if scores_schedule:
    for date_entry in (scores_schedule.get('dates') or []):
        for game in (date_entry.get('games') or []):
            status = game.get('status', {}).get('detailedState', '')
            if 'Final' not in status:
                continue
            away_team = game.get('teams', {}).get('away', {})
            home_team = game.get('teams', {}).get('home', {})
            away_name = away_team.get('team', {}).get('name', '?')
            home_name = home_team.get('team', {}).get('name', '?')
            away_abbr = away_team.get('team', {}).get('abbreviation', '')
            home_abbr = home_team.get('team', {}).get('abbreviation', '')
            away_score = away_team.get('score', '?')
            home_score = home_team.get('score', '?')
            away_rec = f"{away_team.get('leagueRecord',{}).get('wins',0)}-{away_team.get('leagueRecord',{}).get('losses',0)}"
            home_rec = f"{home_team.get('leagueRecord',{}).get('wins',0)}-{home_team.get('leagueRecord',{}).get('losses',0)}"
            score_line = f"{away_name} ({away_rec}) {away_score}, {home_name} ({home_rec}) {home_score}"
            print(f"  Final: {score_line}")
            games_data.append({'score_line': score_line, 'homers': [], 'away': away_abbr, 'home': home_abbr, 'game_pk': game.get('gamePk')})

print(f"Total final games found: {len(games_data)}")

# ── Home runs via MLB Stats API ───────────────────────────────────────────────
print(f"Fetching home runs for {yesterday_iso}...")
all_homers = []

for g in games_data:
    game_pk = g.get('game_pk')
    if not game_pk:
        continue
    pbp = fetch(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/playByPlay")
    if not pbp:
        continue
    game_homers = []
    for play in (pbp.get('allPlays') or []):
        if play.get('result', {}).get('eventType') == 'home_run':
            batter = play.get('matchup', {}).get('batter', {}).get('fullName', 'Unknown')
            team   = play.get('offense', {}).get('team', {}).get('abbreviation', '')
            desc   = play.get('result', {}).get('description', '')
            game_homers.append(f"   💥 {batter} ({team}) — {desc}")
            all_homers.append(f"{batter} ({team}) — {desc}")
    print(f"  {g['score_line'][:50]}... -> {len(game_homers)} HRs")
    g['homers'] = game_homers
    time.sleep(0.1)

print(f"Total home runs: {len(all_homers)}")

# ── Fetch tracked player stats ────────────────────────────────────────────────
print("Fetching tracked player stats...")
rows = []
for p in players:
    name = p.get('name', 'Unknown')
    pid, team = get_player_meta(p)
    if not pid:
        print(f"  SKIP {name}: no ID")
        continue
    import random
    cache_bust = random.randint(100000, 999999)
    url  = f"https://site.web.api.espn.com/apis/common/v3/sports/baseball/mlb/athletes/{pid}/gamelog?season={SEASON}&category=batting&_={cache_bust}"
    data = fetch(url)
    if not data:
        print(f"  SKIP {name}: no data from API (ID:{pid})")
        continue
    stats = parse_gamelog(data, team_fallback=team)
    if not stats:
        names_returned = [str(n) for n in (data.get('names') or [])]
        print(f"  SKIP {name}: parse failed. Columns: {names_returned}")
        continue
    if not stats.get('Team'):
        stats['Team'] = team
    rows.append({'Player': name, **stats, 'As Of': today})
    time.sleep(0.15)
print(f"Got stats for {len(rows)} players")

# AI Summary skipped — GitHub Models endpoint unreachable from GitHub Actions
ai_summary = ''
print("AI summary skipped")

# ── Build CSV ─────────────────────────────────────────────────────────────────
fieldnames = ['Player', 'Team', 'G', 'AB', 'H', 'AVG', 'HR', 'G Drought', 'AB Drought', 'As Of']
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=fieldnames)
writer.writeheader()
rows.sort(key=lambda r: r.get('G Drought', 0), reverse=True)
writer.writerows(rows)
csv_bytes = buf.getvalue().encode('utf-8')

# ── Build HTML email body ────────────────────────────────────────────────────
tracked_names = set(p.get('name', '').lower() for p in players)

def fmt_homer(line, tracked):
    """Bold the line if it contains a tracked player name."""
    line_lower = line.lower()
    for name in tracked:
        if name and name in line_lower:
            return f"<li><strong>{line.strip()}</strong></li>"
    return f"<li>{line.strip()}</li>"

html_parts = []
html_parts.append("""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body { font-family: -apple-system, Arial, sans-serif; font-size: 14px; color: #1a1a1a; max-width: 700px; margin: 0 auto; padding: 20px; }
  h1 { font-size: 22px; color: #1a1a1a; margin-bottom: 4px; }
  h2 { font-size: 16px; color: #333; border-bottom: 2px solid #e5e5e3; padding-bottom: 6px; margin-top: 28px; }
  .subtitle { color: #777; font-size: 13px; margin-bottom: 20px; }
  .recap { background: #f8f9fa; border-left: 4px solid #3b82f6; padding: 12px 16px; border-radius: 4px; margin: 16px 0; font-style: italic; }
  .game { margin-bottom: 20px; }
  .score { font-weight: bold; font-size: 15px; color: #1a1a1a; margin-bottom: 6px; }
  ul { margin: 4px 0 0 20px; padding: 0; }
  li { margin: 3px 0; color: #444; }
  .no-events { color: #999; font-style: italic; margin-left: 20px; font-size: 13px; }
</style></head><body>""")

html_parts.append(f"<h1>⚾ MLB Stats — {today}</h1>")
html_parts.append(f'<p class="subtitle">{len(rows)} players tracked &bull; CSV attached, sorted by HR Drought</p>')

if ai_summary:
    html_parts.append("<h2>📰 Yesterday's Recap</h2>")
    html_parts.append(f'<div class="recap">{ai_summary}</div>')

if games_data:
    html_parts.append(f'<h2>📊 Final Scores & Home Runs — {yesterday_display}</h2>')
    for g in games_data:
        html_parts.append('<div class="game">')
        html_parts.append(f'<div class="score">🔴 {g["score_line"]}</div>')
        if g['homers']:
            html_parts.append('<ul>')
            for h in g['homers']:
                html_parts.append(fmt_homer(h, tracked_names))
            html_parts.append('</ul>')
        else:
            html_parts.append('<p class="no-events">No home runs</p>')
        html_parts.append('</div>')
else:
    html_parts.append(f'<p>No completed games found for {yesterday_display}.</p>')

html_parts.append('</body></html>')
email_body = '\n'.join(html_parts)
print(f"HTML email body length: {len(email_body)} chars")

# ── Send email ────────────────────────────────────────────────────────────────
GMAIL_USER = os.environ['GMAIL_USER']
GMAIL_PASS = os.environ['GMAIL_PASS']
TO_EMAIL   = os.environ['TO_EMAIL']

msg = MIMEMultipart()
msg['From']    = GMAIL_USER
msg['To']      = TO_EMAIL
msg['Subject'] = f"⚾ MLB Stats — {today}"
msg.attach(MIMEText(email_body, 'html'))

att = MIMEBase('application', 'octet-stream')
att.set_payload(csv_bytes)
encoders.encode_base64(att)
att.add_header('Content-Disposition', f'attachment; filename="MLB_Stats_{today}.csv"')
msg.attach(att)

with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())

print(f"✓ MLB email sent to {TO_EMAIL}")
