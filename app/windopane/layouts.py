"""Screen layouts.

Each layout is a list of tiles in normalized screen coordinates (x, y, w, h),
0..1 on both axes. Tile order == slot order, so slot 1 is always the "main"
tile. Tiles flagged as overlays (picture-in-picture) are drawn on top and are
not shrunk by the gap setting.
"""

MAX_SLOTS = 16

T = 1 / 3
TT = 2 / 3

_PIP_W = 0.28
_PIP_M = 0.025

LAYOUTS = {
    "single": {
        "name": "Single",
        "tiles": [(0, 0, 1, 1)],
    },
    "split-v": {
        "name": "Side by side",
        "tiles": [(0, 0, 0.5, 1), (0.5, 0, 0.5, 1)],
    },
    "split-h": {
        "name": "Stacked",
        "tiles": [(0, 0, 1, 0.5), (0, 0.5, 1, 0.5)],
    },
    "pip": {
        "name": "Picture in picture",
        "tiles": [(0, 0, 1, 1),
                  (1 - _PIP_W - _PIP_M, 1 - _PIP_W - _PIP_M, _PIP_W, _PIP_W)],
        "overlay": [1],
    },
    "main-2": {
        "name": "Main + 2",
        "tiles": [(0, 0, TT, 1), (TT, 0, T, 0.5), (TT, 0.5, T, 0.5)],
    },
    "trio": {
        "name": "Trio",
        "tiles": [(0, 0, 0.5, 0.5), (0.5, 0, 0.5, 0.5), (0.25, 0.5, 0.5, 0.5)],
    },
    "quad": {
        "name": "2 x 2",
        "tiles": [(0, 0, 0.5, 0.5), (0.5, 0, 0.5, 0.5),
                  (0, 0.5, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)],
    },
    "main-3-right": {
        "name": "Main + 3 right",
        "tiles": [(0, 0, TT, 1), (TT, 0, T, T), (TT, T, T, T), (TT, TT, T, T)],
    },
    "main-3-bottom": {
        "name": "Main + 3 bottom",
        "tiles": [(0, 0, 1, TT), (0, TT, T, T), (T, TT, T, T), (TT, TT, T, T)],
    },
    "pip-3": {
        "name": "Main + 3 insets",
        "tiles": [(0, 0, 1, 1),
                  (1 - 0.24 - _PIP_M, _PIP_M, 0.24, 0.24),
                  (1 - 0.24 - _PIP_M, 0.38, 0.24, 0.24),
                  (1 - 0.24 - _PIP_M, 1 - 0.24 - _PIP_M, 0.24, 0.24)],
        "overlay": [1, 2, 3],
    },
}


def _grid(cols, rows):
    return [(c / cols, r / rows, 1 / cols, 1 / rows) for r in range(rows) for c in range(cols)]


def _main_plus(n):
    """A main tile of (n-1)x(n-1) cells in an n x n grid, the rest around
    its right side and bottom: main + 5 (n=3), main + 7 (n=4)."""
    cell = 1 / n
    tiles = [(0, 0, (n - 1) * cell, (n - 1) * cell)]
    tiles += [((n - 1) * cell, r * cell, cell, cell) for r in range(n)]
    tiles += [(c * cell, (n - 1) * cell, cell, cell) for c in range(n - 1)]
    return tiles


def _main_12():
    """A 2x2-cell main tile in the top-left of a 4x4 grid plus 12 small tiles."""
    q = 0.25
    tiles = [(0, 0, 2 * q, 2 * q)]
    for r in range(4):
        for c in range(4):
            if r < 2 and c < 2:
                continue
            tiles.append((c * q, r * q, q, q))
    return tiles


LAYOUTS.update({
    "grid-3x2": {"name": "3 x 2", "tiles": _grid(3, 2)},
    "main-5": {"name": "Main + 5", "tiles": _main_plus(3)},
    "grid-3x3": {"name": "3 x 3", "tiles": _grid(3, 3)},
    "main-7": {"name": "Main + 7", "tiles": _main_plus(4)},
    "main-12": {"name": "Main + 12", "tiles": _main_12()},
    "grid-4x4": {"name": "4 x 4", "tiles": _grid(4, 4)},
})

DEFAULT_LAYOUT = "quad"


def tile_count(layout_id):
    return len(LAYOUTS[layout_id]["tiles"])


def is_overlay(layout_id, index):
    return index in LAYOUTS[layout_id].get("overlay", [])


def compute_rects(layout_id, width, height, gap=0):
    """Return integer (x, y, w, h) rectangles for a layout on a screen.

    Edges are snapped from the normalized coordinates so adjacent tiles share
    exact pixel boundaries; ``gap`` pixels of black are left between tiles
    and around the outside of the screen.
    """
    layout = LAYOUTS[layout_id]
    rects = []
    half = gap / 2.0
    for i, (nx, ny, nw, nh) in enumerate(layout["tiles"]):
        x0 = round(nx * width)
        y0 = round(ny * height)
        x1 = round((nx + nw) * width)
        y1 = round((ny + nh) * height)
        if gap and not is_overlay(layout_id, i):
            x0 += int(half)
            y0 += int(half)
            x1 -= int(half + 0.5)
            y1 -= int(half + 0.5)
        rects.append((x0, y0, max(1, x1 - x0), max(1, y1 - y0)))
    return rects


def describe():
    """Layout list for the web UI (normalized tiles for drawing previews)."""
    return [
        {
            "id": lid,
            "name": spec["name"],
            "tiles": [list(t) for t in spec["tiles"]],
            "overlay": spec.get("overlay", []),
        }
        for lid, spec in LAYOUTS.items()
    ]


def next_layout(current, step=1):
    ids = list(LAYOUTS)
    if current not in ids:
        return ids[0]
    return ids[(ids.index(current) + step) % len(ids)]
