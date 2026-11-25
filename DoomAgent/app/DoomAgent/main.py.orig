"""
Doom Agent — Strands agent that plays Doom via AgentCore Browser.

Tools:
  - read_game_state() → returns player health, ammo, position, heading
  - press_keys(keys, hold) → sends keypresses to the browser
  - check_stuck() → returns whether the player is stuck and suggests recovery

The LLM orchestrates the game loop by calling these tools in sequence.
"""

import asyncio
import math
import os
import random

import nest_asyncio
from strands import Agent, tool
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.tools.browser_client import browser_session
from playwright.async_api import async_playwright
from model.load import load_model

nest_asyncio.apply()

app = BedrockAgentCoreApp()
log = app.logger

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
DOOM_URL = "https://silentspacemarine.com/"

# WASM heap addresses (HEAP32 indices) for the player struct
ADDR = {
    "health": 236114, "armor": 236115, "weapon": 236134,
    "ammo": 236145, "cards": 236123, "weapowned": 236136,
    "pos_x": 238573, "pos_y": 238574,
}
WEAPON_NAMES = {0: "fist", 1: "pistol", 2: "shotgun", 3: "chaingun",
                4: "rocket", 5: "plasma", 6: "BFG", 7: "chainsaw"}
KEY_MAP = {
    "forward": "ArrowUp", "backward": "ArrowDown",
    "turn_left": "ArrowLeft", "turn_right": "ArrowRight",
    "shoot": "Control", "use": "e",
    "strafe_left": "a", "strafe_right": "d",
    "run": "Shift",
    "weapon_1": "1", "weapon_2": "2", "weapon_3": "3",
    "weapon_4": "4", "weapon_5": "5", "weapon_6": "6", "weapon_7": "7",
}
MENU_KEYS = {"enter": "Enter"}

# E1M1 "Hangar" waypoints: (x, y, use_door)
# Main route from start to exit switch
E1M1_WAYPOINTS = [
    (1056, -3360, False), (1056, -3072, False),
    (1056, -2880, True),  (1056, -2560, False),
    (1216, -2560, False), (1216, -2304, False),
    (1024, -2304, False), (1024, -2048, False),
    (1088, -1792, True),  (1088, -1536, False),
    (736, -1536, False),  (480, -1536, False),
    (480, -1280, True),   (480, -1024, False),
    (480, -768, False),   (256, -768, False),
    (256, -512, False),   (128, -512, True),
    (128, -256, True),
]
WAYPOINT_REACH_DIST = 60

# JS to read game state from WASM memory
READ_JS = f"""() => {{
    const h = new Int32Array(wasmMemory.buffer);
    return {{
        health: h[{ADDR['health']}], armor: h[{ADDR['armor']}], weapon: h[{ADDR['weapon']}],
        ammo: [h[{ADDR['ammo']}], h[{ADDR['ammo']}+1], h[{ADDR['ammo']}+2], h[{ADDR['ammo']}+3]],
        pos_x: h[{ADDR['pos_x']}], pos_y: h[{ADDR['pos_y']}],
        weapowned: [h[{ADDR['weapowned']}],h[{ADDR['weapowned']}+1],h[{ADDR['weapowned']}+2],h[{ADDR['weapowned']}+3],h[{ADDR['weapowned']}+4],h[{ADDR['weapowned']}+5]],
    }};
}}"""


# ── Shared browser state (per-session) ──────────────────────────────────────
class DoomSession:
    """Holds the browser page and position history for one game session."""

    def __init__(self):
        self.page = None
        self.session_id = None
        self.positions: list[tuple[float, float]] = []
        self.prev_state: dict | None = None
        self.step = 0
        self.stuck_count = 0
        self.last_unstick_dir = "right"
        self.current_wp = 0

    def track_position(self, raw_x: int, raw_y: int) -> dict:
        x, y = raw_x / 65536, raw_y / 65536
        self.positions.append((x, y))
        self.step += 1

        info = {"x": x, "y": y, "moved": 0.0, "heading": "?", "stuck": False, "step": self.step}

        if len(self.positions) >= 2:
            px, py = self.positions[-2]
            dx, dy = x - px, y - py
            dist = math.sqrt(dx*dx + dy*dy)
            info["moved"] = dist

            if dist < 3:
                self.stuck_count += 1
            else:
                self.stuck_count = 0
            info["stuck"] = self.stuck_count >= 3

            if dist > 5:
                angle = math.degrees(math.atan2(dy, dx))
                if angle < 0: angle += 360
                dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
                info["heading"] = dirs[int((angle + 22.5) % 360 / 45)]

        # Circle detection
        if len(self.positions) >= 10:
            sx, sy = self.positions[-10]
            info["progress_10"] = math.sqrt((x-sx)**2 + (y-sy)**2)

        return info


