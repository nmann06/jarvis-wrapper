"""/colour: the whole window in another colour than Jarvis blue.

Everything is drawn in theme.py's blues, so rather than a second palette for every widget the
window turns those blues into the chosen colour on the way to the terminal (Painter, wrapped
around Textual's driver.write). That covers text already on the screen, Textual's own focus and
scrollbar colours, and the dialogs, and changing it is just a full repaint. Reds, greens and
ambers aren't blue, so errors, diffs and warnings keep their meaning.
"""

import colorsys
import re
from functools import lru_cache

BLUE = 205  # the hue of Jarvis's blues (theme.ACCENT and the rest), in degrees
BAND = (175, 245)  # hues that count as Jarvis's blues: cyan through to periwinkle
MIN_SATURATION = 0.06  # greys stay grey

# name: the hue Jarvis's blue becomes, or None for grey. In the order /colour lists them.
COLOURS = {"blue": BLUE, "cyan": 188, "teal": 168, "green": 135, "yellow": 52, "orange": 30,
           "red": 2, "pink": 330, "purple": 275, "grey": None}
ALIASES = {"gray": "grey", "default": "blue"}
DEFAULT = "blue"

SGR = re.compile(r"\x1b\[([0-9;]*)m")
PARTIAL = re.compile(r"\x1b(\[[0-9;]*)?$")  # a sequence cut off at the end of a write
CUBE = [0, 95, 135, 175, 215, 255]  # the 256-colour palette's 6x6x6 cube levels


def name_of(name):
    """A colour's name as COLOURS has it, or None if there's no such colour."""
    name = ALIASES.get((name or "").strip().lower(), (name or "").strip().lower())
    return name if name in COLOURS else None


@lru_cache(maxsize=4096)
def recolour(rgb, name):
    """One colour, (r, g, b) in 0-255, as it's drawn in the named colour."""
    target = COLOURS.get(name, BLUE)
    if target == BLUE:
        return rgb
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    if s < MIN_SATURATION or not BAND[0] <= h * 360 <= BAND[1]:
        return rgb
    if target is None:
        s = 0.0
    else:
        h = (h + (target - BLUE) / 360) % 1.0
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb(h, l, s))


def _from_256(index):
    if 16 <= index <= 231:
        index -= 16
        return CUBE[index // 36], CUBE[index // 6 % 6], CUBE[index % 6]
    return None  # the 16 terminal colours and the grey ramp: never Jarvis blue


def _to_256(rgb):
    levels = [min(range(6), key=lambda i: abs(CUBE[i] - c)) for c in rgb]
    return 16 + 36 * levels[0] + 6 * levels[1] + levels[2]


@lru_cache(maxsize=4096)
def _sgr(params, name):
    """An SGR sequence's parameters with any colours in it recoloured."""
    parts = params.split(";")
    out, i = [], 0
    while i < len(parts):
        part = parts[i]
        if part in ("38", "48", "58") and i + 4 < len(parts) and parts[i + 1] == "2":
            try:
                rgb = recolour(tuple(int(p) for p in parts[i + 2:i + 5]), name)
            except ValueError:
                out.append(part)
                i += 1
                continue
            out += [part, "2", *map(str, rgb)]
            i += 5
        elif part in ("38", "48", "58") and i + 2 < len(parts) and parts[i + 1] == "5":
            rgb = _from_256(int(parts[i + 2])) if parts[i + 2].isdigit() else None
            out += [part, "5", str(_to_256(recolour(rgb, name))) if rgb else parts[i + 2]]
            i += 3
        else:
            out.append(part)
            i += 1
    return ";".join(out)


class Painter:
    """Recolours what the window writes to the terminal (escape codes and all)."""

    def __init__(self, name=DEFAULT):
        self.name = name_of(name) or DEFAULT
        self.carry = ""  # the start of an escape code that the last write cut off

    def paint(self, data):
        if self.carry:
            data, self.carry = self.carry + data, ""
        if self.name == DEFAULT:
            return data
        cut = PARTIAL.search(data[-64:])
        if cut:  # Windows writes in chunks, which can split a sequence: finish it next time
            self.carry = cut.group(0)
            data = data[:len(data) - len(self.carry)]
        return SGR.sub(lambda m: f"\x1b[{_sgr(m.group(1), self.name)}m", data)
