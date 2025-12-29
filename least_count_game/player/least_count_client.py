import argparse
import json
import math
import queue
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
screen = pygame.Surface(BASE_SIZE)
pygame.display.set_caption("Raspberry Pi Gaming Hub (Player)")
clock = pygame.time.Clock()

USE_SMOOTH_SCALE = True


_sprite_cache: dict[tuple[str, int, int, int], pygame.Surface] = {}
_halo_cache: dict[tuple[int, int], pygame.Surface] = {}


def get_sprite(card_name: str, size: tuple[int, int], angle: int = 0) -> pygame.Surface:
    w, h = size
    key = (card_name, w, h, angle)
    cached = _sprite_cache.get(key)
    if cached is not None:
        return cached
    base = load_card_image(card_name)
    scaled = pygame.transform.smoothscale(base, (w, h)) if USE_SMOOTH_SCALE else pygame.transform.scale(base, (w, h))
    if angle:
        scaled = pygame.transform.rotate(scaled, angle)
    _sprite_cache[key] = scaled
    return scaled


def get_halo(size: tuple[int, int]) -> pygame.Surface:
    key = (size[0], size[1])
    cached = _halo_cache.get(key)
    if cached is not None:
        return cached
    halo = pygame.Surface(size, pygame.SRCALPHA)
    pygame.draw.ellipse(halo, (255, 235, 120, 55), halo.get_rect())
    _halo_cache[key] = halo
    return halo


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

# Round-designated joker (rank like "Q") + display card (like "QH")
joker_rank: str | None = None
joker_card: str | None = None
show_limit: int = 8


def load_card_image(card_name: str) -> pygame.Surface:
    key = f"{card_name}.png"
    if key in _image_cache:
        return _image_cache[key]
    img = pygame.image.load(str(ASSETS_DIR / key)).convert_alpha()
    _image_cache[key] = img
    return img


def card_sort_key(card: str) -> tuple[int, int]:
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


def sort_hand(cards: list[str]) -> list[str]:
    return sorted(cards, key=card_sort_key)


def card_points(card: str) -> int:
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


def hand_total(cards: list[str]) -> int:
    return sum(card_points(c) for c in cards)


def send_json(sock: socket.socket, payload: dict) -> None:
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    sock.sendall(data)


def recv_loop(sock: socket.socket, inbox: "queue.Queue[dict]") -> None:
    buf = ""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    inbox.put(json.loads(line))
                except Exception:
                    continue
    finally:
        inbox.put({"action": "disconnect"})


@dataclass(frozen=True)
class Button:
    rect: pygame.Rect
    label: str


def draw_button(btn: Button, enabled: bool = True) -> None:
    mouse = mouse_canvas_pos()
    hovering = btn.rect.collidepoint(mouse)
    base = (55, 155, 200) if enabled else (70, 70, 70)
    hover = (70, 175, 230) if enabled else (70, 70, 70)
    color = hover if hovering else base

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
            self.active = self.rect.collidepoint(to_canvas(event.pos))
        if event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_BACKSPACE:
                self.value = self.value[:-1]
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.active = False
            else:
                if len(self.value) < 24 and event.unicode and event.unicode.isprintable():
                    self.value += event.unicode

    def draw(self, label: str) -> None:
        pygame.draw.rect(screen, (0, 0, 0), self.rect.move(0, 3), border_radius=10)
        pygame.draw.rect(screen, (240, 245, 255) if self.active else (220, 225, 235), self.rect, border_radius=10)
        pygame.draw.rect(screen, (30, 30, 30), self.rect, width=2, border_radius=10)
        lab = FONT_SM.render(label, True, (230, 235, 245))
        screen.blit(lab, (self.rect.x, self.rect.y - 22))
        txt = FONT.render(self.value or "", True, (10, 10, 10))
        screen.blit(txt, (self.rect.x + 10, self.rect.y + 10))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="192.168.1.247", help="Host IP address")
    p.add_argument("--port", type=int, default=5000, help="Host port")
    p.add_argument("--name", default="", help="Optional player display name")
    return p.parse_args()


args = parse_args()
host_ip = args.host
port = args.port
player_name = args.name.strip()

