import asyncio
import heapq
import math
import os
import sys
import time

import doom_control as dc
import map_store
from level_map import seg_dist
from vision_map import DOOR, FREE, WALL, WINDOW, VisionMap

STEPS8 = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
SIDES4 = ((1, 0), (-1, 0), (0, 1), (0, -1))
WALKABLE = (FREE, DOOR)
MF_DROPPED = 0x20000
KINDS = {
    2011: "health", 2012: "health", 2013: "health", 2014: "health",
    2015: "armor", 2018: "armor", 2019: "armor",
    2007: "ammo", 2048: "ammo", 2008: "ammo", 2049: "ammo", 2010: "ammo", 2046: "ammo", 2047: "ammo",
    17: "ammo", 8: "ammo",
    2001: "weapon", 2002: "weapon", 2003: "weapon", 2004: "weapon", 2005: "weapon", 2006: "weapon",
    5: "key", 6: "key", 13: "key", 38: "key", 39: "key", 40: "key",
}
WEAPON_SLOT = {2001: 2, 2002: 3, 2003: 4, 2004: 5, 2006: 6, 2005: 7}
HEALING = (2011, 2012, 2013)


def kind_of(t):
    if t["flags"] & dc.MF_COUNTKILL:
        return "enemy"
    if t["ednum"] == 2035:
        return "barrel"
    if t["flags"] & dc.MF_SPECIAL:
        return KINDS.get(t["ednum"], "other")
    return None


