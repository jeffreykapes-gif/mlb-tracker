import os, csv, io, json, time, requests, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import date, timedelta
import firebase_admin
from firebase_admin import credentials, firestore

print("=== NHL EMAIL SCRIPT STARTING ===")

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

# ── Build roster index dynamically ───────────────────────────────────────────
# Skip ESPN roster index — ESPN blocks this endpoint from GitHub Actions
# All players use Firebase IDs directly
roster_index = {}
print("Skipping ESPN roster index (blocked) — using Firebase IDs directly")

def get_player_meta(entry):
    # Roster index is empty (ESPN blocked), use Firebase data directly
    return entry.get('id'), entry.get('team', ''), entry.get('jersey', '')

def parse_toi(raw):
    if not raw: return 0
    s = str(raw)
    parts = s.split(':')
    if len(parts) == 2:
        try: return int(parts[0]) * 60 + int(parts[1])
        except: pass
    try: return int(float(s))
    except: return 0

def fmt_toi(secs):
    if not secs: return '—'
    return f"{secs // 60}:{str(secs % 60).zfill(2)}"

def parse_gamelog(data, team_fallback=''):
    names = [str(n) for n in (data.get('names') or [])]
    g_idx   = next((i for i, n in enumerate(names) if n == 'goals'), -1)
    s_idx   = next((i for i, n in enumerate(names) if n == 'shotsTotal'), -1)
    toi_idx = next((i for i, n in enumerate(names) if n == 'timeOnIcePerGame'), -1)
    if g_idx < 0: return None
    events_meta = data.get('events', {})
    games = []
    for st in (data.get('seasonTypes') or []):
        for cat in (st.get('categories') or []):
            for ev in (cat.get('events') or []):
                stats = ev.get('stats', [])
                if not stats: continue
                meta = events_meta.get(ev.get('eventId'), {})
                games.append({'date': meta.get('gameDate', ''), 'goals': int(stats[g_idx] or 0), 'shots': int(stats[s_idx] or 0) if s_idx >= 0 else 0, 'toi': parse_toi(stats[toi_idx]) if toi_idx >= 0 else 0})
    games.sort(key=lambda g: g['date'])
    total_goals = sum(g['goals'] for g in games)
    total_shots = sum(g['shots'] for g in games)
    g_drought = shot_drought = 0
    for g in reversed(games):
        if g['goals'] > 0: break
        g_drought += 1
        shot_drought += g['shots']
    last10  = [g for g in games[-10:] if g['toi'] > 0]
    avg_toi = round(sum(g['toi'] for g in last10) / len(last10)) if last10 else None
    team = (data.get('seasonTypes') or [{}])[0].get('displayTeam', '') or team_fallback
    return {'G': len(games), 'Goals': total_goals, 'Shots': total_shots, 'G Drought': g_drought, 'Shots Since Goal': shot_drought, 'Avg TOI (L10)': fmt_toi(avg_toi), 'Team': team}

# ── Yesterday's scores ────────────────────────────────────────────────────────
yesterday = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
yesterday_display = (date.today() - timedelta(days=1)).strftime('%B %d, %Y')
today = date.today().isoformat()

# Use official NHL API for scores — ESPN is blocked from GitHub Actions
print(f"Fetching NHL scores via NHL API for {nhl_date}...")
scores_data = None  # not used anymore
nhl_score_data = fetch(f"https://api-web.nhle.com/v1/score/{nhl_date}")
print(f"NHL score API: {'OK - ' + str(len(nhl_score_data.get('games',[]))) + ' games' if nhl_score_data else 'FAILED'}")

games_data = []
all_goals  = []

