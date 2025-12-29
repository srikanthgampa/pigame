import json
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
screen = pygame.display.set_mode((980, 620))
pygame.display.set_caption("Raspberry Pi Gaming Hub (Host)")
clock = pygame.time.Clock()

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
    mouse = pygame.mouse.get_pos()
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

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self.active = self.rect.collidepoint(event.pos)
        if event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_BACKSPACE:
                self.value = self.value[:-1]
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.active = False
            else:
                if len(self.value) < 6 and event.unicode and event.unicode.isprintable():
                    if event.unicode.isdigit():
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

score_bar = pygame.Rect(20, 10, 940, 64)

# Game-table layout (full screen during play)
draw_pile_rect = pygame.Rect(410, 200, 150, 210)
discard_rect = pygame.Rect(580, 200, 150, 210)
btn_show = Button(pygame.Rect(820, 140, 120, 36), "SHOW")

CARD_W, CARD_H = 92, 138
HAND_Y = 450

# Double-click handling (discard action)
DOUBLE_CLICK_MS = 350
last_click_ms = 0
last_click_card: str | None = None

points_input = TextInput(pygame.Rect(60, 360, 180, 50), value=str(max_points_out))

def build_deck():
    values = [str(v) for v in range(2, 11)] + ["J","Q","K","A"]
    suits = ["S","C","D","H"]
    deck = [f"{v}{s}" for v in values for s in suits]
    deck += ["ZB","ZR"]
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
    }
    for pid, conn in list(connections.items()):
        try:
            send_json(conn, payload)
        except Exception:
            pass


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
    hand_totals = {pid: hand_total(pid) for pid in turn_order}
    state_msg = {
        "discard_top": discard_pile[-1] if discard_pile else None,
        "turn": current_pid,
        "turn_phase": turn_phase.get(current_pid, "draw") if current_pid is not None else "draw",
        "deck_count": len(deck),
        "players": turn_order[:],
        "scores_total": scores_total,
        "eliminated": sorted(eliminated),
        "round_no": round_no,
        "joker_card": joker_card,
        "joker_rank": joker_rank,
        "hand_totals": hand_totals,
        "round_over": round_over,
        "max_points_out": max_points_out,
    }
    for pid, conn in list(connections.items()):
        try:
            send_json(conn, {"action": "update", "state": state_msg})
        except Exception:
            pass


def start_match() -> None:
    global game_started, scores_total, eliminated, round_no, last_round_summary
    game_started = True
    last_round_summary = None
    round_no = 0
    eliminated = set()
    scores_total = {HOST_ID: 0}
    for pid in connections.keys():
        scores_total[pid] = 0
    start_round()


def start_round() -> None:
    global deck, discard_pile, turn_order, current_turn_idx, turn_phase, joker_card, joker_rank, round_no, round_over, last_round_summary
    last_round_summary = None
    round_over = False
    round_no += 1

    deck = build_deck()
    discard_pile = []

    # Active players exclude eliminated.
    active = [HOST_ID] + [pid for pid in sorted(connections.keys()) if pid not in eliminated]
    # If host is eliminated, also exclude them from play (but host still controls UI).
    if HOST_ID in eliminated:
        active = [pid for pid in active if pid != HOST_ID]

    turn_order = active
    current_turn_idx = 0
    turn_phase = {pid: "draw" for pid in turn_order}

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
        broadcast_lobby()

