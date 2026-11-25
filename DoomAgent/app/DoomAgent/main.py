import asyncio
import os
import shutil
import threading

from strands import Agent, tool
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.tools.browser_client import browser_session
from playwright.async_api import async_playwright
from model.load import load_model

from doom_control import (
    FIRE, THING_NAMES, USE, Route,
    bearing, enter_game, hold, read, turn_needed,
)
from explorer import Explorer
from level_map import Level, fetch_wad
from vision_map import VisionMap

# Point Playwright at system node instead of its bundled binary.
_sys_node = shutil.which("node")
if _sys_node:
    os.environ["PLAYWRIGHT_NODEJS_PATH"] = _sys_node

_bg_loop = asyncio.new_event_loop()
_bg_thread = threading.Thread(target=_bg_loop.run_forever, daemon=True)
_bg_thread.start()

app = BedrockAgentCoreApp()
log = app.logger

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

WEAPON_NAMES = {0: "fist", 1: "pistol", 2: "shotgun", 3: "chaingun",
                4: "rocket", 5: "plasma", 6: "BFG", 7: "chainsaw"}
CARD_NAMES = ["blue", "yellow", "red"]
KEY_MAP = {
    "forward": "ArrowUp", "backward": "ArrowDown",
    "turn_left": "ArrowLeft", "turn_right": "ArrowRight",
    "shoot": FIRE, "use": USE,
    "strafe_left": "a", "strafe_right": "d",
    "run": "Shift",
    "weapon_1": "1", "weapon_2": "2", "weapon_3": "3",
    "weapon_4": "4", "weapon_5": "5", "weapon_6": "6", "weapon_7": "7",
}
NOT_STARTED = "ERROR: Browser not started. Call start_doom_game first."


class DoomSession:
    def __init__(self):
        self.page = None
        self.session_id = None
        self.route = None
        self.vmap = None
        self.explorer = None
        self.step = 0


doom = DoomSession()


def _run(coro):
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop).result()


async def _setup_game(page):
    await enter_game(page)
    level = Level(await fetch_wad(page))
    segments = level.segments()
    doom.page = page
    doom.route = Route([s for s in segments if s[4] in (0, 1)])
    doom.vmap = VisionMap(segments)
    doom.explorer = Explorer(doom.route, doom.vmap, level)
    doom.step = 0


@tool
def start_doom_game() -> str:
    """Start a new Doom game in AgentCore Browser.
    Opens the browser, navigates to Doom, starts a solo game and loads the map memory
    saved by earlier runs. Call this once at the beginning.
    Returns the session ID for live viewing."""

    async def _start():
        doom.browser_ctx = browser_session(REGION)
        client = doom.browser_ctx.__enter__()
        ws_url, headers = client.generate_ws_headers()
        doom.session_id = client.session_id

        doom.pw = await async_playwright().__aenter__()
        browser = await doom.pw.chromium.connect_over_cdp(ws_url, headers=headers)
        await _setup_game(browser.contexts[0].pages[0])

    _run(_start())

    return (
        f"Doom started! Session ID: {doom.session_id}\n"
        f"Watch live: AWS Console → AgentCore → Browser ({REGION}) → View live session\n"
        f"This is run {doom.explorer.runs} on this level. "
        f"Map memory loaded: {doom.explorer.loaded_tiles} tiles known from earlier runs.\n"
        f"Now call read_game_state(), then explore()."
    )


@tool
def read_game_state() -> str:
    """Read the current game state from Doom's memory.
    Returns HP, armor, weapon, ammo, position, view angle, visible awake enemies
    (with distance and how far to turn), objects in view right now, objects remembered
    from earlier (this run or previous runs), keycards and map memory size.
    Also records what is currently in view into the map memory.
    Call this at the start of each turn."""

    if doom.page is None:
        return NOT_STARTED

    s = _run(read(doom.page))
    doom.step += 1
    new_tiles = doom.explorer.observe(s)

    ammo = s["ammo"]
    weapon = WEAPON_NAMES.get(s["weapon"], f"w{s['weapon']}")
    owned = [WEAPON_NAMES.get(n, f"w{n}") for n, have in enumerate(s["weapons"]) if have]
    cards = [name for name, have in zip(CARD_NAMES, s["cards"]) if have]
    exit_known = "yes" if doom.explorer.exit_seen else "not yet"

    lines = [
        f"Step {doom.step}:",
        f"  HP:{s['health']} ARMOR:{s['armor']} WEAPON:{weapon}",
        f"  AMMO: bullets={ammo['bullets']} shells={ammo['shells']} rockets={ammo['rockets']} cells={ammo['cells']}",
        f"  POS: ({s['x']:.0f},{s['y']:.0f}) VIEW ANGLE:{s['angle']:.0f} (0=east, 90=north)",
        f"  WEAPONS: {', '.join(owned)}",
        f"  KEYCARDS: {', '.join(cards) if cards else 'none'}",
        f"  MAP: {len(doom.vmap.tiles)} tiles known, {new_tiles} new, exit switch seen: {exit_known}",
    ]

    enemies = doom.route.threats(s)
    if enemies:
        lines.append("  ENEMIES in view and hunting you:")
        for d, e in enemies[:6]:
            turn = turn_needed(s, bearing(s, e["x"], e["y"]))
            side = "left" if turn > 0 else "right"
            name = THING_NAMES.get(e["ednum"], "enemy")
            lines.append(f"    {name} hp={e['health']} dist={d:.0f} turn {side} {abs(turn):.0f} deg")
    else:
        lines.append("  ENEMIES: none in view")

    in_view = doom.explorer.visible(s)
    if in_view:
        names = [f"{THING_NAMES.get(t['ednum'], kind)} ({d:.0f})" for d, t, kind in in_view[:10]]
        lines.append(f"  OBJECTS IN VIEW NOW (distance): {', '.join(names)}")
    else:
        lines.append("  OBJECTS IN VIEW NOW: none")

    known = doom.explorer.remembered(s)
    lines.append(f"  REMEMBERED OBJECTS (seen earlier in this or a previous run, may be out of view): {len(known)}")
    for d, o in known[:10]:
        lines.append(f"    {o['name']} [{o['kind']}] at ({o['x']},{o['y']}) dist={d:.0f}")

    if doom.explorer.done:
        lines.append("  LEVEL COMPLETE")
    if s["health"] <= 0:
        lines.append("  !! DEAD — game over")

    return "\n".join(lines)


