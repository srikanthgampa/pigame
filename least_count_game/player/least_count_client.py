import argparse
import json
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
screen = pygame.display.set_mode((980, 620))
pygame.display.set_caption("Raspberry Pi Gaming Hub (Player)")
clock = pygame.time.Clock()

FONT = pygame.font.SysFont("dejavusans", 26)
FONT_SM = pygame.font.SysFont("dejavusans", 18)
FONT_LG = pygame.font.SysFont("dejavusans", 44)

_image_cache: dict[str, pygame.Surface] = {}


def load_card_image(card_name: str) -> pygame.Surface:
    key = f"{card_name}.png"
    if key in _image_cache:
        return _image_cache[key]
    img = pygame.image.load(str(ASSETS_DIR / key)).convert_alpha()
    _image_cache[key] = img
    return img


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
    mouse = pygame.mouse.get_pos()
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
            self.active = self.rect.collidepoint(event.pos)
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
players_list: list[dict] = []
last_results: dict | None = None

dragging_card: str | None = None
offset_x = 0
offset_y = 0

# Layout
panel_left = pygame.Rect(40, 40, 360, 560)
panel_right = pygame.Rect(420, 40, 520, 560)

btn_game_least = Button(pygame.Rect(60, 160, 320, 70), "Least Count")
btn_connect = Button(pygame.Rect(60, 330, 320, 56), "Connect")
btn_disconnect = Button(pygame.Rect(60, 520, 320, 56), "Disconnect")
btn_back = Button(pygame.Rect(60, 520, 320, 56), "Back to Menu")

draw_pile_rect = pygame.Rect(600, 150, 150, 210)
discard_rect = pygame.Rect(770, 150, 150, 210)
btn_draw = Button(pygame.Rect(600, 380, 150, 54), "Draw")
btn_least = Button(pygame.Rect(770, 380, 150, 54), "Least Count")

CARD_W, CARD_H = 92, 138
HAND_Y = 460

ip_input = TextInput(pygame.Rect(60, 250, 320, 50), value=host_ip)
status_line = "Select Least Count, then connect to the host."


def connected() -> bool:
    return client is not None


def connect_to_host() -> None:
    global client, net_thread, state, status_line
    if client is not None:
        return
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((ip_input.value.strip() or host_ip, port))
        client = sock
        net_thread = threading.Thread(target=recv_loop, args=(sock, inbox), daemon=True)
        net_thread.start()
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
            continue

        if action == "hand":
            hand = list(data.get("hand", []))
            continue

        if action == "update":
            state = STATE_PLAYING
            st = data.get("state", {})
            discard_top = st.get("discard_top")
            current_turn = st.get("turn")
            deck_count = int(st.get("deck_count", 0) or 0)
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

    draw_panel(panel_left, "Control")
    draw_panel(panel_right, "Table")

    # UI
    if state == STATE_MENU:
        screen.blit(FONT.render("Select game:", True, (230, 235, 245)), (panel_left.x + 20, 110))
        draw_button(btn_game_least, enabled=True)
        ip_input.draw("Host IP")
        draw_button(btn_connect, enabled=True)
        screen.blit(FONT_SM.render(status_line, True, (210, 220, 235)), (panel_left.x + 18, panel_left.bottom - 36))

    elif state == STATE_CONNECTING:
        screen.blit(FONT.render("Connecting...", True, (230, 235, 245)), (panel_left.x + 20, 110))
        screen.blit(FONT_SM.render(status_line, True, (210, 220, 235)), (panel_left.x + 20, 150))
        draw_button(btn_disconnect, enabled=True)

    elif state == STATE_LOBBY:
        screen.blit(FONT.render("Lobby", True, (230, 235, 245)), (panel_left.x + 20, 86))
        y = panel_left.y + 140
        chip = pygame.transform.smoothscale(load_card_image("BlueChip"), (34, 34))
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
        if current_turn == player_id:
            turn_text = "Your Turn"
        else:
            turn_text = f"Waiting for Player {current_turn}"
        screen.blit(FONT.render(turn_text, True, (255, 235, 120)), (panel_left.x + 20, 100))
        screen.blit(FONT_SM.render(f"Deck: {deck_count} cards", True, (210, 220, 235)), (panel_left.x + 20, 138))
        draw_button(btn_draw, enabled=(current_turn == player_id and deck_count > 0))
        draw_button(btn_least, enabled=True)
        draw_button(btn_disconnect, enabled=True)

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
        if discard_top:
            img = pygame.transform.smoothscale(load_card_image(discard_top), (120, 170))
            screen.blit(img, (discard_rect.x + 15, discard_rect.y + 18))
        else:
            blank = pygame.transform.smoothscale(load_card_image("BlankCard"), (120, 170))
            screen.blit(blank, (discard_rect.x + 15, discard_rect.y + 18))

        # hand
        hx = panel_right.x + 22
        for i, card in enumerate(hand):
            rect = pygame.Rect(hx + i * (CARD_W + 16), HAND_Y, CARD_W, CARD_H)
            img = pygame.transform.smoothscale(load_card_image(card), (CARD_W, CARD_H))
            if dragging_card == card:
                mx, my = pygame.mouse.get_pos()
                rect.x = mx + offset_x
                rect.y = my + offset_y
            screen.blit(img, rect.topleft)
            pygame.draw.rect(screen, (10, 10, 10), rect, width=2, border_radius=8)

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

    pygame.display.flip()
    clock.tick(60)

    # Events
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

        if state == STATE_MENU:
            ip_input.handle_event(event)

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            pos = event.pos

            if state == STATE_MENU:
                if btn_connect.rect.collidepoint(pos):
                    connect_to_host()

            elif state in (STATE_CONNECTING, STATE_LOBBY, STATE_PLAYING, STATE_RESULTS):
                if btn_disconnect.rect.collidepoint(pos):
                    disconnect()

            if state == STATE_PLAYING:
                if btn_draw.rect.collidepoint(pos) and current_turn == player_id:
                    send_action("draw")
                elif btn_least.rect.collidepoint(pos):
                    send_action("least_count")

                # drag from hand
                hx = panel_right.x + 22
                for i, card in enumerate(hand):
                    rect = pygame.Rect(hx + i * (CARD_W + 16), HAND_Y, CARD_W, CARD_H)
                    if rect.collidepoint(pos) and current_turn == player_id:
                        dragging_card = card
                        offset_x = rect.x - pos[0]
                        offset_y = rect.y - pos[1]
                        break

            if state == STATE_RESULTS and btn_back.rect.collidepoint(pos):
                state = STATE_MENU
                status_line = "Select Least Count, then connect to the host."

        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if state == STATE_PLAYING and dragging_card:
                if current_turn == player_id:
                    mx, my = event.pos
                    drop_rect = pygame.Rect(mx + offset_x, my + offset_y, CARD_W, CARD_H)
                    if discard_rect.colliderect(drop_rect):
                        send_action("discard", dragging_card)
                        # Server will echo authoritative hand via "hand" message.
                dragging_card = None
