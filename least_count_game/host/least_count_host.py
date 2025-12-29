import json
import math
import os
import queue
import random
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pygame

# --- Paths / assets ---
BASE_DIR = Path(__file__).resolve().parents[1]  # /least_count_game
ASSETS_DIR = BASE_DIR / "assets" / "cards"

pygame.init()
BASE_SIZE = (1280, 720)  # virtual canvas; we scale to window size
WINDOWED_SIZE = BASE_SIZE
_is_fullscreen = False
window = pygame.display.set_mode(WINDOWED_SIZE, pygame.RESIZABLE)
screen = pygame.Surface(BASE_SIZE)  # draw everything here, then scale to window
pygame.display.set_caption("Raspberry Pi Gaming Hub (Host)")
clock = pygame.time.Clock()


def to_canvas(pos: tuple[int, int]) -> tuple[int, int]:
    wx, wy = pos
    win_w, win_h = window.get_size()
    if win_w <= 0 or win_h <= 0:
        return (0, 0)
    cx = int(wx * BASE_SIZE[0] / win_w)
    cy = int(wy * BASE_SIZE[1] / win_h)
    return (max(0, min(BASE_SIZE[0] - 1, cx)), max(0, min(BASE_SIZE[1] - 1, cy)))


def mouse_canvas_pos() -> tuple[int, int]:
    return to_canvas(pygame.mouse.get_pos())

FONT = pygame.font.SysFont("dejavusans", 26)
FONT_SM = pygame.font.SysFont("dejavusans", 18)
FONT_LG = pygame.font.SysFont("dejavusans", 44)
FONT_XS = pygame.font.SysFont("dejavusans", 16)


_image_cache: dict[str, pygame.Surface] = {}


def load_card_image(card_name: str) -> pygame.Surface:
    # Cache + robust pathing regardless of current working directory.
    key = f"{card_name}.png"
    if key in _image_cache:
        return _image_cache[key]
    img = pygame.image.load(str(ASSETS_DIR / key)).convert_alpha()
    _image_cache[key] = img
    return img


def card_sort_key(card: str) -> tuple[int, int]:
    """
    Ascending: jokers, A, 2..10, J, Q, K; then suit.
    Jokers are always zero-count.
    """
    # Designated joker is by rank (e.g. all Queens if joker is any Q*).
    face = card[:-1] if len(card) > 1 else card
    if (joker_rank is not None and face == joker_rank) or card in ("ZB", "ZR"):
        rank = 0
        suit = 0
    else:
        suit_char = card[-1]
        suit_order = {"S": 0, "C": 1, "D": 2, "H": 3}
        suit = suit_order.get(suit_char, 9)
        if face == "A":
            rank = 1
        elif face == "J":
            rank = 11
        elif face == "Q":
            rank = 12
        elif face == "K":
            rank = 13
        else:
            try:
                rank = int(face)
            except Exception:
                rank = 99
    return (rank, suit)


def sort_hand(pid: int) -> None:
    hands[pid] = sorted(hands.get(pid, []), key=card_sort_key)


def send_json(sock: socket.socket, payload: dict) -> None:
    # Newline-delimited JSON to avoid TCP message boundary bugs.
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    sock.sendall(data)


def safe_peer(sock: socket.socket) -> str:
    try:
        host, port = sock.getpeername()
        return f"{host}:{port}"
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class Button:
    rect: pygame.Rect
    label: str


def draw_button(btn: Button, enabled: bool = True) -> None:
    mouse = mouse_canvas_pos()
    hovering = btn.rect.collidepoint(mouse)
    base = (50, 160, 95) if enabled else (70, 70, 70)
    hover = (60, 190, 110) if enabled else (70, 70, 70)
    color = hover if hovering else base

    # Shadow + rounded rect
    shadow = btn.rect.move(0, 3)
    pygame.draw.rect(screen, (0, 0, 0), shadow, border_radius=10)
    pygame.draw.rect(screen, color, btn.rect, border_radius=10)
    pygame.draw.rect(screen, (255, 255, 255), btn.rect, width=2, border_radius=10)

    text = FONT.render(btn.label, True, (10, 10, 10))
    screen.blit(text, text.get_rect(center=btn.rect.center))


def draw_panel(rect: pygame.Rect, title: str | None = None) -> None:
    pygame.draw.rect(screen, (0, 0, 0), rect.move(0, 4), border_radius=14)
    pygame.draw.rect(screen, (25, 28, 35), rect, border_radius=14)
    pygame.draw.rect(screen, (80, 90, 110), rect, width=2, border_radius=14)
    if title:
        t = FONT.render(title, True, (230, 235, 245))
        screen.blit(t, (rect.x + 14, rect.y + 12))


class TextInput:
    def __init__(self, rect: pygame.Rect, value: str = "") -> None:
        self.rect = rect
        self.value = value
        self.active = False
        self._replace_on_next_key = False

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            clicked = self.rect.collidepoint(to_canvas(event.pos))
            # If the user clicks into the box, make it easy to replace the entire value.
            if clicked and not self.active:
                self._replace_on_next_key = True
            self.active = clicked
        if event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_BACKSPACE:
                self.value = self.value[:-1]
                self._replace_on_next_key = False
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.active = False
                self._replace_on_next_key = False
            else:
                if len(self.value) < 6 and event.unicode and event.unicode.isprintable():
                    if event.unicode.isdigit():
                        if self._replace_on_next_key:
                            self.value = event.unicode
                            self._replace_on_next_key = False
                        else:
                            self.value += event.unicode

    def draw(self, label: str) -> None:
        pygame.draw.rect(screen, (0, 0, 0), self.rect.move(0, 3), border_radius=10)
        pygame.draw.rect(screen, (240, 245, 255) if self.active else (220, 225, 235), self.rect, border_radius=10)
        pygame.draw.rect(screen, (30, 30, 30), self.rect, width=2, border_radius=10)
        lab = FONT_SM.render(label, True, (230, 235, 245))
        screen.blit(lab, (self.rect.x, self.rect.y - 22))
        txt = FONT.render(self.value or "", True, (10, 10, 10))
        screen.blit(txt, (self.rect.x + 10, self.rect.y + 10))


# --- Networking / game state ---
HOST_ID = 1
PORT = 5000

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", PORT))
server.listen(8)

connections: dict[int, socket.socket] = {}
player_names: dict[int, str] = {}
incoming: "queue.Queue[tuple[int, dict]]" = queue.Queue()

hands: dict[int, list[str]] = {HOST_ID: []}
discard_pile: list[str] = []
turn_order: list[int] = []
current_turn_idx = 0
game_started = False
deck: list[str] = []
turn_phase: dict[int, str] = {}  # pid -> "draw" | "discard"

