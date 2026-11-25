import base64
import heapq
import math
import struct
import sys
import time

CELL = 16
CHUNK = 524288
WALL, DOOR, WINDOW, LEDGE = 0, 1, 2, 3
DOOR_SPECIALS = {1, 26, 27, 28, 31, 32, 33, 34, 117, 118}
DAMAGING = {4, 5, 7, 11, 16}
SOLID_THINGS = {
    2035: 10, 48: 16, 2028: 16, 85: 16, 86: 16, 47: 16, 43: 16, 54: 32, 46: 16, 55: 16, 56: 16, 57: 16,
    44: 16, 45: 16, 70: 10, 30: 16, 31: 16, 32: 16, 33: 16, 35: 16, 36: 16, 37: 16, 41: 16, 42: 16,
}
STEPS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]

LOAD_JS = """async () => {
    window.__wad = new Uint8Array(await (await fetch('doom1.wad')).arrayBuffer());
    return window.__wad.length;
}"""
SLICE_JS = """([ofs, len]) => {
    const part = window.__wad.subarray(ofs, ofs + len);
    let s = '';
    for (let i = 0; i < part.length; i += 0x8000) {
        s += String.fromCharCode.apply(null, part.subarray(i, i + 0x8000));
    }
    return btoa(s);
}"""


async def fetch_wad(page):
    size = await page.evaluate(LOAD_JS)
    out = bytearray()
    for ofs in range(0, size, CHUNK):
        out += base64.b64decode(await page.evaluate(SLICE_JS, [ofs, CHUNK]))
    return bytes(out)


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0 if length == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def crosses(ax, ay, bx, by, seg):
    x1, y1, x2, y2 = seg[:4]
    rx, ry, sx, sy = bx - ax, by - ay, x2 - x1, y2 - y1
    den = rx * sy - ry * sx
    if den == 0:
        return False
    t = ((x1 - ax) * sy - (y1 - ay) * sx) / den
    w = ((x1 - ax) * ry - (y1 - ay) * rx) / den
    return 0 <= t <= 1 and 0 <= w <= 1