client: socket.socket | None = None
inbox: "queue.Queue[dict]" = queue.Queue()
net_thread: threading.Thread | None = None

# States
STATE_MENU = "menu"
STATE_CONNECTING = "connecting"
STATE_LOBBY = "lobby"
STATE_PLAYING = "playing"
STATE_RESULTS = "results"
state = STATE_MENU

# Player/game state
hand: list[str] = []
discard_top: str | None = None
current_turn: int | None = None
player_id: int | None = None
deck_count: int = 0
turn_phase: str = "draw"  # phase for the current turn player ("draw" or "discard")
players_list: list[dict] = []
last_results: dict | None = None
scores_total: dict = {}
eliminated: list[int] = []
round_no: int = 0
round_over: bool = False
round_summary: dict | None = None
hand_counts: dict = {}
players_in_round: list[int] = []
show_enabled: bool = False
pick_discard_card: str | None = None
round_history: list[dict] = []
player_names: dict = {}
scroll_rows_from_bottom = 0  # for scoreboard scrolling (round rows only)

# Layout
panel_left = pygame.Rect(40, 40, 360, 560)
panel_right = pygame.Rect(420, 40, 520, 560)

btn_game_least = Button(pygame.Rect(60, 160, 320, 70), "Least Count")
btn_connect = Button(pygame.Rect(60, 420, 320, 56), "Connect")
btn_disconnect = Button(pygame.Rect(60, 520, 320, 56), "Disconnect")
btn_back = Button(pygame.Rect(60, 520, 320, 56), "Back to Menu")