# Match / round rules
HAND_SIZE = 7
SHOW_LIMIT = 8
SHOW_PENALTY = 40
max_points_out = 200  # configurable: out if score > max_points_out (e.g. 200 -> out at 201)

round_no = 0
joker_card: str | None = None  # designated joker for the round (0 points)
joker_rank: str | None = None  # e.g. "Q" when joker_card is any "Q*"
scores_total: dict[int, int] = {HOST_ID: 0}
eliminated: set[int] = set()
round_over = False
last_round_summary: dict | None = None
show_available: dict[int, bool] = {}  # only true at start of the player's turn (before any action)
# For "discard first, but still take the previously-open discard card":
# we remember the discard-top that was open at the *start* of the player's turn.
turn_open_discard: dict[int, dict] = {}  # pid -> {"idx": int, "card": str} or {}
round_history: list[dict] = []  # list of {"round_no": int, "round_points": {pid:int}, "show_pid": int, "outcome": str}

# Scoreboard scrolling (round rows only)
scroll_rows_from_bottom = 0

# Match end
match_over = False
match_winner: int | None = None
match_end_reason: str | None = None  # "points" | "player_exit" | "host_closed"
dealer_pid: int = HOST_ID
player_order: list[int] = [HOST_ID]


def active_players() -> list[int]:
    # Anyone in scores_total who isn't eliminated.
    return [pid for pid in sorted(scores_total.keys()) if pid not in eliminated]


def take_open_discard_for_turn(pid: int) -> str | None:
    """
    For 'discard first, then pick' we allow picking the discard card that was open
    at the start of the player's turn (not the card they just discarded).
    """
    if not discard_pile:
        return None
    ref = turn_open_discard.get(pid, {})
    idx = ref.get("idx")
    ref_card = ref.get("card")
    if isinstance(idx, int) and isinstance(ref_card, str) and 0 <= idx < len(discard_pile) and discard_pile[idx] == ref_card:
        return discard_pile.pop(idx)
    if isinstance(ref_card, str):
        for j in range(len(discard_pile) - 1, -1, -1):
            if discard_pile[j] == ref_card:
                return discard_pile.pop(j)
    return discard_pile.pop()


def num_decks_for_players(n_players: int) -> int:
    # Number of decks:
    # 2 - for players <5
    # 3 - for players 5 to 6
    # 4 - for players 7 to 8
    if n_players < 5:
        return 2
    if n_players <= 6:
        return 3
    return 4


STATE_MENU = "menu"
STATE_LOBBY = "lobby"
STATE_PLAYING = "playing"
STATE_RESULTS = "results"
state = STATE_MENU

status_line = "Select a game to host."
last_results: dict | None = None

# UI layout
btn_game_least = Button(pygame.Rect(60, 160, 320, 70), "Least Count")
btn_to_lobby = Button(pygame.Rect(60, 250, 320, 54), "Create Lobby")
btn_start = Button(pygame.Rect(60, 520, 320, 56), "Start Game")
btn_back_menu = Button(pygame.Rect(60, 520, 320, 56), "Back to Menu")
btn_next_round = Button(pygame.Rect(760, 540, 180, 52), "Next Round")

panel_left = pygame.Rect(40, 90, 360, 500)

# Scoreboard (tabular)
score_bar = pygame.Rect(20, 10, BASE_SIZE[0] - 40, 110)