class Explorer:
    def __init__(self, route, vmap, level, store_key="E1M1"):
        self.route = route
        self.vmap = vmap
        self.level = level
        self.store_key = store_key
        self.read, self.face, self.hold = dc.read, dc.face, dc.hold
        self.sleep, self.clock = asyncio.sleep, time.time
        self.switch, self.stand = level.exit_switch()
        segs = level.segments()
        self.sight = [s for s in segs if s[4] in (0, 1)]
        self.solid = [s for s in segs if s[4] in (0, 2)]
        self.buckets = {}
        t = vmap.tile
        for n, (x1, y1, x2, y2, _) in enumerate(self.solid):
            a = vmap.key(min(x1, x2) - t, min(y1, y2) - t)
            b = vmap.key(max(x1, x2) + t, max(y1, y2) + t)
            for tx in range(a[0], b[0] + 1):
                for ty in range(a[1], b[1] + 1):
                    if seg_dist((tx + 0.5) * t, (ty + 0.5) * t, x1, y1, x2, y2) < t * 1.5:
                        self.buckets.setdefault((tx, ty), []).append(n)
        self._pass = {}
        self._static = {}
        self.exit_seen = False
        self.objects = {}
        self.ids = {}
        self.gone = set()
        self.skipped = set()
        self.detours = 0
        self.visited = set()
        self.bumped = set()
        self.runs = 0
        self.plans = 0
        self.looked = False
        self.careful = False
        self.fails = {}
        self.away = False
        self.start = level.player_start()[:2]
        self.done = False
        self.saved = None
        self.restore(map_store.load(store_key))
        self.loaded_tiles = len(vmap.tiles)
        self.loaded_objects = len(self.objects)
        self.runs += 1

    def centre(self, k):
        return (k[0] + 0.5) * self.vmap.tile, (k[1] + 0.5) * self.vmap.tile

    def snapshot(self):
        tiles = self.vmap.tiles
        data = {"version": 1, "level": self.store_key, "tile": self.vmap.tile, "runs": self.runs,
                "exit_seen": self.exit_seen, "visited": sorted(map(list, self.visited)),
                "bumped": sorted(map(list, self.bumped)), "x0": 0, "y0": 0, "rows": [],
                "objects": sorted([k[0], k[1], k[2], o["kind"]] for k, o in self.objects.items()
                                  if not o["dropped"])}
        if tiles:
            x0, y0 = min(k[0] for k in tiles), min(k[1] for k in tiles)
            x1, y1 = max(k[0] for k in tiles), max(k[1] for k in tiles)
            data["x0"], data["y0"] = x0, y0
            data["rows"] = ["".join(tiles.get((x, y), "?") for x in range(x0, x1 + 1)) for y in range(y0, y1 + 1)]
        return data

    def restore(self, data):
        if not data or data.get("tile") != self.vmap.tile or data.get("level") != self.store_key:
            return
        for j, row in enumerate(data.get("rows", [])):
            for i, state in enumerate(row):
                if state != "?":
                    self.vmap.tiles[(data["x0"] + i, data["y0"] + j)] = state
        self.exit_seen = bool(data.get("exit_seen"))
        for ednum, x, y, kind in data.get("objects", []):
            self.objects[(ednum, x, y)] = {"name": dc.THING_NAMES.get(ednum, kind), "kind": kind,
                                           "x": x, "y": y, "dropped": False}
        self.visited = {tuple(k) for k in data.get("visited", [])}
        self.bumped = {tuple(k) for k in data.get("bumped", [])}
        self.runs = int(data.get("runs", 0))

    def save(self):
        data = self.snapshot()
        if data != self.saved and map_store.save(self.store_key, data):
            self.saved = data

    def observe(self, s):
        learned = self.vmap.observe(s["x"], s["y"], s["angle"])
        if not self.exit_seen and self.sees_exit(s["x"], s["y"], s["angle"]):
            self.exit_seen = True
        self.notice(s)
        return learned

    def in_view(self, x, y, angle, px, py):
        if math.hypot(px - x, py - y) > 1024:
            return False
        turn = (math.degrees(math.atan2(py - y, px - x)) - angle + 180) % 360 - 180
        return abs(turn) <= 45 and dc.can_see(self.sight, x, y, px, py, False)

    def sees_exit(self, x, y, angle):
        (sx, sy), (tx, ty) = self.switch, self.stand
        d = math.hypot(tx - sx, ty - sy)
        return self.in_view(x, y, angle, sx + (tx - sx) / d * 2, sy + (ty - sy) / d * 2)

    def visible(self, s):
        out = []
        for t in s.get("things", []):
            kind = kind_of(t)
            if kind is None or (kind == "enemy" and t["health"] <= 0):
                continue
            if self.in_view(s["x"], s["y"], s["angle"], t["x"], t["y"]):
                out.append((math.hypot(t["x"] - s["x"], t["y"] - s["y"]), t, kind))
        return sorted(out, key=lambda e: e[0])

    def notice(self, s):
        things = s.get("things", [])
        if not things:
            return
        present = set()
        for t in things:
            kind = kind_of(t)
            if kind == "enemy" and t["health"] <= 0 and t["id"] in self.ids:
                self.gone.add(self.ids[t["id"]])
            elif kind not in (None, "enemy"):
                present.add((t["ednum"], round(t["x"]), round(t["y"])))
        for _, t, kind in self.visible(s):
            key = (t["ednum"], round(t["x"]), round(t["y"]))
            if kind == "enemy":
                if t["id"] in self.ids:
                    continue
                bound = set(self.ids.values())
                key = next((k for k, o in self.objects.items()
                            if o["kind"] == "enemy" and k[0] == t["ednum"] and k not in bound
                            and math.hypot(k[1] - t["x"], k[2] - t["y"]) < 256), key)
                self.ids[t["id"]] = key
            self.objects.setdefault(key, {
                "name": dc.THING_NAMES.get(t["ednum"], kind), "kind": kind, "x": key[1], "y": key[2],
                "dropped": bool(t["flags"] & MF_DROPPED)})
        for key, o in self.objects.items():
            if o["kind"] != "enemy" and key not in present:
                self.gone.add(key)

    def remembered(self, s):
        out = [(math.hypot(o["x"] - s["x"], o["y"] - s["y"]), o)
               for key, o in self.objects.items() if key not in self.gone]
        return sorted(out, key=lambda e: e[0])

    def wanted(self, s, dist):
        owned = s.get("weapons") or []
        best = {}
        for key, o in self.objects.items():
            if key in self.gone or key in self.skipped:
                continue
            tile = self.vmap.key(o["x"], o["y"])
            if tile not in dist:
                continue
            if s["health"] < 50 and key[0] in HEALING:
                rank = 0
            elif key[0] in WEAPON_SLOT and owned and not owned[WEAPON_SLOT[key[0]]]:
                rank = 1
            else:
                continue
            if rank not in best or dist[tile] < best[rank][0]:
                best[rank] = (dist[tile], key, tile)
        return best[min(best)] if best else None

    def static(self, k):
        if k not in self._static:
            x, y = self.centre(k)
            near = [self.solid[n] for n in self.buckets.get(k, ())]
            tight = any(seg_dist(x, y, *s[:4]) < 15 for s in near)
            self._static[k] = (tight, self.level.is_damaging(x, y))
        return self._static[k]

    def passable(self, a, b):
        if (a, b) in self._pass:
            return self._pass[(a, b)]
        (ax, ay), (bx, by) = self.centre(a), self.centre(b)
        near = {n for k in (a, b, (a[0], b[1]), (b[0], a[1])) for n in self.buckets.get(k, ())}
        ok = all(dc.ray_hit(ax, ay, bx, by, self.solid[n]) is None for n in near)
        if ok:
            floors = [self.level.floor.get(self.level.cell(ax + (bx - ax) * f, ay + (by - ay) * f))
                      for f in (0, 0.5, 1)]
            if None in floors or any(q - p > 24 for p, q in zip(floors, floors[1:])):
                ok = False
            elif floors[0] - floors[2] > 24 and self.static(b)[1]:
                ok = False
        self._pass[(a, b)] = ok
        return ok

    def moves(self, c, avoid):
        tiles = self.vmap.tiles
        here = tiles.get(c)
        wet = self.static(c)[1]
        for dx, dy in STEPS8:
            n = (c[0] + dx, c[1] + dy)
            state = tiles.get(n)
            if state not in WALKABLE:
                continue
            if dx and dy:
                if DOOR in (state, here):
                    continue
                if tiles.get((c[0] + dx, c[1])) not in WALKABLE or tiles.get((c[0], c[1] + dy)) not in WALKABLE:
                    continue
            if not self.passable(c, n):
                continue
            tight, damaging = self.static(n)
            if damaging and avoid and not wet:
                continue
            cost = math.hypot(dx, dy) + (4 if tight else 0) + (15 if damaging else 0)
            if any(tiles.get((n[0] + ox, n[1] + oy)) in (WALL, WINDOW) for ox, oy in SIDES4):
                cost += 0.6
            yield n, cost

    def search(self, src, avoid):
        dist = {src: 0.0}
        prev = {}
        pq = [(0.0, src)]
        while pq:
            d, c = heapq.heappop(pq)
            if d > dist[c]:
                continue
            for n, cost in self.moves(c, avoid):
                nd = d + cost
                if nd < dist.get(n, 1e18):
                    dist[n] = nd
                    prev[n] = c
                    heapq.heappush(pq, (nd, n))
        return dist, prev

    def clear(self, a, b, allowed):
        tiles = self.vmap.tiles
        (ax, ay), (bx, by) = self.centre(a), self.centre(b)
        length = math.hypot(bx - ax, by - ay)
        ux, uy = (bx - ax) / length, (by - ay) / length
        near = set()
        for off in (0, 12, -12):
            ox, oy = -uy * off, ux * off
            last = None
            for n in range(int(length / 8) + 1):
                k = self.vmap.key(ax + ox + ux * n * 8, ay + oy + uy * n * 8)
                if k == last:
                    continue
                if tiles.get(k) != FREE:
                    return False
                if k not in allowed and self.static(k)[1]:
                    return False
                if off == 0 and last is not None and not self.passable(last, k):
                    return False
                near.update(self.buckets.get(k, ()))
                last = k
        for off in (0, 12, -12):
            ox, oy = -uy * off, ux * off
            if any(dc.ray_hit(ax + ox, ay + oy, bx + ox, by + oy, self.solid[n]) is not None for n in near):
                return False
        return True

    def waypoints(self, cells, final=None):
        tiles = self.vmap.tiles
        last = len(cells) - 1
        approach, keep = set(), {0, last}
        for i in range(last):
            a, b = tiles.get(cells[i]), tiles.get(cells[i + 1])
            if b == DOOR and a != DOOR:
                approach.add(i)
                keep.add(i)
            if a == DOOR and b != DOOR:
                keep.add(i + 1)
        way = []
        if 0 in approach:
            way.append((*self.centre(cells[0]), True))
        if self.careful:
            way += [(*self.centre(c), i in approach) for i, c in enumerate(cells) if i]
            if final and (not way or math.hypot(final[0] - way[-1][0], final[1] - way[-1][1]) > 4):
                way.append((final[0], final[1], False))
            return way
        allowed = set(cells)
        order = sorted(keep)
        for a, b in zip(order, order[1:]):
            if DOOR in (tiles.get(cells[a + 1]), tiles.get(cells[b - 1]), tiles.get(cells[b])):
                way.append((*self.centre(cells[b]), b in approach))
                continue
            i = a
            while i < b:
                j = b
                while j > i + 1 and not self.clear(cells[i], cells[j], allowed):
                    j -= 1
                way.append((*self.centre(cells[j]), j in approach))
                i = j
        if final and (not way or math.hypot(final[0] - way[-1][0], final[1] - way[-1][1]) > 4):
            way.append((final[0], final[1], False))
        return way

    def trace(self, prev, src, goal):
        cells = [goal]
        while cells[-1] != src:
            cells.append(prev[cells[-1]])
        cells.reverse()
        return cells

    def frontier(self, s, dist):
        tiles = self.vmap.tiles
        best = None
        for k, d in dist.items():
            if k in self.visited or (best is not None and d >= best[0]):
                continue
            if all((k[0] + ox, k[1] + oy) in tiles for ox, oy in SIDES4):
                continue
            x, y = self.centre(k)
            if math.hypot(x - s["x"], y - s["y"]) < self.vmap.tile * 1.5:
                continue
            best = (d, k)
        return None if best is None else best[1]

    def plan(self, s):
        tiles = self.vmap.tiles
        src = self.vmap.key(s["x"], s["y"])
        goal = self.vmap.key(*self.stand)
        dist, prev = self.search(src, True)
        item = self.wanted(s, dist)
        if item:
            _, key, tile = item
            o = self.objects[key]
            spot = (o["x"], o["y"])
            return {"kind": "item", "target": spot, "tile": tile, "key": key, "name": o["name"],
                    "way": self.waypoints(self.trace(prev, src, tile), spot)}
        for avoid in (True, False):
            if not avoid:
                dist, prev = self.search(src, False)
            if self.exit_seen and goal in dist:
                cells = self.trace(prev, src, goal)
                return {"kind": "exit", "target": self.stand, "tile": goal, "way": self.waypoints(cells, self.stand)}
            k = self.frontier(s, dist)
            if k is not None:
                kind = "door" if tiles.get(k) == DOOR else "frontier"
                return {"kind": kind, "target": self.centre(k), "tile": k,
                        "way": self.waypoints(self.trace(prev, src, k))}
        return None

    async def look_around(self, page):
        s = await self.read(page)
        self.observe(s)
        base = s["angle"]
        for turn in (90, 180, 270):
            angle = (base + turn) % 360
            probe = VisionMap(self.vmap.walls, self.vmap.tile)
            probe.tiles = dict(self.vmap.tiles)
            new = probe.observe(s["x"], s["y"], angle)
            if new == 0 and (self.exit_seen or not self.sees_exit(s["x"], s["y"], angle)):
                continue
            await self.face(page, angle)
            s = await self.read(page)
            self.observe(s)
        for k in list(self.vmap.tiles):
            if self.vmap.tiles[k] == FREE:
                x, y = self.centre(k)
                if math.hypot(x - s["x"], y - s["y"]) < self.vmap.tile * 1.5:
                    self.visited.add(k)
        return s

    def respawned(self, s, x, y):
        sx, sy = self.start
        home = math.hypot(s["x"] - sx, s["y"] - sy)
        if home > 400:
            self.away = True
        if not (self.away and home < 48 and math.hypot(x - sx, y - sy) > 200):
            return False
        self.away = False
        self.ids.clear()
        self.gone.clear()
        self.skipped.clear()
        return True

    def bump(self, s, x, y):
        k = self.vmap.key(x, y)
        if k == self.vmap.key(s["x"], s["y"]) or self.vmap.tiles.get(k) != FREE:
            return False
        self.vmap.mark_blocked(x, y)
        self.bumped.add(k)
        return True

    def forget_bumps(self):
        for k in self.bumped:
            if self.vmap.tiles.get(k) == WALL:
                self.vmap.tiles[k] = FREE
        self.bumped.clear()
        self.visited.clear()

    async def press_switch(self, page):
        sx, sy = self.switch
        for _ in range(4):
            prev = await self.read(page)
            cur = prev
            for _ in range(10):
                await self.sleep(0.15)
                cur = await self.read(page)
                if math.hypot(cur["x"] - prev["x"], cur["y"] - prev["y"]) < 1:
                    break
                prev = cur
            if math.hypot(sx - cur["x"], sy - cur["y"]) > 60:
                await self.route.goto(page, self.stand[0], self.stand[1], reach=16, budget=6.0)
                continue
            await self.face(page, dc.bearing(cur, sx, sy))
            before = await self.read(page)
            await self.hold(page, [dc.USE], 0.2)
            await self.sleep(1.0)
            await self.hold(page, ["ArrowLeft"], 0.15)
            after = await self.read(page)
            if after["angle"] == before["angle"] and after["health"] > 0:
                return True
        return False

    def describe(self, plan):
        known = len(self.vmap.tiles)
        if plan is None:
            return f"Exploring: {known} tiles known, nothing reachable is left to explore and the exit was not found."
        x, y = plan["target"]
        if plan["kind"] == "item":
            return (f"Exploring: {known} tiles known, detouring to pick up the remembered {plan['name']} "
                    f"at ({x:.0f},{y:.0f}). Call explore again.")
        what = {"exit": "the exit switch", "door": "an unopened door", "frontier": "unexplored space"}[plan["kind"]]
        return f"Exploring: {known} tiles known, heading to {what} at ({x:.0f},{y:.0f}). Call explore again."

    async def step(self, page, budget=20.0, fight=True):
        try:
            return await self.run(page, budget, fight)
        finally:
            self.save()

    async def run(self, page, budget, fight):
        t0 = self.clock()
        if self.done:
            return "LEVEL COMPLETE"
        s = await self.read(page)
        if s["health"] <= 0:
            return "DEAD"
        self.observe(s)
        if not self.looked:
            if self.route.threats(s):
                if not fight:
                    return self.threat(s)
                if await self.route.attack(page) == "dead":
                    return "DEAD"
            s = await self.look_around(page)
            self.looked = True
        plan = None
        misses = 0
        retried = False
        for _ in range(40):
            if self.clock() - t0 >= budget:
                break
            plan = self.plan(s)
            self.plans += 1
            if plan is None:
                if (self.bumped or self.visited) and not retried:
                    retried = True
                    self.forget_bumps()
                    continue
                return self.describe(None)
            way = plan["way"]
            arrived = True
            for n, (x, y, use) in enumerate(way):
                last = n == len(way) - 1
                tight = use or (last and plan["kind"] != "frontier")
                reach = 16 if tight else 20 if self.careful else dc.REACH
                status, s = await self.route.goto(page, x, y, reach=reach, budget=5.0 if self.careful else 10.0)
                self.observe(s)
                if status == "dead" or self.respawned(s, x, y):
                    return "DEAD"
                if status == "threat":
                    if not fight:
                        return self.threat(s)
                    if await self.route.attack(page) == "dead":
                        return "DEAD"
                    s = await self.read(page)
                    arrived = False
                    break
                if status == "timeout":
                    if not self.careful:
                        self.careful = True
                    elif not self.bump(s, x, y):
                        self.fails[plan["tile"]] = self.fails.get(plan["tile"], 0) + 1
                        if self.fails[plan["tile"]] >= 2 and plan["kind"] == "item":
                            self.skipped.add(plan["key"])
                        elif self.fails[plan["tile"]] >= 2 and plan["kind"] != "exit":
                            self.visited.add(plan["tile"])
                    arrived = False
                    break
                if use and not last:
                    nx, ny, _ = way[n + 1]
                    await self.face(page, dc.bearing(s, nx, ny))
                    await self.hold(page, [dc.USE], 0.2)
                    self.route.doors_open_until = time.time() + 6
                    await self.sleep(1.3)
                if not last and self.clock() - t0 >= budget:
                    arrived = False
                    break
            if not arrived:
                continue
            self.careful = False
            if plan["kind"] == "item":
                self.skipped.add(plan["key"])
                self.detours += 1
                s = await self.read(page)
                self.observe(s)
                continue
            if plan["kind"] == "exit":
                if await self.press_switch(page):
                    self.done = True
                    return "LEVEL COMPLETE"
                misses += 1
                if misses >= 2:
                    return "At the exit switch but the press missed. Call explore again."
                s = await self.read(page)
                continue
            if plan["kind"] == "door" and len(way) > 1:
                (ax, ay, _), (bx, by, _) = way[-2], way[-1]
                await self.face(page, math.degrees(math.atan2(by - ay, bx - ax)) % 360)
                await self.hold(page, ["ArrowUp"], 0.45)
            s = await self.look_around(page)
            self.visited.add(plan["tile"])
        return self.describe(plan)

    def threat(self, s):
        d, e = self.route.threats(s)[0]
        name = dc.THING_NAMES.get(e["ednum"], "enemy")
        return f"THREAT: {name} at {d:.0f} units, HP {s['health']}. Call attack_nearest_enemy now."


