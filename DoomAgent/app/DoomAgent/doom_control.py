import asyncio
import math
import sys
import time

DOOM_URL = "https://silentspacemarine.com/"
PLAYER = 236104
FIRE = " "
USE = "e"
REACH = 40
EXIT_SWITCH = (2912, -4768)
E1M1_WAYPOINTS = [
    (1168, -2992, False), (1264, -2880, False), (1344, -2560, False),
    (1440, -2528, False), (1472, -2528, True), (1600, -2528, False),
    (2016, -2560, False), (2096, -2624, False), (2736, -2672, False),
    (2912, -2864, False), (2976, -3088, False), (2976, -3216, False),
    (3120, -3360, False), (3040, -3648, False), (3040, -3968, True),
    (3040, -4096, False), (3008, -4576, True), (3008, -4704, False),
    (2944, -4768, False),
]
MF_SPECIAL, MF_COUNTKILL = 0x1, 0x400000
THING_NAMES = {
    3004: "zombieman", 9: "shotgun guy", 3001: "imp", 3002: "demon", 58: "spectre",
    3006: "lost soul", 3005: "cacodemon", 3003: "baron", 2035: "barrel",
    2011: "stimpack", 2012: "medikit", 2014: "health bonus", 2015: "armor bonus",
    2018: "green armor", 2019: "blue armor", 2013: "soulsphere", 8: "backpack",
    2007: "clip", 2048: "box of bullets", 2008: "shells", 2049: "box of shells",
    2010: "rocket", 2046: "box of rockets", 2047: "cell", 17: "cell pack",
    2001: "shotgun", 2002: "chaingun", 2003: "rocket launcher", 2004: "plasma rifle",
    2005: "chainsaw", 2006: "BFG",
    5: "blue keycard", 6: "yellow keycard", 13: "red keycard",
}

READ_JS = """() => {
    const i = new Int32Array(wasmMemory.buffer);
    const u = new Uint32Array(wasmMemory.buffer);
    const P = %d;
    const pmo = u[P];
    const pm = pmo >>> 2;
    const fn = u[pm + 2];
    const things = [];
    let t = u[pm + 1], guard = 0;
    while (t !== pmo && t !== 0 && guard++ < 5000) {
        const m = t >>> 2;
        if (u[m + 2] === fn) {
            things.push({
                id: t, ednum: i[u[m + 23] >>> 2],
                x: i[m + 3] / 65536, y: i[m + 4] / 65536,
                health: i[m + 27], flags: u[m + 26], awake: u[m + 30] === pmo,
            });
        }
        t = u[m + 1];
    }
    return {
        health: i[P + 10], armor: i[P + 11], weapon: i[P + 30],
        cards: [i[P + 19], i[P + 20], i[P + 21]],
        weapons: [i[P + 32], i[P + 33], i[P + 34], i[P + 35], i[P + 36], i[P + 37], i[P + 38], i[P + 39]],
        ammo: {bullets: i[P + 41], shells: i[P + 42], cells: i[P + 43], rockets: i[P + 44]},
        x: i[pm + 3] / 65536, y: i[pm + 4] / 65536,
        angle: u[pm + 8] / 4294967296 * 360,
        things: things,
    };
}""" % PLAYER

WALLS_JS = """async (level) => {
    const dv = new DataView(await (await fetch('doom1.wad')).arrayBuffer());
    const count = dv.getInt32(4, true), dir = dv.getInt32(8, true);
    const name = k => {
        let s = '';
        for (let j = 0; j < 8; j++) {
            const c = dv.getUint8(dir + k * 16 + 8 + j);
            if (!c) break;
            s += String.fromCharCode(c);
        }
        return s;
    };
    let start = 0;
    while (start < count && name(start) !== level) start++;
    const lump = wanted => {
        for (let k = start + 1; k < start + 11; k++) {
            if (name(k) === wanted) return [dv.getInt32(dir + k * 16, true), dv.getInt32(dir + k * 16 + 4, true)];
        }
    };
    const [vp] = lump('VERTEXES');
    const [lp, ls] = lump('LINEDEFS');
    const doors = [1, 26, 27, 28, 31, 32, 33, 34, 117, 118];
    const out = [];
    for (let o = lp; o < lp + ls; o += 14) {
        const v1 = dv.getUint16(o, true), v2 = dv.getUint16(o + 2, true);
        const door = doors.includes(dv.getUint16(o + 6, true));
        if (dv.getUint16(o + 12, true) === 0xFFFF || door) {
            out.push([dv.getInt16(vp + v1 * 4, true), dv.getInt16(vp + v1 * 4 + 2, true),
                      dv.getInt16(vp + v2 * 4, true), dv.getInt16(vp + v2 * 4 + 2, true), door ? 1 : 0]);
        }
    }
    return out;
}"""

