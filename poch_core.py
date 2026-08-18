"""
POCH — Fantasy Premier League decision agent (single-file Colab version)
==========================================================================
Orchestrator + Senate (6 reviewers) + 9 sub-apps + shared knowledge base.
This is a consolidated copy of the full multi-file project - same logic,
one file, so it drops straight into a Google Colab cell with no import
juggling. Run cells top to bottom; the demo/usage section is at the very
bottom.

Quick start in Colab:
    1. Paste this whole file into a cell and run it.
    2. In a new cell:
         kb = load_live_knowledge_base()          # pulls real FPL data
         kb.your_squad = [...]                     # your 15 player ids
         kb.bank = 0.5
         poch = Poch(kb)
         print(poch.pick_lineup())
    3. See the bottom of this file for a full worked example, including
       how to find your player ids and your FPL team ID.
"""
import requests
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ============================================================
# FPL CLIENT — thin wrapper around the free, public FPL API
# ============================================================
FPL_BASE = "https://fantasy.premierleague.com/api/"


class FPLClient:
    def __init__(self, session=None, timeout=15):
        self.session = session or requests.Session()
        self.timeout = timeout

    def get_bootstrap(self):
        r = self.session.get(FPL_BASE + "bootstrap-static/", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_fixtures(self, event=None):
        params = {"event": event} if event else {}
        r = self.session.get(FPL_BASE + "fixtures/", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_element_summary(self, player_id):
        r = self.session.get(FPL_BASE + f"element-summary/{player_id}/", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_entry(self, team_id):
        r = self.session.get(FPL_BASE + f"entry/{team_id}/", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_entry_picks(self, team_id, event):
        r = self.session.get(FPL_BASE + f"entry/{team_id}/event/{event}/picks/", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_entry_history(self, team_id):
        r = self.session.get(FPL_BASE + f"entry/{team_id}/history/", timeout=self.timeout)
        r.raise_for_status()
        return r.json()


# ============================================================
# KNOWLEDGE BASE — shared state for every sub-app and senator
# ============================================================
POSITION_NAMES = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
STATUS_NAMES = {"a": "available", "d": "doubtful", "i": "injured", "s": "suspended", "u": "unavailable", "n": "on loan elsewhere"}


@dataclass
class Player:
    id: int
    web_name: str
    team: int
    element_type: int
    now_cost: int
    total_points: int
    form: float
    selected_by_percent: float
    status: str
    chance_of_playing_next_round: Optional[int]
    points_per_game: float

    @property
    def cost_m(self):
        return self.now_cost / 10.0

    @property
    def position(self):
        return POSITION_NAMES[self.element_type]

    @property
    def is_nailed(self):
        if self.status != "a":
            return False
        if self.chance_of_playing_next_round is not None and self.chance_of_playing_next_round < 75:
            return False
        return True


@dataclass
class KnowledgeBase:
    players: Dict[int, Player] = field(default_factory=dict)
    teams: Dict[int, dict] = field(default_factory=dict)
    fixtures: List[dict] = field(default_factory=list)
    current_event: Optional[int] = None
    your_squad: List[int] = field(default_factory=list)
    bank: float = 0.0
    free_transfers: int = 1
    chips_used: List[str] = field(default_factory=list)
    strategy: dict = field(default_factory=lambda: {
        "risk_tolerance": "medium", "template_defence": True, "differential_attack": False,
    })
    decision_log: List[dict] = field(default_factory=list)
    community_signals: List = field(default_factory=list)

    def load_from_bootstrap(self, data: dict):
        for t in data["teams"]:
            self.teams[t["id"]] = t
        for e in data["elements"]:
            self.players[e["id"]] = Player(
                id=e["id"], web_name=e["web_name"], team=e["team"], element_type=e["element_type"],
                now_cost=e["now_cost"], total_points=e["total_points"], form=float(e.get("form") or 0),
                selected_by_percent=float(e.get("selected_by_percent") or 0), status=e.get("status", "a"),
                chance_of_playing_next_round=e.get("chance_of_playing_next_round"),
                points_per_game=float(e.get("points_per_game") or 0),
            )
        current = next((ev for ev in data["events"] if ev.get("is_current")), None)
        nxt = next((ev for ev in data["events"] if ev.get("is_next")), None)
        self.current_event = (current or nxt or {}).get("id")

    def load_fixtures(self, fixtures: list):
        self.fixtures = fixtures

    def team_name(self, team_id):
        t = self.teams.get(team_id)
        return t["short_name"] if t else "UNK"

    def fixture_difficulty_run(self, team_id, num_gws=5):
        upcoming = [f for f in self.fixtures if not f.get("finished") and (f["team_h"] == team_id or f["team_a"] == team_id)]
        upcoming = sorted(upcoming, key=lambda f: f.get("event") or 999)[:num_gws]
        return [f["team_h_difficulty"] if f["team_h"] == team_id else f["team_a_difficulty"] for f in upcoming]

    def fixtures_in_event(self, team_id, event):
        return len([f for f in self.fixtures if f.get("event") == event and (f["team_h"] == team_id or f["team_a"] == team_id)])

    def squad_players(self):
        return [self.players[pid] for pid in self.your_squad if pid in self.players]


# ============================================================
# RULES VALIDATOR — hard guardrail, never overruled by the senate
# ============================================================
SQUAD_SIZE = 15
SQUAD_POSITION_COUNTS = {1: 2, 2: 5, 3: 5, 4: 3}
BUDGET_TENTHS = 1000
MAX_PER_CLUB = 3


def validate_squad(player_ids, kb):
    errors = []
    if len(player_ids) != SQUAD_SIZE:
        errors.append(f"Squad must have {SQUAD_SIZE} players, has {len(player_ids)}")
    pos_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    club_counts = {}
    total_cost = 0
    for pid in player_ids:
        p = kb.players.get(pid)
        if not p:
            errors.append(f"Unknown player id {pid}")
            continue
        pos_counts[p.element_type] += 1
        club_counts[p.team] = club_counts.get(p.team, 0) + 1
        total_cost += p.now_cost
    for pos, required in SQUAD_POSITION_COUNTS.items():
        if pos_counts[pos] != required:
            errors.append(f"Need {required} {POSITION_NAMES[pos]}, squad has {pos_counts[pos]}")
    for team, count in club_counts.items():
        if count > MAX_PER_CLUB:
            errors.append(f"Too many players from {kb.team_name(team)}: {count} (max {MAX_PER_CLUB})")
    if total_cost > BUDGET_TENTHS:
        errors.append(f"Squad costs £{total_cost / 10:.1f}m, over the £{BUDGET_TENTHS / 10:.1f}m budget")
    return errors


def validate_lineup(starting_ids, kb):
    errors = []
    if len(starting_ids) != 11:
        errors.append(f"Starting XI must have 11 players, has {len(starting_ids)}")
    pos_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for pid in starting_ids:
        p = kb.players.get(pid)
        if p:
            pos_counts[p.element_type] += 1
    if pos_counts[1] != 1:
        errors.append(f"Must start exactly 1 goalkeeper, has {pos_counts[1]}")
    if pos_counts[2] < 3:
        errors.append(f"Must start at least 3 defenders, has {pos_counts[2]}")
    if pos_counts[4] < 1:
        errors.append(f"Must start at least 1 forward, has {pos_counts[4]}")
    if sum(pos_counts.values()) != 11:
        errors.append("Position counts don't sum to 11")
    return errors


# ============================================================
# FIXTURE ANALYST — shared fixture-difficulty scoring
# ============================================================
def fixture_score(team_id, kb, num_gws=5):
    diffs = kb.fixture_difficulty_run(team_id, num_gws=num_gws)
    if not diffs:
        return 0.0
    return sum(6 - d for d in diffs) / len(diffs)


def blank_or_double_gameweeks(kb, team_ids, num_gws=6):
    if kb.current_event is None:
        return {}
    flags = {}
    for gw in range(kb.current_event, kb.current_event + num_gws):
        for team_id in team_ids:
            count = kb.fixtures_in_event(team_id, gw)
            if count == 0:
                flags.setdefault(gw, {}).setdefault("blank", []).append(team_id)
            elif count >= 2:
                flags.setdefault(gw, {}).setdefault("double", []).append(team_id)
    return flags


# ============================================================
# PROJECTION — shared "projected points" heuristic
# ============================================================
FORM_WEIGHT = 0.5
PPG_WEIGHT = 0.3
FIXTURE_WEIGHT = 0.2


def projected_points(player, kb, num_gws=1):
    minutes_discount = 1.0 if player.is_nailed else 0.4
    fscore = fixture_score(player.team, kb, num_gws=num_gws)
    raw = FORM_WEIGHT * player.form + PPG_WEIGHT * player.points_per_game + FIXTURE_WEIGHT * fscore
    return round(raw * minutes_discount, 2)


def value_score(player, kb):
    pts = projected_points(player, kb)
    cost = max(player.cost_m, 0.1)
    return round(pts / cost, 3)


# ============================================================
# SQUAD BUILDER — greedy, value-ranked, budget/club-cap aware
# (MVP heuristic; a true optimum would need an ILP solver e.g. PuLP)
# ============================================================
def build_squad(kb, num_gws=5, exclude_ids=None):
    exclude_ids = exclude_ids or set()
    budget_left = BUDGET_TENTHS
    club_counts = {}
    squad = []

    candidates_by_pos = {pos: [] for pos in SQUAD_POSITION_COUNTS}
    for p in kb.players.values():
        if p.id in exclude_ids:
            continue
        candidates_by_pos[p.element_type].append(p)
    for pos, plist in candidates_by_pos.items():
        plist.sort(key=lambda p: value_score(p, kb), reverse=True)

    for pos, required in SQUAD_POSITION_COUNTS.items():
        filled = 0
        for p in candidates_by_pos[pos]:
            if filled >= required:
                break
            if p.now_cost > budget_left or club_counts.get(p.team, 0) >= MAX_PER_CLUB:
                continue
            squad.append(p.id)
            budget_left -= p.now_cost
            club_counts[p.team] = club_counts.get(p.team, 0) + 1
            filled += 1
        if filled < required:
            for p in sorted(candidates_by_pos[pos], key=lambda p: p.now_cost):
                if filled >= required:
                    break
                if p.id in squad or p.now_cost > budget_left or club_counts.get(p.team, 0) >= MAX_PER_CLUB:
                    continue
                squad.append(p.id)
                budget_left -= p.now_cost
                club_counts[p.team] = club_counts.get(p.team, 0) + 1
                filled += 1

    return {
        "player_ids": squad,
        "bank_remaining": round(budget_left / 10, 1),
        "total_projected": round(sum(projected_points(kb.players[pid], kb, num_gws=num_gws) for pid in squad), 2),
    }


# ============================================================
# LINEUP SELECTOR — best valid XI + bench order from the squad
# ============================================================
VALID_FORMATIONS = [(3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2), (4, 5, 1), (5, 2, 3), (5, 3, 2), (5, 4, 1)]


def pick_lineup(kb, num_gws=1):
    squad = kb.squad_players()
    by_pos = {1: [], 2: [], 3: [], 4: []}
    for p in squad:
        by_pos[p.element_type].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: projected_points(p, kb, num_gws=num_gws), reverse=True)

    best = None
    for defs, mids, fwds in VALID_FORMATIONS:
        if len(by_pos[1]) < 1 or len(by_pos[2]) < defs or len(by_pos[3]) < mids or len(by_pos[4]) < fwds:
            continue
        starters = by_pos[1][:1] + by_pos[2][:defs] + by_pos[3][:mids] + by_pos[4][:fwds]
        total = sum(projected_points(p, kb, num_gws=num_gws) for p in starters)
        if best is None or total > best["total_projected"]:
            starter_ids = [p.id for p in starters]
            if validate_lineup(starter_ids, kb):
                continue
            bench = sorted([p for p in squad if p.id not in starter_ids], key=lambda p: projected_points(p, kb, num_gws=num_gws), reverse=True)
            best = {
                "formation": f"{defs}-{mids}-{fwds}",
                "starting_ids": starter_ids,
                "bench_ids": [p.id for p in bench],
                "total_projected": round(total, 2),
            }
    return best


# ============================================================
# CAPTAINCY ADVISOR
# ============================================================
def suggest_captain(starting_ids, kb, num_gws=1):
    ranked = sorted((kb.players[pid] for pid in starting_ids if pid in kb.players),
                     key=lambda p: projected_points(p, kb, num_gws=num_gws), reverse=True)
    if not ranked:
        return None
    captain, vice = ranked[0], (ranked[1] if len(ranked) > 1 else None)
    return {
        "captain_id": captain.id, "captain_name": captain.web_name,
        "captain_projected": projected_points(captain, kb, num_gws=num_gws),
        "vice_id": vice.id if vice else None, "vice_name": vice.web_name if vice else None,
        "ranking": [{"id": p.id, "name": p.web_name, "projected": projected_points(p, kb, num_gws=num_gws)} for p in ranked],
    }


# ============================================================
# TRANSFER ADVISOR
# ============================================================
HIT_COST = 4


def _club_counts(squad_players):
    counts = {}
    for p in squad_players:
        counts[p.team] = counts.get(p.team, 0) + 1
    return counts


def evaluate_transfer(player_out_id, player_in_id, kb, num_gws=5):
    p_out, p_in = kb.players.get(player_out_id), kb.players.get(player_in_id)
    if not p_out or not p_in:
        return {"error": "unknown player id(s)"}
    if p_out.element_type != p_in.element_type:
        return {"error": "players must be the same position"}
    counts = _club_counts([p for p in kb.squad_players() if p.id != p_out.id])
    if counts.get(p_in.team, 0) >= MAX_PER_CLUB and p_in.team != p_out.team:
        return {"error": f"would exceed {MAX_PER_CLUB}-per-club limit for {kb.team_name(p_in.team)}"}
    cost_delta = (p_in.now_cost - p_out.now_cost) / 10
    if cost_delta > kb.bank:
        return {"error": f"not enough in the bank (need £{cost_delta:.1f}m, have £{kb.bank:.1f}m)"}
    gain = round(projected_points(p_in, kb, num_gws=num_gws) - projected_points(p_out, kb, num_gws=num_gws), 2)
    hit_applies = kb.free_transfers <= 0
    net_gain = round(gain - (HIT_COST if hit_applies else 0), 2)
    return {
        "player_out": p_out.web_name, "player_in": p_in.web_name, "projected_gain_over_horizon": gain,
        "hit_applies": hit_applies, "net_gain_after_hit": net_gain, "cost_delta_m": round(cost_delta, 1),
        "recommend": net_gain > 0,
    }


def suggest_weekly(kb, num_gws=5, top_n=3):
    proposals = []
    for out_p in kb.squad_players():
        candidates = [p for p in kb.players.values() if p.element_type == out_p.element_type and p.id != out_p.id and p.id not in kb.your_squad]
        candidates.sort(key=lambda p: projected_points(p, kb, num_gws=num_gws), reverse=True)
        for in_p in candidates[:5]:
            result = evaluate_transfer(out_p.id, in_p.id, kb, num_gws=num_gws)
            if "error" not in result and result["net_gain_after_hit"] > 0:
                proposals.append(result)
    proposals.sort(key=lambda r: r["net_gain_after_hit"], reverse=True)
    return proposals[:top_n]


# ============================================================
# CHIP STRATEGIST — heuristic flags, reviewed by the senate before surfacing
# ============================================================
ALL_CHIPS = {"wildcard", "freehit", "bboost", "3xc"}


def chip_advice(kb, lineup, num_gws=6):
    advice = []
    remaining = ALL_CHIPS - set(kb.chips_used)
    team_ids = {p.team for p in kb.squad_players()}
    bd_flags = blank_or_double_gameweeks(kb, team_ids, num_gws=num_gws)

    if "bboost" in remaining and lineup:
        bench_total = sum(projected_points(kb.players[pid], kb) for pid in lineup["bench_ids"] if pid in kb.players)
        if bench_total >= 15:
            advice.append({"chip": "Bench Boost", "reason": f"Bench projected at {bench_total:.1f} pts this week - strong candidate."})

    if "3xc" in remaining and lineup and lineup.get("starting_ids"):
        starters = sorted((kb.players[pid] for pid in lineup["starting_ids"] if pid in kb.players),
                           key=lambda p: projected_points(p, kb), reverse=True)
        if len(starters) >= 2:
            gap = projected_points(starters[0], kb) - projected_points(starters[1], kb)
            if gap >= 2.5:
                advice.append({"chip": "Triple Captain", "reason": f"{starters[0].web_name} projected {gap:.1f} pts clear of next best captain option."})

    for gw, flags in bd_flags.items():
        doubles, blanks = flags.get("double", []), flags.get("blank", [])
        if len(doubles) >= 2 and "wildcard" in remaining:
            names = ", ".join(kb.team_name(t) for t in doubles)
            advice.append({"chip": f"Wildcard (ahead of GW{gw})", "reason": f"Double gameweek forming for {names} - good window to load up before it."})
        if len(blanks) >= 3 and "freehit" in remaining:
            names = ", ".join(kb.team_name(t) for t in blanks)
            advice.append({"chip": f"Free Hit (GW{gw})", "reason": f"Blank gameweek forming for {names} - Free Hit protects your XI that week."})
    return advice


# ============================================================
# COMMUNITY INTEL — forum/blog consensus (fed by you or by Claude
# doing the web-search in chat; nothing here scrapes on its own)
# ============================================================
SIGNAL_TYPES = {"must_have", "hidden_gem", "enabler", "gw_strategy", "avoid"}


@dataclass
class CommunitySignal:
    signal_type: str
    subject: str
    summary: str
    source_count: int = 1
    sources: List[str] = field(default_factory=list)


def ingest_signals(kb, signals: List[CommunitySignal]):
    kb.community_signals.extend(signals)


def refresh_signals(kb, signals: List[CommunitySignal]):
    kb.community_signals = list(signals)


def by_type(kb, signal_type):
    return [s for s in kb.community_signals if s.signal_type == signal_type]


def must_haves(kb):
    return sorted(by_type(kb, "must_have"), key=lambda s: s.source_count, reverse=True)


def hidden_gems(kb):
    return sorted(by_type(kb, "hidden_gem"), key=lambda s: s.source_count, reverse=True)


def enablers(kb):
    return sorted(by_type(kb, "enabler"), key=lambda s: s.source_count, reverse=True)


def gw_strategy_notes(kb):
    return by_type(kb, "gw_strategy")


def avoids(kb):
    return by_type(kb, "avoid")


def consensus_for_player(kb, player_name):
    return [s for s in kb.community_signals if s.subject.lower() == player_name.lower()]


# ============================================================
# STUB SUB-APPS — News Scout, Price Predictor, Post-Mortem
# ============================================================
def check_late_news(player_names, manual_flags=None):
    manual_flags = manual_flags or {}
    return [{"player": name, "note": manual_flags[name]} for name in player_names if name in manual_flags]


def flag_price_moves(kb, previous_ownership: dict, threshold=1.5):
    moves = []
    for pid, prev_pct in previous_ownership.items():
        p = kb.players.get(pid)
        if not p:
            continue
        delta = p.selected_by_percent - prev_pct
        if abs(delta) >= threshold:
            direction = "rising fast (consider buying before a price rise)" if delta > 0 else "falling fast (consider selling before a price drop)"
            moves.append({"player": p.web_name, "ownership_delta": round(delta, 2), "direction": direction})
    return moves


def log_gameweek(kb, event, recommendation, actual_points_by_player):
    captain_id = recommendation.get("captain", {}).get("captain_id")
    captain_points = actual_points_by_player.get(captain_id, 0) if captain_id else 0
    starting_ids = recommendation.get("lineup", {}).get("starting_ids", [])
    best_id = max(starting_ids, key=lambda pid: actual_points_by_player.get(pid, 0)) if starting_ids else None
    entry = {
        "event": event, "recommendation": recommendation, "captain_actual_points": captain_points,
        "was_top_scoring_captain_choice": (recommendation.get("captain", {}).get("captain_id") == best_id) if best_id else None,
    }
    kb.decision_log.append(entry)
    return entry


def season_summary(kb):
    log = kb.decision_log
    if not log:
        return {"gameweeks_logged": 0}
    correct = sum(1 for e in log if e.get("was_top_scoring_captain_choice"))
    return {"gameweeks_logged": len(log), "captain_hit_rate": round(correct / len(log), 2)}


# ============================================================
# SENATE — 6 specialist reviewers: approve / caution / veto
# ============================================================
APPROVE, CAUTION, VETO = "approve", "caution", "veto"


@dataclass
class Verdict:
    senator: str
    status: str
    reason: str
    flagged_ids: tuple = ()


def _risk_senator(context, kb):
    flagged = [kb.players[pid] for pid in context.get("player_ids", []) if pid in kb.players and not kb.players[pid].is_nailed]
    if not flagged:
        return Verdict("Risk", APPROVE, "No rotation/injury/fitness doubts detected.")
    status = VETO if len(flagged) >= 3 else CAUTION
    return Verdict("Risk", status, f"Fitness/rotation doubt on: {', '.join(p.web_name for p in flagged)}.", tuple(p.id for p in flagged))


def _value_senator(context, kb):
    player_ids = context.get("player_ids", [])
    if not player_ids:
        return Verdict("Value", APPROVE, "Nothing to assess.")
    scores = [value_score(kb.players[pid], kb) for pid in player_ids if pid in kb.players]
    avg = sum(scores) / len(scores) if scores else 0
    if avg < 0.3:
        return Verdict("Value", CAUTION, f"Squad's points-per-£m average ({avg:.2f}) looks low.")
    return Verdict("Value", APPROVE, f"Points-per-£m average is healthy ({avg:.2f}).")


def _fixture_senator(context, kb):
    bad = [kb.players[pid].web_name for pid in context.get("player_ids", []) if pid in kb.players and fixture_score(kb.players[pid].team, kb, num_gws=3) < 2.5]
    if len(bad) >= 4:
        return Verdict("Fixture", CAUTION, f"Several picks face a tough near-term run: {', '.join(bad[:4])}.")
    return Verdict("Fixture", APPROVE, "Fixture spread looks reasonable over the next few gameweeks.")


def _contrarian_senator(context, kb):
    player_ids = context.get("player_ids", [])
    high_owned = [kb.players[pid].web_name for pid in player_ids if pid in kb.players and kb.players[pid].selected_by_percent >= 45]
    if len(high_owned) >= 8:
        return Verdict("Contrarian", CAUTION, f"Squad is very template ({len(high_owned)} picks over 45% owned) - fine for rank protection, won't help you climb a mini-league.")
    return Verdict("Contrarian", APPROVE, "Reasonable balance of template and differential picks.")


def _consistency_senator(context, kb):
    if not kb.strategy.get("template_defence"):
        return Verdict("Consistency", APPROVE, "No defence template preference set.")
    player_ids = context.get("player_ids", [])
    defs = [kb.players[pid] for pid in player_ids if pid in kb.players and kb.players[pid].element_type == 2]
    if defs and all(p.selected_by_percent < 20 for p in defs):
        return Verdict("Consistency", CAUTION, "You said you want a template defence, but every defender picked is a low-owned differential.")
    return Verdict("Consistency", APPROVE, "Consistent with your stated strategy.")


def _community_senator(context, kb):
    if not kb.community_signals:
        return Verdict("Community", APPROVE, "No community intel loaded yet.")
    player_ids = context.get("player_ids", [])
    avoid_names, gem_names = [], []
    for pid in player_ids:
        p = kb.players.get(pid)
        if not p:
            continue
        matches = consensus_for_player(kb, p.web_name)
        if any(s.signal_type == "avoid" for s in matches):
            avoid_names.append(p.web_name)
        if any(s.signal_type == "hidden_gem" for s in matches):
            gem_names.append(p.web_name)
    if avoid_names:
        status = VETO if len(avoid_names) >= 2 else CAUTION
        flagged_ids = tuple(pid for pid in player_ids if kb.players.get(pid) and kb.players[pid].web_name in avoid_names)
        return Verdict("Community", status, f"Community consensus flags concerns on: {', '.join(avoid_names)}.", flagged_ids)
    if gem_names:
        return Verdict("Community", APPROVE, f"Nice - {', '.join(gem_names)} lines up with community differential picks.")
    return Verdict("Community", APPROVE, "No community red flags on these picks.")


SENATORS = [_risk_senator, _value_senator, _fixture_senator, _contrarian_senator, _consistency_senator, _community_senator]


def review(context, kb) -> List[Verdict]:
    return [senator(context, kb) for senator in SENATORS]


def has_veto(verdicts: List[Verdict]) -> bool:
    return any(v.status == VETO for v in verdicts)


def cautions(verdicts: List[Verdict]) -> List[Verdict]:
    return [v for v in verdicts if v.status == CAUTION]


# ============================================================
# ORCHESTRATOR — Poch. The one thing you actually talk to.
# ============================================================
class Poch:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb

    def build_squad(self, num_gws=5, max_retries=1):
        exclude = set()
        for attempt in range(max_retries + 1):
            proposal = build_squad(self.kb, num_gws=num_gws, exclude_ids=exclude)
            errors = validate_squad(proposal["player_ids"], self.kb)
            if errors:
                return {"error": "Rules Validator rejected squad", "details": errors}
            verdicts = review({"player_ids": proposal["player_ids"]}, self.kb)
            if has_veto(verdicts) and attempt < max_retries:
                for v in verdicts:
                    if v.status == VETO:
                        exclude |= set(v.flagged_ids)
                continue
            self.kb.your_squad = proposal["player_ids"]
            return {"squad": proposal, "senate_verdicts": [v.__dict__ for v in verdicts], "cautions": [v.reason for v in cautions(verdicts)]}

    def pick_lineup(self, num_gws=1):
        lineup = pick_lineup(self.kb, num_gws=num_gws)
        if not lineup:
            return {"error": "Could not find a valid lineup from current squad."}
        errors = validate_lineup(lineup["starting_ids"], self.kb)
        if errors:
            return {"error": "Rules Validator rejected lineup", "details": errors}
        verdicts = review({"player_ids": lineup["starting_ids"]}, self.kb)
        captain = suggest_captain(lineup["starting_ids"], self.kb, num_gws=num_gws)
        chips = chip_advice(self.kb, lineup)
        return {"lineup": lineup, "captain": captain, "chip_advice": chips,
                "senate_verdicts": [v.__dict__ for v in verdicts], "cautions": [v.reason for v in cautions(verdicts)]}

    def community_recommendations(self):
        if not self.kb.community_signals:
            return {"error": "No community intel loaded yet - call ingest_signals(kb, [...]) first."}
        return {
            "must_haves": [s.__dict__ for s in must_haves(self.kb)],
            "hidden_gems": [s.__dict__ for s in hidden_gems(self.kb)],
            "enablers": [s.__dict__ for s in enablers(self.kb)],
            "gw_strategy": [s.__dict__ for s in gw_strategy_notes(self.kb)],
            "avoids": [s.__dict__ for s in avoids(self.kb)],
        }

    def suggest_transfers(self, num_gws=5, top_n=3):
        proposals = suggest_weekly(self.kb, num_gws=num_gws, top_n=top_n)
        reviewed = []
        for prop in proposals:
            in_id = next((pid for pid, p in self.kb.players.items() if p.web_name == prop["player_in"]), None)
            verdicts = review({"player_ids": [in_id]}, self.kb) if in_id else []
            if has_veto(verdicts):
                continue
            prop["cautions"] = [v.reason for v in cautions(verdicts)]
            reviewed.append(prop)
        return reviewed

    def evaluate_transfer(self, player_out_id, player_in_id, num_gws=5):
        result = evaluate_transfer(player_out_id, player_in_id, self.kb, num_gws=num_gws)
        if "error" in result:
            return result
        verdicts = review({"player_ids": [player_in_id]}, self.kb)
        result["senate_verdicts"] = [v.__dict__ for v in verdicts]
        result["cautions"] = [v.reason for v in cautions(verdicts)]
        if has_veto(verdicts):
            result["recommend"] = False
            result["veto_reason"] = next(v.reason for v in verdicts if v.status == VETO)
        return result


# ============================================================
# CONVENIENCE LOADER — pulls live data (works in Colab; this
# sandbox has no internet, so use the mock demo below instead)
# ============================================================
def load_live_knowledge_base(team_id: Optional[int] = None):
    client = FPLClient()
    kb = KnowledgeBase()
    kb.load_from_bootstrap(client.get_bootstrap())
    kb.load_fixtures(client.get_fixtures())
    if team_id and kb.current_event:
        try:
            picks = client.get_entry_picks(team_id, kb.current_event)
            kb.your_squad = [p["element"] for p in picks["picks"]]
            kb.bank = picks["entry_history"]["bank"] / 10
            kb.free_transfers = picks.get("entry_history", {}).get("event_transfers", 1)
        except requests.exceptions.HTTPError:
            print(
                f"Note: FPL doesn't expose picks for gameweek {kb.current_event} yet "
                "(this is normal before that gameweek's deadline has passed). "
                "Loaded live prices/fixtures only - set kb.your_squad yourself, "
                "e.g. with set_squad_by_names(kb, [...]) below."
            )
    return kb


def set_squad_by_names(kb, web_names: List[str]):
    """
    Set your squad using player web_names instead of raw ids - handy when
    load_live_knowledge_base() couldn't pull your picks automatically (e.g.
    before a gameweek deadline has passed - see load_live_knowledge_base).
    Names must match FPL's short display name (e.g. "Haaland", "B.Fernandes").
    Prints anything it couldn't match so you can fix a typo or try the
    player's other known name.
    """
    by_name = {p.web_name.lower(): p.id for p in kb.players.values()}
    ids, missing = [], []
    for name in web_names:
        pid = by_name.get(name.lower())
        if pid is None:
            missing.append(name)
        else:
            ids.append(pid)
    if missing:
        print(f"Could not find: {missing} - check spelling against kb.players[...].web_name")
    kb.your_squad = ids
    return ids


# ============================================================
# PLAIN-ENGLISH OUTPUT — turns the raw dicts above into readable
# text, same shape as what you'd see in a chat conversation with
# Poch. Use these instead of print(poch.pick_lineup()) directly.
# ============================================================
def explain(result, kb):
    """Auto-detects which kind of result you've passed in and prints
    it readably. Works with pick_lineup(), suggest_transfers(),
    community_recommendations(), build_squad(), and evaluate_transfer()."""
    if "error" in result:
        print(f"Couldn't do that: {result['error']}")
        if "details" in result:
            for d in result["details"]:
                print(f"  - {d}")
        return
    if "lineup" in result:
        _explain_lineup(result, kb)
    elif "squad" in result:
        _explain_squad(result, kb)
    elif "must_haves" in result:
        _explain_community(result)
    elif isinstance(result, list):
        _explain_transfers(result)
    elif "player_out" in result:
        _explain_single_transfer(result)
    else:
        print(result)


def _name(kb, pid):
    p = kb.players.get(pid)
    return p.web_name if p else f"#{pid}"


def _explain_lineup(result, kb):
    lu = result["lineup"]
    print(f"Formation: {lu['formation']}  (projected {lu['total_projected']} pts)\n")
    print("Starting XI:")
    for pid in lu["starting_ids"]:
        print(f"  - {_name(kb, pid)}")
    print("\nBench (in order):")
    for pid in lu["bench_ids"]:
        print(f"  - {_name(kb, pid)}")
    cap = result.get("captain")
    if cap:
        print(f"\nCaptain: {cap['captain_name']}  |  Vice-captain: {cap['vice_name']}")
    if result.get("chip_advice"):
        print("\nChip advice:")
        for c in result["chip_advice"]:
            print(f"  - {c['chip']}: {c['reason']}")
    if result.get("cautions"):
        print("\nSenate cautions:")
        for c in result["cautions"]:
            print(f"  - {c}")
    else:
        print("\nSenate: no cautions raised.")


def _explain_squad(result, kb):
    sq = result["squad"]
    print(f"Squad ({len(sq['player_ids'])} players), bank remaining £{sq['bank_remaining']}m, "
          f"projected {sq['total_projected']} pts:\n")
    for pid in sq["player_ids"]:
        p = kb.players[pid]
        print(f"  - {p.web_name} ({p.position}, £{p.cost_m}m)")
    if result.get("cautions"):
        print("\nSenate cautions:")
        for c in result["cautions"]:
            print(f"  - {c}")
    else:
        print("\nSenate: no cautions raised.")


def _explain_transfers(proposals):
    if not proposals:
        print("No transfers currently clear the senate with a positive net gain.")
        return
    print("Suggested transfers:\n")
    for t in proposals:
        hit = " (after a -4 hit)" if t["hit_applies"] else ""
        print(f"  OUT: {t['player_out']}  ->  IN: {t['player_in']}")
        print(f"    net gain: {t['net_gain_after_hit']} pts{hit}, cost change: £{t['cost_delta_m']}m")
        for c in t.get("cautions", []):
            print(f"    caution: {c}")
        print()


def _explain_single_transfer(t):
    verdict = "RECOMMENDED" if t.get("recommend") else "NOT recommended"
    print(f"{t['player_out']} -> {t['player_in']}: {verdict}")
    print(f"  projected gain: {t['projected_gain_over_horizon']} pts, net after hit: {t['net_gain_after_hit']}")
    print(f"  cost change: £{t['cost_delta_m']}m")
    if t.get("veto_reason"):
        print(f"  senate veto: {t['veto_reason']}")
    for c in t.get("cautions", []):
        print(f"  caution: {c}")


def _explain_community(rec):
    def _section(title, items):
        print(f"\n{title}:")
        if not items:
            print("  (none loaded yet)")
        for s in items:
            print(f"  - {s['subject']}: {s['summary']}  ({s['source_count']} source(s))")
    _section("Must-haves", rec["must_haves"])
    _section("Hidden gems", rec["hidden_gems"])
    _section("Enablers", rec["enablers"])
    _section("Gameweek strategy", rec["gw_strategy"])
    _section("Avoid", rec["avoids"])


# ============================================================
# RICH (MARKDOWN) OUTPUT — renders as actual formatted text in
# Colab (bold, headers, tables) instead of plain print() lines.
# Use explain_md(...) in place of explain(...) for this.
# ============================================================
def explain_md(result, kb):
    """Same auto-detection as explain(), but renders as formatted
    Markdown in the Colab cell output (bold, headers, a table for
    the lineup) instead of plain text. Requires IPython, which is
    already available in every Colab notebook - no install needed."""
    from IPython.display import display, Markdown

    if "error" in result:
        lines = [f"**Couldn't do that:** {result['error']}"]
        for d in result.get("details", []):
            lines.append(f"- {d}")
        display(Markdown("\n".join(lines)))
        return

    if "lineup" in result:
        md = _md_lineup(result, kb)
    elif "squad" in result:
        md = _md_squad(result, kb)
    elif "must_haves" in result:
        md = _md_community(result)
    elif isinstance(result, list):
        md = _md_transfers(result)
    elif "player_out" in result:
        md = _md_single_transfer(result)
    else:
        md = f"```\n{result}\n```"
    display(Markdown(md))


def _md_lineup(result, kb):
    lu = result["lineup"]
    lines = [f"### Gameweek lineup — {lu['formation']} ({lu['total_projected']} pts projected)", ""]
    lines.append("| Starting XI |")
    lines.append("|---|")
    for pid in lu["starting_ids"]:
        lines.append(f"| {_name(kb, pid)} |")
    lines.append("")
    lines.append("**Bench (in order):** " + ", ".join(_name(kb, pid) for pid in lu["bench_ids"]))
    cap = result.get("captain")
    if cap:
        lines.append("")
        lines.append(f"**Captain:** {cap['captain_name']}  |  **Vice-captain:** {cap['vice_name']}")
    if result.get("chip_advice"):
        lines.append("")
        lines.append("**Chip advice:**")
        for c in result["chip_advice"]:
            lines.append(f"- {c['chip']}: {c['reason']}")
    lines.append("")
    if result.get("cautions"):
        lines.append("**Senate cautions:**")
        for c in result["cautions"]:
            lines.append(f"- {c}")
    else:
        lines.append("*Senate: no cautions raised.*")
    return "\n".join(lines)


def _md_squad(result, kb):
    sq = result["squad"]
    lines = [f"### Squad — {len(sq['player_ids'])} players, "
             f"£{sq['bank_remaining']}m in the bank ({sq['total_projected']} pts projected)", ""]
    lines.append("| Player | Position | Price |")
    lines.append("|---|---|---|")
    for pid in sq["player_ids"]:
        p = kb.players[pid]
        lines.append(f"| {p.web_name} | {p.position} | £{p.cost_m}m |")
    lines.append("")
    if result.get("cautions"):
        lines.append("**Senate cautions:**")
        for c in result["cautions"]:
            lines.append(f"- {c}")
    else:
        lines.append("*Senate: no cautions raised.*")
    return "\n".join(lines)


def _md_transfers(proposals):
    if not proposals:
        return "*No transfers currently clear the senate with a positive net gain.*"
    lines = ["### Suggested transfers", "", "| Out | In | Net gain | Cost change |", "|---|---|---|---|"]
    for t in proposals:
        hit = " (after -4 hit)" if t["hit_applies"] else ""
        lines.append(f"| {t['player_out']} | {t['player_in']} | {t['net_gain_after_hit']} pts{hit} | £{t['cost_delta_m']}m |")
    cautions_present = [c for t in proposals for c in t.get("cautions", [])]
    if cautions_present:
        lines.append("")
        lines.append("**Senate cautions:**")
        for c in cautions_present:
            lines.append(f"- {c}")
    return "\n".join(lines)


def _md_single_transfer(t):
    verdict = "**Recommended**" if t.get("recommend") else "**Not recommended**"
    lines = [f"### {t['player_out']} → {t['player_in']}: {verdict}", "",
             f"Projected gain: {t['projected_gain_over_horizon']} pts "
             f"(net after hit: {t['net_gain_after_hit']}) | Cost change: £{t['cost_delta_m']}m"]
    if t.get("veto_reason"):
        lines.append(f"\n**Senate veto:** {t['veto_reason']}")
    for c in t.get("cautions", []):
        lines.append(f"- caution: {c}")
    return "\n".join(lines)


def _md_community(rec):
    def _section(title, items):
        out = [f"**{title}:**"]
        if not items:
            out.append("*(none loaded yet)*")
        for s in items:
            out.append(f"- {s['subject']}: {s['summary']} ({s['source_count']} source(s))")
        return out
    lines = ["### Community intel", ""]
    for title, key in [("Must-haves", "must_haves"), ("Hidden gems", "hidden_gems"),
                        ("Enablers", "enablers"), ("Gameweek strategy", "gw_strategy"), ("Avoid", "avoids")]:
        lines += _section(title, rec[key])
        lines.append("")
    return "\n".join(lines)


# The offline synthetic-data demo that used to live here (for running this
# file standalone with zero network access, e.g. in a sandboxed notebook)
# has been removed in the app version — poch_app.py is the entry point now.