MONSTERS = {3004: 20, 9: 30, 3001: 60, 3002: 150, 58: 150, 3003: 1000, 3005: 400, 3006: 100}


class SimPage:
    def __init__(self, level):
        self.level = level
        self.blocked = level._blocked(16, False)
        self.x, self.y, self.angle = level.player_start()
        self.time = 0.0
        self.door_until = 0.0
        self.finished = False
        self.walked = 0.0
        self.bumps = 0
        self.health = 100
        self.weapons = [1, 1, 0, 0, 0, 0, 0, 0]
        self.things = []
        for n, (x, y, _, ednum, flags) in enumerate(level.things):
            if not flags & 1 or flags & 16:
                continue
            if ednum in MONSTERS:
                self.things.append({"id": n + 1, "ednum": ednum, "x": x, "y": y, "health": MONSTERS[ednum],
                                    "flags": dc.MF_COUNTKILL, "awake": False})
            elif ednum in KINDS or ednum == 2035:
                self.things.append({"id": n + 1, "ednum": ednum, "x": x, "y": y, "health": 1000,
                                    "flags": 0 if ednum == 2035 else dc.MF_SPECIAL, "awake": False})

    def state(self):
        return {"x": self.x, "y": self.y, "angle": self.angle % 360, "health": self.health,
                "things": [dict(t) for t in self.things], "weapons": list(self.weapons),
                "ammo": {"bullets": 50, "shells": 0}}

    def collect(self):
        for t in list(self.things):
            if not t["flags"] & dc.MF_SPECIAL or math.hypot(t["x"] - self.x, t["y"] - self.y) > 30:
                continue
            if t["ednum"] in (2011, 2012) and self.health >= 100:
                continue
            if t["ednum"] in WEAPON_SLOT:
                self.weapons[WEAPON_SLOT[t["ednum"]]] = 1
            if t["ednum"] in HEALING:
                self.health = min(100, self.health + 25)
            self.things.remove(t)

    def walk(self, tx, ty):
        level = self.level
        total = math.hypot(tx - self.x, ty - self.y)
        if total < 0.01:
            return True
        ux, uy = (tx - self.x) / total, (ty - self.y) / total
        self.angle = math.degrees(math.atan2(uy, ux)) % 360
        last = level.cell(self.x, self.y)
        done = 0.0
        while done < total:
            step = min(4.0, total - done)
            nx, ny = self.x + ux * step, self.y + uy * step
            c = level.cell(nx, ny)
            if c != last:
                free = level.inside(c) and (c not in self.blocked or last in self.blocked)
                if free and level.inside(last) and level.floor[c] - level.floor[last] > 24:
                    free = False
                if free and c in level.door_cells and self.time > self.door_until:
                    free = False
                if not free:
                    self.bumps += 1
                    self.time += 3
                    return False
                last = c
            self.x, self.y = nx, ny
            done += step
            self.walked += step
            self.time += step / 400
            self.collect()
        return True


