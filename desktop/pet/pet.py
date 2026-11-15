import math
import json
import ctypes
from ctypes import wintypes
import os
import random
import sys
import time
import tkinter as tk
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageTk

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
ATLAS = ROOT / 'bee-parts.png'
KEY = '#ff00ff'
WIDTH, HEIGHT = 260, 280
DISPLAY_WIDTH, DISPLAY_HEIGHT = 130, 140

def parts(variant='black'):
    atlas = Image.open(ROOT / ('yellow-bee-parts.png' if variant == 'yellow' else 'bee-parts.png')).convert('RGBA')
    regions = {'head': (30, 125, 490, 570), 'body': (525, 285, 900, 555),
               'wing_l': (910, 285, 1170, 535), 'wing_r': (1200, 285, 1455, 535),
               'laptop': (1480, 310, 1775, 530)}
    result = {}
    for name, box in regions.items():
        image = atlas.crop(box)
        # Discard translucent AI-generated glow before framing each part.
        mask = image.getchannel('A').point(lambda a: 255 if a >= 210 else 0)
        image.putalpha(mask)
        bbox = mask.getbbox()
        result[name] = image.crop(bbox)
    return result

def resized(im, width, height):
    return im.resize((width, height), Image.Resampling.NEAREST)

def remove_neighbor_fragments(image):
    """Clear small disconnected slivers along sprite-cell boundaries."""
    alpha = image.getchannel('A')
    width, height = image.size
    pixels = alpha.load()
    visited = set()
    for y in range(height):
        for x in list(range(min(8, width))) + list(range(max(0, width - 8), width)):
            if (x, y) in visited or pixels[x, y] < 128:
                continue
            stack, component = [(x, y)], []
            visited.add((x, y))
            while stack:
                px, py = stack.pop()
                component.append((px, py))
                for nx, ny in ((px-1,py), (px+1,py), (px,py-1), (px,py+1)):
                    if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in visited and pixels[nx, ny] >= 128:
                        visited.add((nx, ny))
                        stack.append((nx, ny))
            if len(component) < 500:
                for px, py in component:
                    pixels[px, py] = 0
    image.putalpha(alpha.point(lambda value: 255 if value >= 128 else 0))
    return image

def expression_sprites():
    sheet = Image.open(ROOT / 'expressions-clean.png').convert('RGBA')
    sprites = []
    for n in range(4):
        im = sheet.crop((round(n * sheet.width / 4), 100, round((n + 1) * sheet.width / 4), 650))
        mask = Image.new('L', im.size, 0)
        px, target = im.load(), mask.load()
        for y in range(im.height):
            for x in range(im.width):
                r, g, b, a = px[x, y]
                if a >= 180 and g > 180 and g > r * 1.15 and g > b * 1.4:
                    target[x, y] = 255
        mask = mask.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
        bbox = mask.getbbox()
        # Preserve generated silhouettes while keeping the display's green uniform.
        shape = Image.new('RGBA', im.size, (164, 240, 0, 0))
        shape.putalpha(mask)
        sprites.append(shape.crop(bbox))
    return sprites

def animated_lie_face(frame, sprites, dx=0, dy=0, question=0.0, squash=0.0):
    """Replace only green eye pixels on the last lying pose; keep the artwork."""
    frame = frame.copy()
    pixels = frame.load()
    for y in range(166, 199):
        for x in range(147, 201):
            r, g, b, a = pixels[x, y]
            if a > 128 and g > 45 and g > r * 1.25 and g > b * 1.3:
                pixels[x, y] = (12, 12, 12, 255)
    q = max(0., min(1., question))
    index = min(3, int(q * 4))
    sprite = sprites[index]
    width = 43 if index < 3 else 34
    height = 23 if index == 0 else 30
    height = max(12, round(height * (1 - .28 * squash * q)))
    sprite = sprite.resize((width, height), Image.Resampling.NEAREST)
    frame.alpha_composite(sprite, (round(174 - width / 2) + dx, 168 + dy + round(3 * squash * q)))
    return frame

class GUITHREADINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('hwndActive', wintypes.HWND), ('hwndFocus', wintypes.HWND),
                ('hwndCapture', wintypes.HWND), ('hwndMenuOwner', wintypes.HWND),
                ('hwndMoveSize', wintypes.HWND), ('hwndCaret', wintypes.HWND),
                ('rcCaret', wintypes.RECT)]