class Level:
    def __init__(self, wad, name="E1M1"):
        _, count, ofs = struct.unpack_from("<4sii", wad, 0)
        lumps = []
        for n in range(count):
            pos, size, raw = struct.unpack_from("<ii8s", wad, ofs + n * 16)
            lumps.append((raw.rstrip(b"\0").decode("ascii", "replace"), pos, size))
        start = next(n for n, l in enumerate(lumps) if l[0] == name)
        found = {l[0]: l for l in lumps[start + 1:start + 11]}

        def lump(lname, fmt):
            _, pos, size = found[lname]
            return list(struct.iter_unpack(fmt, wad[pos:pos + size]))

        self.name = name
        self.verts = lump("VERTEXES", "<hh")
        self.lines = lump("LINEDEFS", "<HHHHHHH")
        self.sides = lump("SIDEDEFS", "<hh8s8s8sH")
        self.sectors = lump("SECTORS", "<hh8s8sHHH")
        self.things = lump("THINGS", "<hhHHH")
        self.nodes = lump("NODES", "<hhhh4h4hHH")
        self.ssectors = lump("SSECTORS", "<HH")
        self.segs = lump("SEGS", "<HHhHHh")
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        self.x0, self.y0 = min(xs) // CELL * CELL, min(ys) // CELL * CELL
        self.w, self.h = (max(xs) - self.x0) // CELL + 2, (max(ys) - self.y0) // CELL + 2
        self.sector = {}
        self.floor = {}
        self.door_cells = set()
        self.damage_cells = set()
        for cx in range(self.w):
            for cy in range(self.h):
                x, y = self.centre((cx, cy))
                s = self.sector_at(x + 0.5, y + 0.5)
                self.sector[(cx, cy)] = s
                self.floor[(cx, cy)] = self.sectors[s][0]
                if self.sectors[s][0] == self.sectors[s][1]:
                    self.door_cells.add((cx, cy))
                if self.sectors[s][5] in DAMAGING:
                    self.damage_cells.add((cx, cy))
        self.solid = [[*self.verts[l[0]], *self.verts[l[1]], WALL]
                      for l in self.lines if l[6] == 0xFFFF and l[3] not in DOOR_SPECIALS]
        self._grids = {}
        self._segments = None
        self.closed_lines = 0

    def cell(self, x, y):
        return round((x - self.x0) / CELL), round((y - self.y0) / CELL)

    def centre(self, c):
        return self.x0 + c[0] * CELL, self.y0 + c[1] * CELL

    def inside(self, c):
        return 0 <= c[0] < self.w and 0 <= c[1] < self.h

    def sector_at(self, x, y):
        n = len(self.nodes) - 1
        while not n & 0x8000:
            nx, ny, ndx, ndy = self.nodes[n][:4]
            side = 0 if (y - ny) * ndx < ndy * (x - nx) else 1
            n = self.nodes[n][12 + side]
        _, first = self.ssectors[n & 0x7FFF]
        _, _, _, linedef, direction, _ = self.segs[first]
        return self.sides[self.lines[linedef][5 + direction]][5]

    def is_damaging(self, x, y):
        c = self.cell(x, y)
        return c in self.damage_cells

    def player_start(self):
        t = next(t for t in self.things if t[3] == 1)
        return t[0], t[1], t[2]

    def exit_switch(self):
        line = next(l for l in self.lines if l[3] == 11)
        ax, ay = self.verts[line[0]]
        bx, by = self.verts[line[1]]
        mx, my = (ax + bx) / 2, (ay + by) / 2
        nx, ny = by - ay, -(bx - ax)
        length = math.hypot(nx, ny)
        return (mx, my), (mx + nx / length * 40, my + ny / length * 40)

    def _blocked(self, clearance, avoid):
        key = (clearance, avoid)
        if key in self._grids:
            return self._grids[key]
        blocked = set(self.damage_cells) if avoid else set()
        for v1, v2, flags, special, tag, right, left in self.lines:
            ax, ay = self.verts[v1]
            bx, by = self.verts[v2]
            hard = left == 0xFFFF or flags & 1
            high = None
            if not hard:
                fs, bs = self.sectors[self.sides[right][5]], self.sectors[self.sides[left][5]]
                if special not in DOOR_SPECIALS and min(fs[1], bs[1]) - max(fs[0], bs[0]) < 56:
                    hard = not (fs[0] == fs[1] or bs[0] == bs[1])
                if abs(fs[0] - bs[0]) > 24:
                    high = max(fs[0], bs[0])
            if not hard and high is None:
                continue
            c0 = self.cell(min(ax, bx) - clearance, min(ay, by) - clearance)
            c1 = self.cell(max(ax, bx) + clearance, max(ay, by) + clearance)
            for cx in range(max(0, c0[0] - 1), min(self.w, c1[0] + 2)):
                for cy in range(max(0, c0[1] - 1), min(self.h, c1[1] + 2)):
                    x, y = self.centre((cx, cy))
                    if seg_dist(x, y, ax, ay, bx, by) < clearance:
                        if hard or self.floor[(cx, cy)] + 24 < high:
                            blocked.add((cx, cy))
        for x, y, angle, kind, flags in self.things:
            if kind in SOLID_THINGS and flags & 7:
                r = SOLID_THINGS[kind] + 16 + 6
                c = self.cell(x, y)
                for cx in range(c[0] - 4, c[0] + 5):
                    for cy in range(c[1] - 4, c[1] + 5):
                        px, py = self.centre((cx, cy))
                        if math.hypot(px - x, py - y) < r:
                            blocked.add((cx, cy))
        self._grids[key] = blocked
        return blocked

    def _ok(self, blocked, a, b):
        return self.inside(b) and b not in blocked and self.floor[b] - self.floor[a] <= 24

    def _moves(self, blocked, c):
        for dx, dy in STEPS:
            n = (c[0] + dx, c[1] + dy)
            if not self._ok(blocked, c, n):
                continue
            if dx and dy and not (self._ok(blocked, c, (c[0] + dx, c[1])) and self._ok(blocked, c, (c[0], c[1] + dy))):
                continue
            yield n, math.hypot(dx, dy)

    def _reachable(self, blocked, src):
        seen = {src}
        todo = [src]
        while todo:
            c = todo.pop()
            for n, _ in self._moves(blocked, c):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        return seen

    def _search(self, blocked, src, goal):
        dist = {src: 0}
        prev = {}
        pq = [(0, src)]
        while pq:
            d, c = heapq.heappop(pq)
            if c == goal:
                break
            if d > dist[c]:
                continue
            for n, cost in self._moves(blocked, c):
                nd = d + cost
                if nd < dist.get(n, 1e18):
                    dist[n] = nd
                    prev[n] = c
                    heapq.heappush(pq, (nd, n))
        if goal != src and goal not in prev:
            return None
        cells = [goal]
        while cells[-1] != src:
            cells.append(prev[cells[-1]])
        cells.reverse()
        return cells

    def _snap(self, blocked, x, y):
        c = self.cell(x, y)
        if self.inside(c) and c not in blocked:
            return c
        best = None
        for dx in range(-4, 5):
            for dy in range(-4, 5):
                n = (c[0] + dx, c[1] + dy)
                if not self.inside(n) or n in blocked:
                    continue
                px, py = self.centre(n)
                d = math.hypot(px - x, py - y)
                if (best is None or d < best[0]) and not any(crosses(x, y, px, py, s) for s in self.solid):
                    best = (d, n)
        return None if best is None else best[1]

    def _los(self, blocked, a, b):
        (x0, y0), (x1, y1) = self.centre(a), self.centre(b)
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / 4))
        last = a
        for k in range(1, n + 1):
            c = self.cell(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n)
            if c != last:
                if not self._ok(blocked, last, c):
                    return False
                last = c
        return True

    def _waypoints(self, blocked, cells, goal):
        must = {0: False, len(cells) - 1: False}
        k = 0
        while k < len(cells):
            if cells[k] in self.door_cells:
                j = k
                while j < len(cells) and cells[j] in self.door_cells:
                    j += 1
                must[max(0, k - 4)] = True
                must.setdefault(min(len(cells) - 1, j + 3), False)
                k = j
            else:
                k += 1
        keys = sorted(must)
        way = []
        for a, b in zip(keys, keys[1:]):
            i = a
            while i < b:
                j = b
                while j > i + 1 and not self._los(blocked, cells[i], cells[j]):
                    j -= 1
                x, y = self.centre(cells[j])
                way.append((x, y, must.get(j, False)))
                i = j
        last = way[-1] if way else self.centre(cells[-1])
        if not way or math.hypot(goal[0] - last[0], goal[1] - last[1]) > 8:
            way.append((goal[0], goal[1], False))
        return way

    def path(self, start, goal, clearance=32):
        clears = [c for c in dict.fromkeys((clearance, 24, 20)) if c <= clearance]
        for avoid in (True, False):
            for clear in clears:
                blocked = self._blocked(clear, avoid)
                src = self._snap(blocked, start[0], start[1])
                dst = self._snap(blocked, goal[0], goal[1])
                if src is None or dst is None:
                    continue
                cells = self._search(blocked, src, dst)
                if cells:
                    return self._waypoints(blocked, cells, goal)
        return None

    def segments(self):
        if self._segments is not None:
            return self._segments
        blocked = self._blocked(16, False)
        sx, sy, _ = self.player_start()
        reach = self._reachable(blocked, self.cell(sx, sy))
        walkable = {self.sector[c] for c in reach}
        out = []
        self.closed_lines = 0
        for v1, v2, flags, special, tag, right, left in self.lines:
            if special in DOOR_SPECIALS:
                kind = DOOR
            elif left == 0xFFFF:
                kind = WALL
            else:
                front, back = self.sides[right][5], self.sides[left][5]
                fs, bs = self.sectors[front], self.sectors[back]
                opening = min(fs[1], bs[1]) - max(fs[0], bs[0])
                if opening <= 0:
                    self.closed_lines += 1
                    continue
                if flags & 1 or opening < 56:
                    kind = WINDOW
                elif abs(fs[0] - bs[0]) > 24:
                    high = front if fs[0] > bs[0] else back
                    kind = LEDGE if high in walkable else WINDOW
                else:
                    continue
            out.append([*self.verts[v1], *self.verts[v2], kind])
        self._segments = out
        return out