class SimRoute:
    def __init__(self):
        self.walls = []
        self.doors_open_until = 0

    def threats(self, s, max_dist=400):
        return []

    async def goto(self, page, tx, ty, reach=40, budget=25.0):
        page.time += 0.05
        d = math.hypot(tx - page.x, ty - page.y)
        if d >= reach:
            stop = max(0.0, d - reach * 0.5)
            page.walk(page.x + (tx - page.x) / d * stop, page.y + (ty - page.y) / d * stop)
        ok = math.hypot(tx - page.x, ty - page.y) < reach
        return ("reached" if ok else "timeout"), page.state()


def make_sim(level, segs):
    page = SimPage(level)
    ex = Explorer(SimRoute(), VisionMap(segs), level)

    async def read(p):
        return p.state()

    async def face(p, target, tol=6):
        if not p.finished:
            p.angle = target % 360
        p.time += 0.5

    async def hold(p, keys, secs):
        p.time += secs
        if dc.USE in keys:
            sx, sy = ex.switch
            aim = (math.degrees(math.atan2(sy - p.y, sx - p.x)) - p.angle + 180) % 360 - 180
            if math.hypot(sx - p.x, sy - p.y) <= 64 and abs(aim) < 30:
                p.finished = True
            p.door_until = p.time + 5
        if "ArrowUp" in keys and not p.finished:
            a = math.radians(p.angle)
            p.walk(p.x + math.cos(a) * secs * 200, p.y + math.sin(a) * secs * 200)
        if "ArrowLeft" in keys and not p.finished:
            p.angle = (p.angle + secs * 110) % 360

    async def sleep(secs):
        page.time += secs

    ex.read, ex.face, ex.hold, ex.sleep, ex.clock = read, face, hold, sleep, lambda: page.time
    return ex, page


