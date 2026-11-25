"""
Doom Agent — An AI agent that plays Doom via AgentCore Browser.

Explores any DOOM level using a grid-based map built from movement data.
The agent tracks visited cells, detects walls by failed movement, prefers
unvisited areas, and remembers locked doors to revisit after finding keycards.

The LLM is consulted only for combat decisions (when health drops).

Usage:
    python doom_agent.py
"""

import asyncio
import json
import math
import os
import random
import re
import time

import boto3
import nest_asyncio
from playwright.async_api import async_playwright, Page

nest_asyncio.apply()

# ── Config ──────────────────────────────────────────────────────────────────
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
MODEL_ID = os.environ.get("DOOM_MODEL_ID", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")
ACTION_INTERVAL = 0.15
MAX_STEPS = int(os.environ.get("MAX_STEPS", "500"))
DOOM_URL = "https://silentspacemarine.com/"

# Navigation tuning
CELL_SIZE = 128            # grid cell size in map units (rooms are 128-512)
STUCK_THRESHOLD = 4        # steps without movement before unstick maneuver
ANGLE_TOLERANCE = 15       # degrees — close enough to face target
USE_PRESS_INTERVAL = 6     # press "use" every N steps (opens doors/switches)
DOOR_STUCK_THRESHOLD = 8   # stuck steps at same spot before marking as locked door

# WASM heap addresses (HEAP32 indices)
ADDR_HEALTH    = 236114
ADDR_ARMOR     = 236115
ADDR_WEAPON    = 236134
ADDR_AMMO      = 236145
ADDR_MAXAMMO   = 236149
ADDR_CARDS     = 236123
ADDR_WEAPOWNED = 236136
ADDR_POS_X     = 238573    # fixed-point (value / 65536 = map units)
ADDR_POS_Y     = 238574
ADDR_ANGLE     = 238575    # fixed-point angle

WEAPON_NAMES = {0: "fist", 1: "pistol", 2: "shotgun", 3: "chaingun",
                4: "rocket", 5: "plasma", 6: "BFG", 7: "chainsaw"}

VALID_KEYS = {
    "forward":      "ArrowUp",
    "backward":     "ArrowDown",
    "turn_left":    "ArrowLeft",
    "turn_right":   "ArrowRight",
    "shoot":        "Control",
    "use":          "e",
    "strafe_left":  "a",
    "strafe_right": "d",
    "run":          "Shift",
    "weapon_1":     "1",
    "weapon_2":     "2",
    "weapon_3":     "3",
    "enter":        "Enter",
}

MEMORY_NAME = os.environ.get("DOOM_MEMORY_NAME", "DoomAgentMapMemory")
MEMORY_ACTOR = "doom-agent"

bedrock = boto3.client("bedrock-runtime", region_name=REGION)

# ── JS to read game state ───────────────────────────────────────────────────
READ_STATE_JS = f"""() => {{
    const h = new Int32Array(wasmMemory.buffer);
    return {{
        health:  h[{ADDR_HEALTH}],
        armor:   h[{ADDR_ARMOR}],
        weapon:  h[{ADDR_WEAPON}],
        ammo:    [h[{ADDR_AMMO}], h[{ADDR_AMMO}+1], h[{ADDR_AMMO}+2], h[{ADDR_AMMO}+3]],
        cards:   [h[{ADDR_CARDS}], h[{ADDR_CARDS}+1], h[{ADDR_CARDS}+2], h[{ADDR_CARDS}+3], h[{ADDR_CARDS}+4], h[{ADDR_CARDS}+5]],
        weapowned: [h[{ADDR_WEAPOWNED}], h[{ADDR_WEAPOWNED}+1], h[{ADDR_WEAPOWNED}+2], h[{ADDR_WEAPOWNED}+3], h[{ADDR_WEAPOWNED}+4], h[{ADDR_WEAPOWNED}+5], h[{ADDR_WEAPOWNED}+6], h[{ADDR_WEAPOWNED}+7], h[{ADDR_WEAPOWNED}+8]],
        pos_x:   h[{ADDR_POS_X}],
        pos_y:   h[{ADDR_POS_Y}],
        angle:   h[{ADDR_ANGLE}],
    }};
}}"""

# ── AgentCore Memory for map persistence ────────────────────────────────────
class MapMemory:
    """Persists exploration map data across deaths/restarts using AgentCore Memory.

    Stores visited cells, wall edges, and locked door positions as structured
    events. On startup, retrieves past exploration data to seed the navigator.
    """

    def __init__(self, region: str = REGION):
        from bedrock_agentcore.memory import MemoryClient
        self.client = MemoryClient(region_name=region)
        self.memory_id: str | None = None
        self.run_count = 0

    def initialize(self):
        """Find or create the memory store."""
        try:
            memories = self.client.list_memories()
            for mem in memories.get("memories", []):
                if mem.get("name") == MEMORY_NAME:
                    self.memory_id = mem["id"]
                    print(f"  Memory: found existing store {self.memory_id}")
                    return
        except Exception:
            pass

        try:
            result = self.client.create_memory(
                name=MEMORY_NAME,
                description="Doom agent exploration map — visited cells, walls, locked doors across runs",
            )
            self.memory_id = result.get("id")
            print(f"  Memory: created new store {self.memory_id}")
        except Exception as e:
            print(f"  Memory: failed to create store — {e}")
            print("  Memory: running without persistence (map resets on death)")

    def save_map(self, nav: "ExplorationNavigator", death_pos: tuple[float, float] | None = None):
        """Save the current exploration state as a memory event."""
        if not self.memory_id:
            return

        self.run_count += 1
        session_id = f"doom-run-{self.run_count}-{int(time.time())}"

        map_data = {
            "visited_cells": [list(c) for c in nav.visited],
            "wall_edges": [list(e) for e in nav.wall_edges],
            "locked_doors": nav.locked_doors,
            "cells_explored": nav.cells_visited_count,
        }

        summary = (
            f"Run #{self.run_count}: explored {nav.cells_visited_count} cells, "
            f"found {len(nav.wall_edges)} walls, "
            f"found {len(nav.locked_doors)} locked doors."
        )
        if death_pos:
            summary += f" Died at ({death_pos[0]:.0f},{death_pos[1]:.0f})."

        messages = [
            (f"Map exploration data: {json.dumps(map_data)}", "TOOL"),
            (summary, "ASSISTANT"),
        ]

        try:
            self.client.create_event(
                memory_id=self.memory_id,
                actor_id=MEMORY_ACTOR,
                session_id=session_id,
                messages=messages,
            )
            print(f"  Memory: saved map — {summary}")
        except Exception as e:
            print(f"  Memory: failed to save — {e}")

    def load_map(self, nav: "ExplorationNavigator"):
        """Retrieve past exploration data and seed the navigator."""
        if not self.memory_id:
            return

        try:
            results = self.client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=f"/{MEMORY_ACTOR}/",
                query="map exploration visited cells walls locked doors",
            )
        except Exception as e:
            print(f"  Memory: retrieve failed — {e}")
            return

        loaded_cells = 0
        loaded_walls = 0
        loaded_doors = 0

        memories = results if isinstance(results, list) else results.get("memories", [])
        for mem in memories:
            content = mem.get("content", "") if isinstance(mem, dict) else str(mem)
            # Extract JSON map data from the memory content
            try:
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    data = json.loads(content[start:end])

                    for c in data.get("visited_cells", []):
                        cell = tuple(c)
                        if cell not in nav.visited:
                            nav.visited.add(cell)
                            loaded_cells += 1

                    for e in data.get("wall_edges", []):
                        edge = tuple(e)
                        if edge not in nav.wall_edges:
                            nav.wall_edges.add(edge)
                            loaded_walls += 1

                    for door in data.get("locked_doors", []):
                        already = any(
                            dist(door[0], door[1], d[0], d[1]) < CELL_SIZE
                            for d in nav.locked_doors
                        )
                        if not already:
                            nav.locked_doors.append(tuple(door))
                            loaded_doors += 1
            except (json.JSONDecodeError, KeyError, IndexError):
                continue

        nav.cells_visited_count = len(nav.visited)
        if loaded_cells or loaded_walls or loaded_doors:
            print(f"  Memory: loaded {loaded_cells} cells, {loaded_walls} walls, {loaded_doors} doors from past runs")
        else:
            print("  Memory: no past exploration data found — starting fresh")