_pile_w, _pile_h = 160, 224
draw_pile_rect = pygame.Rect(BASE_SIZE[0] // 2 - 190, BASE_SIZE[1] // 2 - 120, _pile_w, _pile_h)
discard_rect = pygame.Rect(BASE_SIZE[0] // 2 + 30, BASE_SIZE[1] // 2 - 120, _pile_w, _pile_h)
btn_least = Button(pygame.Rect(60, 220, 180, 40), "Least Count")
btn_show = Button(pygame.Rect(BASE_SIZE[0] - 170, 128, 120, 36), "SHOW")
btn_disconnect_game = Button(pygame.Rect(BASE_SIZE[0] - 110, 20, 90, 32), "Exit")

CARD_W, CARD_H = 92, 138
HAND_Y = BASE_SIZE[1] - 190

ip_input = TextInput(pygame.Rect(60, 250, 320, 50), value=host_ip)
name_input = TextInput(pygame.Rect(60, 340, 320, 50), value=player_name)
status_line = "Select Least Count, then connect to the host."

# Double-click handling (discard action)
DOUBLE_CLICK_MS = 350
last_click_ms = 0
last_click_card: str | None = None


def connected() -> bool:
    return client is not None


def connect_to_host() -> None:
    global client, net_thread, state, status_line, player_name
    if client is not None:
        return
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((ip_input.value.strip() or host_ip, port))
        client = sock
        net_thread = threading.Thread(target=recv_loop, args=(sock, inbox), daemon=True)
        net_thread.start()
        player_name = name_input.value.strip()
        if player_name:
            try:
                send_json(sock, {"action": "hello", "name": player_name})
            except Exception:
                pass
        state = STATE_CONNECTING
        status_line = f"Connected to {ip_input.value.strip()}:{port}. Waiting for lobby/game..."
    except Exception as e:
        status_line = f"Connect failed: {e}"
        client = None


def disconnect() -> None:
    global client, state, status_line, player_id, players_list, hand, discard_top, current_turn, last_results
    if client:
        try:
            client.close()
        except Exception:
            pass
    client = None
    player_id = None
    players_list = []
    hand = []
    discard_top = None
    current_turn = None
    last_results = None
    state = STATE_MENU
    status_line = "Disconnected."
    # reset scoreboard scroll
    global scroll_rows_from_bottom
    scroll_rows_from_bottom = 0


def send_action(action: str, card: str | None = None) -> None:
    if not client:
        return
    msg: dict = {"action": action}
    if card:
        msg["card"] = card
    try:
        send_json(client, msg)
    except Exception:
        pass


running = True
while running:
    # Apply server messages
    while True:
        try:
            data = inbox.get_nowait()
        except queue.Empty:
            break

        action = data.get("action")
        if action == "disconnect":
            disconnect()
            status_line = "Disconnected from host."
            break

        if action == "welcome":
            player_id = int(data.get("player_id", 0)) or player_id
            continue

        if action == "lobby":
            players_list = list(data.get("players", []))
            state = STATE_LOBBY
            continue

        if action == "start":
            state = STATE_PLAYING
            rules = data.get("rules", {}) or {}
            try:
                show_limit = int(rules.get("show_limit", show_limit) or show_limit)
            except Exception:
                show_limit = show_limit
            round_summary = None
            continue

        if action == "hand":
            hand = sort_hand(list(data.get("hand", [])))
            continue

        if action == "update":
            state = STATE_PLAYING
            st = data.get("state", {})
            discard_top = st.get("discard_top")
            current_turn = st.get("turn")
            deck_count = int(st.get("deck_count", 0) or 0)
            turn_phase = str(st.get("turn_phase", "discard") or "discard")
            show_enabled = bool(st.get("show_enabled", False))
            pick_discard_card = st.get("pick_discard_card")
            joker_card = st.get("joker_card")
            joker_rank = st.get("joker_rank")
            scores_total = st.get("scores_total", {}) or {}
            eliminated = list(st.get("eliminated", []) or [])
            round_no = int(st.get("round_no", 0) or 0)
            round_over = bool(st.get("round_over", False))
            hand_counts = st.get("hand_counts", {}) or {}
            players_in_round = list(st.get("players", []) or [])
            round_history = list(st.get("round_history", []) or [])
            player_names = st.get("player_names", {}) or {}
            continue

        if action == "round_end":
            # Round ended summary; state will also reflect round_over.
            state = STATE_PLAYING
            round_over = True
            round_summary = data.get("summary")
            continue

        if action == "match_end":
            winner = data.get("winner")
            print("Match over! Winner:", winner, "Final totals:", data.get("scores_total"))
            round_over = True
            continue

        if action == "end":
            last_results = {"scores": data.get("scores", {}), "winner": data.get("winner")}
            state = STATE_RESULTS
            continue

    # Draw background + header
    screen.fill((14, 16, 20))
    pygame.draw.rect(screen, (10, 70, 45), pygame.Rect(0, 0, 980, 620))
    pygame.draw.rect(screen, (0, 0, 0), pygame.Rect(0, 0, 980, 620), width=6)

    header = FONT_LG.render("Raspberry Pi Gaming Hub", True, (245, 248, 255))
    screen.blit(header, (40, 6))
    sub = FONT_SM.render("Player • Connect • Least Count", True, (200, 210, 225))
    screen.blit(sub, (44, 52))

    # Panels are used for setup screens only (game screen is full table).
    if state != STATE_PLAYING:
        draw_panel(panel_left, "Control")
        draw_panel(panel_right, "Table")

    # UI
    if state == STATE_MENU:
        screen.blit(FONT.render("Select game:", True, (230, 235, 245)), (panel_left.x + 20, 110))
        draw_button(btn_game_least, enabled=True)
        ip_input.draw("Host IP")
        name_input.draw("Player name")
        draw_button(btn_connect, enabled=True)
        screen.blit(FONT_SM.render(status_line, True, (210, 220, 235)), (panel_left.x + 18, panel_left.bottom - 36))

    elif state == STATE_CONNECTING:
        screen.blit(FONT.render("Connecting...", True, (230, 235, 245)), (panel_left.x + 20, 110))
        screen.blit(FONT_SM.render(status_line, True, (210, 220, 235)), (panel_left.x + 20, 150))
        draw_button(btn_disconnect, enabled=True)

    elif state == STATE_LOBBY:
        screen.blit(FONT.render("Lobby", True, (230, 235, 245)), (panel_left.x + 20, 86))
        y = panel_left.y + 140
        chip = get_sprite("BlueChip", (34, 34))
        for p in players_list:
            screen.blit(chip, (panel_left.x + 20, y))
            name = str(p.get("name") or f"Player {p.get('id')}")
            pid = p.get("id")
            you = " (You)" if player_id is not None and pid == player_id else ""
            screen.blit(FONT.render(f"{name}{you}", True, (240, 240, 240)), (panel_left.x + 62, y + 2))
            y += 44
        screen.blit(FONT_SM.render("Waiting for host to start...", True, (205, 215, 230)), (panel_left.x + 20, panel_left.bottom - 78))
        draw_button(btn_disconnect, enabled=True)

    elif state == STATE_PLAYING:
        # Simplified full-screen play view: scoreboard + table + hand
        score_bar = pygame.Rect(20, 10, BASE_SIZE[0] - 40, 110)
        pygame.draw.rect(screen, (0, 0, 0), score_bar.move(0, 3), border_radius=14)
        pygame.draw.rect(screen, (25, 28, 35), score_bar, border_radius=14)
        pygame.draw.rect(screen, (90, 100, 120), score_bar, width=2, border_radius=14)

        # Scoreboard: columns are player names, rows are rounds. Total row + Cards row pinned at bottom.
        header_h = 26
        footer_h = 44
        row_h = 18
        inner = pygame.Rect(score_bar.x + 10, score_bar.y + 8, score_bar.width - 20, score_bar.height - 16)
        # derive player order
        pids = [int(p) for p in players_in_round] if players_in_round else sorted([int(k) for k in scores_total.keys() if str(k).isdigit()])
        if player_id is not None and player_id in pids:
            # keep local player as first column for readability
            pids = [player_id] + [p for p in pids if p != player_id]

        # compute column widths
        col0_w = 80  # "Round"/labels
        remaining = max(1, inner.width - col0_w)
        col_w = max(90, remaining // max(1, len(pids)))
        max_cols = max(1, min(len(pids), remaining // 90))
        pids = pids[:max_cols]

        # header row (pinned)
        header_rect = pygame.Rect(inner.x, inner.y, inner.width, header_h)
        pygame.draw.rect(screen, (18, 20, 26), header_rect, border_radius=10)
        screen.blit(FONT_XS.render("Round", True, (210, 220, 235)), (header_rect.x + 8, header_rect.y + 6))
        for i, pid in enumerate(pids):
            name = player_names.get(pid, player_names.get(str(pid), f"P{pid}"))
            x = header_rect.x + col0_w + i * col_w
            col_rect = pygame.Rect(x, header_rect.y, col_w, header_h)
            if (pid == current_turn) and (not round_over):
                pygame.draw.rect(screen, (255, 235, 120), col_rect, border_radius=10)
                fg = (20, 20, 20)
            else:
                fg = (210, 220, 235)
            screen.blit(FONT_XS.render(str(name)[:10], True, fg), (x + 6, header_rect.y + 6))

        # scrollable round rows
        body_rect = pygame.Rect(inner.x, inner.y + header_h, inner.width, inner.height - header_h - footer_h)
        pygame.draw.rect(screen, (0, 0, 0), body_rect, width=1, border_radius=10)

        # determine visible slice
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
        # Total row
        screen.blit(FONT_XS.render("Total", True, (210, 220, 235)), (footer_rect.x + 8, footer_rect.y + 4))
        for i, pid in enumerate(pids):
            total = scores_total.get(pid, scores_total.get(str(pid), 0))
            x = footer_rect.x + col0_w + i * col_w
            screen.blit(FONT_XS.render(str(total), True, (235, 240, 248)), (x + 6, footer_rect.y + 4))
        # Cards row
        screen.blit(FONT_XS.render("Cards", True, (210, 220, 235)), (footer_rect.x + 8, footer_rect.y + 24))
        for i, pid in enumerate(pids):
            count = hand_counts.get(pid, hand_counts.get(str(pid), "-"))
            x = footer_rect.x + col0_w + i * col_w
            txt = str(count)
            if (pid == current_turn) and (not round_over):
                txt = f"{txt} *"
            screen.blit(FONT_XS.render(txt, True, (235, 240, 248)), (x + 6, footer_rect.y + 24))

        # Current-round totals are hidden during play; shown only in round-end summary.

        # Turn hint
        hint = ""
        if round_over:
            hint = "Round over. Waiting for host to start next round."
        elif current_turn == player_id:
            hint = "Double-click a card to discard (first)" if turn_phase == "discard" else "Click deck/discard to pick (after discard)"
        if hint:
            screen.blit(FONT_SM.render(hint, True, (255, 235, 120)), (30, score_bar.bottom + 10))

        # Show which discard card you will take this turn
        if (not round_over) and (current_turn == player_id) and (turn_phase == "draw") and pick_discard_card:
            screen.blit(FONT_XS.render(f"Discard pick: {pick_discard_card}", True, (210, 220, 235)), (30, score_bar.bottom + 34))

        # Show allowed only at the start of your turn (before discard/pick), and only when <= show limit.
        can_show = (not round_over) and (current_turn == player_id) and (turn_phase == "discard") and show_enabled and (hand_total(hand) <= show_limit)
        draw_button(btn_show, enabled=can_show)
        draw_button(btn_disconnect_game, enabled=True)

        # Round outcome banner
        if round_over and round_summary:
            show_pid = round_summary.get("show_pid")
            show_total = round_summary.get("show_total")
            outcome = round_summary.get("outcome")
            if outcome == "win":
                msg = f"Player {show_pid} SHOWED {show_total} and WON (0 pts)."
            else:
                same_or_less = round_summary.get("same_or_less_players", [])
                msg = f"Player {show_pid} SHOWED {show_total} and got PENALTY (+40). Same/less: {same_or_less}"
            banner = pygame.Rect(180, 92, 780, 34)
            pygame.draw.rect(screen, (0, 0, 0), banner.move(0, 2), border_radius=10)
            pygame.draw.rect(screen, (40, 35, 20), banner, border_radius=10)
            pygame.draw.rect(screen, (130, 110, 70), banner, width=2, border_radius=10)
            screen.blit(FONT_SM.render(msg, True, (245, 235, 200)), (banner.x + 10, banner.y + 8))

        # draw pile
        pygame.draw.rect(screen, (0, 0, 0), draw_pile_rect.move(0, 4), border_radius=14)
        pygame.draw.rect(screen, (35, 40, 52), draw_pile_rect, border_radius=14)
        pygame.draw.rect(screen, (90, 100, 120), draw_pile_rect, width=2, border_radius=14)
        # Joker peeking under the deck (designated joker for the round)
        peek_name = joker_card or "ZB"
        peek_joker = get_sprite(peek_name, (92, 130), angle=-18)
        screen.blit(peek_joker, (draw_pile_rect.x + 10, draw_pile_rect.bottom - 78))
        back = get_sprite("CardBack", (120, 170))
        screen.blit(back, (draw_pile_rect.x + 15, draw_pile_rect.y + 18))

        # discard pile
        pygame.draw.rect(screen, (0, 0, 0), discard_rect.move(0, 4), border_radius=14)
        pygame.draw.rect(screen, (35, 40, 52), discard_rect, border_radius=14)
        pygame.draw.rect(screen, (90, 100, 120), discard_rect, width=2, border_radius=14)
        if discard_top:
            img = get_sprite(discard_top, (120, 170))
            screen.blit(img, (discard_rect.x + 15, discard_rect.y + 18))
        else:
            blank = get_sprite("BlankCard", (120, 170))
            screen.blit(blank, (discard_rect.x + 15, discard_rect.y + 18))

        # hand (sorted + overlapping)
        hand = sort_hand(hand)
        n = len(hand)
        available = BASE_SIZE[0] - 60 - CARD_W
        step = 40 if n <= 1 else max(28, min(46, available // max(1, n - 1)))
        hx = 30
        for i, card in enumerate(hand):
            rect = pygame.Rect(hx + i * step, HAND_Y, CARD_W, CARD_H)
            img = get_sprite(card, (CARD_W, CARD_H))
            screen.blit(img, rect.topleft)
            pygame.draw.rect(screen, (10, 10, 10), rect, width=2, border_radius=8)

        # Live-table: other players' hands face down + spotlight active player
        def _draw_seat(pid: int, rect: pygame.Rect, active_pid: int | None) -> None:
            is_active = (active_pid == pid) and (not round_over)
            if is_active:
                halo = get_halo(rect.inflate(90, 50).size)
                screen.blit(halo, halo.get_rect(center=rect.center))
                pygame.draw.rect(screen, (255, 235, 120), rect.inflate(16, 16), width=4, border_radius=14)
            pygame.draw.rect(screen, (0, 0, 0), rect.move(0, 3), border_radius=12)
            pygame.draw.rect(screen, (25, 28, 35), rect, border_radius=12)
            pygame.draw.rect(screen, (90, 100, 120), rect, width=2, border_radius=12)

            back_small = get_sprite("CardBack", (48, 68))
            # host sends int keys; be tolerant
            count = hand_counts.get(pid, hand_counts.get(str(pid), 0))
            stacks = min(int(count or 0), 5)
            sx = rect.x + 10
            sy = rect.y + 10
            for i in range(stacks):
                screen.blit(back_small, (sx + i * 6, sy + i * 2))
            screen.blit(FONT_XS.render(f"P{pid} ({count})", True, (235, 240, 248)), (rect.x + 10, rect.bottom - 20))

        active_pid = current_turn
        # Arc layout: other players on top arc (your own seat face-down is not shown).
        others = [pid for pid in players_in_round if pid != player_id]
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
            for pid, sc in sorted(scores.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999):
                screen.blit(FONT.render(f"Player {pid}: {sc}", True, (235, 240, 248)), (panel_left.x + 20, y))
                y += 34
        draw_button(btn_disconnect, enabled=True)
        draw_button(btn_back, enabled=True)

    window.blit(pygame.transform.scale(screen, window.get_size()), (0, 0))
    pygame.display.flip()
    clock.tick(60)

    # Events
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
            # scroll round rows inside scoreboard (up shows older rounds)
            # keep pinned header + footer
            scroll_rows_from_bottom = max(0, scroll_rows_from_bottom + (-event.y))

        if state == STATE_MENU:
            ip_input.handle_event(event)
            name_input.handle_event(event)

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            pos = to_canvas(event.pos)

            if state == STATE_MENU:
                if btn_connect.rect.collidepoint(pos):
                    connect_to_host()

            elif state in (STATE_CONNECTING, STATE_LOBBY, STATE_RESULTS):
                if btn_disconnect.rect.collidepoint(pos):
                    disconnect()

            if state == STATE_PLAYING:
                if btn_disconnect_game.rect.collidepoint(pos):
                    disconnect()
                if btn_show.rect.collidepoint(pos) and (not round_over) and current_turn == player_id and (turn_phase == "discard") and show_enabled and (hand_total(hand) <= show_limit):
                    send_action("show")
                elif current_turn == player_id and (not round_over) and turn_phase == "draw" and draw_pile_rect.collidepoint(pos):
                    send_action("draw_deck")
                elif current_turn == player_id and (not round_over) and turn_phase == "draw" and discard_rect.collidepoint(pos):
                    send_action("draw_discard")
                elif current_turn == player_id and (not round_over) and turn_phase == "discard":
                    # Double-click a hand card to discard it.
                    hand = sort_hand(hand)
                    n = len(hand)
                    available = BASE_SIZE[0] - 60 - CARD_W
                    step = 40 if n <= 1 else max(28, min(46, available // max(1, n - 1)))
                    hx = 30
                    clicked: str | None = None
                    for i in range(n - 1, -1, -1):
                        card = hand[i]
                        rect = pygame.Rect(hx + i * step, HAND_Y, CARD_W, CARD_H)
                        if rect.collidepoint(pos):
                            clicked = card
                            break
                    if clicked:
                        now = pygame.time.get_ticks()
                        is_double = clicked == last_click_card and (now - last_click_ms) <= DOUBLE_CLICK_MS
                        last_click_card = clicked
                        last_click_ms = now
                        if is_double:
                            send_action("discard", clicked)

            if state == STATE_RESULTS and btn_back.rect.collidepoint(pos):
                state = STATE_MENU
                status_line = "Select Least Count, then connect to the host."
        # (drag/drop removed; discard is done via double-click)