def play_sim(ex, page, label, limit=400):
    async def play():
        calls = 0
        result = ""
        while calls < limit:
            calls += 1
            result = await ex.step(page, budget=20.0)
            if result == "LEVEL COMPLETE" or "nothing reachable" in result:
                break
        return calls, result

    t0 = time.time()
    calls, result = asyncio.run(play())
    tiles = ex.vmap.tiles
    stats = {"run": ex.runs, "loaded": ex.loaded_tiles, "result": result, "calls": calls, "plans": ex.plans,
             "distance": round(page.walked), "game_seconds": round(page.time), "bumps": page.bumps,
             "tiles": len(tiles), "doors": sum(1 for v in tiles.values() if v == DOOR),
             "windows": sum(1 for v in tiles.values() if v == WINDOW),
             "objects_at_start": ex.loaded_objects, "objects": len(ex.objects), "detours": ex.detours,
             "cpu_seconds": round(time.time() - t0, 1)}
    print(label, stats)
    return stats


def demo(wad_path):
    import tempfile
    from level_map import Level
    level = Level(open(wad_path, "rb").read())
    segs = level.segments()
    sight = [s for s in segs if s[4] in (0, 1)]

    def really_visible(s, t):
        turn = (math.degrees(math.atan2(t["y"] - s["y"], t["x"] - s["x"])) - s["angle"] + 180) % 360 - 180
        clear = all(dc.ray_hit(s["x"], s["y"], t["x"], t["y"], w) is None for w in sight)
        return abs(turn) <= 45 and math.hypot(t["x"] - s["x"], t["y"] - s["y"]) <= 1024 and clear

    with tempfile.TemporaryDirectory() as folder:
        os.environ["DOOM_MAP_DIR"] = folder
        os.environ.pop("DOOM_MEMORY_ID", None)
        os.environ.pop("BEDROCK_AGENTCORE_MEMORY_ID", None)

        ex1, page1 = make_sim(level, segs)
        s = page1.state()
        assert not ex1.objects and not ex1.remembered(s)
        ex1.observe(s)
        seen = {(t["ednum"], round(t["x"]), round(t["y"])) for t in s["things"] if really_visible(s, t)}
        hidden = [t for t in s["things"] if not really_visible(s, t)]
        assert set(ex1.objects) == seen, (set(ex1.objects) ^ seen)
        assert hidden and all((t["ednum"], round(t["x"]), round(t["y"])) not in ex1.objects for t in hidden)
        behind = [t for t in hidden if t["y"] < s["y"]]
        print(f"objects in level: {len(s['things'])}, known after first look north from spawn: {len(seen)}, "
              f"not known: {len(hidden)} (of which behind the player: {len(behind)})")
        print("first look:", sorted(o["name"] for o in ex1.objects.values()))

        first = play_sim(ex1, page1, "run 1 (empty memory):")
        assert first["result"] == "LEVEL COMPLETE", first
        assert first["loaded"] == 0 and first["objects_at_start"] == 0 and ex1.exit_seen
        sx, sy = ex1.switch
        assert math.hypot(page1.x - sx, page1.y - sy) <= 64
        print(ex1.vmap.ascii(page1.x, page1.y, radius=14))
        print("saved map blob bytes:", len(map_store.pack(ex1.snapshot())))
        kept = {k for k, o in ex1.objects.items() if not o["dropped"]}
        kinds = {}
        for o in ex1.objects.values():
            kinds[o["kind"]] = kinds.get(o["kind"], 0) + 1
        print("objects known after run 1 by kind:", kinds, "collected or dead this run:", len(ex1.gone))

        ex2, page2 = make_sim(level, segs)
        assert set(ex2.objects) == kept and ex2.loaded_objects == len(kept), (len(ex2.objects), len(kept))
        near = ex2.remembered(page2.state())[:5]
        assert near
        print("run 2 starts knowing", len(ex2.objects), "objects; nearest:",
              [(o["name"], round(d)) for d, o in near])
        second = play_sim(ex2, page2, "run 2 (saved map):  ")
        assert second["result"] == "LEVEL COMPLETE", second
        assert second["run"] == 2 and second["loaded"] == first["tiles"], (second, first)
        assert second["plans"] * 3 <= first["plans"], (first["plans"], second["plans"])
        assert second["distance"] < first["distance"], (first["distance"], second["distance"])
        assert second["game_seconds"] * 2 <= first["game_seconds"], (first["game_seconds"], second["game_seconds"])

        ex3, page3 = make_sim(level, segs)
        hurt = page3.state()
        hurt["health"] = 40
        plan = ex3.plan(hurt)
        healing = [k for k in ex3.objects if k[0] in HEALING]
        print("known healing items:", len(healing), "plan at 40 HP:", plan["kind"], plan.get("name"), plan["target"])
        assert healing and plan["kind"] == "item" and plan["key"][0] in HEALING, plan
        fine = ex3.plan(page3.state())
        print("plan at 100 HP:", fine["kind"], fine.get("name", ""), fine["target"])
        assert fine["kind"] != "item" or fine["key"][0] in WEAPON_SLOT

        direct = level.path(level.player_start()[:2], level.exit_switch()[1])
        pts = [level.player_start()[:2]] + [(w[0], w[1]) for w in direct]
        best = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))
        print(f"full-map planner distance for comparison: {best:.0f}")
        del os.environ["DOOM_MAP_DIR"]
    print("ok")


if __name__ == "__main__":
    demo(sys.argv[1])
