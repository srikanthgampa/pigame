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


_image_cache: dict[str, pygame.Surface] = {}


def load_card_image(card_name: str) -> pygame.Surface:
    # Cache + robust pathing regardless of current working directory.
    key = f"{card_name}.png"
    if key in _image_cache:
        return _image_cache[key]
    img = pygame.image.load(str(ASSETS_DIR / key)).convert_alpha()
    _image_cache[key] = img
    return img


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


STATE_MENU = "menu"
STATE_LOBBY = "lobby"
STATE_PLAYING = "playing"
STATE_RESULTS = "results"
state = STATE_MENU

status_line = "Select a game to host."
last_results: dict | None = None

dragging_card: str | None = None
offset_x = 0
offset_y = 0

# UI layout
btn_game_least = Button(pygame.Rect(60, 160, 320, 70), "Least Count")
btn_to_lobby = Button(pygame.Rect(60, 250, 320, 54), "Create Lobby")
btn_start = Button(pygame.Rect(60, 520, 320, 56), "Start Game")
btn_back_menu = Button(pygame.Rect(60, 520, 320, 56), "Back to Menu")

panel_left = pygame.Rect(40, 40, 360, 560)
panel_right = pygame.Rect(420, 40, 520, 560)

discard_rect = pygame.Rect(770, 150, 150, 210)
draw_pile_rect = pygame.Rect(600, 150, 150, 210)
btn_draw = Button(pygame.Rect(600, 380, 150, 54), "Draw")
btn_least = Button(pygame.Rect(770, 380, 150, 54), "Least Count")

CARD_W, CARD_H = 92, 138
HAND_Y = 460

def build_deck():
    values = [str(v) for v in range(2, 11)] + ["J","Q","K","A"]
    suits = ["S","C","D","H"]
    deck = [f"{v}{s}" for v in values for s in suits]
    deck += ["ZB","ZR"]
    random.shuffle(deck)
    return deck

def broadcast_lobby() -> None:
    player_list = [{"id": HOST_ID, "name": player_names.get(HOST_ID, "Host")}]
    for pid in sorted(connections.keys()):
        player_list.append({"id": pid, "name": player_names.get(pid, f"Player {pid}")})
    payload = {"action": "lobby", "players": player_list, "port": PORT}
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
    try:
        send_json(conn, {"action": "hand", "hand": hands.get(pid, [])})
    except Exception:
        pass


def broadcast_state() -> None:
    state_msg = {
        "discard_top": discard_pile[-1] if discard_pile else None,
        "turn": turn_order[current_turn_idx] if turn_order else None,
        "deck_count": len(deck),
        "players": turn_order[:],
    }
    for pid, conn in list(connections.items()):
        try:
            send_json(conn, {"action": "update", "state": state_msg})
        except Exception:
            pass


def start_game() -> None:
    global game_started, deck, discard_pile, turn_order, current_turn_idx, last_results
    last_results = None
    game_started = True
    deck = build_deck()
    discard_pile = []

    # Build turn order once, consistently.
    turn_order = [HOST_ID] + sorted(connections.keys())
    current_turn_idx = 0

    # Deal hands
    hands[HOST_ID] = [deck.pop() for _ in range(5)]
    for pid in sorted(connections.keys()):
        hands[pid] = [deck.pop() for _ in range(5)]
        send_hand(pid)

    # Flip one card to start discard pile (common for this style of game).
    if deck:
        discard_pile.append(deck.pop())

    # Let clients know the game has started (they’ll also get update/hand messages).
    for pid, conn in list(connections.items()):
        try:
            send_json(
                conn,
                {
                    "action": "start",
                    "rules": {
                        "hand_size": 5,
                        "notes": "Lowest total wins. On your turn: draw OR discard a card. Press 'Least Count' to end and score hands.",
                    },
                },
            )
        except Exception:
            pass

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
    global game_started, last_results
    scores = {pid: sum(card_value(c) for c in hands.get(pid, [])) for pid in hands}
    winner = min(scores, key=scores.get) if scores else None
    last_results = {"scores": scores, "winner": winner}

    for pid, conn in list(connections.items()):
        try:
            send_json(conn, {"action": "end", "scores": scores, "winner": winner})
        except Exception:
            pass
    game_started = False
    print("Game Over! Scores:", scores, "Winner:", winner)

