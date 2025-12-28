import pygame, socket, json, os, sys

pygame.init()
screen = pygame.display.set_mode((800, 600))
pygame.display.set_caption("Card Games")
font = pygame.font.SysFont(None, 40)

# Path to card images
CARD_PATH = os.path.join("assets", "cards")
def load_card_image(card_name):
    filename = f"{card_name}.png"
    return pygame.image.load(os.path.join(CARD_PATH, filename))

# Host IP (your Pi host)
host_ip = "192.168.1.247"
client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Game states
STATE_MENU = "menu"
STATE_CONNECTING = "connecting"
STATE_PLAYING = "playing"
state = STATE_MENU

# Player state
hand = []
discard_top = None
current_turn = None
player_id = None
dragging_card = None
offset_x = offset_y = 0

# UI zones
discard_rect = pygame.Rect(600, 200, 120, 160)
draw_button = pygame.Rect(600, 400, 120, 50)
least_button = pygame.Rect(600, 470, 120, 50)

def send_action(action, card=None):
    msg = {"action":action}
    if card: msg["card"] = card
    try:
        client.send(json.dumps(msg).encode())
    except:
        pass

running = True
clock = pygame.time.Clock()

while running:
    screen.fill((0,100,0))

    # --- MENU SCREEN ---
    if state == STATE_MENU:
        screen.blit(font.render("Select Game:", True, (255,255,255)), (300, 200))
        screen.blit(font.render("Least Count", True, (255,255,0)), (320, 260))

    # --- CONNECTING SCREEN ---
    elif state == STATE_CONNECTING:
        screen.blit(font.render("Connecting to game...", True, (255,255,255)), (250, 250))

    # --- PLAYING SCREEN ---
    elif state == STATE_PLAYING:
        # Discard pile
        pygame.draw.rect(screen, (200,0,0), discard_rect)
        if discard_top:
            img = load_card_image(discard_top)
            img = pygame.transform.scale(img, (100,140))
            screen.blit(img, (discard_rect.x+10, discard_rect.y+10))

        # Buttons
        pygame.draw.rect(screen, (0,200,0), draw_button)
        screen.blit(font.render("DRAW", True, (0,0,0)), (620, 415))
        pygame.draw.rect(screen, (0,0,200), least_button)
        screen.blit(font.render("LEAST COUNT", True, (255,255,255)), (605, 485))

        # Turn indicator
        if current_turn == player_id:
            turn_text = "Your Turn!"
        else:
            turn_text = f"Waiting for Player {current_turn}"
        screen.blit(font.render(turn_text, True, (255,255,0)), (50, 50))

        # Draw hand
        for i, card in enumerate(hand):
            rect = pygame.Rect(50 + i*90, 400, 80, 120)
            img = load_card_image(card)
            img = pygame.transform.scale(img, (80,120))
            if dragging_card == card:
                rect.x, rect.y = pygame.mouse.get_pos()
            screen.blit(img, (rect.x, rect.y))

    pygame.display.flip()
    clock.tick(30)

    # --- EVENT HANDLING ---
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

        # Menu selection
        elif state == STATE_MENU and event.type == pygame.MOUSEBUTTONDOWN:
            state = STATE_CONNECTING
            try:
                client.connect((host_ip, 5000))
                client.setblocking(False)  # only after connect
            except BlockingIOError:
                client.setblocking(False)

        # Gameplay events
        elif state == STATE_PLAYING:
            if event.type == pygame.MOUSEBUTTONDOWN:
                pos = event.pos
                if draw_button.collidepoint(pos):
                    send_action("draw")
                elif least_button.collidepoint(pos):
                    send_action("least_count")
                for i, card in enumerate(hand):
                    rect = pygame.Rect(50 + i*90, 400, 80, 120)
                    if rect.collidepoint(pos):
                        dragging_card = card
                        offset_x = rect.x - pos[0]
                        offset_y = rect.y - pos[1]

            elif event.type == pygame.MOUSEBUTTONUP:
                if dragging_card:
                    pos = event.pos
                    rect = pygame.Rect(pos[0]+offset_x, pos[1]+offset_y, 80, 120)
                    if discard_rect.colliderect(rect):
                        send_action("discard", dragging_card)   # ✅ FIXED LINE
                        hand.remove(dragging_card)
                    dragging_card = None

    # --- SERVER UPDATES ---
    try:
        msg = client.recv(1024).decode()
        if msg:
            data = json.loads(msg)
            if data["action"] == "deal":
                hand = data["hand"]
                player_id = data.get("player_id", 1)
                state = STATE_PLAYING
            elif data["action"] == "update":
                state = STATE_PLAYING
                st = data["state"]
                discard_top = st["discard"][-1] if st["discard"] else None
                current_turn = st["turn"]
            elif data["action"] == "end":
                print("Game Over! Scores:", data["scores"], "Winner:", data["winner"])
                running = False
    except BlockingIOError:
        pass
    except:
        pass