SETUP_JS = """() => {
    const c = document.querySelector('canvas');
    if (c) {
        for (const evt of ['mousemove','mousedown','mouseup','click','contextmenu','wheel']) {
            c.addEventListener(evt, e => e.stopImmediatePropagation(), true);
        }
    }
    const allowed = new Set([
        'ArrowUp','ArrowDown','ArrowLeft','ArrowRight',
        ' ','e','a','d','Shift',
        '1','2','3','4','5','6','7','Enter'
    ]);
    document.addEventListener('keydown', e => {
        if (!allowed.has(e.key)) e.stopImmediatePropagation();
    }, true);
    document.addEventListener('keyup', e => {
        if (!allowed.has(e.key)) e.stopImmediatePropagation();
    }, true);
}"""


async def enter_game(page):
    await page.goto(DOOM_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(5)
    btn = await page.query_selector("text=Play Solo")
    if btn:
        await btn.click()
    await asyncio.sleep(11)
    canvas = await page.query_selector("canvas")
    if canvas:
        await canvas.click()
    await asyncio.sleep(1)
    for _ in range(5):
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)
    await asyncio.sleep(2)
    await page.evaluate(SETUP_JS)
    return await page.evaluate(WALLS_JS, "E1M1")


async def read(page):
    return await page.evaluate(READ_JS)


def bearing(s, x, y):
    return math.degrees(math.atan2(y - s["y"], x - s["x"])) % 360


def turn_needed(s, target):
    return (target - s["angle"] + 180) % 360 - 180


def ray_hit(ax, ay, bx, by, wall):
    x1, y1, x2, y2 = wall[:4]
    rx, ry, sx, sy = bx - ax, by - ay, x2 - x1, y2 - y1
    den = rx * sy - ry * sx
    if den == 0:
        return None
    t = ((x1 - ax) * sy - (y1 - ay) * sx) / den
    w = ((x1 - ax) * ry - (y1 - ay) * rx) / den
    return t if 0 <= t <= 1 and 0 <= w <= 1 else None


def can_see(walls, ax, ay, bx, by, through_doors=True):
    return all((w[4] and through_doors) or ray_hit(ax, ay, bx, by, w) is None for w in walls)


def pickups(s, max_dist=400):
    out = []
    for t in s["things"]:
        if t["flags"] & MF_SPECIAL:
            d = math.hypot(t["x"] - s["x"], t["y"] - s["y"])
            if d < max_dist:
                out.append((d, t))
    return sorted(out, key=lambda e: e[0])


async def hold(page, keys, secs):
    for k in keys:
        await page.keyboard.down(k)
    await asyncio.sleep(secs)
    for k in keys:
        await page.keyboard.up(k)
    await asyncio.sleep(0.05)


async def face(page, target, tol=6):
    for _ in range(8):
        s = await read(page)
        diff = turn_needed(s, target)
        if abs(diff) < tol:
            return
        key = "ArrowLeft" if diff > 0 else "ArrowRight"
        await hold(page, [key], min(max(0.07 + (abs(diff) - 4) / 120, 0.04), 0.8))