# Use NHL API directly for scores and goals (ESPN blocked from GitHub Actions)
if nhl_score_data:
    for game in (nhl_score_data.get('games') or []):
        game_state = game.get('gameState', '')
        if game_state not in ('FINAL', 'OFF'):
            continue

        away_team  = game.get('awayTeam', {})
        home_team  = game.get('homeTeam', {})
        away_name  = away_team.get('name', {}).get('default', away_team.get('abbrev', '?'))
        home_name  = home_team.get('name', {}).get('default', home_team.get('abbrev', '?'))
        away_abbr  = away_team.get('abbrev', '')
        home_abbr  = home_team.get('abbrev', '')
        away_score = away_team.get('score', '?')
        home_score = home_team.get('score', '?')

        # Records
        away_rec = f"{away_team.get('record', '')}" if away_team.get('record') else ''
        home_rec = f"{home_team.get('record', '')}" if home_team.get('record') else ''
        away_display = f"{away_name} ({away_rec})" if away_rec else away_name
        home_display = f"{home_name} ({home_rec})" if home_rec else home_name

        # OT/SO
        period = game.get('periodDescriptor', {}).get('number', 3)
        period_type = game.get('periodDescriptor', {}).get('periodType', '')
        note = ' (OT)' if period == 4 else ' (SO)' if period_type == 'SO' or period > 4 else ''

        score_line = f"{away_display} {away_score}, {home_display} {home_score}{note}"
        print(f"  Final: {score_line}")

        # Get goals from play-by-play
        game_id    = game.get('id')
        game_goals = []
        if game_id:
            pbp = fetch(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play")
            if pbp:
                roster   = {p.get('playerId'): f"{p.get('firstName',{}).get('default','')} {p.get('lastName',{}).get('default','')}".strip() for p in (pbp.get('rosterSpots') or [])}
                team_map = {t.get('id'): t.get('abbrev','') for t in [pbp.get('homeTeam',{}), pbp.get('awayTeam',{})]}
                for play in (pbp.get('plays') or []):
                    if play.get('typeDescKey') == 'goal':
                        det    = play.get('details', {})
                        sid    = det.get('scoringPlayerId')
                        tid    = det.get('eventOwnerTeamId')
                        pnum   = play.get('periodDescriptor', {}).get('number', '')
                        tstr   = play.get('timeInPeriod', '')
                        sname  = roster.get(sid, str(sid))
                        tabbr  = team_map.get(tid, '')
                        goal_line = f"  🥅 {tabbr} — {sname} (P{pnum}, {tstr})"
                        game_goals.append(goal_line)
                        all_goals.append(goal_line.strip())
            print(f"    Goals: {len(game_goals)}")
            time.sleep(0.1)

        games_data.append({'score_line': score_line, 'goals': game_goals})

# ── Fetch tracked player stats ────────────────────────────────────────────────
print("Fetching tracked player stats...")
rows = []
for p in players:
    name = p.get('name', 'Unknown')
    pid, team, jersey = get_player_meta(p)
    if not pid:
        continue
    url  = f"https://site.web.api.espn.com/apis/common/v3/sports/hockey/nhl/athletes/{pid}/gamelog?season={SEASON}"
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
print(f"Got stats for {len(rows)} players")

# AI Summary skipped — GitHub Models endpoint unreachable from GitHub Actions
ai_summary = ''
print('AI summary skipped')

# ── Build CSV ─────────────────────────────────────────────────────────────────
fieldnames = ['Player', 'Jersey', 'Team', 'G', 'Goals', 'Shots', 'G Drought', 'Shots Since Goal', 'Avg TOI (L10)', 'As Of']
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=fieldnames)
writer.writeheader()
rows.sort(key=lambda r: r.get('G Drought', 0), reverse=True)
writer.writerows(rows)
csv_bytes = buf.getvalue().encode('utf-8')

# ── Build HTML email body ────────────────────────────────────────────────────
tracked_names = set(p.get('name', '').lower() for p in players)

def fmt_goal(line, tracked):
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

html_parts.append(f"<h1>🏒 NHL Stats — {today}</h1>")
html_parts.append(f'<p class="subtitle">{len(rows)} players tracked &bull; CSV attached, sorted by G Drought</p>')

if ai_summary:
    html_parts.append("<h2>📰 Yesterday's Recap</h2>")
    html_parts.append(f'<div class="recap">{ai_summary}</div>')

if games_data:
    html_parts.append(f'<h2>📊 Final Scores & Goal Scorers — {yesterday_display}</h2>')
    for g in games_data:
        html_parts.append('<div class="game">')
        html_parts.append(f'<div class="score">🔴 {g["score_line"]}</div>')
        if g['goals']:
            html_parts.append('<ul>')
            for goal in g['goals']:
                html_parts.append(fmt_goal(goal, tracked_names))
            html_parts.append('</ul>')
        else:
            html_parts.append('<p class="no-events">No scoring play data</p>')
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
msg['Subject'] = f"🏒 NHL Stats — {today}"
msg.attach(MIMEText(email_body, 'html'))

att = MIMEBase('application', 'octet-stream')
att.set_payload(csv_bytes)
encoders.encode_base64(att)
att.add_header('Content-Disposition', f'attachment; filename="NHL_Stats_{today}.csv"')
msg.attach(att)

with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())

print(f"✓ NHL email sent to {TO_EMAIL}")