# ── Angle math ──────────────────────────────────────────────────────────────
def angle_to_target(px: float, py: float, tx: float, ty: float) -> float:
    dx = tx - px
    dy = ty - py
    return math.degrees(math.atan2(dy, dx)) % 360


def angle_diff(current: float, target: float) -> float:
    """Signed difference: positive = turn left, negative = turn right."""
    return (target - current + 180) % 360 - 180


def dist(px: float, py: float, tx: float, ty: float) -> float:
    return math.sqrt((tx - px) ** 2 + (ty - py) ** 2)


def to_cell(x: float, y: float) -> tuple[int, int]:
    return (int(math.floor(x / CELL_SIZE)), int(math.floor(y / CELL_SIZE)))


def cell_center(cx: int, cy: int) -> tuple[float, float]:
    return ((cx + 0.5) * CELL_SIZE, (cy + 0.5) * CELL_SIZE)


# ── Exploration Navigator ──────────────────────────────────────────────────
CARD_NAMES = ["blue", "yellow", "red", "blue_skull", "yellow_skull", "red_skull"]


class ExplorationNavigator:
    """Explores the map by tracking visited cells, walls, and locked doors.

    Strategy:
    1. If we just found a new keycard and remember locked doors → revisit nearest door
    2. Otherwise → steer toward the nearest frontier cell (unvisited, adjacent to visited)
    3. If truly stuck → escalating unstick maneuvers
    """

    def __init__(self):
        self.visited: set[tuple[int, int]] = set()
        self.wall_edges: set[tuple[int, int, int, int]] = set()
        self.locked_doors: list[tuple[float, float]] = []
        self.cards_held: list[int] = [0] * 6
        self.cards_at_last_door_check: list[int] = [0] * 6

        self.target: tuple[float, float] | None = None
        self.target_reason: str = ""

        self.last_positions: list[tuple[float, float]] = []
        self.stuck_count = 0
        self.stuck_at: tuple[float, float] | None = None
        self.unstick_phase = 0
        self.unstick_steps_left = 0
        self.cells_visited_count = 0

    def update_cards(self, cards: list[int]):
        old = self.cards_held
        self.cards_held = list(cards)
        new_cards = [i for i in range(6) if cards[i] and not old[i]]
        if new_cards:
            names = [CARD_NAMES[i] for i in new_cards]
            print(f"  *** PICKED UP KEYCARD: {', '.join(names)} ***")
            if self.locked_doors and self.cards_held != self.cards_at_last_door_check:
                self.target = None

    def _record_position(self, px: float, py: float):
        cell = to_cell(px, py)
        if cell not in self.visited:
            self.visited.add(cell)
            self.cells_visited_count += 1

        self.last_positions.append((px, py))
        if len(self.last_positions) > 6:
            self.last_positions = self.last_positions[-6:]

        if len(self.last_positions) >= 3:
            ox, oy = self.last_positions[-3]
            if dist(ox, oy, px, py) < 5:
                self.stuck_count += 1
                if self.stuck_at is None:
                    self.stuck_at = (px, py)
            else:
                self.stuck_count = 0
                self.stuck_at = None
                self.unstick_steps_left = 0

    def _mark_wall(self, px: float, py: float, angle: float):
        """When stuck, mark the edge in front of us as a wall."""
        cx, cy = to_cell(px, py)
        rad = math.radians(angle)
        ahead_x = px + math.cos(rad) * CELL_SIZE * 0.6
        ahead_y = py + math.sin(rad) * CELL_SIZE * 0.6
        ax, ay = to_cell(ahead_x, ahead_y)
        if (cx, cy) != (ax, ay):
            edge = (min(cx, ax), min(cy, ay), max(cx, ax), max(cy, ay))
            self.wall_edges.add(edge)

    def _mark_locked_door(self, px: float, py: float):
        """Remember a position where we got deeply stuck (probably a locked door)."""
        for dx, dy in self.locked_doors:
            if dist(dx, dy, px, py) < CELL_SIZE:
                return
        self.locked_doors.append((px, py))
        self.cards_at_last_door_check = list(self.cards_held)
        print(f"  *** LOCKED DOOR at ({px:.0f},{py:.0f}) — need keycard ***")

    def _is_wall(self, cx1: int, cy1: int, cx2: int, cy2: int) -> bool:
        edge = (min(cx1, cx2), min(cy1, cy2), max(cx1, cx2), max(cy1, cy2))
        return edge in self.wall_edges

    def _find_frontier(self, px: float, py: float) -> tuple[float, float] | None:
        """Find the nearest unvisited cell adjacent to a visited cell."""
        best = None
        best_dist = float("inf")
        cur_cell = to_cell(px, py)

        for cx, cy in self.visited:
            for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                nx, ny = cx + dx, cy + dy
                if (nx, ny) in self.visited:
                    continue
                if self._is_wall(cx, cy, nx, ny):
                    continue
                fx, fy = cell_center(nx, ny)
                d = dist(px, py, fx, fy)
                if d < best_dist:
                    best_dist = d
                    best = (fx, fy)

        return best

    def _find_locked_door_target(self, px: float, py: float) -> tuple[float, float] | None:
        """If we have new keycards since last check, revisit the nearest locked door."""
        if self.cards_held == self.cards_at_last_door_check:
            return None
        if not self.locked_doors:
            return None
        best = min(self.locked_doors, key=lambda d: dist(px, py, d[0], d[1]))
        return best

    def decide(self, px: float, py: float, player_angle: float,
               step: int, cards: list[int]) -> tuple[list[str], float, str]:
        self.update_cards(cards)
        self._record_position(px, py)

        # Mark walls when stuck
        if self.stuck_count >= STUCK_THRESHOLD:
            self._mark_wall(px, py, player_angle)

        # Mark locked doors after prolonged stuck + use attempts
        if self.stuck_count >= DOOR_STUCK_THRESHOLD:
            self._mark_locked_door(px, py)
            self.target = None

        # Unstick maneuver takes priority
        if self.stuck_count >= STUCK_THRESHOLD or self.unstick_steps_left > 0:
            return self._unstick(px, py, step)

        # Pick a target if we don't have one (or reached it)
        if self.target is not None:
            if dist(px, py, self.target[0], self.target[1]) < CELL_SIZE * 0.8:
                self.target = None

        if self.target is None:
            # Priority 1: revisit a locked door if we have new keycards
            door_target = self._find_locked_door_target(px, py)
            if door_target:
                self.target = door_target
                self.target_reason = "DOOR_RETRY"
            else:
                # Priority 2: nearest frontier cell
                frontier = self._find_frontier(px, py)
                if frontier:
                    self.target = frontier
                    self.target_reason = "FRONTIER"
                else:
                    # No frontier — pick a random direction to try
                    rad = math.radians(player_angle + random.choice([-90, 90, 180]))
                    self.target = (px + math.cos(rad) * CELL_SIZE * 3,
                                   py + math.sin(rad) * CELL_SIZE * 3)
                    self.target_reason = "RANDOM"

        # Steer toward target
        tx, ty = self.target
        target_dist = dist(px, py, tx, ty)
        desired = angle_to_target(px, py, tx, ty)
        diff = angle_diff(player_angle, desired)

        actions = []
        hold = 0.3

        if abs(diff) > ANGLE_TOLERANCE:
            if diff > 0:
                actions.append("turn_left")
            else:
                actions.append("turn_right")
            hold = min(0.05 + abs(diff) / 360 * 0.6, 0.5)
            reason = f"{self.target_reason} turn {'L' if diff > 0 else 'R'} {abs(diff):.0f}° d={target_dist:.0f}"
        else:
            actions = ["forward", "run"]
            hold = min(0.15 + target_dist / 500 * 0.3, 0.5)
            reason = f"{self.target_reason} run d={target_dist:.0f}"

        if step % USE_PRESS_INTERVAL == 0:
            actions.append("use")

        cells = self.cells_visited_count
        walls = len(self.wall_edges)
        doors = len(self.locked_doors)
        reason += f" [cells={cells} walls={walls} doors={doors}]"

        return actions, hold, reason

    def _unstick(self, px: float, py: float, step: int) -> tuple[list[str], float, str]:
        if self.unstick_steps_left <= 0:
            self.unstick_phase = (self.unstick_phase + 1) % 5
            self.unstick_steps_left = [3, 4, 3, 5, 4][self.unstick_phase]

        self.unstick_steps_left -= 1
        phase = self.unstick_phase

        if phase == 0:
            return ["turn_right"], 0.5, f"UNSTICK turn_right (stuck={self.stuck_count})"
        elif phase == 1:
            return ["turn_left", "forward", "run"], 0.4, f"UNSTICK left+run (stuck={self.stuck_count})"
        elif phase == 2:
            return ["backward", "turn_right", "run"], 0.5, f"UNSTICK back+right (stuck={self.stuck_count})"
        elif phase == 3:
            side = "strafe_right" if step % 2 == 0 else "strafe_left"
            return [side, "forward", "use"], 0.4, f"UNSTICK {side}+use (stuck={self.stuck_count})"
        else:
            angle = random.randint(0, 360)
            turn = "turn_left" if random.random() > 0.5 else "turn_right"
            return ["backward", turn, "run"], 0.6, f"UNSTICK random retreat (stuck={self.stuck_count})"