class Route:
    def __init__(self, walls):
        self.walls = walls
        self.wp = 0
        self.ignore = {}
        self.done = False
        self.doors_open_until = 0
        self.last_hp = None

    def note(self, s):
        if self.last_hp is not None and s["health"] < self.last_hp:
            self.doors_open_until = max(self.doors_open_until, time.time() + 3)
        self.last_hp = s["health"]

    def threats(self, s, max_dist=400):
        now = time.time()
        through = now < self.doors_open_until
        out = []
        if s["ammo"]["bullets"] + s["ammo"]["shells"] == 0:
            return out
        for t in s["things"]:
            if not (t["flags"] & MF_COUNTKILL and t["health"] > 0 and t["awake"]):
                continue
            d = math.hypot(t["x"] - s["x"], t["y"] - s["y"])
            if d >= max_dist or self.ignore.get(t["id"], 0) > now:
                continue
            if can_see(self.walls, s["x"], s["y"], t["x"], t["y"], through):
                out.append((d, t))
        return sorted(out, key=lambda e: e[0])

    async def attack(self, page, budget=8.0):
        t0 = time.time()
        misses = {}
        while time.time() - t0 < budget:
            s = await read(page)
            self.note(s)
            if s["health"] <= 0:
                return "dead"
            targets = self.threats(s)
            if not targets:
                return "clear"
            d, e = targets[0]
            await face(page, bearing(s, e["x"], e["y"]), tol=3)
            await hold(page, [FIRE], 0.8 if d < 350 else 0.45)
            s2 = await read(page)
            now = next((t for t in s2["things"] if t["id"] == e["id"]), None)
            if now is not None and now["health"] >= e["health"]:
                misses[e["id"]] = misses.get(e["id"], 0) + 1
                if misses[e["id"]] >= 2:
                    if can_see(self.walls, s2["x"], s2["y"], now["x"], now["y"], False):
                        self.ignore[e["id"]] = time.time() + 6
                    else:
                        self.doors_open_until = 0
                    misses[e["id"]] = 0
            else:
                misses[e["id"]] = 0
        return "timeout"

    async def goto(self, page, tx, ty, reach=REACH, budget=25.0):
        t0 = time.time()
        stuck = 0
        side = "a"
        while time.time() - t0 < budget:
            s = await read(page)
            self.note(s)
            if s["health"] <= 0:
                return "dead", s
            dist = math.hypot(tx - s["x"], ty - s["y"])
            if dist < reach:
                return "reached", s
            if self.threats(s):
                return "threat", s
            target = bearing(s, tx, ty)
            if abs(turn_needed(s, target)) > 10:
                await face(page, target)
            if dist < 150:
                await hold(page, ["ArrowUp"], min(max(dist / 300, 0.08), 0.4))
            else:
                await hold(page, ["ArrowUp", "Shift"], min(dist / 500, 0.6))
            s2 = await read(page)
            if math.hypot(s2["x"] - s["x"], s2["y"] - s["y"]) < 4:
                stuck += 1
                if stuck == 1:
                    await hold(page, [USE], 0.15)
                    self.doors_open_until = time.time() + 6
                    await asyncio.sleep(1.2)
                else:
                    side = "d" if side == "a" else "a"
                    await hold(page, [side], 0.3)
            else:
                stuck = 0
        return "timeout", await read(page)

    async def press_exit(self, page):
        for _ in range(4):
            prev = await read(page)
            cur = prev
            for _ in range(10):
                await asyncio.sleep(0.15)
                cur = await read(page)
                if math.hypot(cur["x"] - prev["x"], cur["y"] - prev["y"]) < 1:
                    break
                prev = cur
            sx, sy = EXIT_SWITCH
            if math.hypot(sx - cur["x"], sy - cur["y"]) > 56:
                tx, ty, _ = E1M1_WAYPOINTS[-1]
                await self.goto(page, tx, ty, reach=16)
                continue
            await face(page, bearing(cur, sx, sy))
            before = await read(page)
            await hold(page, [USE], 0.2)
            await asyncio.sleep(1.0)
            await hold(page, ["ArrowLeft"], 0.15)
            after = await read(page)
            if after["angle"] == before["angle"] and after["health"] > 0:
                return True
        return False

    async def advance(self, page, budget=20.0, fight=False):
        t0 = time.time()
        total = len(E1M1_WAYPOINTS)
        while time.time() - t0 < budget:
            if self.done:
                return "LEVEL COMPLETE"
            if self.wp >= total:
                self.done = await self.press_exit(page)
                if not self.done:
                    return "At the exit switch but the press missed. Call advance again."
                continue
            tx, ty, door = E1M1_WAYPOINTS[self.wp]
            last = self.wp == total - 1
            status, s = await self.goto(page, tx, ty, reach=16 if last else REACH)
            if status == "dead":
                return "DEAD"
            if status == "threat" and fight:
                if await self.attack(page) == "dead":
                    return "DEAD"
                continue
            if status == "threat":
                d, e = self.threats(s)[0]
                name = THING_NAMES.get(e["ednum"], "enemy")
                return f"THREAT: {name} at {d:.0f} units, HP {s['health']}. Call attack_nearest_enemy now."
            if status == "timeout":
                return f"STUCK near ({s['x']:.0f},{s['y']:.0f}) heading to waypoint {self.wp}. Call advance again."
            if door and not last:
                nx, ny, _ = E1M1_WAYPOINTS[self.wp + 1]
                await face(page, bearing(s, nx, ny))
                await hold(page, [USE], 0.2)
                self.doors_open_until = time.time() + 6
                await asyncio.sleep(1.3)
            self.wp += 1
        return f"Moving. Waypoint {self.wp}/{total} next. Call advance again."


async def demo(chrome_path=None):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=chrome_path, headless=True)
        page = await browser.new_page()
        walls = await enter_game(page)
        print("wall segments:", len(walls), "doors:", sum(w[4] for w in walls))
        route = Route(walls)
        t0 = time.time()
        result = ""
        while time.time() - t0 < 240:
            result = await route.advance(page)
            s = await read(page)
            print(f"t={time.time() - t0:5.1f}s hp={s['health']:3d} wp={route.wp:2d} {result}")
            if result.startswith("THREAT"):
                print("   attack ->", await route.attack(page))
            if result in ("LEVEL COMPLETE", "DEAD"):
                break
        await browser.close()
        assert result == "LEVEL COMPLETE", result


if __name__ == "__main__":
    asyncio.run(demo(sys.argv[1] if len(sys.argv) > 1 else None))