# Game-table layout (full screen during play)
_pile_w, _pile_h = 160, 224
draw_pile_rect = pygame.Rect(BASE_SIZE[0] // 2 - 190, BASE_SIZE[1] // 2 - 120, _pile_w, _pile_h)
discard_rect = pygame.Rect(BASE_SIZE[0] // 2 + 30, BASE_SIZE[1] // 2 - 120, _pile_w, _pile_h)
btn_show = Button(pygame.Rect(BASE_SIZE[0] - 170, 128, 120, 36), "SHOW")
btn_next_round = Button(pygame.Rect(BASE_SIZE[0] - 220, BASE_SIZE[1] - 80, 180, 52), "Next Round")
btn_back_lobby = Button(pygame.Rect(BASE_SIZE[0] - 260, BASE_SIZE[1] - 80, 220, 52), "Back to Lobby")
btn_exit_host = Button(pygame.Rect(BASE_SIZE[0] - 150, 20, 130, 34), "Close Game")

CARD_W, CARD_H = 92, 138
HAND_Y = BASE_SIZE[1] - 190

# Double-click handling (discard action)
DOUBLE_CLICK_MS = 350
last_click_ms = 0
last_click_card: str | None = None

points_input = TextInput(pygame.Rect(60, 360, 180, 50), value=str(max_points_out))

def build_deck(num_decks: int = 1) -> list[str]:
    values = [str(v) for v in range(2, 11)] + ["J","Q","K","A"]
    suits = ["S","C","D","H"]
    single = [f"{v}{s}" for v in values for s in suits] + ["ZB", "ZR"]
    deck = single * max(1, int(num_decks))
    random.shuffle(deck)
    return deck


def card_points(card: str) -> int:
    # 0 points for jokers + designated joker rank for this round.
    face = card[:-1] if len(card) > 1 else card
    if (joker_rank is not None and face == joker_rank) or card in ("ZB", "ZR"):
        return 0
    if face == "A":
        return 1
    if face in ("J", "Q", "K"):
        return 10
    try:
        return int(face)
    except Exception:
        return 0


def hand_total(pid: int) -> int:
    return sum(card_points(c) for c in hands.get(pid, []))

def broadcast_lobby() -> None:
    player_list = [{"id": HOST_ID, "name": player_names.get(HOST_ID, "Host")}]
    for pid in sorted(connections.keys()):
        player_list.append({"id": pid, "name": player_names.get(pid, f"Player {pid}")})
    payload = {
        "action": "lobby",
        "players": player_list,
        "port": PORT,
        "config": {"max_points_out": max_points_out, "hand_size": HAND_SIZE},
        "dealer_pid": dealer_pid,
        "player_order": player_order,
    }
    for pid, conn in list(connections.items()):
        try:
            send_json(conn, payload)
        except Exception:
            pass


def sync_player_order() -> None:
    """
    Keep current custom order stable while syncing with connections.
    - remove players who disconnected
    - append new players at end (after host)
    """
    global player_order, dealer_pid
    current = set([HOST_ID] + list(connections.keys()))
    # prune missing, keep relative order
    player_order = [pid for pid in player_order if pid in current]
    if HOST_ID not in player_order:
        player_order.insert(0, HOST_ID)
    # append any new players
    for pid in sorted(current):
        if pid not in player_order:
            player_order.append(pid)
    if dealer_pid not in player_order:
        dealer_pid = player_order[0] if player_order else HOST_ID


def recompute_player_order() -> None:
    # Backwards-compat alias for older call sites.
    sync_player_order()


def send_hand(pid: int) -> None:
    if pid == HOST_ID:
        return
    conn = connections.get(pid)
    if not conn:
        return
    sort_hand(pid)
    try:
        send_json(conn, {"action": "hand", "hand": hands.get(pid, [])})
    except Exception:
        pass


def broadcast_state() -> None:
    current_pid = turn_order[current_turn_idx] if turn_order else None
    hand_counts = {pid: len(hands.get(pid, [])) for pid in turn_order}
    pick_discard_card = None
    if current_pid is not None:
        pick_discard_card = turn_open_discard.get(current_pid, {}).get("card")
    state_msg = {
        "discard_top": discard_pile[-1] if discard_pile else None,
        "turn": current_pid,
        "turn_phase": turn_phase.get(current_pid, "discard") if current_pid is not None else "discard",
        "show_enabled": show_available.get(current_pid, False) if current_pid is not None else False,
        "pick_discard_card": pick_discard_card,
        "deck_count": len(deck),
        "players": turn_order[:],
        "hand_counts": hand_counts,
        "player_names": {pid: player_names.get(pid, f"Player {pid}") for pid in scores_total.keys()},
        "round_history": round_history[-50:],
        "match_over": match_over,
        "match_winner": match_winner,
        "match_end_reason": match_end_reason,
        "dealer_pid": dealer_pid,
        "scores_total": scores_total,
        "eliminated": sorted(eliminated),
        "round_no": round_no,
        "joker_card": joker_card,
        "joker_rank": joker_rank,
        "round_over": round_over,
        "max_points_out": max_points_out,
    }
    for pid, conn in list(connections.items()):
        try:
            send_json(conn, {"action": "update", "state": state_msg})
        except Exception:
            pass


def start_match() -> None:
    global game_started, scores_total, eliminated, round_no, last_round_summary, match_over, match_winner, match_end_reason
    game_started = True
    last_round_summary = None
    round_no = 0
    eliminated = set()
    round_history.clear()
    match_over = False
    match_winner = None
    match_end_reason = None
    recompute_player_order()
    scores_total = {HOST_ID: 0}
    for pid in connections.keys():
        scores_total[pid] = 0
    start_round()


def start_round() -> None:
    global deck, discard_pile, turn_order, current_turn_idx, turn_phase, joker_card, joker_rank, round_no, round_over, last_round_summary, show_available, turn_open_discard, dealer_pid
    last_round_summary = None
    round_over = False
    round_no += 1
    turn_open_discard = {}

    # Determine active players in the configured order (skip eliminated)
    active_in_order = [pid for pid in player_order if pid in scores_total and pid not in eliminated]
    if not active_in_order:
        return

    # Rotate dealer each new round (after round 1)
    if round_no == 1:
        if dealer_pid not in active_in_order:
            dealer_pid = active_in_order[0]
    else:
        # advance to next active player in player_order
        cur = dealer_pid
        for _ in range(len(player_order) + 1):
            idx = (player_order.index(cur) + 1) % len(player_order) if cur in player_order else 0
            cand = player_order[idx]
            if cand in active_in_order:
                dealer_pid = cand
                break
            cur = cand

    # Turn order starts from player after dealer
    if dealer_pid in active_in_order:
        di = active_in_order.index(dealer_pid)
        turn_order = active_in_order[di + 1 :] + active_in_order[: di + 1]
    else:
        turn_order = active_in_order[:]

    # Number of decks depends on number of players in the round
    deck = build_deck(num_decks_for_players(len(turn_order)))
    discard_pile = []

    current_turn_idx = 0
    # Rule: discard first, then pick.
    turn_phase = {pid: "discard" for pid in turn_order}
    show_available = {pid: False for pid in turn_order}
    if turn_order:
        show_available[turn_order[0]] = True

    # Pick designated joker card for this round (actual card, 0 points for round).
    joker_card = deck.pop() if deck else None
    joker_rank = (joker_card[:-1] if joker_card and joker_card not in ("ZB", "ZR") else joker_card) if joker_card else None

    # Deal hands
    for pid in turn_order:
        hands[pid] = [deck.pop() for _ in range(HAND_SIZE)]
        sort_hand(pid)
        if pid != HOST_ID:
            send_hand(pid)

    # Start discard pile
    if deck:
        discard_pile.append(deck.pop())
    # Capture the open discard for the first player of the round (fixes first turn behavior)
    if turn_order and discard_pile:
        turn_open_discard[turn_order[0]] = {"idx": len(discard_pile) - 1, "card": discard_pile[-1]}

    # Inform clients
    for pid, conn in list(connections.items()):
        try:
            send_json(
                conn,
                {
                    "action": "start",
                    "rules": {
                        "hand_size": HAND_SIZE,
                        "show_limit": SHOW_LIMIT,
                        "show_penalty": SHOW_PENALTY,
                        "max_points_out": max_points_out,
                        "discard_first": True,
                        "dealer_pid": dealer_pid,
                    },
                },
            )
        except Exception:
            pass
        send_hand(pid)

    broadcast_state()

def recv_loop(conn: socket.socket, pid: int) -> None:
    # Blocking line reader keeps JSON framing simple and reliable.
    buf = ""
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    incoming.put((pid, json.loads(line)))
                except Exception:
                    continue
    finally:
        incoming.put((pid, {"action": "disconnect"}))


def accept_loop() -> None:
    next_pid = 2  # host is Player 1
    while True:
        conn, addr = server.accept()
        pid = next_pid
        next_pid += 1
        connections[pid] = conn
        player_names[pid] = f"Player {pid}"
        try:
            send_json(conn, {"action": "welcome", "player_id": pid})
        except Exception:
            pass
        print(f"Player {pid} connected from {addr}")
        threading.Thread(target=recv_loop, args=(conn, pid), daemon=True).start()
        recompute_player_order()
        broadcast_lobby()

def next_turn() -> None:
    global current_turn_idx
    if turn_order:
        current_turn_idx = (current_turn_idx + 1) % len(turn_order)
        pid = turn_order[current_turn_idx]
        # show is only available at the start of the turn
        show_available[pid] = True
        # remember which discard card was open at the start of this player's turn
        if discard_pile:
            turn_open_discard[pid] = {"idx": len(discard_pile) - 1, "card": discard_pile[-1]}
        else:
            turn_open_discard.pop(pid, None)

def end_game():
    global game_started, last_results, turn_phase
    scores = {pid: sum(card_value(c) for c in hands.get(pid, [])) for pid in hands}
    winner = min(scores, key=scores.get) if scores else None
    last_results = {"scores": scores, "winner": winner}

    for pid, conn in list(connections.items()):
        try:
            send_json(conn, {"action": "end", "scores": scores, "winner": winner})
        except Exception:
            pass
    game_started = False
    turn_phase = {}
    print("Game Over! Scores:", scores, "Winner:", winner)


def reset_to_lobby(message: str | None = None) -> None:
    global game_started, match_over, match_winner, match_end_reason, round_over, last_round_summary, turn_order, current_turn_idx, turn_phase, turn_open_discard
    game_started = False
    match_over = False
    match_winner = None
    match_end_reason = None
    round_over = False
    last_round_summary = None
    turn_order = []
    current_turn_idx = 0
    turn_phase = {}
    turn_open_discard = {}
    if message:
        global status_line
        status_line = message
    sync_player_order()
    state_to_lobby = globals().get("STATE_LOBBY")
    globals()["state"] = state_to_lobby
    broadcast_lobby()


def close_game_by_host() -> None:
    """
    Host closes the game: regardless of players remaining, end match with reason and disconnect everyone.
    """
    global game_started, match_over, match_winner, match_end_reason
    match_over = True
    match_winner = None
    match_end_reason = "host_closed"
    game_started = False
    for pid, conn in list(connections.items()):
        try:
            send_json(conn, {"action": "match_end", "winner": None, "reason": match_end_reason, "scores_total": scores_total})
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    connections.clear()
    sync_player_order()
    reset_to_lobby("Host closed the game.")

def resolve_show(show_pid: int) -> None:
    """
    Implements Least Count show rule:
    - allowed only if hand_total(show_pid) <= SHOW_LIMIT
    - if no other player has <= show_total -> show_pid gets 0, others get their totals
    - else show_pid gets SHOW_PENALTY; players with lowest total get 0; others get their totals
    Updates cumulative totals + elimination (> max_points_out) and marks round_over.
    """
    global round_over, last_round_summary

    if round_over:
        return
    if show_pid not in turn_order:
        return

    totals = {pid: hand_total(pid) for pid in turn_order}
    show_total = totals.get(show_pid, 999)
    if show_total > SHOW_LIMIT:
        return

    others = {pid: tot for pid, tot in totals.items() if pid != show_pid}
    same_or_less_players = [pid for pid, tot in others.items() if tot <= show_total]
    other_has_same_or_less = len(same_or_less_players) > 0

    round_points: dict[int, int] = {}
    if not other_has_same_or_less:
        round_points[show_pid] = 0
        for pid, tot in others.items():
            round_points[pid] = tot
        outcome = "win"
        min_players: list[int] = []
    else:
        round_points[show_pid] = SHOW_PENALTY
        min_other = min(others.values()) if others else show_total
        min_players = [pid for pid, tot in others.items() if tot == min_other]
        for pid, tot in others.items():
            round_points[pid] = 0 if tot == min_other else tot
        outcome = "penalty"

    for pid, pts in round_points.items():
        scores_total[pid] = int(scores_total.get(pid, 0)) + int(pts)

    newly_out: list[int] = []
    for pid, total_pts in scores_total.items():
        if pid in eliminated:
            continue
        if total_pts > max_points_out:
            eliminated.add(pid)
            newly_out.append(pid)

    last_round_summary = {
        "round_no": round_no,
        "joker_card": joker_card,
        "joker_rank": joker_rank,
        "show_pid": show_pid,
        "show_total": show_total,
        "totals": totals,
        "round_points": round_points,
        "scores_total": scores_total,
        "eliminated": sorted(eliminated),
        "outcome": outcome,
        "same_or_less_players": same_or_less_players,
        "min_players": min_players,
        "newly_out": newly_out,
    }
    round_history.append(
        {
            "round_no": round_no,
            "round_points": round_points,
            "show_pid": show_pid,
            "outcome": outcome,
        }
    )
    round_over = True

    # Match end: if only one active player remains, declare winner and end match.
    global match_over, match_winner, game_started, match_end_reason
    remaining = active_players()
    if len(remaining) == 1:
        match_over = True
        match_winner = remaining[0]
        match_end_reason = "points"
        game_started = False
        for pid, conn in list(connections.items()):
            try:
                send_json(conn, {"action": "match_end", "winner": match_winner, "reason": match_end_reason, "scores_total": scores_total})
            except Exception:
                pass
        broadcast_state()
        return

    # Tell clients round ended
    for pid, conn in list(connections.items()):
        try:
            send_json(conn, {"action": "round_end", "summary": last_round_summary})
        except Exception:
            pass
    broadcast_state()

threading.Thread(target=accept_loop, daemon=True).start()

running = True
while running:
    # --- Process incoming network messages ---
    while True:
        try:
            pid, data = incoming.get_nowait()
        except queue.Empty:
            break

        action = data.get("action")
        if action == "disconnect":
            conn = connections.pop(pid, None)
            player_names.pop(pid, None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            # If this happens during a game, treat as player exiting the match.
            if game_started and pid in scores_total:
                eliminated.add(pid)
                # Remove from current turn order if present
                if pid in turn_order:
                    idx = turn_order.index(pid)
                    turn_order = [p for p in turn_order if p != pid]
                    # If they were before current index, adjust
                    if idx <= current_turn_idx and current_turn_idx > 0:
                        current_turn_idx -= 1
                    if current_turn_idx >= len(turn_order):
                        current_turn_idx = 0
                # Determine if match ends due to exit
                remaining = active_players()
                if len(remaining) == 1:
                    match_over = True
                    match_winner = remaining[0]
                    match_end_reason = "player_exit"
                    game_started = False
                    for opid, oconn in list(connections.items()):
                        try:
                            send_json(oconn, {"action": "match_end", "winner": match_winner, "reason": match_end_reason, "scores_total": scores_total})
                        except Exception:
                            pass
                broadcast_state()
            else:
                recompute_player_order()
                broadcast_lobby()
            continue

        if action == "exit":
            # Explicit exit from a player; close socket and handle like disconnect.
            conn = connections.pop(pid, None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            incoming.put((pid, {"action": "disconnect"}))
            continue

        if action == "hello":
            name = str(data.get("name", "")).strip()
            if name:
                player_names[pid] = name[:18]
                if not game_started:
                    broadcast_lobby()
            continue

        if not game_started:
            continue

        if not turn_order:
            continue

        is_turn = pid == turn_order[current_turn_idx]
        phase = turn_phase.get(pid, "discard")
        if action == "discard" and is_turn and phase == "discard" and not round_over:
            card = data.get("card")
            if isinstance(card, str) and card in hands.get(pid, []):
                prev_top = discard_pile[-1] if discard_pile else None
                prev_top_face = prev_top[:-1] if prev_top and prev_top not in ("ZB", "ZR") else prev_top
                face = card[:-1] if len(card) > 1 else card
                removed = [c for c in hands.get(pid, []) if (c[:-1] if len(c) > 1 else c) == face]
                hands[pid] = [c for c in hands.get(pid, []) if (c[:-1] if len(c) > 1 else c) != face]
                sort_hand(pid)
                discard_pile.extend(removed)
                show_available[pid] = False  # show is no longer allowed after discard
                send_hand(pid)
                # Exception: if open card is same rank, player may discard without picking.
                if prev_top_face is not None and prev_top_face == face:
                    turn_phase[pid] = "discard"
                    next_turn()
                else:
                    turn_phase[pid] = "draw"
                broadcast_state()
        elif action == "draw_deck" and is_turn and phase == "draw" and not round_over:
            if deck:
                hands[pid].append(deck.pop())
                sort_hand(pid)
                send_hand(pid)
                turn_phase[pid] = "discard"
                show_available[pid] = False
                next_turn()
                broadcast_state()
        elif action == "draw_discard" and is_turn and phase == "draw" and not round_over:
            if discard_pile:
                card_to_take = take_open_discard_for_turn(pid)
                if card_to_take is not None:
                    hands[pid].append(card_to_take)
                sort_hand(pid)
                send_hand(pid)
                turn_phase[pid] = "discard"
                show_available[pid] = False
                next_turn()
                broadcast_state()
        elif action == "show" and is_turn and phase == "discard" and show_available.get(pid, False) and not round_over:
            resolve_show(pid)

    # --- Draw background ---
    screen.fill((14, 16, 20))
    # subtle felt
    pygame.draw.rect(screen, (10, 70, 45), pygame.Rect(0, 0, 980, 620))
    pygame.draw.rect(screen, (0, 0, 0), pygame.Rect(0, 0, 980, 620), width=6)

    # Header
    header = FONT_LG.render("Least Count (Host)", True, (245, 248, 255))
    screen.blit(header, (30, 12))
    sub = FONT_SM.render("Setup → Start → Play rounds until players are out", True, (200, 210, 225))
    screen.blit(sub, (34, 54))

    # Left panel content
    if state == STATE_MENU:
        draw_panel(panel_left, "Setup")
        screen.blit(FONT.render("Select a game to host:", True, (230, 235, 245)), (panel_left.x + 20, 130))
        draw_button(btn_game_least, enabled=True)
        points_input.draw("Out after points >")
        screen.blit(FONT_XS.render("(Example: 200 → out at 201)", True, (205, 215, 230)), (points_input.rect.x, points_input.rect.bottom + 6))
        draw_button(btn_to_lobby, enabled=True)
        screen.blit(FONT_SM.render(status_line, True, (210, 220, 235)), (panel_left.x + 18, panel_left.bottom - 36))

    elif state == STATE_LOBBY:
        draw_panel(panel_left, "Lobby")
        screen.blit(FONT.render("Lobby", True, (230, 235, 245)), (panel_left.x + 20, 110))
        screen.blit(
            FONT_SM.render(f"Listening on port {PORT}. Connected players:", True, (200, 210, 225)),
            (panel_left.x + 20, 146),
        )

        # Table setup: dealer + play order
        sync_player_order()
        screen.blit(FONT_SM.render("Dealer & turn order (edit before Start):", True, (210, 220, 235)), (panel_left.x + 20, 178))
        y = panel_left.y + 120
        row_h = 44
        start_y = 220

        for idx, pid in enumerate(player_order):
            ry = start_y + idx * row_h
            if ry + row_h > panel_left.bottom - 90:
                break
            name = "Host" if pid == HOST_ID else player_names.get(pid, f"Player {pid}")
            is_dealer = pid == dealer_pid
            # row background
            row_rect = pygame.Rect(panel_left.x + 16, ry, panel_left.width - 32, 38)
            pygame.draw.rect(screen, (30, 34, 42), row_rect, border_radius=10)
            pygame.draw.rect(screen, (90, 100, 120), row_rect, width=1, border_radius=10)

            # dealer toggle button
            d_rect = pygame.Rect(row_rect.x + 8, row_rect.y + 7, 24, 24)
            pygame.draw.rect(screen, (255, 235, 120) if is_dealer else (200, 210, 225), d_rect, border_radius=6)
            screen.blit(FONT_XS.render("D", True, (20, 20, 20)), (d_rect.x + 7, d_rect.y + 4))

            # up/down buttons
            up_rect = pygame.Rect(row_rect.right - 56, row_rect.y + 7, 22, 22)
            dn_rect = pygame.Rect(row_rect.right - 28, row_rect.y + 7, 22, 22)
            pygame.draw.rect(screen, (220, 225, 235), up_rect, border_radius=6)
            pygame.draw.rect(screen, (220, 225, 235), dn_rect, border_radius=6)
            pygame.draw.polygon(screen, (20, 20, 20), [(up_rect.centerx, up_rect.y + 5), (up_rect.x + 5, up_rect.bottom - 5), (up_rect.right - 5, up_rect.bottom - 5)])
            pygame.draw.polygon(screen, (20, 20, 20), [(dn_rect.centerx, dn_rect.bottom - 5), (dn_rect.x + 5, dn_rect.y + 5), (dn_rect.right - 5, dn_rect.y + 5)])

            # order index + name
            screen.blit(FONT_XS.render(f"{idx+1}.", True, (235, 240, 248)), (row_rect.x + 42, row_rect.y + 10))
            screen.blit(FONT.render(name, True, (245, 245, 245)), (row_rect.x + 70, row_rect.y + 6))
            if is_dealer:
                screen.blit(FONT_XS.render("Dealer", True, (255, 235, 120)), (row_rect.x + 70, row_rect.y + 22))

        can_start = True
        draw_button(btn_start, enabled=can_start)
        hint = "Tip: start with 1+ remote players, or play solo as Host."
        screen.blit(FONT_SM.render(hint, True, (205, 215, 230)), (panel_left.x + 18, panel_left.bottom - 36))

    elif state == STATE_PLAYING:
        # Full-screen game table (no half-screen panels)
        # Scoreboard: columns are player names, rows are round points (scrollable),
        # pinned bottom rows: Total + Cards (turn highlighted).
        pygame.draw.rect(screen, (0, 0, 0), score_bar.move(0, 3), border_radius=14)
        pygame.draw.rect(screen, (25, 28, 35), score_bar, border_radius=14)
        pygame.draw.rect(screen, (90, 100, 120), score_bar, width=2, border_radius=14)

        header_h = 26
        footer_h = 44
        row_h = 18
        inner = pygame.Rect(score_bar.x + 10, score_bar.y + 8, score_bar.width - 20, score_bar.height - 16)

        pids = [HOST_ID] + sorted([pid for pid in scores_total.keys() if pid != HOST_ID])
        col0_w = 80
        remaining = max(1, inner.width - col0_w)
        col_w = max(100, remaining // max(1, len(pids)))
        max_cols = max(1, min(len(pids), remaining // 100))
        pids = pids[:max_cols]

        # header row (pinned)
        header_rect = pygame.Rect(inner.x, inner.y, inner.width, header_h)
        pygame.draw.rect(screen, (18, 20, 26), header_rect, border_radius=10)
        screen.blit(FONT_XS.render(f"Round (Joker {joker_rank or '-'})", True, (210, 220, 235)), (header_rect.x + 8, header_rect.y + 6))

        active_pid = turn_order[current_turn_idx] if turn_order else None
        for i, pid in enumerate(pids):
            name = "Host" if pid == HOST_ID else player_names.get(pid, f"P{pid}")
            x = header_rect.x + col0_w + i * col_w
            col_rect = pygame.Rect(x, header_rect.y, col_w, header_h)
            if (pid == active_pid) and (not round_over):
                pygame.draw.rect(screen, (255, 235, 120), col_rect, border_radius=10)
                fg = (20, 20, 20)
            else:
                fg = (210, 220, 235)
            screen.blit(FONT_XS.render(str(name)[:10], True, fg), (x + 6, header_rect.y + 6))

        # scrollable rounds body
        body_rect = pygame.Rect(inner.x, inner.y + header_h, inner.width, inner.height - header_h - footer_h)
        pygame.draw.rect(screen, (0, 0, 0), body_rect, width=1, border_radius=10)
        rounds = list(round_history)
        max_visible = max(1, body_rect.height // row_h)
        start = max(0, len(rounds) - max_visible - scroll_rows_from_bottom)
        end = min(len(rounds), start + max_visible)
        y = body_rect.y
        for idx in range(start, end):
            entry = rounds[idx]
            rno = entry.get("round_no", idx + 1)
            pts_map = entry.get("round_points", {}) or {}
            screen.blit(FONT_XS.render(str(rno), True, (235, 240, 248)), (body_rect.x + 8, y + 2))
            for i, pid in enumerate(pids):
                pts = pts_map.get(pid, pts_map.get(str(pid), 0))
                x = body_rect.x + col0_w + i * col_w
                screen.blit(FONT_XS.render(str(pts), True, (235, 240, 248)), (x + 6, y + 2))
            y += row_h

        # footer (pinned): Total + Cards
        footer_rect = pygame.Rect(inner.x, inner.bottom - footer_h, inner.width, footer_h)
        pygame.draw.rect(screen, (18, 20, 26), footer_rect, border_radius=10)
        screen.blit(FONT_XS.render("Total", True, (210, 220, 235)), (footer_rect.x + 8, footer_rect.y + 4))
        for i, pid in enumerate(pids):
            total = scores_total.get(pid, 0)
            x = footer_rect.x + col0_w + i * col_w
            screen.blit(FONT_XS.render(str(total), True, (235, 240, 248)), (x + 6, footer_rect.y + 4))
        screen.blit(FONT_XS.render("Cards", True, (210, 220, 235)), (footer_rect.x + 8, footer_rect.y + 24))
        for i, pid in enumerate(pids):
            count = len(hands.get(pid, [])) if pid in turn_order else "-"
            x = footer_rect.x + col0_w + i * col_w
            txt = str(count)
            if (pid == active_pid) and (not round_over):
                txt = f"{txt} *"
            screen.blit(FONT_XS.render(txt, True, (235, 240, 248)), (x + 6, footer_rect.y + 24))

        # Turn hint
        turn = turn_order[current_turn_idx] if turn_order else None
        phase = turn_phase.get(turn, "discard") if turn is not None else "discard"
        hint = ""
        if not round_over and turn == HOST_ID:
            hint = "Double-click a card to discard (first)" if phase == "discard" else "Click deck/discard to pick (after discard)"
        elif round_over:
            hint = "Round over. Click Next Round (host) to continue."
        if hint:
            screen.blit(FONT_SM.render(hint, True, (255, 235, 120)), (30, score_bar.bottom + 10))

        # Make it obvious which discard card you'll take this turn
        if (not round_over) and (not match_over) and turn_order and (turn_order[current_turn_idx] == HOST_ID) and (phase == "draw"):
            pick_card = turn_open_discard.get(HOST_ID, {}).get("card")
            if pick_card:
                screen.blit(FONT_XS.render(f"Discard pick: {pick_card}", True, (210, 220, 235)), (30, score_bar.bottom + 34))

        # Show is allowed only at the start of your turn (before discard/pick), and only when <= show limit.
        can_show = (not round_over) and (turn == HOST_ID) and (phase == "discard") and show_available.get(HOST_ID, False) and (hand_total(HOST_ID) <= SHOW_LIMIT)
        draw_button(btn_show, enabled=can_show)
        # Control buttons: next round only when round_over and match not over.
        if match_over:
            draw_button(btn_back_lobby, enabled=True)
        elif round_over:
            draw_button(btn_next_round, enabled=True)
        draw_button(btn_exit_host, enabled=True)
        if match_over and match_winner is not None:
            msg = f"GAME OVER — Winner: {player_names.get(match_winner, f'Player {match_winner}')}"
            banner = pygame.Rect(180, score_bar.bottom + 6, BASE_SIZE[0] - 200, 34)
            pygame.draw.rect(screen, (0, 0, 0), banner.move(0, 2), border_radius=10)
            pygame.draw.rect(screen, (60, 40, 15), banner, border_radius=10)
            pygame.draw.rect(screen, (255, 235, 120), banner, width=2, border_radius=10)
            screen.blit(FONT_SM.render(msg, True, (245, 235, 200)), (banner.x + 10, banner.y + 8))
        elif round_over and last_round_summary:
            show_pid = last_round_summary.get("show_pid")
            show_total = last_round_summary.get("show_total")
            outcome = last_round_summary.get("outcome")
            if outcome == "win":
                msg = f"Player {show_pid} SHOWED {show_total} and WON (0 pts)."
            else:
                same_or_less = last_round_summary.get("same_or_less_players", [])
                msg = f"Player {show_pid} SHOWED {show_total} and got PENALTY (+{SHOW_PENALTY}). Same/less: {same_or_less}"
            banner = pygame.Rect(180, score_bar.bottom + 6, BASE_SIZE[0] - 200, 34)
            pygame.draw.rect(screen, (0, 0, 0), banner.move(0, 2), border_radius=10)
            pygame.draw.rect(screen, (40, 35, 20), banner, border_radius=10)
            pygame.draw.rect(screen, (130, 110, 70), banner, width=2, border_radius=10)
            screen.blit(FONT_SM.render(msg, True, (245, 235, 200)), (banner.x + 10, banner.y + 8))

        # Live-table: other players' hands face down + spotlight active player
        def _draw_seat(pid: int, rect: pygame.Rect, active_pid: int | None) -> None:
            is_active = (active_pid == pid) and (not round_over)
            if is_active:
                halo = pygame.Surface(rect.inflate(90, 50).size, pygame.SRCALPHA)
                pygame.draw.ellipse(halo, (255, 235, 120, 55), halo.get_rect())
                screen.blit(halo, halo.get_rect(center=rect.center))
                pygame.draw.rect(screen, (255, 235, 120), rect.inflate(16, 16), width=4, border_radius=14)
            pygame.draw.rect(screen, (0, 0, 0), rect.move(0, 3), border_radius=12)
            pygame.draw.rect(screen, (25, 28, 35), rect, border_radius=12)
            pygame.draw.rect(screen, (90, 100, 120), rect, width=2, border_radius=12)

            back_small = pygame.transform.smoothscale(load_card_image("CardBack"), (48, 68))
            count = len(hands.get(pid, []))
            stacks = min(count, 5)
            sx = rect.x + 10
            sy = rect.y + 10
            for i in range(stacks):
                screen.blit(back_small, (sx + i * 6, sy + i * 2))
            label = "Host" if pid == HOST_ID else f"P{pid}"
            screen.blit(FONT_XS.render(f"{label} ({count})", True, (235, 240, 248)), (rect.x + 10, rect.bottom - 20))

        active_pid = turn_order[current_turn_idx] if turn_order else None
        # Professional arc layout: other players on top arc (your own face-down seat is not shown).
        others = [pid for pid in turn_order if pid != HOST_ID]
        if others:
            arc_center_x = BASE_SIZE[0] // 2
            arc_top_y = score_bar.bottom + 20
            radius = 520
            start_deg = -62
            end_deg = 62
            if len(others) == 1:
                angles = [0.0]
            else:
                angles = [start_deg + (end_deg - start_deg) * (i / (len(others) - 1)) for i in range(len(others))]
            for pid, deg in zip(others, angles):
                theta = math.radians(deg)
                x = arc_center_x + radius * math.sin(theta)
                y = arc_top_y + int(110 * (1 - math.cos(theta)))
                seat = pygame.Rect(int(x - 70), int(y), 140, 80)
                _draw_seat(pid, seat, active_pid)

    elif state == STATE_RESULTS:
        screen.blit(FONT.render("Results", True, (230, 235, 245)), (panel_left.x + 20, 86))
        if last_results:
            winner = last_results.get("winner")
            scores = last_results.get("scores", {})
            y = panel_left.y + 140
            screen.blit(FONT.render(f"Winner: Player {winner}", True, (255, 235, 120)), (panel_left.x + 20, y))
            y += 44
            for pid, sc in sorted(scores.items(), key=lambda kv: kv[0]):
                screen.blit(FONT.render(f"Player {pid}: {sc}", True, (235, 240, 248)), (panel_left.x + 20, y))
                y += 34
        draw_button(btn_back_menu, enabled=True)

    # Game table (during play/results)
    if state in (STATE_PLAYING, STATE_RESULTS):
        # Draw pile
        pygame.draw.rect(screen, (0, 0, 0), draw_pile_rect.move(0, 4), border_radius=14)
        pygame.draw.rect(screen, (35, 40, 52), draw_pile_rect, border_radius=14)
        pygame.draw.rect(screen, (90, 100, 120), draw_pile_rect, width=2, border_radius=14)
        # Designated joker peeking under the deck (0 points for this round)
        peek_name = joker_card or "ZB"
        peek_joker = pygame.transform.smoothscale(load_card_image(peek_name), (92, 130))
        peek_joker = pygame.transform.rotate(peek_joker, -18)
        screen.blit(peek_joker, (draw_pile_rect.x + 10, draw_pile_rect.bottom - 78))
        back = pygame.transform.smoothscale(load_card_image("CardBack"), (120, 170))
        screen.blit(back, (draw_pile_rect.x + 15, draw_pile_rect.y + 18))

        # discard pile
        pygame.draw.rect(screen, (0, 0, 0), discard_rect.move(0, 4), border_radius=14)
        pygame.draw.rect(screen, (35, 40, 52), discard_rect, border_radius=14)
        pygame.draw.rect(screen, (90, 100, 120), discard_rect, width=2, border_radius=14)
        if discard_pile:
            top = discard_pile[-1]
            img = pygame.transform.smoothscale(load_card_image(top), (120, 170))
            screen.blit(img, (discard_rect.x + 15, discard_rect.y + 18))
        else:
            blank = pygame.transform.smoothscale(load_card_image("BlankCard"), (120, 170))
            screen.blit(blank, (discard_rect.x + 15, discard_rect.y + 18))

        # Host hand (sorted + overlapping)
        if HOST_ID in hands:
            sort_hand(HOST_ID)
        hand_cards = hands.get(HOST_ID, [])
        n = len(hand_cards)
        available = BASE_SIZE[0] - 60 - CARD_W
        step = 40 if n <= 1 else max(28, min(46, available // max(1, n - 1)))
        hx = 30
        hy = HAND_Y
        for i, card in enumerate(hand_cards):
            rect = pygame.Rect(hx + i * step, hy, CARD_W, CARD_H)
            img = pygame.transform.smoothscale(load_card_image(card), (CARD_W, CARD_H))
            screen.blit(img, rect.topleft)
            pygame.draw.rect(screen, (10, 10, 10), rect, width=2, border_radius=8)

    # --- Events ---
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_F11:
                _is_fullscreen = not _is_fullscreen
                if _is_fullscreen:
                    window = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
                else:
                    window = pygame.display.set_mode(WINDOWED_SIZE, pygame.RESIZABLE)
            elif event.key == pygame.K_ESCAPE and _is_fullscreen:
                _is_fullscreen = False
                window = pygame.display.set_mode(WINDOWED_SIZE, pygame.RESIZABLE)

        if event.type == pygame.VIDEORESIZE and not _is_fullscreen:
            window = pygame.display.set_mode(event.size, pygame.RESIZABLE)

        if event.type == pygame.MOUSEWHEEL and state == STATE_PLAYING:
            # scroll scoreboard round rows (up shows older rounds)
            scroll_rows_from_bottom = max(0, scroll_rows_from_bottom + (-event.y))

        if state == STATE_MENU:
            points_input.handle_event(event)

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            pos = to_canvas(event.pos)

            if state == STATE_MENU:
                if btn_game_least.rect.collidepoint(pos):
                    status_line = "Least Count selected. Create a lobby to start."
                elif btn_to_lobby.rect.collidepoint(pos):
                    # Apply config
                    try:
                        val = int(points_input.value.strip() or "200")
                        if val <= 0:
                            val = 200
                        max_points_out = val
                    except Exception:
                        max_points_out = 200
                    state = STATE_LOBBY
                    status_line = "Lobby created. Waiting for players to connect..."
                    player_names[HOST_ID] = "Host"
                    broadcast_lobby()

            elif state == STATE_LOBBY:
                # Handle dealer/order UI clicks
                sync_player_order()
                row_h = 44
                start_y = 220
                for idx, pid in enumerate(player_order):
                    ry = start_y + idx * row_h
                    if ry + row_h > panel_left.bottom - 90:
                        break
                    row_rect = pygame.Rect(panel_left.x + 16, ry, panel_left.width - 32, 38)
                    d_rect = pygame.Rect(row_rect.x + 8, row_rect.y + 7, 24, 24)
                    up_rect = pygame.Rect(row_rect.right - 56, row_rect.y + 7, 22, 22)
                    dn_rect = pygame.Rect(row_rect.right - 28, row_rect.y + 7, 22, 22)

                    if d_rect.collidepoint(pos):
                        dealer_pid = pid
                        broadcast_lobby()
                        break
                    if up_rect.collidepoint(pos) and idx > 0:
                        player_order[idx - 1], player_order[idx] = player_order[idx], player_order[idx - 1]
                        broadcast_lobby()
                        break
                    if dn_rect.collidepoint(pos) and idx < len(player_order) - 1:
                        player_order[idx + 1], player_order[idx] = player_order[idx], player_order[idx + 1]
                        broadcast_lobby()
                        break

                if btn_start.rect.collidepoint(pos):
                    state = STATE_PLAYING
                    start_match()

            elif state == STATE_PLAYING:
                turn = turn_order[current_turn_idx] if turn_order else None
                phase = turn_phase.get(HOST_ID, "discard")

                if btn_exit_host.rect.collidepoint(pos):
                    close_game_by_host()
                    break
                if btn_back_lobby.rect.collidepoint(pos) and match_over:
                    reset_to_lobby("Back to lobby.")
                    break
                if btn_next_round.rect.collidepoint(pos) and round_over and (not match_over):
                    # Continue match if possible
                    active = [pid for pid in turn_order if pid not in eliminated]
                    if len(active) >= 1:
                        start_round()
                elif btn_show.rect.collidepoint(pos) and (not round_over) and (turn == HOST_ID) and (phase == "discard") and show_available.get(HOST_ID, False) and (hand_total(HOST_ID) <= SHOW_LIMIT):
                    resolve_show(HOST_ID)
                elif (not round_over) and turn == HOST_ID and phase == "draw" and draw_pile_rect.collidepoint(pos):
                    if deck:
                        hands[HOST_ID].append(deck.pop())
                        sort_hand(HOST_ID)
                        turn_phase[HOST_ID] = "discard"
                        show_available[HOST_ID] = False
                        next_turn()
                        broadcast_state()
                elif (not round_over) and turn == HOST_ID and phase == "draw" and discard_rect.collidepoint(pos):
                    if discard_pile:
                        card_to_take = take_open_discard_for_turn(HOST_ID)
                        if card_to_take is not None:
                            hands[HOST_ID].append(card_to_take)
                        sort_hand(HOST_ID)
                        turn_phase[HOST_ID] = "discard"
                        show_available[HOST_ID] = False
                        next_turn()
                        broadcast_state()
                elif (not round_over) and turn == HOST_ID and phase == "discard":
                    # Double-click a hand card to discard it.
                    sort_hand(HOST_ID)
                    hand_cards = hands.get(HOST_ID, [])
                    n = len(hand_cards)
                    available = BASE_SIZE[0] - 60 - CARD_W
                    step = 40 if n <= 1 else max(28, min(46, available // max(1, n - 1)))
                    hx = 30
                    hy = HAND_Y
                    clicked: str | None = None
                    # Reverse so topmost (rightmost) card wins overlaps.
                    for i in range(n - 1, -1, -1):
                        card = hand_cards[i]
                        rect = pygame.Rect(hx + i * step, hy, CARD_W, CARD_H)
                        if rect.collidepoint(pos):
                            clicked = card
                            break
                    if clicked:
                        now = pygame.time.get_ticks()
                        is_double = clicked == last_click_card and (now - last_click_ms) <= DOUBLE_CLICK_MS
                        last_click_card = clicked
                        last_click_ms = now
                        if is_double and clicked in hands.get(HOST_ID, []):
                            face = clicked[:-1] if len(clicked) > 1 else clicked
                            prev_top = discard_pile[-1] if discard_pile else None
                            prev_top_face = prev_top[:-1] if prev_top and prev_top not in ("ZB", "ZR") else prev_top
                            removed = [c for c in hands.get(HOST_ID, []) if (c[:-1] if len(c) > 1 else c) == face]
                            hands[HOST_ID] = [c for c in hands.get(HOST_ID, []) if (c[:-1] if len(c) > 1 else c) != face]
                            sort_hand(HOST_ID)
                            discard_pile.extend(removed)
                            show_available[HOST_ID] = False
                            # Exception: if open card is same rank, you may discard without picking.
                            if prev_top_face is not None and prev_top_face == face:
                                turn_phase[HOST_ID] = "discard"
                                next_turn()
                            else:
                                turn_phase[HOST_ID] = "draw"
                            broadcast_state()

            elif state == STATE_RESULTS:
                if btn_back_menu.rect.collidepoint(pos):
                    state = STATE_MENU
                    status_line = "Select a game to host."

        # (drag/drop removed; discard is done via double-click)

    # Present: scale virtual canvas to window size
    window.blit(pygame.transform.smoothscale(screen, window.get_size()), (0, 0))
    pygame.display.flip()
    clock.tick(60)