def demo(path):
    t0 = time.time()
    level = Level(open(path, "rb").read())
    print(f"parsed {level.name}: {len(level.lines)} lines, {len(level.sectors)} sectors, "
          f"{len(level.things)} things, grid {level.w}x{level.h} in {time.time() - t0:.1f}s")
    segs = level.segments()
    base = [s for s in segs if s[4] in (WALL, DOOR)]
    doors = [s for s in segs if s[4] == DOOR]
    windows = [s for s in segs if s[4] == WINDOW]
    ledges = [s for s in segs if s[4] == LEDGE]
    print("walls+doors:", len(base), "doors:", len(doors), "windows:", len(windows), "ledges:", len(ledges),
          "closed two-sided lines skipped:", level.closed_lines)
    print("window examples:", windows[:4])
    print("ledge examples:", ledges[:4])
    assert len(base) == 310 and len(doors) == 8, (len(base), len(doors))
    assert windows

    sx, sy, sangle = level.player_start()
    switch, stand = level.exit_switch()
    print("player start:", (sx, sy, sangle), "exit switch:", switch, "stand:", stand)
    t1 = time.time()
    route = level.path((sx, sy), stand)
    print(f"path to exit: {len(route)} waypoints in {time.time() - t1:.1f}s")
    assert route
    assert sum(1 for w in route if w[2]) == 3, route
    pts = [(sx, sy)] + [(w[0], w[1]) for w in route]
    for a, b in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 8))
        for k in range(n + 1):
            assert not level.is_damaging(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n), (a, b)
    try:
        from doom_control import E1M1_WAYPOINTS as fixed
    except Exception as e:
        fixed = []
        print("could not import doom_control waypoints:", e)
    print("computed vs hard-coded:")
    for n in range(max(len(route), len(fixed))):
        left = route[n] if n < len(route) else ""
        right = fixed[n] if n < len(fixed) else ""
        print(f"  {n:2d} {str(left):28s} {right}")
    if fixed:
        print("identical:", [tuple(w) for w in route] == [tuple(w) for w in fixed])

    armor = next(t for t in level.things if t[3] == 2018)
    to_armor = level.path((sx, sy), (armor[0], armor[1]))
    print("path to green armor at", (armor[0], armor[1]), ":", to_armor)
    assert to_armor
    pickups = [t for t in level.things if t[3] in (2011, 2012, 2014, 2015, 2018, 2019, 2001, 2007, 2008, 2048, 2049)]
    reachable = sum(1 for t in pickups if level.path((sx, sy), (t[0], t[1])) is not None)
    print("pickups reachable with static heights:", reachable, "of", len(pickups))

    assert level.path((sx, sy), (-5000, -5000)) is None
    blocked = level._blocked(20, False)
    reach = level._reachable(blocked, level.cell(sx, sy))
    void = next(c for c in sorted(level.floor) if c not in blocked and c not in reach
                and all((c[0] + dx, c[1] + dy) not in blocked for dx, dy in STEPS))
    assert level.path((sx, sy), level.centre(void)) is None, level.centre(void)
    print("unreachable point", level.centre(void), "-> None")
    assert level.path((sx, sy), (sx, sy)) == [(sx, sy, False)]
    print(f"ok in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    demo(sys.argv[1])