@tool
def explore() -> str:
    """Explore the level for up to 20 seconds using only what has been seen so far.
    Heads for the exit switch once it has been seen, otherwise for the nearest unexplored
    space or unopened door. Steering, door opening, fighting enemies in view and the exit
    switch are handled for you. Everything seen is saved to the map memory for future runs.
    Returns a line starting with Exploring (call again), THREAT, DEAD, or LEVEL COMPLETE."""

    if doom.page is None:
        return NOT_STARTED
    return _run(doom.explorer.step(doom.page))


@tool
def attack_nearest_enemy() -> str:
    """Aim at the nearest visible enemy and fire until it is dead, for up to 8 seconds.
    explore already fights enemies in view. Call this only when explore reports THREAT
    or read_game_state lists enemies and you want them dead before moving on.
    Returns clear (no visible enemies left), timeout (enemies remain, call again) or dead."""

    if doom.page is None:
        return NOT_STARTED
    return _run(doom.route.attack(doom.page))


@tool
def look_at_map() -> str:
    """Show the map memory around the player as text.
    It contains only what has been in view in this run or earlier runs:
    Legend: # wall, D door, W window, . open floor, ? unknown, @ you. North is up."""

    if doom.page is None:
        return NOT_STARTED
    s = _run(read(doom.page))
    doom.explorer.observe(s)
    return doom.vmap.ascii(s["x"], s["y"])


@tool
def press_keys(keys: str, hold_seconds: float = 0.3) -> str:
    """Press game keys simultaneously for `hold_seconds` seconds. Manual escape hatch only.

    Args:
        keys: Comma-separated key names. Valid: forward, backward, turn_left, turn_right,
              strafe_left, strafe_right, shoot, use, run,
              weapon_1 (fist), weapon_2 (pistol), weapon_3 (shotgun),
              weapon_4 (chaingun), weapon_5 (rocket), weapon_6 (plasma), weapon_7 (BFG)
        hold_seconds: How long to hold the keys (0.1 to 0.8 seconds)

    Example: press_keys("backward,strafe_left", 0.5)
    """
    if doom.page is None:
        return NOT_STARTED

    key_list = [k.strip() for k in keys.split(",")]
    mapped = [KEY_MAP[k] for k in key_list if k in KEY_MAP]
    invalid = [k for k in key_list if k not in KEY_MAP]

    if not mapped:
        return f"ERROR: No valid keys. Valid: {', '.join(KEY_MAP.keys())}"

    hold_seconds = min(max(hold_seconds, 0.1), 0.8)
    _run(hold(doom.page, mapped, hold_seconds))

    result = f"Pressed {'+'.join(k for k in key_list if k in KEY_MAP)} for {hold_seconds:.1f}s"
    if invalid:
        result += f" (ignored invalid: {invalid})"
    return result


SYSTEM_PROMPT = """You are a Doom player AI. Your GOAL is to find the exit of E1M1 and press the exit switch.

You start without a map. You only know what you have seen, in this run or in earlier runs.
Everything you see is saved to memory, so later runs start with the map you built.

You decide what to do. The tools do the steering, aiming and door opening.

LOOP — repeat every turn:
1. explore()
2. If it returns a line starting with Exploring, call explore() again.
3. If it returns THREAT, call attack_nearest_enemy() until it returns clear, then explore() again.
4. Every few turns call read_game_state() to check HP, ammo and how much of the map is known.

RULES:
- Stop only when a tool returns LEVEL COMPLETE or DEAD.
- Do not steer with press_keys. Use it only if explore() makes no progress three times in a row,
  for example press_keys("backward,strafe_left", 0.5), then go back to explore().
- press_keys("weapon_3") switches to the shotgun once WEAPONS lists it.
- look_at_map() shows what is known so far. Use it if you need to describe where you are.

Start by calling start_doom_game(), then enter the loop."""

tools = [start_doom_game, read_game_state, explore, attack_nearest_enemy, look_at_map, press_keys]


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

    prompt = payload.get("prompt", "Start playing Doom! Call start_doom_game, then play until LEVEL COMPLETE or DEAD.")

    async for event in agent.stream_async(prompt):
        if not isinstance(event, dict) or "event" not in event:
            continue
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