def caret_position():
    user32 = ctypes.windll.user32
    foreground = user32.GetForegroundWindow()
    thread_id = user32.GetWindowThreadProcessId(foreground, None)
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if not thread_id or not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)) or not info.hwndCaret:
        return None
    point = wintypes.POINT(info.rcCaret.left, info.rcCaret.bottom)
    if not user32.ClientToScreen(info.hwndCaret, ctypes.byref(point)):
        return None
    return point.x, point.y

def gaze_offset(target, bounds):
    """Use absolute screen coordinates, never cursor movement direction."""
    left, top, right, bottom = bounds
    center_x, center_y = (left + right) / 2, (top + bottom) / 2
    dx = round(max(-4, min(4, (target[0] - center_x) / 100)))
    dy = round(max(-3, min(1, (target[1] - center_y) / 100)))
    squash = max(0., min(1., (target[1] - bottom) / 180))
    return dx, dy, squash

def window_screen_bounds(root):
    # GetCursorPos and GetGUIThreadInfo use Windows screen coordinates; get
    # the window rectangle from the same API so DPI scaling cannot mix spaces.
    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    hwnd = user32.GetAncestor(root.winfo_id(), 2) or root.winfo_id()
    rect = wintypes.RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return rect.left, rect.top, rect.right, rect.bottom
    x, y = root.winfo_rootx(), root.winfo_rooty()
    return x, y, x + DISPLAY_WIDTH, y + DISPLAY_HEIGHT