def card_value(card):
    if card in ["ZB","ZR"]: return 0
    if card[0] == "A": return 1
    if card[0] == "J": return 11
    if card[0] == "Q": return 12
    if card[0] == "K": return 13
    try:
        return int(card[:-1]) if card[:-1].isdigit() else int(card[0])
    except:
        return 0

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
        if action == "discard" and is_turn:
            card = data.get("card")
            if isinstance(card, str) and card in hands.get(pid, []):
                hands[pid].remove(card)
                discard_pile.append(card)
                send_hand(pid)
                next_turn()
                broadcast_state()
        elif action == "draw" and is_turn:
            if deck:
                hands[pid].append(deck.pop())
                send_hand(pid)
                next_turn()
                broadcast_state()
        elif action == "least_count":
            end_game()
            state = STATE_RESULTS

    # --- Draw background ---
    screen.fill((14, 16, 20))
    # subtle felt
    pygame.draw.rect(screen, (10, 70, 45), pygame.Rect(0, 0, 980, 620))
    pygame.draw.rect(screen, (0, 0, 0), pygame.Rect(0, 0, 980, 620), width=6)

    # Header
    header = FONT_LG.render("Raspberry Pi Gaming Hub", True, (245, 248, 255))
    screen.blit(header, (40, 6))
    sub = FONT_SM.render("Host • Lobby • Least Count", True, (200, 210, 225))
    screen.blit(sub, (44, 52))

    draw_panel(panel_left, "Control")
    draw_panel(panel_right, "Table")

    # Left panel content
    if state == STATE_MENU:
        screen.blit(FONT.render("Select a game to host:", True, (230, 235, 245)), (panel_left.x + 20, 110))
        draw_button(btn_game_least, enabled=True)
        draw_button(btn_to_lobby, enabled=True)
        screen.blit(FONT_SM.render(status_line, True, (210, 220, 235)), (panel_left.x + 18, panel_left.bottom - 36))

    elif state == STATE_LOBBY:
        screen.blit(FONT.render("Lobby", True, (230, 235, 245)), (panel_left.x + 20, 86))
        screen.blit(
            FONT_SM.render(f"Listening on port {PORT}. Connected players:", True, (200, 210, 225)),
            (panel_left.x + 20, 122),
        )

        y = panel_left.y + 160
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

        can_start = True  # allow solo, but show status
        draw_button(btn_start, enabled=can_start)
        hint = "Tip: start with 1+ remote players, or play solo as Host."
        screen.blit(FONT_SM.render(hint, True, (205, 215, 230)), (panel_left.x + 18, panel_left.bottom - 36))

    elif state == STATE_PLAYING:
        # Left: turn + controls
        turn = turn_order[current_turn_idx] if turn_order else None
        turn_text = "Your Turn" if turn == HOST_ID else f"Waiting for Player {turn}"
        screen.blit(FONT.render(turn_text, True, (255, 235, 120)), (panel_left.x + 20, 100))
        screen.blit(FONT_SM.render(f"Deck: {len(deck)} cards", True, (210, 220, 235)), (panel_left.x + 20, 138))
        draw_button(btn_draw, enabled=(turn == HOST_ID and bool(deck)))
        draw_button(btn_least, enabled=True)

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

    # Right panel: table rendering (discard, draw pile, host hand)
    if state in (STATE_PLAYING, STATE_RESULTS):
        # draw pile
        pygame.draw.rect(screen, (0, 0, 0), draw_pile_rect.move(0, 4), border_radius=14)
        pygame.draw.rect(screen, (35, 40, 52), draw_pile_rect, border_radius=14)
        pygame.draw.rect(screen, (90, 100, 120), draw_pile_rect, width=2, border_radius=14)
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

        # host hand
        hx = panel_right.x + 22
        hy = HAND_Y
        for i, card in enumerate(hands.get(HOST_ID, [])):
            rect = pygame.Rect(hx + i * (CARD_W + 16), hy, CARD_W, CARD_H)
            img = pygame.transform.smoothscale(load_card_image(card), (CARD_W, CARD_H))
            if dragging_card == card:
                mx, my = pygame.mouse.get_pos()
                rect.x = mx + offset_x
                rect.y = my + offset_y
            screen.blit(img, rect.topleft)
            pygame.draw.rect(screen, (10, 10, 10), rect, width=2, border_radius=8)

    # --- Events ---
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            pos = event.pos

            if state == STATE_MENU:
                if btn_game_least.rect.collidepoint(pos):
                    status_line = "Least Count selected. Create a lobby to start."
                elif btn_to_lobby.rect.collidepoint(pos):
                    state = STATE_LOBBY
                    status_line = "Lobby created. Waiting for players to connect..."
                    player_names[HOST_ID] = "Host"
                    broadcast_lobby()

            elif state == STATE_LOBBY:
                if btn_start.rect.collidepoint(pos):
                    state = STATE_PLAYING
                    start_game()

            elif state == STATE_PLAYING:
                turn = turn_order[current_turn_idx] if turn_order else None
                if btn_draw.rect.collidepoint(pos) and turn == HOST_ID:
                    if deck:
                        hands[HOST_ID].append(deck.pop())
                        next_turn()
                        broadcast_state()
                elif btn_least.rect.collidepoint(pos):
                    end_game()
                    state = STATE_RESULTS

                # drag cards from hand
                hx = panel_right.x + 22
                hy = HAND_Y
                for i, card in enumerate(hands.get(HOST_ID, [])):
                    rect = pygame.Rect(hx + i * (CARD_W + 16), hy, CARD_W, CARD_H)
                    if rect.collidepoint(pos) and turn == HOST_ID:
                        dragging_card = card
                        offset_x = rect.x - pos[0]
                        offset_y = rect.y - pos[1]
                        break

            elif state == STATE_RESULTS:
                if btn_back_menu.rect.collidepoint(pos):
                    state = STATE_MENU
                    status_line = "Select a game to host."

        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if state == STATE_PLAYING and dragging_card:
                turn = turn_order[current_turn_idx] if turn_order else None
                if turn == HOST_ID:
                    # drop onto discard pile
                    mx, my = event.pos
                    drop_rect = pygame.Rect(mx + offset_x, my + offset_y, CARD_W, CARD_H)
                    if discard_rect.colliderect(drop_rect):
                        discard_pile.append(dragging_card)
                        hands[HOST_ID].remove(dragging_card)
                        next_turn()
                        broadcast_state()
                dragging_card = None

    pygame.display.flip()
    clock.tick(60)
