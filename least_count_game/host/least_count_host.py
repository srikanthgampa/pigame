import pygame, socket, threading, json, random, os, sys

pygame.init()
screen = pygame.display.set_mode((800, 600))
pygame.display.set_caption("Host - Card Games")
font = pygame.font.SysFont(None, 40)

CARD_PATH = os.path.join("assets", "cards")
def load_card_image(card_name):
    filename = f"{card_name}.png"
    return pygame.image.load(os.path.join(CARD_PATH, filename))

# Networking
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", 5000))
server.listen(8)

players = {}
hands = {}
discard_pile = []
turn_order = []
current_turn = 0
game_started = False
deck = []

STATE_MENU = "menu"
STATE_LOBBY = "lobby"
STATE_PLAYING = "playing"
state = STATE_MENU

start_button = pygame.Rect(320, 400, 160, 50)
discard_rect = pygame.Rect(600, 200, 120, 160)
draw_button = pygame.Rect(600, 400, 120, 50)
least_button = pygame.Rect(600, 470, 120, 50)

host_id = 1
hands[host_id] = []
dragging_card = None
offset_x = offset_y = 0

def build_deck():
    values = [str(v) for v in range(2, 11)] + ["J","Q","K","A"]
    suits = ["S","C","D","H"]
    deck = [f"{v}{s}" for v in values for s in suits]
    deck += ["ZB","ZR"]
    random.shuffle(deck)
    return deck

def deal_cards():
    global game_started, deck
    game_started = True
    deck = build_deck()
    # Deal to host
    hands[host_id] = [deck.pop() for _ in range(5)]
    # Deal to connected players
    for pid in list(players.keys()):
        hands[pid] = [deck.pop() for _ in range(5)]
        try:
            players[pid].send(json.dumps({
                "action":"deal",
                "hand":hands[pid],
                "player_id":pid
            }).encode())
        except Exception as e:
            print(f"Could not deal to Player {pid}: {e}")
    broadcast_state()

def broadcast_state():
    state_msg = {
        "discard": discard_pile,
        "turn": turn_order[current_turn] if turn_order else None,
        "players": list(players.keys()) + [host_id]
    }
    for pid, conn in list(players.items()):
        try:
            conn.send(json.dumps({"action":"update","state":state_msg}).encode())
        except Exception as e:
            print(f"Could not update Player {pid}: {e}")


def accept_loop():
    pid = 2  # host is Player 1
    while True:
        conn, addr = server.accept()
        print(f"Player {pid} connected from {addr}")
        players[pid] = conn
        turn_order.append(pid)
        threading.Thread(target=handle_client, args=(conn, pid), daemon=True).start()
        pid += 1

def handle_client(conn, pid):
    while True:
        try:
            msg = conn.recv(1024).decode()
            if not msg: break
            data = json.loads(msg)
            if not game_started: continue
            if data["action"] == "discard" and pid == turn_order[current_turn]:
                card = data["card"]
                if card in hands[pid]:
                    hands[pid].remove(card)
                    discard_pile.append(card)
                    next_turn()
            elif data["action"] == "draw" and pid == turn_order[current_turn]:
                if deck:
                    hands[pid].append(deck.pop())
                    next_turn()
            elif data["action"] == "least_count":
                end_game()
                break
            broadcast_state()
        except:
            break

def next_turn():
    global current_turn
    if turn_order:
        current_turn = (current_turn + 1) % len(turn_order)

def end_game():
    scores = {pid: sum(card_value(c) for c in hands[pid]) for pid in hands}
    winner = min(scores, key=scores.get)
    for pid, conn in list(players.items()):
        try:
            conn.send(json.dumps({
                "action":"end",
                "scores":scores,
                "winner":winner
            }).encode())
        except:
            pass
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
clock = pygame.time.Clock()

while running:
    screen.fill((0,100,0))

    if state == STATE_MENU:
        screen.blit(font.render("Select Game:", True, (255,255,255)), (300, 200))
        screen.blit(font.render("Least Count", True, (255,255,0)), (320, 260))

    elif state == STATE_LOBBY:
        screen.blit(font.render("Waiting for players...", True, (255,255,255)), (250, 200))
        y = 250
        for pid in players.keys():
            screen.blit(font.render(f"Player {pid} joined", True, (255,255,0)), (300, y))
            y += 40
        pygame.draw.rect(screen, (0,200,0), start_button)
        screen.blit(font.render("START GAME", True, (0,0,0)), (330, 410))

    elif state == STATE_PLAYING:
        # Discard pile
        pygame.draw.rect(screen, (200,0,0), discard_rect)
        if discard_pile:
            img = load_card_image(discard_pile[-1])
            img = pygame.transform.scale(img, (100,140))
            screen.blit(img, (discard_rect.x+10, discard_rect.y+10))

        # Buttons
        pygame.draw.rect(screen, (0,200,0), draw_button)
        screen.blit(font.render("DRAW", True, (0,0,0)), (620, 415))
        pygame.draw.rect(screen, (0,0,200), least_button)
        screen.blit(font.render("LEAST COUNT", True, (255,255,255)), (605, 485))

        # Turn indicator
        if turn_order and turn_order[current_turn] == host_id:
            turn_text = "Your Turn!"
        else:
            turn_text = f"Waiting for Player {turn_order[current_turn]}" if turn_order else "No turn"
        screen.blit(font.render(turn_text, True, (255,255,0)), (50, 50))

        # Draw host hand
        for i, card in enumerate(hands[host_id]):
            rect = pygame.Rect(50 + i*90, 400, 80, 120)
            img = load_card_image(card)
            img = pygame.transform.scale(img, (80,120))
            if dragging_card == card:
                rect.x, rect.y = pygame.mouse.get_pos()
            screen.blit(img, (rect.x, rect.y))

    pygame.display.flip()
    clock.tick(30)

    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        elif state == STATE_MENU and event.type == pygame.MOUSEBUTTONDOWN:
            state = STATE_LOBBY
            turn_order.append(host_id)  # host is Player 1
        elif state == STATE_LOBBY and event.type == pygame.MOUSEBUTTONDOWN:
            if start_button.collidepoint(event.pos):
                if players or hands[host_id]:
                    deal_cards()
                    state = STATE_PLAYING
        elif state == STATE_PLAYING:
            if event.type == pygame.MOUSEBUTTONDOWN:
                pos = event.pos
                if draw_button.collidepoint(pos) and turn_order[current_turn] == host_id:
                    if deck:
                        hands[host_id].append(deck.pop())
                        next_turn()
                        broadcast_state()
                elif least_button.collidepoint(pos):
                    end_game()
                    running = False
                for i, card in enumerate(hands[host_id]):
                    rect = pygame.Rect(50 + i*90, 400, 80, 120)
                    if rect.collidepoint(pos):
                        dragging_card = card
                        offset_x = rect.x - pos[0]
                        offset_y = rect.y - pos[1]
            elif event.type == pygame.MOUSEBUTTONUP:
                if dragging_card:
                    pos = event.pos
                    rect = pygame.Rect(pos[0]+offset_x, pos[1]+offset_y, 80, 120)
                    if discard_rect.colliderect(rect) and turn_order[current_turn] == host_id:
                        discard_pile.append(dragging_card)
                        hands[host_id].remove(dragging_card)
                        next_turn()
                        broadcast_state()
                    dragging_card = None
