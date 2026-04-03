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
        print(f"  Event: {away.get('team',{}).get('abbreviation','?')} @ {home.get('team',{}).get('abbreviation','?')} | name='{status_name}' | desc='{status_desc}'")

        is_final = 'STATUS_FINAL' in status_name or 'final' in status_desc.lower() or 'final' in status_name.lower()

        if is_final:
            home_name = home.get('team', {}).get('displayName', home.get('team', {}).get('abbreviation', '?'))
            away_name = away.get('team', {}).get('displayName', away.get('team', {}).get('abbreviation', '?'))
            home_rec  = home.get('records', [{}])[0].get('summary', '') if home.get('records') else ''
            away_rec  = away.get('records', [{}])[0].get('summary', '') if away.get('records') else ''
            if home_rec: home_name = f"{home_name} ({home_rec})"
            if away_rec: away_name = f"{away_name} ({away_rec})"
            short_detail = event.get('status', {}).get('type', {}).get('shortDetail', '')
            note = ' (OT)' if 'OT' in short_detail else ' (SO)' if 'SO' in short_detail else ''
            score_line = f"{away_name} {away.get('score','?')}, {home_name} {home.get('score','?')}{note}"
            print(f"  -> Final: {score_line}")

            game_goals = []
            event_id   = event.get('id')
            if event_id:
                # Try ESPN summary first
                box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event={event_id}")
                if box:
                    print(f"    ESPN summary keys: {list(box.keys())}")
                    scoring = box.get('scoringPlays') or box.get('scoring') or []
                    for play in scoring:
                        text      = play.get('text', '') or play.get('description', '')
                        team_abbr = play.get('team', {}).get('abbreviation', '')
                        period    = play.get('period', {}).get('displayValue', '') or str(play.get('period', ''))
                        clock     = play.get('clock', {}).get('displayValue', '') or play.get('clock', '')
                        if text:
                            goal_line = f"  🥅 {team_abbr} — {text} ({period}, {clock})"
                            game_goals.append(goal_line)
                            all_goals.append(f"{team_abbr} — {text} ({period}, {clock})")

                # If ESPN returned nothing, try official NHL API
                if not game_goals:
                    # Get NHL game ID from ESPN event - need to find it via schedule
                    nhl_date = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
                    nhl_sched = fetch(f"https://api-web.nhle.com/v1/score/{nhl_date}")
                    if nhl_sched:
                        away_abbr = away.get('team', {}).get('abbreviation', '')
                        home_abbr = home.get('team', {}).get('abbreviation', '')
                        # ESPN <-> NHL API abbreviation map
                        abbr_map = {
                            'NJ': 'NJD', 'TB': 'TBL', 'LA': 'LAK', 'SJ': 'SJS',
                            'CLB': 'CBJ', 'NAS': 'NSH', 'MON': 'MTL', 'WIN': 'WPG',
                            'ANH': 'ANA', 'VEG': 'VGK', 'UTA': 'UTAH',
                        }
                        def to_nhl(abbr):
                            return abbr_map.get(abbr, abbr)

                        for g in (nhl_sched.get('games') or []):
                            g_away = g.get('awayTeam', {}).get('abbrev', '')
                            g_home = g.get('homeTeam', {}).get('abbrev', '')
                            if g_away == to_nhl(away_abbr) and g_home == to_nhl(home_abbr):
                                nhl_game_id = g.get('id')
                                pbp = fetch(f"https://api-web.nhle.com/v1/gamecenter/{nhl_game_id}/play-by-play")
                                if pbp:
                                    for play in (pbp.get('plays') or []):
                                        if play.get('typeDescKey') == 'goal':
                                            details   = play.get('details', {})
                                            scorer_id = details.get('scoringPlayerId')
                                            team_id   = details.get('eventOwnerTeamId')
                                            period    = play.get('periodDescriptor', {}).get('number', '')
                                            time_str  = play.get('timeInPeriod', '')
                                            # Get scorer name from roster
                                            scorer_name = str(scorer_id)
                                            for p_entry in (pbp.get('rosterSpots') or []):
                                                if p_entry.get('playerId') == scorer_id:
                                                    scorer_name = f"{p_entry.get('firstName',{}).get('default','')} {p_entry.get('lastName',{}).get('default','')}".strip()
                                                    break
                                            # Get team abbrev
                                            t_abbr = ''
                                            for t in [pbp.get('homeTeam',{}), pbp.get('awayTeam',{})]:
                                                if t.get('id') == team_id:
                                                    t_abbr = t.get('abbrev', '')
                                                    break
                                            goal_line = f"  🥅 {t_abbr} — {scorer_name} (P{period}, {time_str})"
                                            game_goals.append(goal_line)
                                            all_goals.append(f"{t_abbr} — {scorer_name} (P{period}, {time_str})")
                                break

                print(f"    Found {len(game_goals)} goals")
                time.sleep(0.15)

            games_data.append({'score_line': score_line, 'goals': game_goals})

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

# ── Build email body ──────────────────────────────────────────────────────────
lines = [f"🏒 NHL Stats — {today}", f"{len(rows)} players tracked. CSV attached, sorted by G Drought.", ""]

if ai_summary:
    lines += ["📰 Yesterday's Recap", "─" * 40, ai_summary, ""]

if games_data:
    lines += [f"📊 Final Scores & Goal Scorers — {yesterday_display}", "═" * 50]
    for g in games_data:
        lines.append(f"🔴 {g['score_line']}")
        if g['goals']:
            lines += g['goals']
        else:
            lines.append("   No scoring play data available")
        lines.append("─" * 50)
        lines.append("")
else:
    lines += [f"No completed games found for {yesterday_display}.", ""]

email_body = '\n'.join(lines)
print(f"Email body length: {len(email_body)} chars")
print(f"First 300 chars:\n{email_body[:300]}")

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