def next_turn() -> None:
    global current_turn_idx
    if turn_order:
        current_turn_idx = (current_turn_idx + 1) % len(turn_order)

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
    other_has_same_or_less = any(tot <= show_total for tot in others.values())

    round_points: dict[int, int] = {}
    if not other_has_same_or_less:
        round_points[show_pid] = 0
        for pid, tot in others.items():
            round_points[pid] = tot
        outcome = "win"
    else:
        round_points[show_pid] = SHOW_PENALTY
        min_other = min(others.values()) if others else show_total
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
        "show_pid": show_pid,
        "show_total": show_total,
        "totals": totals,
        "round_points": round_points,
        "scores_total": scores_total,
        "eliminated": sorted(eliminated),
        "outcome": outcome,
        "newly_out": newly_out,
    }
    round_over = True

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
            if not game_started:
                broadcast_lobby()
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
        phase = turn_phase.get(pid, "draw")
        if action == "discard" and is_turn and phase == "discard":
            card = data.get("card")
            if isinstance(card, str) and card in hands.get(pid, []):
                face = card[:-1] if len(card) > 1 else card
                removed = [c for c in hands.get(pid, []) if (c[:-1] if len(c) > 1 else c) == face]
                hands[pid] = [c for c in hands.get(pid, []) if (c[:-1] if len(c) > 1 else c) != face]
                sort_hand(pid)
                discard_pile.extend(removed)
                send_hand(pid)
                turn_phase[pid] = "draw"
                next_turn()
                broadcast_state()
        elif action == "draw_deck" and is_turn and phase == "draw":
            if deck:
                hands[pid].append(deck.pop())
                sort_hand(pid)
                send_hand(pid)
                turn_phase[pid] = "discard"
                broadcast_state()
        elif action == "draw_discard" and is_turn and phase == "draw":
            if discard_pile:
                hands[pid].append(discard_pile.pop())
                sort_hand(pid)
                send_hand(pid)
                turn_phase[pid] = "discard"
                broadcast_state()
        elif action == "show" and is_turn:
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

        y = panel_left.y + 120
        chip_blue = pygame.transform.smoothscale(load_card_image("BlueChip"), (34, 34))
        chip_red = pygame.transform.smoothscale(load_card_image("RedChip"), (34, 34))

        # Host entry
        screen.blit(chip_blue, (panel_left.x + 20, y))
        screen.blit(FONT.render(f"{player_names.get(HOST_ID, 'Host')} (You)", True, (245, 245, 245)), (panel_left.x + 62, y + 2))
        y += 48

        for pid in sorted(connections.keys()):
            screen.blit(chip_red, (panel_left.x + 20, y))
            screen.blit(
                FONT.render(player_names.get(pid, f"Player {pid}"), True, (240, 240, 240)),
                (panel_left.x + 62, y + 2),
            )
            y += 44

        can_start = True
        draw_button(btn_start, enabled=can_start)
        hint = "Tip: start with 1+ remote players, or play solo as Host."
        screen.blit(FONT_SM.render(hint, True, (205, 215, 230)), (panel_left.x + 18, panel_left.bottom - 36))

    elif state == STATE_PLAYING:
        # Full-screen game table (no half-screen panels)
        # Score bar at top
        pygame.draw.rect(screen, (0, 0, 0), score_bar.move(0, 3), border_radius=14)
        pygame.draw.rect(screen, (25, 28, 35), score_bar, border_radius=14)
        pygame.draw.rect(screen, (90, 100, 120), score_bar, width=2, border_radius=14)

        x = score_bar.x + 16
        y = score_bar.y + 18
        for pid in turn_order:
            name = player_names.get(pid, f"Player {pid}") if pid != HOST_ID else "Host"
            pts = scores_total.get(pid, 0)
            out = " OUT" if pid in eliminated else ""
            txt = FONT_SM.render(f"{name}: {pts}{out}", True, (235, 240, 248))
            screen.blit(txt, (x, y))
            x += txt.get_width() + 18

        # Show per-hand totals at top-right for current round
        totals_line = "  ".join([f"P{pid}:{hand_total(pid)}" for pid in turn_order])
        screen.blit(FONT_XS.render(totals_line, True, (210, 220, 235)), (score_bar.x + 16, score_bar.y + 42))

        # Turn hint
        turn = turn_order[current_turn_idx] if turn_order else None
        phase = turn_phase.get(turn, "draw") if turn is not None else "draw"
        hint = ""
        if not round_over and turn == HOST_ID:
            hint = "Click deck/discard to draw" if phase == "draw" else "Double-click a card to discard"
        elif round_over:
            hint = "Round over. Click Next Round (host) to continue."
        if hint:
            screen.blit(FONT_SM.render(hint, True, (255, 235, 120)), (30, 92))

        # Show button (only when host turn and <= show limit)
        can_show = (not round_over) and (turn == HOST_ID) and (hand_total(HOST_ID) <= SHOW_LIMIT)
        draw_button(btn_show, enabled=can_show)
        if round_over:
            draw_button(btn_next_round, enabled=True)

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
        available = 940 - 60 - CARD_W
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

        if state == STATE_MENU:
            points_input.handle_event(event)

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            pos = event.pos

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
                if btn_start.rect.collidepoint(pos):
                    state = STATE_PLAYING
                    start_match()

            elif state == STATE_PLAYING:
                turn = turn_order[current_turn_idx] if turn_order else None
                phase = turn_phase.get(HOST_ID, "draw")

                if btn_next_round.rect.collidepoint(pos) and round_over:
                    # Continue match if possible
                    active = [pid for pid in turn_order if pid not in eliminated]
                    if len(active) >= 1:
                        start_round()
                elif btn_show.rect.collidepoint(pos) and (not round_over) and (turn == HOST_ID) and (hand_total(HOST_ID) <= SHOW_LIMIT):
                    resolve_show(HOST_ID)
                elif (not round_over) and turn == HOST_ID and phase == "draw" and draw_pile_rect.collidepoint(pos):
                    if deck:
                        hands[HOST_ID].append(deck.pop())
                        sort_hand(HOST_ID)
                        turn_phase[HOST_ID] = "discard"
                        broadcast_state()
                elif (not round_over) and turn == HOST_ID and phase == "draw" and discard_rect.collidepoint(pos):
                    if discard_pile:
                        hands[HOST_ID].append(discard_pile.pop())
                        sort_hand(HOST_ID)
                        turn_phase[HOST_ID] = "discard"
                        broadcast_state()
                elif (not round_over) and turn == HOST_ID and phase == "discard":
                    # Double-click a hand card to discard it.
                    sort_hand(HOST_ID)
                    hand_cards = hands.get(HOST_ID, [])
                    n = len(hand_cards)
                    available = 940 - 60 - CARD_W
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
                            removed = [c for c in hands.get(HOST_ID, []) if (c[:-1] if len(c) > 1 else c) == face]
                            hands[HOST_ID] = [c for c in hands.get(HOST_ID, []) if (c[:-1] if len(c) > 1 else c) != face]
                            sort_hand(HOST_ID)
                            discard_pile.extend(removed)
                            turn_phase[HOST_ID] = "draw"
                            next_turn()
                            broadcast_state()

            elif state == STATE_RESULTS:
                if btn_back_menu.rect.collidepoint(pos):
                    state = STATE_MENU
                    status_line = "Select a game to host."

        # (drag/drop removed; discard is done via double-click)

    pygame.display.flip()
    clock.tick(60)