def sprite_frames(filename, columns, rows, y_bounds=None, variant='black'):
    atlas = Image.open(ROOT / (('yellow-' if variant == 'yellow' else '') + filename)).convert('RGBA')
    result = []
    for row in range(rows):
        for col in range(columns):
            x0, x1 = round(col * atlas.width / columns), round((col + 1) * atlas.width / columns)
            y0, y1 = (round(row * atlas.height / rows), round((row + 1) * atlas.height / rows)) if y_bounds is None else y_bounds
            image = atlas.crop((x0, y0, x1, y1))
            if filename == 'lie-work.png':
                image = remove_neighbor_fragments(image)
            if filename == 'lie-work.png' and col == 4:
                # Bring the falling laptop inside the viewport instead of
                # clipping it at the top while keeping the pet's feet fixed.
                laptop = image.crop((0, 0, image.width, 160))
                ImageDraw.Draw(image).rectangle((0, 0, image.width, 159), fill=(0, 0, 0, 0))
                image.alpha_composite(laptop, (0, 65))
            # Both dimensions use the same factor so the character never
            # stretches. Place the feet on the same baseline as the idle pet.
            scale, ground = {'transition.png': (0.99, 310), 'lie.png': (0.79, 332),
                             'fly.png': (0.88, 307), 'typing.png': (0.56, 409),
                             'lie-work.png': (0.78, 438)}[filename]
            image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.NEAREST)
            canvas = Image.new('RGBA', (WIDTH, HEIGHT), (0, 0, 0, 0))
            top = round(245-ground*scale) + (14 if filename == 'fly.png' else 0)
            horizontal = round(10 * max(0, 1 - (col / 5))) if filename == 'lie-work.png' else 0
            canvas.paste(image, ((WIDTH-image.width)//2 + horizontal, top))
            result.append(canvas)
    return result

def status_frames(filename, variant):
    atlas = Image.open(ROOT / ('aligned-' + ('yellow-' if variant == 'yellow' else '') + filename)).convert('RGBA')
    sequences = []
    for row in range(3):
        frames = []
        for col in range(6):
            frames.append(atlas.crop((col*WIDTH, row*HEIGHT, (col+1)*WIDTH, (row+1)*HEIGHT)))
        sequences.append(frames)
    return sequences

def thinking_gaze(frame, offset):
    """Move only the right eye inside the face; keep the rest of the sprite fixed."""
    frame = frame.copy()
    pixels = frame.load()
    points = []
    for y in range(85, 136):
        for x in range(138, 180):
            r, g, b, a = pixels[x, y]
            if a > 200 and g > 130 and g > r * 1.25 and g > b * 1.4:
                points.append((x, y, pixels[x, y]))
    for x, y, _ in points:
        pixels[x, y] = (16, 16, 16, 255)
    for x, y, color in points:
        pixels[x + offset, y] = color
    return frame

class BeePet:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('BeeCode Pet')
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-transparentcolor', KEY)
        self.root.configure(bg=KEY)
        self.canvas = tk.Canvas(self.root, width=DISPLAY_WIDTH, height=DISPLAY_HEIGHT, bg=KEY,
                                bd=0, highlightthickness=0)
        self.canvas.pack()
        self.preferences_path = Path(os.environ.get('APPDATA', str(Path.home()))) / 'BeeCodePet' / 'preferences.json'
        try:
            self.variant = json.loads(self.preferences_path.read_text(encoding='utf-8')).get('variant', 'black')
        except (OSError, ValueError, AttributeError):
            self.variant = 'black'
        if self.variant not in ('black', 'yellow'):
            self.variant = 'black'
        self.load_variant(self.variant)
        self.expressions = expression_sprites()
        self.started = time.monotonic()
        self.mode = 'idle'
        self.phase = 'idle'
        self.phase_started = self.started
        self.dragging = False
        self.drag = None
        self.current = None
        self.last_frame = None
        self.visual_state = None
        self.blend_from = None
        self.blend_start = 0
        self.blink_at = self.started + 3
        self.blink_until = 0
        self.cursor_last = None
        self.cursor_motion_at = 0
        self.keys_were_down = False
        self.typing_reaction_at = 0
        self.typing_started_at = 0
        self.watch_at = self.started + 18
        self.watch_started = 0
        self.manual_lie = False
        self.fly_started = 0
        self.fly_at = self.started + random.uniform(35, 55)
        self.status_key = None
        self.status_at = 0
        self.status_finishing = False
        self.status_return_phase = 'idle'
        self.bridge_path = self.preferences_path.parent / 'beecode-state.json'
        self.bridge_seen = None
        self.bridge_poll = 0
        self.bridge_managed = False
        self.bridge_state = None
        self.bridge_hidden = False
        self.idle_animation_at = self.started + random.uniform(60, 100)
        x = max(0, self.root.winfo_screenwidth() - DISPLAY_WIDTH - 80)
        y = max(0, self.root.winfo_screenheight() - DISPLAY_HEIGHT - 100)
        self.root.geometry(f'{DISPLAY_WIDTH}x{DISPLAY_HEIGHT}+{x}+{y}')
        self.menu = tk.Menu(self.root, tearoff=False)
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.move)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        self.canvas.bind('<Button-3>', self.open_menu)
        self.root.bind('<Escape>', lambda _e: self.root.destroy())
        self.tick()

    def set_mode(self, mode):
        if mode == self.mode:
            return
        was_lying = self.manual_lie or bool(self.watch_started)
        self.status_key = None
        self.status_finishing = False
        self.mode = mode
        self.manual_lie = False
        self.phase = ('lie_enter' if was_lying else 'enter') if mode == 'working' else 'exit'
        self.phase_started = time.monotonic()

    def load_variant(self, variant):
        self.images = parts(variant)
        self.transition_frames = sprite_frames('transition.png', 8, 1, (180, 520), variant)
        self.typing_frames = sprite_frames('typing.png', 4, 2, variant=variant)
        self.lie_frames = sprite_frames('lie.png', 8, 1, (180, 520), variant)
        self.fly_frames = sprite_frames('fly.png', 8, 1, (180, 520), variant)
        self.lie_work_frames = sprite_frames('lie-work.png', 8, 1, (100, 600), variant)
        a, b, c = (status_frames('status-'+name+'.png', variant) for name in 'abc')
        self.status_animations = dict(zip(('Думает','Ждёт тебя','Готово','Ошибка','Остановлено','Нет сети','Потянуться','Поправить наушники','Почистить крыло'), a+b+c))
        self.thinking_frames = [thinking_gaze(a[0][-1], dx) for dx in (0, 2, 3, 2, 0, -2, -3, -2)]

    def set_variant(self, variant):
        if variant == self.variant:
            return
        self.load_variant(variant)
        self.variant = variant
        self.last_frame = None
        self.blend_from = None
        try:
            self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
            self.preferences_path.write_text(json.dumps({'variant': variant}), encoding='utf-8')
        except OSError:
            pass

    def press(self, event):
        self.dragging = True
        self.drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def move(self, event):
        if self.drag:
            x, y = event.x_root - self.drag[0], event.y_root - self.drag[1]
            self.root.geometry(f'+{max(0, x)}+{max(0, y)}')

    def release(self, _event):
        self.dragging = False
        self.drag = None

    def open_menu(self, event):
        try:
            self.menu.delete(0, 'end')
            self.menu.add_command(label='Настройки пета — в BeeCode', state='disabled')
            self.menu.add_command(label='Закрыть пета', command=self.root.destroy)
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def start_fly(self):
        if self.mode == 'idle' and self.phase == 'idle':
            self.manual_lie = False
            self.watch_started = 0
            self.fly_started = time.monotonic()

    def play_status(self, name):
        self.watch_started = 0
        self.manual_lie = False
        self.fly_started = 0
        self.status_return_phase = 'typing' if self.mode == 'working' else 'idle'
        if name in ('Думает', 'Ждёт тебя', 'Нет сети', 'Ошибка') and self.mode != 'working':
            self.mode = 'working'
            self.status_return_phase = 'typing'
        self.status_key = name
        self.status_at = time.monotonic()
        self.status_finishing = False

    def finish_status(self):
        if self.status_key:
            self.status_finishing = True
            self.status_at = time.monotonic()

    def toggle_lie(self):
        if self.manual_lie:
            self.manual_lie = False
            self.watch_started = time.monotonic() - 16.88
        else:
            self.fly_started = 0
            self.manual_lie = True
            self.watch_started = time.monotonic()

    def tick(self):
        now = time.monotonic()
        if now >= self.bridge_poll:
            self.bridge_poll = now + .2
            bridge = {}
            try:
                bridge = json.loads(self.bridge_path.read_text(encoding='utf-8'))
                revision = bridge.get('revision')
                if revision != self.bridge_seen:
                    self.bridge_seen = revision
                    if not bridge.get('enabled', False):
                        if self.bridge_managed:
                            self.root.destroy()
                            return
                        bridge = {}
                    else:
                        self.bridge_managed = True
                    scene = bridge.get('scene')
                    scene_valid = time.time() - bridge.get('scene_time', 0) < 8
                    hide = self.bridge_managed and scene == 'settings' and scene_valid
                    if hide != self.bridge_hidden:
                        self.bridge_hidden = hide
                        if hide:
                            self.root.withdraw()
                        else:
                            self.root.deiconify()
                    landing = bridge.get('landing')
                    if not hide and isinstance(landing, dict):
                        try:
                            x = max(0, min(self.root.winfo_screenwidth() - DISPLAY_WIDTH, int(landing['x'])))
                            y = max(0, min(self.root.winfo_screenheight() - DISPLAY_HEIGHT, int(landing['y'])))
                            self.root.geometry(f'+{x}+{y}')
                        except (KeyError, ValueError, TypeError):
                            pass
                    variant = bridge.get('variant')
                    if variant in ('black', 'yellow'):
                        self.set_variant(variant)
                    state = bridge.get('state')
                    if state == self.bridge_state:
                        state = None
                    else:
                        self.bridge_state = state
                    if state == 'working':
                        if self.status_key in ('Думает', 'Ждёт тебя', 'Нет сети', 'Ошибка'):
                            self.finish_status()
                        elif self.mode != 'working':
                            self.set_mode('working')
                    elif state == 'thinking':
                        if self.status_key != 'Думает':
                            self.play_status('Думает')
                    elif state in ('done', 'error', 'stopped', 'waiting'):
                        self.play_status({'done':'Готово', 'error':'Ошибка', 'stopped':'Остановлено', 'waiting':'Ждёт тебя'}[state])
            except (OSError, ValueError, AttributeError):
                pass
            if self.bridge_hidden and time.time() - bridge.get('scene_time', 0) >= 8:
                self.bridge_hidden = False
                self.root.deiconify()
        t = now - self.started
        point = wintypes.POINT()
        # Only the fact that a key is pressed is used; no key identities or text are stored.
        keys_down = any(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000 for vk in (list(range(0x30, 0x5B)) + [0x20, 0x08]))
        if keys_down and not self.keys_were_down and self.watch_started:
            if now - self.typing_reaction_at >= 2:
                self.typing_started_at = now
            self.typing_reaction_at = now
            # Keep the lying pose while the user is actively typing.
            if now - self.watch_started > 5:
                self.watch_started = now - 5
        self.keys_were_down = keys_down
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            cursor = (point.x, point.y)
            if self.cursor_last is not None and abs(cursor[0] - self.cursor_last[0]) + abs(cursor[1] - self.cursor_last[1]) > 5:
                self.cursor_motion_at = now
            self.cursor_last = cursor
        if self.mode == 'idle' and self.phase == 'idle' and not self.dragging and not self.watch_started and not self.fly_started and now >= self.watch_at and now - self.cursor_motion_at < 2:
            self.watch_started = now
        watching = bool(self.watch_started and self.mode == 'idle' and self.phase == 'idle' and not self.dragging)
        if self.watch_started and (not watching or (not self.manual_lie and now - self.watch_started > 18)):
            self.watch_started = 0
            self.watch_at = now + random.uniform(25, 45)
            watching = False
        if now >= self.blink_at:
            self.blink_until = now + .14
            self.blink_at = now + random.uniform(2.5, 5.5)
        bob = int(3 * math.sin(t * 2.1)) if not self.dragging else -5
        flap = int(8 * math.sin(t * (9 if self.dragging else 3.5)))
        frame = Image.new('RGBA', (WIDTH, HEIGHT), (0, 0, 0, 0))
        def place(part, box, angle=0):
            sprite = resized(self.images[part], box[2], box[3])
            if angle:
                sprite = sprite.rotate(angle, resample=Image.Resampling.NEAREST, expand=False)
            frame.alpha_composite(sprite, (box[0], box[1] + bob))
        working = self.mode == 'working'
        place('wing_l', (22, 137, 74, 75), flap)
        place('wing_r', (164, 137, 74, 75), -flap)
        place('body', (75, 151, 110, 88))
        tilt = int(2 * math.sin(t * 1.2))
        if watching and self.cursor_last:
            tilt = max(-9, min(9, int((self.cursor_last[0] - self.root.winfo_rootx() - DISPLAY_WIDTH / 2) / 80)))
        place('head', (45, 18, 170, 159), tilt)
        if now < self.blink_until:
            draw = ImageDraw.Draw(frame)
            draw.rectangle((111, 119 + bob, 150, 141 + bob), fill=(12, 12, 12, 255))
            draw.line((113, 131 + bob, 123, 131 + bob), fill='#a8f500', width=3)
            draw.line((138, 131 + bob, 148, 131 + bob), fill='#a8f500', width=3)
        # One shared standing pose anchors every sequence at its endpoints.
        idle_frame = frame.copy()
        if working:
            place('laptop', (98, 183, 115, 75), int(2 * math.sin(t * 5)))
        if watching:
            elapsed = now - self.watch_started
            index = min(7, int(elapsed / .14)) if self.manual_lie or elapsed < 16.88 else max(0, 7 - int((elapsed - 16.88) / .14))
            frame = idle_frame.copy() if index == 0 else self.lie_frames[index].copy()
            if index == 7:
                age = now - self.typing_reaction_at
                reaction = min(1, (now - self.typing_started_at) / .35, (2.0 - age) / .35) if 0 <= age < 2.0 else 0
                target = (caret_position() if reaction > 0 else None) or self.cursor_last
                bounds = window_screen_bounds(self.root)
                target = target or ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
                dx, dy, squash = gaze_offset(target, bounds)
                frame = animated_lie_face(frame, self.expressions, dx, dy, max(0, reaction), squash)
        if self.mode == 'idle' and self.phase == 'idle' and not self.dragging and not watching and not self.fly_started and now >= self.fly_at:
            self.fly_started = now
        if self.fly_started:
            index = int((now - self.fly_started) / .18)
            if self.mode != 'idle' or self.dragging or index >= len(self.fly_frames):
                self.fly_started = 0
                self.fly_at = now + random.uniform(45, 75)
            else:
                frame = idle_frame.copy() if index in (0, len(self.fly_frames)-1) else self.fly_frames[index].copy()
        if self.phase in ('enter', 'lie_enter', 'exit', 'typing'):
            index = int((now - self.phase_started) / .16)
            if self.phase in ('enter', 'lie_enter'):
                frames = self.lie_work_frames if self.phase == 'lie_enter' else self.transition_frames
                if index >= len(frames):
                    self.phase, self.phase_started, index = 'typing', now, 0
                    frame = self.typing_frames[0].copy()
                else:
                    if self.phase == 'lie_enter' and index == 0:
                        frame = self.lie_frames[-1].copy()
                    elif self.phase == 'lie_enter' and index == len(frames) - 1:
                        frame = self.typing_frames[0].copy()
                    else:
                        frame = idle_frame.copy() if index == 0 and self.phase == 'enter' else frames[index].copy()
            elif self.phase == 'exit':
                if index >= len(self.transition_frames):
                    self.phase = 'idle'
                    frame = idle_frame.copy()
                else:
                    frame = idle_frame.copy() if index == len(self.transition_frames)-1 else self.transition_frames[-1-index].copy()
                    if index == 5:
                        draw = ImageDraw.Draw(frame)
                        for x, y, s in [(187,85,7),(200,75,5),(196,94,6),(210,88,4)]:
                            draw.rectangle((x,y,x+s,y+s), fill='#f51d9b')
            else:
                frame = self.typing_frames[index % len(self.typing_frames)].copy()
        if self.mode == 'idle' and self.phase == 'idle' and not watching and not self.fly_started and not self.status_key and now >= self.idle_animation_at:
            self.play_status(random.choice(('Потянуться','Поправить наушники','Почистить крыло')))
            self.idle_animation_at = now + random.uniform(90, 150)
        if self.status_key:
            sequence = self.status_animations[self.status_key]
            index = int((now-self.status_at)/.24)
            looping = self.status_key in ('Думает', 'Ждёт тебя', 'Нет сети', 'Ошибка')
            if self.status_finishing:
                if index >= len(sequence):
                    self.status_key = None
                    self.status_finishing = False
                    self.phase = self.status_return_phase
                    self.phase_started = now
                else:
                    frame = sequence[-1-index].copy()
            elif index >= len(sequence) and looping:
                if self.status_key == 'Думает':
                    frame = self.thinking_frames[(index-len(sequence)) % len(self.thinking_frames)].copy()
                elif self.status_key == 'Ошибка':
                    frame = sequence[-1].copy()
                else:
                    cycle = sequence + sequence[-2:0:-1]
                    frame = cycle[(index-len(sequence)) % len(cycle)].copy()
            elif index >= len(sequence):
                ended = self.status_key
                self.status_key = None
                if ended in ('Готово', 'Остановлено'):
                    self.mode = 'idle'
                    self.phase, self.phase_started = 'exit', now
                else:
                    self.phase, self.phase_started = self.status_return_phase, now
            else:
                frame = sequence[index].copy()
        visual_state = ('status:' + self.status_key) if self.status_key else (self.phase if self.phase != 'idle' else ('watch' if watching else 'fly' if self.fly_started else 'idle'))
        if self.visual_state != visual_state:
            self.blend_from = self.last_frame.copy() if self.last_frame else None
            self.blend_start = now
            self.visual_state = visual_state
        if self.blend_from is not None:
            fraction = min(1.0, (now - self.blend_start) / .28)
            if fraction < 1:
                frame = Image.blend(self.blend_from, frame, fraction)
            else:
                self.blend_from = None
        self.last_frame = frame.copy()
        # Windows colour-key transparency is binary. Never blend edges with
        # the magenta key: those blended pixels would remain visible as halos.
        alpha = frame.getchannel('A')
        opaque = alpha.point(lambda value: 255 if value >= 128 else 0)
        outline = opaque.filter(ImageFilter.MaxFilter(5))
        dark = Image.new('RGB', frame.size, (15, 15, 15))
        dark.paste(frame, mask=alpha)
        background = Image.new('RGB', frame.size, (255, 0, 255))
        background.paste((155, 94, 231), mask=outline)
        background.paste(dark, mask=opaque)
        self.current = ImageTk.PhotoImage(background.resize((DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.Resampling.NEAREST))
        self.canvas.delete('all')
        self.canvas.create_image(0, 0, image=self.current, anchor='nw')
        self.root.after(50, self.tick)

if __name__ == '__main__':
    BeePet().root.mainloop()