# ── Combat decisions via LLM ─────────────────────────────────────────────
def ask_llm_combat(health: int, armor: int, ammo: list, weapon: int,
                      hp_delta: int, pos_info: str) -> dict:
    """Ask the LLM only when we're taking damage — what to do in combat."""
    system = """You are a Doom combat AI. You are ONLY called when the player is taking damage.

Pick combat actions. Available: shoot, turn_left, turn_right, strafe_left, strafe_right, forward, backward, run

RULES:
- Always include "shoot" — you're being attacked
- Strafe to dodge (strafe_left or strafe_right)
- If low HP (<30), include "backward" to retreat
- Pick the best weapon if you have shotgun (weapon_2) or chaingun (weapon_3)

Respond ONLY with JSON: {"a":["shoot","strafe_left"],"h":0.3}
a=actions, h=hold(0.1-0.5). No markdown, no explanation."""

    msg = f"HP:{health}({hp_delta:+d}) ARMOR:{armor} WEAPON:{WEAPON_NAMES.get(weapon,'?')} AMMO:bul={ammo[0]} shel={ammo[1]} {pos_info}"
    messages = [{"role": "user", "content": [{"text": msg}]}]

    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system}],
        messages=messages,
        inferenceConfig={"maxTokens": 60, "temperature": 0.2},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0].strip()

    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        d = json.loads(m.group()) if m else {}

    actions = d.get("a") or d.get("actions") or ["shoot", "strafe_left"]
    hold = d.get("h") or d.get("hold") or 0.3
    if "shoot" not in actions:
        actions.insert(0, "shoot")
    return {"actions": actions, "hold": min(hold, 0.5)}


