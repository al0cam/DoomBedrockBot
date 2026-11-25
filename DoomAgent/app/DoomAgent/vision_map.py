import math
import struct
import sys

from doom_control import ray_hit

FREE, WALL, DOOR, WINDOW = ".", "#", "D", "W"
DOOR_SPECIALS = {1, 26, 27, 28, 31, 32, 33, 34, 117, 118}
NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1))


class VisionMap:
    def __init__(self, walls, tile=32):
        self.walls = walls
        self.tile = tile
        self.tiles = {}

    def key(self, x, y):
        return math.floor(x / self.tile), math.floor(y / self.tile)

    def observe(self, x, y, angle_deg, fov=90, rays=61, max_range=1024):
        before = len(self.tiles)
        step = self.tile / 2
        for n in range(rays):
            a = math.radians(angle_deg - fov / 2 + fov * n / max(rays - 1, 1))
            dx, dy = math.cos(a), math.sin(a)
            ex, ey = x + dx * max_range, y + dy * max_range
            best, door, panes = None, 0, []
            for w in self.walls:
                if w[4] > 2:
                    continue
                t = ray_hit(x, y, ex, ey, w)
                if t is None:
                    continue
                if w[4] == 2:
                    panes.append(t)
                elif best is None or t < best:
                    best, door = t, w[4]
            reach = max_range if best is None else best * max_range
            for t in panes:
                if t * max_range < reach:
                    k = self.key(x + dx * (t * max_range + 1), y + dy * (t * max_range + 1))
                    if self.tiles.get(k) in (None, FREE):
                        self.tiles[k] = WINDOW
            d = 0.0
            while d < reach:
                self.tiles.setdefault(self.key(x + dx * d, y + dy * d), FREE)
                d += step
            if best is not None:
                k = self.key(x + dx * (reach + 1), y + dy * (reach + 1))
                if self.tiles.get(k) in (None, FREE):
                    self.tiles[k] = DOOR if door else WALL
        return len(self.tiles) - before

    def mark_blocked(self, x, y):
        self.tiles[self.key(x, y)] = WALL

    def ascii(self, x, y, radius=12):
        cx, cy = self.key(x, y)
        rows = []
        for ty in range(cy + radius, cy - radius - 1, -1):
            rows.append("".join(
                "@" if (tx, ty) == (cx, cy) else self.tiles.get((tx, ty), "?")
                for tx in range(cx - radius, cx + radius + 1)
            ))
        return "\n".join(rows)

    def frontier(self, x, y):
        best = None
        for (tx, ty), state in self.tiles.items():
            if state != FREE:
                continue
            if all((tx + ox, ty + oy) in self.tiles for ox, oy in NEIGHBOURS):
                continue
            px, py = (tx + 0.5) * self.tile, (ty + 0.5) * self.tile
            d = math.hypot(px - x, py - y)
            if best is None or d < best[0]:
                best = (d, px, py)
        return None if best is None else (best[1], best[2])


def load_walls(path, level="E1M1"):
    data = open(path, "rb").read()
    _, count, ofs = struct.unpack_from("<4sii", data, 0)
    lumps = []
    for n in range(count):
        pos, size, name = struct.unpack_from("<ii8s", data, ofs + n * 16)
        lumps.append((name.rstrip(b"\0").decode("ascii", "replace"), pos, size))
    start = next(n for n, l in enumerate(lumps) if l[0] == level)
    found = {l[0]: l for l in lumps[start + 1:start + 11]}
    _, vp, vs = found["VERTEXES"]
    _, lp, ls = found["LINEDEFS"]
    verts = list(struct.iter_unpack("<hh", data[vp:vp + vs]))
    walls = []
    for v1, v2, flags, special, tag, right, left in struct.iter_unpack("<HHHHHHH", data[lp:lp + ls]):
        door = special in DOOR_SPECIALS
        if left == 0xFFFF or door:
            walls.append([*verts[v1], *verts[v2], 1 if door else 0])
    return walls


def demo(path):
    walls = load_walls(path)
    print("wall segments:", len(walls), "doors:", sum(w[4] for w in walls))
    vmap = VisionMap(walls)
    x, y, angle = 1056, -3616, 90
    learned = vmap.observe(x, y, angle)
    counts = {s: sum(1 for v in vmap.tiles.values() if v == s) for s in (FREE, WALL, DOOR, WINDOW)}
    print("learned", learned, "tiles:", counts)
    print(vmap.ascii(x, y, radius=26))
    assert learned > 100, learned
    assert vmap.tiles.get(vmap.key(1056, -2870)) == WALL, vmap.tiles.get(vmap.key(1056, -2870))
    assert vmap.tiles.get(vmap.key(1056, -3200)) == FREE
    assert vmap.tiles.get(vmap.key(1056, -2900)) == FREE
    assert vmap.key(1056, -2800) not in vmap.tiles
    assert vmap.key(1056, -3700) not in vmap.tiles
    assert vmap.observe(x, y, angle) == 0
    spot = vmap.frontier(x, y)
    print("frontier:", spot)
    assert spot is not None
    vmap.mark_blocked(1056, -3200)
    assert vmap.tiles[vmap.key(1056, -3200)] == WALL
    vmap.observe(x, y, angle)
    assert vmap.tiles[vmap.key(1056, -3200)] == WALL

    from level_map import Level
    segs = Level(open(path, "rb").read()).segments()
    solid = [s for s in segs if s[4] in (0, 1)]
    assert sorted(solid) == sorted(walls)
    print("level segments:", len(segs), "windows:", sum(1 for s in segs if s[4] == 2),
          "ledges:", sum(1 for s in segs if s[4] == 3))

    at_door = VisionMap(segs)
    at_door.observe(1472, -2528, 0)
    assert at_door.tiles.get(at_door.key(1537, -2528)) == DOOR, at_door.tiles.get(at_door.key(1537, -2528))
    assert at_door.key(1700, -2528) not in at_door.tiles
    print("door tile seen from (1472,-2528) facing east")
    print(at_door.ascii(1472, -2528, radius=8))

    found = None
    for x1, y1, x2, y2, kind in segs:
        if kind != 2 or found:
            continue
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        length = math.hypot(x2 - x1, y2 - y1)
        nx, ny = (y2 - y1) / length, -(x2 - x1) / length
        for sign in (1, -1):
            px, py = mx + nx * 96 * sign, my + ny * 96 * sign
            if any(ray_hit(px, py, mx, my, s) is not None for s in solid):
                continue
            dx, dy = -nx * sign, -ny * sign
            look = VisionMap(segs)
            look.observe(px, py, math.degrees(math.atan2(dy, dx)) % 360)
            pane = look.tiles.get(look.key(mx + dx, my + dy))
            beyond = look.tiles.get(look.key(mx + dx * 48, my + dy * 48))
            if pane == WINDOW and beyond == FREE:
                found = (px, py, math.degrees(math.atan2(dy, dx)) % 360, (x1, y1, x2, y2), look)
                break
    assert found, "no window pose produced a WINDOW tile"
    px, py, facing, pane, look = found
    before = dict(look.tiles)
    look.observe(px, py, facing)
    assert look.tiles == before
    assert WINDOW in look.tiles.values()
    print(f"window {pane} seen from ({px:.0f},{py:.0f}) facing {facing:.0f}, tiles beyond it learned")
    print(look.ascii(px, py, radius=10))
    print("ok")


if __name__ == "__main__":
    demo(sys.argv[1])
