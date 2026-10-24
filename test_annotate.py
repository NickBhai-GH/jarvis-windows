"""Run: .venv/bin/python test_annotate.py  (checks annotate maps screenshot pixels to the desktop like click_at)"""
from pctools import LAST_SHOT, shapes_to_desktop, to_desktop

LAST_SHOT.update(x=0, y=0, scale=2.0)          # 'both': 3840x1080 shrunk to 1920x540
ring, arrow = shapes_to_desktop([{"type": "ring", "x": 1000, "y": 300, "radius": 20},
                                 {"type": "arrow", "x": 10, "y": 20, "from_x": 50, "from_y": 60}])
assert (ring["x"], ring["y"], ring["radius"]) == (2000, 600, 40), ring
assert (arrow["x"], arrow["y"], arrow["from_x"], arrow["from_y"]) == (20, 40, 100, 120), arrow

LAST_SHOT.update(x=1920, y=0, scale=1.0)       # 'right' monitor, full size
box, = shapes_to_desktop([{"type": "rect", "x": 100, "y": 200, "w": 300, "h": 50, "label": "Save"}])
assert (box["x"], box["y"], box["w"], box["h"], box["label"]) == (2020, 200, 300, 50, "Save"), box

LAST_SHOT.update(x=500, y=120, scale=1.0)      # 'window' shot of a window at 500,120
ring, = shapes_to_desktop([{"type": "ring", "x": 30, "y": 40, "radius": 25}])
assert (ring["x"], ring["y"]) == to_desktop(30, 40) == (530, 160), ring
print("ok")