# ── Keyboard ────────────────────────────────────────────────────────────────
async def do_actions(page: Page, actions: list, hold: float):
    keys = [VALID_KEYS[a] for a in actions if a in VALID_KEYS]
    if not keys:
        return
    for k in keys:
        await page.keyboard.down(k)
    await asyncio.sleep(min(max(hold, 0.05), 0.8))
    for k in keys:
        await page.keyboard.up(k)


# ── Game startup ────────────────────────────────────────────────────────────
async def start_doom(page: Page):
    print(f"  Navigating to {DOOM_URL}")
    await page.goto(DOOM_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(5)

    btn = await page.query_selector("text=Play Solo")
    if btn:
        print("  Clicking 'Play Solo'")
        await btn.click()
        await asyncio.sleep(3)

    await asyncio.sleep(8)

    canvas = await page.query_selector("canvas")
    if canvas:
        await canvas.click()
        await asyncio.sleep(1)

    print("  Navigating menus...")
    for _ in range(5):
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)

    await asyncio.sleep(2)
    print("  Game started!")


# ── Main loop ───────────────────────────────────────────────────────────────
async def run_doom_agent():
    from bedrock_agentcore.tools.browser_client import browser_session

    print("=" * 60)
    print("    DOOM AGENT v6 — exploration + memory + combat AI")
    print("=" * 60)
    print(f"  Model:     {MODEL_ID} (combat only)")
    print(f"  Max steps: {MAX_STEPS}")
    print(f"  Cell size: {CELL_SIZE} map units")
    print()

    # Initialize persistent memory
    memory = MapMemory(REGION)
    memory.initialize()
    print()

    with browser_session(REGION) as client:
        ws_url, headers = client.generate_ws_headers()
        print(f"  Session ID: {client.session_id}")
        print()
        print("  >>> WATCH LIVE <<<")
        print("  AWS Console → AgentCore → Browser → find session → View live")
        print("  Region: us-west-2 (Oregon)")
        print()

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(ws_url, headers=headers)
            page = browser.contexts[0].pages[0]

            try:
                await start_doom(page)

                state = await page.evaluate(READ_STATE_JS)
                print(f"  Initial: HP={state['health']} ammo={state['ammo']}")
                start_x = state["pos_x"] / 65536
                start_y = state["pos_y"] / 65536
                start_angle = (state["angle"] / 65536) % 360
                print(f"  Start pos: ({start_x:.0f}, {start_y:.0f}) angle={start_angle:.0f}°")

                if state["health"] <= 0:
                    for _ in range(3):
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(1)

                nav = ExplorationNavigator()
                memory.load_map(nav)
                prev_health = state["health"]
                combat_cooldown = 0

                print()
                print("=" * 60)
                print("  STEP | TIME | HP  | POS            | ACTION")
                print("=" * 60)

                for step in range(1, MAX_STEPS + 1):
                    t0 = time.time()

                    # 1. Read state
                    state = await page.evaluate(READ_STATE_JS)
                    px = state["pos_x"] / 65536
                    py = state["pos_y"] / 65536
                    player_angle = (state["angle"] / 65536) % 360
                    hp = state["health"]
                    hp_delta = hp - prev_health

                    # 2. Decide: combat or exploration
                    if hp_delta < 0 and combat_cooldown <= 0:
                        try:
                            combat = ask_llm_combat(
                                hp, state["armor"], state["ammo"],
                                state["weapon"], hp_delta,
                                f"pos=({px:.0f},{py:.0f}) angle={player_angle:.0f}"
                            )
                            actions = combat["actions"]
                            hold = combat["hold"]
                            reasoning = f"COMBAT hp{hp_delta:+d} → {'+'.join(actions)}"
                            combat_cooldown = 3
                        except Exception as e:
                            actions = ["shoot", "strafe_left"]
                            hold = 0.3
                            reasoning = "COMBAT(err) → shoot+strafe"
                            combat_cooldown = 3
                    else:
                        actions, hold, reasoning = nav.decide(
                            px, py, player_angle, step, state["cards"]
                        )
                        combat_cooldown = max(0, combat_cooldown - 1)

                    # 3. Execute
                    await do_actions(page, actions, hold)

                    elapsed = time.time() - t0
                    act_str = "+".join(actions)

                    print(f"  [{step:3d}] {elapsed:.1f}s | HP:{hp:3d} | ({px:7.0f},{py:7.0f}) {player_angle:5.0f}° | {act_str:25s} | {reasoning}")

                    if hp <= 0:
                        print("\n  *** DOOMGUY DIED ***")
                        memory.save_map(nav, death_pos=(px, py))
                        break

                    prev_health = hp
                    await asyncio.sleep(ACTION_INTERVAL)

                # Save map on normal completion too
                memory.save_map(nav)
                print("\n--- Done ---")

            except KeyboardInterrupt:
                print("\n--- Stopped ---")
            except Exception as e:
                print(f"\n--- Error: {e} ---")
                import traceback
                traceback.print_exc()
            finally:
                await page.close()
                await browser.close()

    print("Session closed.")


if __name__ == "__main__":
    asyncio.run(run_doom_agent())