# Global session — lives for the duration of the runtime process
doom = DoomSession()


# ── Tools ───────────────────────────────────────────────────────────────────

@tool
def read_game_state() -> str:
    """Read the current game state from Doom's WASM memory.
    Returns health, armor, ammo, position, heading, and movement info.
    Call this at the start of each turn to see what's happening."""

    if doom.page is None:
        return "ERROR: Browser not started. Call start_doom_game first."

    loop = asyncio.get_event_loop()
    raw = loop.run_until_complete(doom.page.evaluate(READ_JS))
    pos = doom.track_position(raw["pos_x"], raw["pos_y"])

    weapon = WEAPON_NAMES.get(raw["weapon"], f"w{raw['weapon']}")
    ammo = raw["ammo"]
    weapons = [WEAPON_NAMES.get(i, f"w{i}") for i in range(6) if raw["weapowned"][i]]

    lines = [
        f"Step {pos['step']}:",
        f"  HP:{raw['health']} ARMOR:{raw['armor']} WEAPON:{weapon}",
        f"  AMMO: bullets={ammo[0]} shells={ammo[1]} rockets={ammo[3]} cells={ammo[2]}",
        f"  POS: ({pos['x']:.0f},{pos['y']:.0f}) HEADING:{pos['heading']} MOVED:{pos['moved']:.0f}",
        f"  WEAPONS: {', '.join(weapons)}",
    ]

    if doom.prev_state:
        hp_d = raw["health"] - doom.prev_state["health"]
        ammo_d = ammo[0] - doom.prev_state["ammo"][0]
        if hp_d < 0: lines.append(f"  !! TAKING DAMAGE: {hp_d}")
        if ammo_d < 0: lines.append(f"  Fired {-ammo_d} bullets")

    if pos["stuck"]:
        lines.append(f"  !! STUCK for {doom.stuck_count} turns — use force_unstick tool!")
    if pos.get("progress_10", 999) < 30:
        lines.append("  !! GOING IN CIRCLES — try completely different direction")
    if raw["health"] <= 0:
        lines.append("  !! DEAD — game over")

    doom.prev_state = raw
    return "\n".join(lines)


@tool
def press_keys(keys: str, hold: float = 0.3) -> str:
    """Press game keys simultaneously for `hold` seconds.

    Args:
        keys: Comma-separated key names. Valid: forward, backward, turn_left, turn_right,
              strafe_left, strafe_right, shoot, use, run,
              weapon_1 (fist), weapon_2 (pistol), weapon_3 (shotgun),
              weapon_4 (chaingun), weapon_5 (rocket), weapon_6 (plasma), weapon_7 (BFG)
        hold: How long to hold the keys (0.1 to 0.8 seconds)

    Example: press_keys("forward,run", 0.5) — sprint forward for 0.5s
    """
    if doom.page is None:
        return "ERROR: Browser not started."

    key_list = [k.strip() for k in keys.split(",")]
    mapped = [KEY_MAP[k] for k in key_list if k in KEY_MAP]
    invalid = [k for k in key_list if k not in KEY_MAP]

    if not mapped:
        return f"ERROR: No valid keys. Valid: {', '.join(KEY_MAP.keys())}"

    hold = min(max(hold, 0.1), 0.8)

    async def _press():
        for k in mapped:
            await doom.page.keyboard.down(k)
        await asyncio.sleep(hold)
        for k in mapped:
            await doom.page.keyboard.up(k)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_press())

    result = f"Pressed {'+'.join(key_list)} for {hold:.1f}s"
    if invalid:
        result += f" (ignored invalid: {invalid})"
    return result


@tool
def force_unstick() -> str:
    """Force a hard turn to escape when stuck against a wall.
    Call this when read_game_state reports STUCK.
    Automatically picks direction and duration."""

    if doom.page is None:
        return "ERROR: Browser not started."

    doom.last_unstick_dir = "left" if doom.last_unstick_dir == "right" else "right"
    direction = f"turn_{doom.last_unstick_dir}"

    if doom.stuck_count <= 5:
        actions = [direction]
        hold = 0.6
    elif doom.stuck_count <= 10:
        actions = [direction, "forward"]
        hold = 0.7
    else:
        actions = ["backward", random.choice(["turn_left", "turn_right"])]
        hold = 0.8

    mapped = [KEY_MAP[a] for a in actions]

    async def _press():
        for k in mapped:
            await doom.page.keyboard.down(k)
        await asyncio.sleep(hold)
        for k in mapped:
            await doom.page.keyboard.up(k)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_press())

    return f"Forced {'+'.join(actions)} for {hold:.1f}s (stuck count: {doom.stuck_count})"


