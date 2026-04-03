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
print("Building NHL roster index...")
roster_index = {}
teams_data = fetch("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams?limit=40")
team_ids = []
if teams_data:
    for t in (teams_data.get('sports', [{}])[0].get('leagues', [{}])[0].get('teams', [])):
        tid = t.get('team', {}).get('id')
        if tid:
            team_ids.append(tid)
print(f"Found {len(team_ids)} NHL teams")
for tid in team_ids:
    d = fetch(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams/{tid}/roster")
    if not d:
        continue
    team_abbr = (d.get('team') or {}).get('abbreviation', '')
    for group in (d.get('athletes') or []):
        for p in (group.get('items') or []):
            if p.get('fullName') and p.get('id'):
                roster_index[p['fullName'].lower()] = {'id': str(p['id']), 'name': p['fullName'], 'team': team_abbr, 'jersey': str(p.get('jersey', ''))}
    time.sleep(0.1)
print(f"Roster index: {len(roster_index)} players")

def get_player_meta(entry):
    key = entry.get('name', '').lower()
    if key in roster_index:
        m = roster_index[key]
        return m['id'], m['team'], m.get('jersey', '')
    for k, v in roster_index.items():
        if key in k or k in key:
            return v['id'], v['team'], v.get('jersey', '')
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

print(f"Fetching NHL scores for {yesterday}...")
scores_data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={yesterday}")
print(f"Scoreboard API result: {'OK - ' + str(len(scores_data.get('events',[]))) + ' events' if scores_data else 'FAILED - None returned'}")

games_data = []
all_goals  = []

# ESPN -> NHL API abbreviation map
ESPN_TO_NHL = {
    'NJ': 'NJD', 'TB': 'TBL', 'LA': 'LAK', 'SJ': 'SJS',
    'CLB': 'CBJ', 'NAS': 'NSH', 'MON': 'MTL', 'WIN': 'WPG',
    'ANH': 'ANA', 'VEG': 'VGK', 'UTA': 'UTAH', 'NSH': 'NSH',
    'WSH': 'WSH', 'PHX': 'ARI',
}

# Fetch NHL schedule once upfront — build lookup: (away_abbr, home_abbr) -> game_id
nhl_date = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
nhl_sched = fetch(f"https://api-web.nhle.com/v1/score/{nhl_date}")
nhl_game_lookup = {}
if nhl_sched:
    for g in (nhl_sched.get('games') or []):
        g_away = g.get('awayTeam', {}).get('abbrev', '')
        g_home = g.get('homeTeam', {}).get('abbrev', '')
        nhl_game_lookup[(g_away, g_home)] = g.get('id')
    print(f"NHL schedule loaded: {len(nhl_game_lookup)} games: {list(nhl_game_lookup.keys())}")

def get_goals_for_game(event_id, away_espn, home_espn):
    goals = []

    # Try ESPN summary first
    box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event={event_id}")
    if box:
        for play in (box.get('scoringPlays') or box.get('scoring') or []):
            text      = play.get('text', '') or play.get('description', '')
            team_abbr = play.get('team', {}).get('abbreviation', '')
            period    = play.get('period', {}).get('displayValue', '') or str(play.get('period', ''))
            clock     = play.get('clock', {}).get('displayValue', '') or play.get('clock', '')
            if text:
                goals.append(f"  🥅 {team_abbr} — {text} ({period}, {clock})")
    if goals:
        return goals

    # Fall back to NHL API
    away_nhl = ESPN_TO_NHL.get(away_espn, away_espn)
    home_nhl = ESPN_TO_NHL.get(home_espn, home_espn)
    nhl_game_id = nhl_game_lookup.get((away_nhl, home_nhl))
    print(f"    ESPN empty -> NHL API: {away_espn}->{away_nhl} @ {home_espn}->{home_nhl} | id={nhl_game_id}")

    # Fuzzy match if exact fails
    if not nhl_game_id:
        for (ga, gh), gid in nhl_game_lookup.items():
            if (away_nhl in ga or ga in away_nhl or away_espn == ga) and (home_nhl in gh or gh in home_nhl or home_espn == gh):
                nhl_game_id = gid
                print(f"    Fuzzy match: ({ga},{gh}) -> {gid}")
                break

    if nhl_game_id:
        pbp = fetch(f"https://api-web.nhle.com/v1/gamecenter/{nhl_game_id}/play-by-play")
        if pbp:
            roster   = {p.get('playerId'): f"{p.get('firstName',{}).get('default','')} {p.get('lastName',{}).get('default','')}".strip() for p in (pbp.get('rosterSpots') or [])}
            team_map = {t.get('id'): t.get('abbrev','') for t in [pbp.get('homeTeam',{}), pbp.get('awayTeam',{})]}
            for play in (pbp.get('plays') or []):
                if play.get('typeDescKey') == 'goal':
                    det    = play.get('details', {})
                    sid    = det.get('scoringPlayerId')
                    tid    = det.get('eventOwnerTeamId')
                    period = play.get('periodDescriptor', {}).get('number', '')
                    tstr   = play.get('timeInPeriod', '')
                    goals.append(f"  🥅 {team_map.get(tid,'')} — {roster.get(sid, str(sid))} (P{period}, {tstr})")
    return goals

if scores_data:
    for event in (scores_data.get('events') or []):
        comps       = event.get('competitions', [{}])[0]
        competitors = comps.get('competitors', [])
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0])
        away = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1])

        status_name = event.get('status', {}).get('type', {}).get('name', '')
        status_desc = event.get('status', {}).get('type', {}).get('description', '')
        is_final    = 'STATUS_FINAL' in status_name or 'final' in status_desc.lower()

        away_abbr = away.get('team', {}).get('abbreviation', '?')
        home_abbr = home.get('team', {}).get('abbreviation', '?')
        print(f"  {away_abbr} @ {home_abbr} | final={is_final}")

        if is_final:
            home_name = home.get('team', {}).get('displayName', home_abbr)
            away_name = away.get('team', {}).get('displayName', away_abbr)
            home_rec  = home.get('records', [{}])[0].get('summary', '') if home.get('records') else ''
            away_rec  = away.get('records', [{}])[0].get('summary', '') if away.get('records') else ''
            if home_rec: home_name = f"{home_name} ({home_rec})"
            if away_rec: away_name = f"{away_name} ({away_rec})"
            short_detail = event.get('status', {}).get('type', {}).get('shortDetail', '')
            note       = ' (OT)' if 'OT' in short_detail else ' (SO)' if 'SO' in short_detail else ''
            score_line = f"{away_name} {away.get('score','?')}, {home_name} {home.get('score','?')}{note}"
            print(f"  -> {score_line}")

            game_goals = get_goals_for_game(event.get('id',''), away_abbr, home_abbr)
            print(f"    Goals: {len(game_goals)}")
            all_goals.extend(game_goals)
            games_data.append({'score_line': score_line, 'goals': game_goals})
            time.sleep(0.2)
print(f"Total final games: {len(games_data)} | Total goals: {len(all_goals)}")

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

# ── AI Summary ────────────────────────────────────────────────────────────────
AI_TOKEN   = os.environ.get('AI_TOKEN', '')
ai_summary = ''
if AI_TOKEN and (games_data or all_goals):
    scores_text = '\n'.join(g['score_line'] for g in games_data) if games_data else 'No completed games.'
    goals_text  = '\n'.join(all_goals[:30]) if all_goals else 'No goals.'
    prompt = f"You are an NHL analyst. Write a short exciting 3-4 sentence summary of yesterday's NHL action ({yesterday_display}). Mention notable scores and goal scorers.\n\nScores:\n{scores_text}\n\nGoals:\n{goals_text}"
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
    print(f"Skipping AI: token={'yes' if AI_TOKEN else 'NO'}, games={len(games_data)}, goals={len(all_goals)}")

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
