from jarvis_code import colour, theme


def rgb(hex_colour):
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


def test_blues_change_and_the_rest_keep_their_meaning():
    for name in ("red", "green", "purple"):
        assert colour.recolour(rgb(theme.ACCENT), name) != rgb(theme.ACCENT)
        for kept in (theme.SUCCESS, theme.ERROR, theme.WARNING, theme.ATTENTION, theme.DIFF_ADDED):
            assert colour.recolour(rgb(kept), name) == rgb(kept)
    r, g, b = colour.recolour(rgb(theme.ACCENT), "red")
    assert r > g and r > b


def test_grey_is_grey():
    for blue in (theme.ACCENT, theme.LABEL, theme.DIM, theme.USER_BG, *theme.GRADIENT):
        r, g, b = colour.recolour(rgb(blue), "grey")
        assert r == g == b


def test_names():
    assert colour.name_of("Gray") == "grey" and colour.name_of(" RED ") == "red"
    assert colour.name_of("mauve") is None and colour.name_of(None) is None
    assert colour.Painter("mauve").name == colour.DEFAULT


def test_painter_rewrites_the_escape_codes():
    painter = colour.Painter("grey")
    out = painter.paint("\x1b[1;38;2;60;190;255;48;2;22;32;46mhi\x1b[0m 38;2;60;190;255")
    assert out == "\x1b[1;38;2;158;158;158;48;2;34;34;34mhi\x1b[0m 38;2;60;190;255"  # text left alone
    assert painter.paint("\x1b[38;5;39mx") == "\x1b[38;5;102mx"  # 256 colours: a blue to the cube's grey
    assert colour.Painter("blue").paint("\x1b[38;2;60;190;255mx") == "\x1b[38;2;60;190;255mx"


def test_a_sequence_split_between_writes_is_still_painted():
    painter = colour.Painter("red")
    first = painter.paint("a\x1b[38;2;60;19")
    second = painter.paint("0;255mb")
    assert first == "a" and second == "\x1b[38;2;255;60;70mb"