@tool
def get_navigation() -> str:
    """Get navigation instructions toward the next level waypoint.
    Call this after read_game_state to know WHERE to go.
    Returns which direction to turn/move to reach the next waypoint."""

    if doom.page is None:
        return "ERROR: Browser not started."
    if not doom.positions:
        return "ERROR: Call read_game_state first so I know your position."

    if doom.current_wp >= len(E1M1_WAYPOINTS):
        return "LEVEL COMPLETE! All waypoints reached. Press use near the exit switch!"

    px, py = doom.positions[-1]
    tx, ty, use_door = E1M1_WAYPOINTS[doom.current_wp]
    dx, dy = tx - px, ty - py
    dist = math.sqrt(dx*dx + dy*dy)

    # Check if we reached this waypoint
    if dist < WAYPOINT_REACH_DIST:
        doom.current_wp += 1
        msg = f"Reached waypoint {doom.current_wp - 1}!"
        if use_door:
            msg += " Press USE to open the door here."
        if doom.current_wp >= len(E1M1_WAYPOINTS):
            return msg + " LEVEL COMPLETE!"
        tx, ty, use_door = E1M1_WAYPOINTS[doom.current_wp]
        dx, dy = tx - px, ty - py
        dist = math.sqrt(dx*dx + dy*dy)

    target_angle = math.degrees(math.atan2(dy, dx))
    if target_angle < 0:
        target_angle += 360

    # Estimate player heading from last 2 positions
    heading_str = "unknown"
    turn_instruction = "Turn around to find the right direction"
    if len(doom.positions) >= 2:
        prev_x, prev_y = doom.positions[-2]
        mdx, mdy = px - prev_x, py - prev_y
        move_dist = math.sqrt(mdx*mdx + mdy*mdy)
        if move_dist > 3:
            player_angle = math.degrees(math.atan2(mdy, mdx))
            if player_angle < 0:
                player_angle += 360
            heading_str = f"{player_angle:.0f}°"

            diff = (target_angle - player_angle + 180) % 360 - 180
            if abs(diff) < 20:
                turn_instruction = "You're facing the right way — RUN FORWARD"
            elif diff > 0:
                if abs(diff) > 90:
                    turn_instruction = f"Turn LEFT a LOT ({abs(diff):.0f}°)"
                else:
                    turn_instruction = f"Turn LEFT ({abs(diff):.0f}°)"
            else:
                if abs(diff) > 90:
                    turn_instruction = f"Turn RIGHT a LOT ({abs(diff):.0f}°)"
                else:
                    turn_instruction = f"Turn RIGHT ({abs(diff):.0f}°)"

    lines = [
        f"WAYPOINT {doom.current_wp}/{len(E1M1_WAYPOINTS)-1}: target=({tx},{ty}) dist={dist:.0f}",
        f"  Your heading: {heading_str} | Target bearing: {target_angle:.0f}°",
        f"  >> {turn_instruction}",
    ]
    if use_door:
        lines.append("  ** This waypoint has a DOOR — press USE when you arrive")
    if doom.current_wp < len(E1M1_WAYPOINTS) - 1:
        lines.append(f"  Progress: {doom.current_wp}/{len(E1M1_WAYPOINTS)-1} waypoints reached")

    return "\n".join(lines)


@tool
def start_doom_game() -> str:
    """Start a new Doom game in AgentCore Browser.
    Opens the browser, navigates to Doom, clicks Play Solo, and gets into the game.
    Call this once at the beginning. Returns the session ID for live viewing."""

    async def _start():
        doom.browser_ctx = browser_session(REGION)
        client = doom.browser_ctx.__enter__()
        ws_url, headers = client.generate_ws_headers()
        doom.session_id = client.session_id

        doom.pw = await async_playwright().__aenter__()
        browser = await doom.pw.chromium.connect_over_cdp(ws_url, headers=headers)
        doom.page = browser.contexts[0].pages[0]

        # Navigate and start the game
        await doom.page.goto(DOOM_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        btn = await doom.page.query_selector("text=Play Solo")
        if btn:
            await btn.click()
            await asyncio.sleep(3)

        await asyncio.sleep(8)

        canvas = await doom.page.query_selector("canvas")
        if canvas:
            await canvas.click()
            await asyncio.sleep(1)

        for _ in range(5):
            await doom.page.keyboard.press("Enter")
            await asyncio.sleep(1.5)

        await asyncio.sleep(2)

        # Disable mouse input and block non-gameplay keys
        await doom.page.evaluate("""() => {
            const c = document.querySelector('canvas');
            if (c) {
                for (const evt of ['mousemove','mousedown','mouseup','click','contextmenu','wheel']) {
                    c.addEventListener(evt, e => e.stopImmediatePropagation(), true);
                }
            }
            // Block non-gameplay keys (tab, t, f, etc.)
            const allowed = new Set([
                'ArrowUp','ArrowDown','ArrowLeft','ArrowRight',
                'Control','e','a','d','Shift',
                '1','2','3','4','5','6','7','Enter'
            ]);
            document.addEventListener('keydown', e => {
                if (!allowed.has(e.key)) e.stopImmediatePropagation();
            }, true);
            document.addEventListener('keyup', e => {
                if (!allowed.has(e.key)) e.stopImmediatePropagation();
            }, true);
        }""")

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_start())

    return (
        f"Doom started! Session ID: {doom.session_id}\n"
        f"Watch live: AWS Console → AgentCore → Browser (us-west-2) → View live session\n"
        f"Now call read_game_state() to see the initial state, then use press_keys() to play!"
    )


# ── Strands Agent setup ────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a Doom player AI. Your GOAL is to complete E1M1 by following waypoints to the exit.

GAMEPLAY LOOP — repeat this cycle EVERY turn:
1. read_game_state() — see HP, ammo, position
2. get_navigation() — get direction to next waypoint
3. press_keys() — follow the navigation instruction
4. Back to step 1

NAVIGATION (most important!):
- ALWAYS call get_navigation() after read_game_state(). It tells you exactly where to go.
- Follow its turn instructions precisely: "Turn LEFT 45°" → press_keys("turn_left", 0.2)
- "RUN FORWARD" → press_keys("forward,run", 0.5)
- When navigation says "press USE" → press_keys("use", 0.3) to open doors
- The goal is to reach each waypoint in sequence until LEVEL COMPLETE

COMBAT (only when taking damage):
- If HP drops → enemy nearby: press_keys("shoot,strafe_left", 0.3)
- Kill the enemy, then resume navigation
- Switch to best weapon: shotgun > chaingun > pistol

WEAPONS (switch with weapon_1 through weapon_7):
- weapon_3: shotgun (best general weapon)
- weapon_4: chaingun (good for groups)
- weapon_5: rocket launcher (DON'T use at close range)
- Switch to shotgun as soon as you pick one up

STUCK RECOVERY:
- If STUCK → call force_unstick(), then get_navigation() to reorient
- If CIRCLES → turn to a completely new direction, then check navigation

RULES:
- NEVER skip get_navigation() — it's your compass through the level
- Follow waypoints in order — don't wander randomly
- Keep playing until LEVEL COMPLETE or dead
- Play for at least 50 turns

Start by calling start_doom_game(), then enter the gameplay loop."""

tools = [start_doom_game, read_game_state, get_navigation, press_keys, force_unstick]


def _make_conversation_manager():
    return NullConversationManager()


def agent_factory():
    from collections import OrderedDict
    cache = OrderedDict()

    def get_or_create_agent(session_id):
        if session_id in cache:
            cache.move_to_end(session_id)
            return cache[session_id]
        if len(cache) >= 4:
            cache.popitem(last=False)
        cache[session_id] = Agent(
            model=load_model(),
            system_prompt=SYSTEM_PROMPT,
            tools=tools,
            conversation_manager=_make_conversation_manager(),
        )
        return cache[session_id]

    return get_or_create_agent


get_or_create_agent = agent_factory()


@app.entrypoint
async def invoke(payload, context):
    log.info("Invoking Doom Agent...")

    session_id = getattr(context, 'session_id', 'default-session')
    agent = get_or_create_agent(session_id)

    prompt = payload.get("prompt", "Start playing Doom! Call start_doom_game, then play for 50 turns.")

    async for event in agent.stream_async(prompt):
        if not isinstance(event, dict) or "event" not in event:
            continue
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
