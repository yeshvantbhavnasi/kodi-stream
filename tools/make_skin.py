#!/usr/bin/env python3
"""Generate Stream's skin: the Home and Grid windows and the images they draw with.

The Home window has twenty-four identical rows, each with its own billboard group and tile layouts, so the XML is
written from one template here rather than kept by hand. Run after changing anything below:

    python3 tools/make_skin.py

Layout notes (1080p coordinates):
  - Top bar 0-90: logo, tabs (pill highlight), search, profile, settings, close. A dark fade keeps it readable
    over the billboard. The genre strip (Movies and Shows) sits under it at 92 and slides everything else down 44.
  - Billboard 0-720: the highlighted title's TMDb backdrop fills the right of the screen behind a fade; the
    title, its facts and a short synopsis sit on the left (the title's own page has the cast and episodes). Tiles without a backdrop fall
    back to their poster.
  - Rows from 500: poster tiles with rounded corners, a white rounded frame and a small zoom on the highlighted
    tile; YouTube rows use wide tiles; the Top 10 row draws Netflix-style big numerals beside the posters.

Touch layouts (script-stream-home-touch.xml, script-stream-grid-touch.xml, script-stream-title-touch.xml) are written from the same templates for
phones and tablets: tiles a quarter bigger, the rows starting lower so the next row's posters peek in and can be tapped,
larger top-bar icons, a Back button on inner screens and taller episode rows.
"""
import math
import os
import re
import struct
import zlib

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'script.stream', 'resources', 'skins', 'Default')
XML = os.path.join(ROOT, '1080i')
MEDIA = os.path.join(ROOT, 'media')

ROWS = 18
FIRST = 200
BROWSE_FIRST = 400
TABS, CLOSE, SEARCH, GEAR, PROFILE, FILTER, BACK = 100, 110, 120, 130, 140, 160, 170
TOUCH = {'on': False}  # set while the touch layouts are written
SCALE = 1.25  # how much bigger tiles are in the touch layouts
BG = (0x14, 0x14, 0x14)
TOP_BAR = 'Control.HasFocus(100) | Control.HasFocus(110) | Control.HasFocus(120) | Control.HasFocus(130) | Control.HasFocus(140) | Control.HasFocus(160)'
SLIDE = '<animation effect="slide" end="0,44" time="0" condition="!String.IsEmpty(Window.Property(filters))">Conditional</animation>'


# ---------- images (plain PNG writer; no PIL on the Mac mini) ----------

def png(path, width, height, pixel):
    """Write an RGBA PNG; pixel(x, y) returns (r, g, b, a)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(pixel(x, y))
    def chunk(kind, data):
        body = kind + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)
    out = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    out += chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(out)


def rounded(path, width, height, radius, color=(255, 255, 255)):
    """A filled rounded rectangle with anti-aliased corners (4x4 supersampling)."""
    def inside(px, py):
        cx = min(max(px, radius), width - radius)
        cy = min(max(py, radius), height - radius)
        return (px - cx) ** 2 + (py - cy) ** 2 <= radius * radius
    def pixel(x, y):
        hits = sum(1 for i in range(4) for j in range(4) if inside(x + (i + 0.5) / 4, y + (j + 0.5) / 4))
        return color + (hits * 255 // 16,)
    png(path, width, height, pixel)


def gradient(path, width, height, color, alpha_at):
    """A gradient strip Kodi stretches over a control; alpha_at(t) for t from 0 (left or top) to 1."""
    def pixel(x, y):
        t = (x / (width - 1)) if width > height else (y / (height - 1))
        return color + (max(0, min(255, int(round(alpha_at(t) * 255)))),)
    png(path, width, height, pixel)


def smooth(t):
    return t * t * (3 - 2 * t)


def _dist_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _stroke_icon(path, size, dist_fn, half_w=2.1):
    """Render a crisp anti-aliased white thin-stroke icon using signed distance + 4x4 supersampling."""
    def pixel(x, y):
        hits = 0
        for i in range(4):
            for j in range(4):
                if dist_fn(x + (i + 0.5) / 4, y + (j + 0.5) / 4) <= half_w:
                    hits += 1
        return (255, 255, 255, hits * 255 // 16)
    png(path, size, size, pixel)


def make_mic_icon(path):
    """64x64 modern thin-stroke microphone icon matching search-thin.png."""
    def d(x, y):
        # Capsule outline: center x=32, y=15..33, radius=8.5
        cy = max(15.0, min(31.0, y))
        cap = abs(math.hypot(x - 32.0, y - cy) - 8.5)
        # Cradle U-arc: center (32, 31), radius=15, y >= 31, plus short vertical arms y=27..31 at x=17,47
        if y >= 31.0:
            arc = abs(math.hypot(x - 32.0, y - 31.0) - 15.0)
        else:
            arc = min(_dist_seg(x, y, 17.0, 27.0, 17.0, 31.0), _dist_seg(x, y, 47.0, 27.0, 47.0, 31.0))
        stem = _dist_seg(x, y, 32.0, 46.0, 32.0, 55.0)
        base = _dist_seg(x, y, 22.0, 55.0, 42.0, 55.0)
        return min(cap, arc, stem, base)
    _stroke_icon(path, 64, d, half_w=2.1)


def make_backspace_icon(path):
    """64x64 modern thin-stroke backspace icon."""
    def d(x, y):
        segs = [
            (22, 16, 54, 16), (54, 16, 54, 48), (54, 48, 22, 48),
            (22, 48, 10, 32), (10, 32, 22, 16),
            (31, 25, 45, 39), (45, 25, 31, 39),
        ]
        return min(_dist_seg(x, y, *s) for s in segs)
    _stroke_icon(path, 64, d, half_w=2.1)


def make_images():
    rounded(os.path.join(MEDIA, 'pill.png'), 48, 48, 24)                 # 9-slice with border="23"
    rounded(os.path.join(MEDIA, 'round.png'), 32, 32, 10)                # 9-slice with border="14": frames and badges
    rounded(os.path.join(MEDIA, 'key_bg.png'), 48, 48, 12)               # 9-slice with border="12": modern keyboard keys
    make_mic_icon(os.path.join(MEDIA, 'mic-thin.png'))
    make_backspace_icon(os.path.join(MEDIA, 'backspace-thin.png'))
    rounded(os.path.join(MEDIA, 'mask_poster.png'), 200, 300, 12)        # diffuse masks round the artwork's corners
    rounded(os.path.join(MEDIA, 'mask_wide.png'), 356, 200, 12)
    rounded(os.path.join(MEDIA, 'mask_card.png'), 200, 200, 12)
    gradient(os.path.join(MEDIA, 'fade_left.png'), 512, 8, BG, lambda t: 1 - smooth(t))
    gradient(os.path.join(MEDIA, 'fade_bottom.png'), 8, 512, BG, lambda t: smooth(max(0.0, (t - 0.25) / 0.75)))
    gradient(os.path.join(MEDIA, 'top_fade.png'), 8, 256, (0, 0, 0), lambda t: 0.85 * (1 - smooth(t)))
    def bg_pixel(x, y):
        t = y / 255
        v = int(round(0x1a - (0x1a - 0x0e) * t))
        return (v, v, v, 255)
    png(os.path.join(MEDIA, 'bg.png'), 8, 256, bg_pixel)


# ---------- tiles ----------

def progress(left, top, width, height, extra=''):
    """A ten-step progress bar (Kodi's lists have no progress control that reads a list item property)."""
    step = width // 10
    out = ['<control type="image"><left>{l}</left><top>{t}</top><width>{w}</width><height>{h}</height><texture colordiffuse="A0404040" border="3">round.png</texture><visible>{x}!String.IsEmpty(ListItem.Property(progress))</visible></control>'.format(l=left, t=top, w=width, h=height, x=extra)]
    for i in range(10):
        out.append('<control type="image"><left>{l}</left><top>{t}</top><width>{w}</width><height>{h}</height><texture colordiffuse="FFE50914">white.png</texture><visible>{x}Integer.IsGreater(ListItem.Property(progress),{p})</visible></control>'.format(
            l=left + i * step, t=top, w=step, h=height, x=extra, p=max(0, i * 10 - 6)))
    return '\n'.join(out)


def poster_tile(rid, focused, offset=0):
    """A 200x300 poster (or a square card for channels and settings) at `offset` from the item's left."""
    card, nocard = '!String.IsEmpty(ListItem.Property(card))', 'String.IsEmpty(ListItem.Property(card))'
    o = offset
    parts = []
    if focused:
        parts.append('<control type="image"><left>{l}</left><top>-5</top><width>210</width><height>310</height><texture border="14">round.png</texture><visible>Control.HasFocus({r}) + {c}</visible></control>'.format(l=o - 5, r=rid, c=nocard))
        parts.append('<control type="image"><left>{l}</left><top>45</top><width>210</width><height>210</height><texture border="14">round.png</texture><visible>Control.HasFocus({r}) + {c}</visible></control>'.format(l=o - 5, r=rid, c=card))
    parts += [
        '<!-- poster tiles: movies and shows -->',
        '<control type="image"><left>{l}</left><top>0</top><width>200</width><height>300</height><texture colordiffuse="FF1E1E26" border="14">round.png</texture><visible>{c}</visible></control>'.format(l=o, c=nocard),
        '<control type="textbox"><left>{l}</left><top>90</top><width>180</width><height>120</height><font>font13</font><align>center</align><textcolor>FFDDDDDD</textcolor><label>$INFO[ListItem.Label]</label><visible>{c}</visible></control>'.format(l=o + 10, c=nocard),
        '<control type="image"><left>{l}</left><top>0</top><width>200</width><height>300</height><aspectratio align="center" aligny="top">scale</aspectratio><texture background="true" diffuse="mask_poster.png">$INFO[ListItem.Art(thumb)]</texture><visible>{c} + String.IsEmpty(ListItem.Property(wide))</visible></control>'.format(l=o, c=nocard),
        '<control type="image"><left>{l}</left><top>94</top><width>200</width><height>112</height><aspectratio>scale</aspectratio><texture background="true">$INFO[ListItem.Art(thumb)]</texture><visible>{c} + !String.IsEmpty(ListItem.Property(wide))</visible></control>'.format(l=o, c=nocard),
        '<control type="image"><left>{l}</left><top>8</top><width>96</width><height>28</height><texture colordiffuse="FFE50914" border="10">round.png</texture><visible>{c} + !String.IsEmpty(ListItem.Property(tag))</visible></control>'.format(l=o + 8, c=nocard),
        '<control type="label"><left>{l}</left><top>8</top><width>96</width><height>28</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Property(tag)][/B]</label><visible>{c} + !String.IsEmpty(ListItem.Property(tag))</visible></control>'.format(l=o + 8, c=nocard),
        '<control type="image"><left>{l}</left><top>262</top><width>84</width><height>28</height><texture colordiffuse="D0000000" border="14">round.png</texture><visible>{c} + !String.IsEmpty(ListItem.Property(rating))</visible></control>'.format(l=o + 8, c=nocard),
        '<control type="label"><left>{l}</left><top>262</top><width>84</width><height>28</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFF5C518</textcolor><label>[B]$INFO[ListItem.Property(rating)][/B]</label><visible>{c} + !String.IsEmpty(ListItem.Property(rating))</visible></control>'.format(l=o + 8, c=nocard),
        progress(o + 10, 286, 180, 6, nocard + ' + '),
        '<control type="image"><left>{l}</left><top>8</top><width>44</width><height>28</height><texture colordiffuse="E0141414" border="10">round.png</texture><visible>{c} + !String.IsEmpty(ListItem.Property(number))</visible></control>'.format(l=o + 148, c=nocard),
        '<control type="label"><left>{l}</left><top>8</top><width>44</width><height>28</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Property(number)][/B]</label><visible>{c} + !String.IsEmpty(ListItem.Property(number))</visible></control>'.format(l=o + 148, c=nocard),
        '<!-- square cards: channels, languages, settings -->',
        '<control type="image"><left>{l}</left><top>50</top><width>200</width><height>200</height><texture colordiffuse="FF1E1E28" border="14">round.png</texture><visible>{c}</visible></control>'.format(l=o, c=card),
        '<control type="image"><left>{l}</left><top>72</top><width>156</width><height>156</height><aspectratio>keep</aspectratio><texture background="true">$INFO[ListItem.Art(thumb)]</texture><visible>{c}</visible></control>'.format(l=o + 22, c=card),
        '<control type="textbox"><left>{l}</left><top>108</top><width>180</width><height>90</height><font>font13</font><align>center</align><textcolor>FFFFFFFF</textcolor><label>$INFO[ListItem.Label]</label><visible>{c} + String.IsEmpty(ListItem.Art(thumb))</visible></control>'.format(l=o + 10, c=card),
        '<control type="label"><left>{l}</left><top>258</top><width>200</width><height>34</height><font>{f}</font><align>center</align><textcolor>{tc}</textcolor><label>{lb}</label><visible>{c} + !String.IsEmpty(ListItem.Art(thumb))</visible></control>'.format(
            l=o, c=card, f='font13' if focused else 'font12', tc='FFFFFFFF' if focused else 'FFB8B8B8', lb='[B]$INFO[ListItem.Label][/B]' if focused else '$INFO[ListItem.Label]'),
    ]
    return '\n'.join(parts)


def wide_tile(rid, focused):
    parts = []
    if focused:
        parts.append('<control type="image"><left>-5</left><top>-5</top><width>366</width><height>210</height><texture border="14">round.png</texture><visible>Control.HasFocus({r})</visible></control>'.format(r=rid))
    parts += [
        '<!-- wide tiles: YouTube videos -->',
        '<control type="image"><left>0</left><top>0</top><width>356</width><height>200</height><texture colordiffuse="FF1E1E28" border="14">round.png</texture></control>',
        '<control type="image"><left>0</left><top>0</top><width>356</width><height>200</height><aspectratio>scale</aspectratio><texture background="true" diffuse="mask_wide.png">$INFO[ListItem.Art(thumb)]</texture></control>',
        progress(10, 186, 336, 6),
        '<control type="image"><left>282</left><top>160</top><width>66</width><height>26</height><texture colordiffuse="D0000000" border="10">round.png</texture><visible>!String.IsEmpty(ListItem.Property(length))</visible></control>',
        '<control type="label"><left>282</left><top>160</top><width>66</width><height>26</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>$INFO[ListItem.Property(length)]</label><visible>!String.IsEmpty(ListItem.Property(length))</visible></control>',
        '<control type="textbox"><left>10</left><top>58</top><width>336</width><height>90</height><font>font13</font><align>center</align><textcolor>FFFFFFFF</textcolor><label>$INFO[ListItem.Label]</label><visible>String.IsEmpty(ListItem.Art(thumb))</visible></control>',
        '<control type="label"><left>0</left><top>208</top><width>356</width><height>34</height><font>{f}</font><textcolor>{tc}</textcolor><label>{lb}</label></control>'.format(
            f='font13' if focused else 'font12', tc='FFFFFFFF' if focused else 'FFB8B8B8', lb='[B]$INFO[ListItem.Label][/B]' if focused else '$INFO[ListItem.Label]'),
    ]
    return '\n'.join(parts)


def zoom(rid):
    """The highlighted tile grows a little (and shrinks back) as the highlight moves: the Netflix focus feel."""
    return '<animation effect="zoom" start="100" end="108" time="160" center="auto" reversible="true" condition="Control.HasFocus({r})">Conditional</animation>'.format(r=rid)


def layouts(n, rid):
    wide, top10 = 'Window.Property(row{0}.wide)'.format(n), 'Window.Property(row{0}.top10)'.format(n)
    poster_if = 'String.IsEmpty({0}) + String.IsEmpty({1})'.format(wide, top10)
    return '''
                    <itemlayout width="372" height="330" condition="!String.IsEmpty({wide})">
                        <control type="group"><left>10</left><top>14</top>
{wide_item}
                        </control>
                    </itemlayout>
                    <focusedlayout width="372" height="330" condition="!String.IsEmpty({wide})">
                        <control type="group"><left>10</left><top>14</top>
                            {zoom}
{wide_focus}
                        </control>
                    </focusedlayout>
                    <itemlayout width="216" height="330" condition="{poster_if}">
                        <control type="group"><left>10</left><top>14</top>
{poster_item}
                        </control>
                    </itemlayout>
                    <focusedlayout width="216" height="330" condition="{poster_if}">
                        <control type="group"><left>10</left><top>14</top>
                            {zoom}
{poster_focus}
                        </control>
                    </focusedlayout>
                    <!-- Top 10: a big numeral beside each poster -->
                    <itemlayout width="306" height="330" condition="!String.IsEmpty({top10})">
                        <control type="group"><left>10</left><top>14</top>
                            <control type="label"><left>0</left><top>120</top><width>110</width><height>80</height><font>font60</font><align>right</align><aligny>center</aligny><textcolor>FF5A5A5A</textcolor><label>[B]$INFO[ListItem.Property(rank)][/B]</label><animation effect="zoom" start="100" end="240" center="110,160" time="0" condition="true">Conditional</animation></control>
{top10_item}
                        </control>
                    </itemlayout>
                    <focusedlayout width="306" height="330" condition="!String.IsEmpty({top10})">
                        <control type="group"><left>10</left><top>14</top>
                            {zoom}
                            <control type="label"><left>0</left><top>120</top><width>110</width><height>80</height><font>font60</font><align>right</align><aligny>center</aligny><textcolor>FF8A8A8A</textcolor><label>[B]$INFO[ListItem.Property(rank)][/B]</label><animation effect="zoom" start="100" end="240" center="110,160" time="0" condition="true">Conditional</animation></control>
{top10_focus}
                        </control>
                    </focusedlayout>'''.format(
        wide=wide, top10=top10, poster_if=poster_if, zoom=zoom(rid),
        wide_item=indent(wide_tile(rid, False), 28), wide_focus=indent(wide_tile(rid, True), 28),
        poster_item=indent(poster_tile(rid, False), 28), poster_focus=indent(poster_tile(rid, True), 28),
        top10_item=indent(poster_tile(rid, False, 90), 28), top10_focus=indent(poster_tile(rid, True, 90), 28))


def scaled(text):
    """Tile markup grown by SCALE for the touch layouts: every position and size, layout box and zoom centre."""
    if not TOUCH['on']:
        return text
    grow = lambda m: str(int(round(int(m) * SCALE)))
    text = re.sub(r'<(left|top|width|height)>(-?\d+)</\1>', lambda m: '<{0}>{1}</{0}>'.format(m.group(1), grow(m.group(2))), text)
    text = re.sub(r'(layout width=")(\d+)(" height=")(\d+)"', lambda m: m.group(1) + grow(m.group(2)) + m.group(3) + grow(m.group(4)) + '"', text)
    return re.sub(r'center="(\d+),(\d+)"', lambda m: 'center="{0},{1}"'.format(grow(m.group(1)), grow(m.group(2))), text)


def indent(text, n):
    pad = ' ' * n
    return '\n'.join(pad + line for line in text.split('\n'))


def row(n):
    rid = FIRST + n
    bid = BROWSE_FIRST + n
    down = rid + 1 if n < ROWS - 1 else 'noop'
    tiles = int(round(330 * SCALE)) if TOUCH['on'] else 330
    if n == 0:  # Up from the first row reaches the genre strip when it is shown, otherwise the tabs
        ups = '<onup condition="Control.IsVisible({flt})">{flt}</onup>\n                    <onup>{tabs}</onup>'.format(flt=FILTER, tabs=TABS)
    else:
        ups = '<onup>{0}</onup>'.format(rid - 1)
    has_browse = '!String.IsEmpty(Window.Property(row{n}.browse))'.format(n=n)
    return '''            <control type="group" id="{gid}">
                <height>{group}</height>
                <visible>!String.IsEmpty(Window.Property(row{n}.title))</visible>
                <control type="label"><left>60</left><top>6</top><width>1560</width><height>40</height><font>font30_title</font><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[Window.Property(row{n}.title)][/B]</label></control>
                <!-- Header Browse all pill: press Left from tile 0 on remote, or tap directly on touch -->
                <control type="image"><left>1660</left><top>4</top><width>200</width><height>38</height><texture colordiffuse="2EFFFFFF" border="19">pill.png</texture><visible>{has_browse} + !Control.HasFocus({bid})</visible></control>
                <control type="image"><left>1660</left><top>4</top><width>200</width><height>38</height><texture colordiffuse="FFFFFFFF" border="19">pill.png</texture><visible>{has_browse} + Control.HasFocus({bid})</visible></control>
                <control type="button" id="{bid}">
                    <left>1660</left><top>4</top><width>200</width><height>38</height>
                    <texturefocus>-</texturefocus>
                    <texturenofocus>-</texturenofocus>
                    <font>font10</font>
                    <align>center</align>
                    <aligny>center</aligny>
                    <textcolor>FFD6D6D6</textcolor>
                    <focusedcolor>FF141414</focusedcolor>
                    <label>[B]$INFO[Window.Property(row{n}.browse)][/B]</label>
                    <visible>{has_browse}</visible>
                    {ups}
                    <onleft>noop</onleft>
                    <onright>{rid}</onright>
                    <ondown>{down}</ondown>
                </control>
                <control type="list" id="{rid}">
                    {ups}
                    <onleft condition="{has_browse}">{bid}</onleft>
                    <onleft>noop</onleft>
                    <ondown>{down}</ondown>
                    <left>50</left><top>48</top><width>1870</width><height>{tiles}</height>
                    <orientation>horizontal</orientation>
                    <scrolltime tween="quadratic">200</scrolltime>{layouts}
                </control>
            </control>
'''.format(gid=300 + n, n=n, rid=rid, bid=bid, has_browse=has_browse, ups=ups, down=down,
           layouts=scaled(layouts(n, rid)), tiles=tiles, group=84 + tiles)


# ---------- billboard ----------

def billboard(n):
    rid = FIRST + n
    bid = BROWSE_FIRST + n
    c = 'Container({0}).ListItem'.format(rid)
    shown = 'Control.HasFocus({r}) | Control.HasFocus({b})'.format(r=rid, b=bid)
    if n == 0:
        shown += ' | [[{0}] + String.IsEmpty(Window.Property(status))]'.format(TOP_BAR)
    has_art, no_art = '!String.IsEmpty({0}.Art(fanart))'.format(c), 'String.IsEmpty({0}.Art(fanart))'.format(c)
    return '''        <control type="group">
            {slide}
            <visible>{shown}</visible>
            <animation effect="fade" start="0" end="100" time="300">Visible</animation>
            <!-- the title's backdrop fills the right of the screen; without one, its poster stands in -->
            <control type="image"><left>640</left><top>0</top><width>1280</width><height>720</height><aspectratio>scale</aspectratio><texture background="true">$INFO[{c}.Art(fanart)]</texture><fadetime>400</fadetime><visible>String.IsEmpty(Window.Property(kids)) + {has_art}</visible></control>
            <control type="image"><left>820</left><top>0</top><width>1100</width><height>620</height><aspectratio>scale</aspectratio><texture background="true" colordiffuse="60FFFFFF">$INFO[{c}.Art(thumb)]</texture><fadetime>400</fadetime><visible>String.IsEmpty(Window.Property(kids)) + {no_art}</visible></control>
            <control type="image"><left>640</left><top>0</top><width>700</width><height>720</height><texture>fade_left.png</texture><visible>String.IsEmpty(Window.Property(kids))</visible></control>
            <control type="image"><left>640</left><top>300</top><width>1280</width><height>420</height><texture>fade_bottom.png</texture><visible>String.IsEmpty(Window.Property(kids))</visible></control>
            <control type="image"><left>1540</left><top>92</top><width>200</width><height>290</height><aspectratio>keep</aspectratio><texture background="true" diffuse="mask_poster.png">$INFO[{c}.Art(thumb)]</texture><visible>{no_art} | !String.IsEmpty(Window.Property(kids))</visible></control>
            <control type="label"><left>60</left><top>124</top><width>1100</width><height>66</height><font>font52_title</font><textcolor>FFFFFFFF</textcolor><scroll>true</scroll><label>[B]$INFO[{c}.Label][/B]</label></control>
            <control type="label"><left>60</left><top>198</top><width>1100</width><height>34</height><font>font13</font><textcolor>FF46D369</textcolor><label>$INFO[{c}.Property(meta)]</label></control>
            <!-- a three-line synopsis: the title's own page has the rest, with the cast and episodes -->
            <control type="textbox"><left>60</left><top>238</top><width>880</width><height>90</height><font>font12</font><textcolor>FFE5E5E5</textcolor><label>$INFO[{c}.Property(plot)]</label></control>
        </control>
'''.format(slide=SLIDE, shown=shown, c=c, has_art=has_art, no_art=no_art)


def filter_strip():
    f = FILTER
    chosen, other = 'String.IsEqual(Window.Property(filter),ListItem.Property(genre))', '!String.IsEqual(Window.Property(filter),ListItem.Property(genre))'
    return '''        <!-- Genre strip under the top bar on Movies and Shows: OK on a chip filters the rows, All clears it -->
        <control type="list" id="{f}">
            <left>50</left><top>92</top><width>1870</width><height>40</height>
            <orientation>horizontal</orientation>
            <scrolltime tween="quadratic">150</scrolltime>
            <onup>{tabs}</onup>
            <ondown>{first}</ondown>
            <onleft>noop</onleft>
            <onright>noop</onright>
            <visible>!String.IsEmpty(Window.Property(filters))</visible>
            <itemlayout width="186" height="40">
                <control type="image"><left>10</left><top>3</top><width>172</width><height>34</height><texture colordiffuse="2EFFFFFF" border="17">pill.png</texture><visible>{other}</visible></control>
                <control type="image"><left>10</left><top>3</top><width>172</width><height>34</height><texture colordiffuse="80FFFFFF" border="17">pill.png</texture><visible>{chosen}</visible></control>
                <control type="label"><left>10</left><top>3</top><width>172</width><height>34</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFE0E0E0</textcolor><label>$INFO[ListItem.Label]</label></control>
            </itemlayout>
            <focusedlayout width="186" height="40">
                <control type="image"><left>10</left><top>3</top><width>172</width><height>34</height><texture colordiffuse="2EFFFFFF" border="17">pill.png</texture><visible>!Control.HasFocus({f}) + {other}</visible></control>
                <control type="image"><left>10</left><top>3</top><width>172</width><height>34</height><texture colordiffuse="80FFFFFF" border="17">pill.png</texture><visible>!Control.HasFocus({f}) + {chosen}</visible></control>
                <control type="image"><left>10</left><top>3</top><width>172</width><height>34</height><texture colordiffuse="FFFFFFFF" border="17">pill.png</texture><visible>Control.HasFocus({f})</visible></control>
                <control type="label"><left>10</left><top>3</top><width>172</width><height>34</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFE0E0E0</textcolor><label>$INFO[ListItem.Label]</label><visible>!Control.HasFocus({f})</visible></control>
                <control type="label"><left>10</left><top>3</top><width>172</width><height>34</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FF141414</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>Control.HasFocus({f})</visible></control>
            </focusedlayout>
        </control>
'''.format(f=f, tabs=TABS, first=FIRST, chosen=chosen, other=other)


def top_bar():
    # Touch layouts make room for seven roomy tabs and 60px icons a finger can hit: search becomes an icon.
    if TOUCH['on']:
        bar = dict(tabs_w=1290, tab_w=180, pill_w=168, search_l=1636, search_w=60, search_i=1650, search_label='', search_t=22, search_h=60, search_it=36, profile_l=1706,
                   gear_l=1776, close_l=1846, icon=60, icon_t=22)
    else:
        bar = dict(tabs_w=1140, tab_w=160, pill_w=148, search_l=1490, search_w=240, search_i=1506, search_label='Search', search_t=28, search_h=48, search_it=36, profile_l=1746,
                   gear_l=1800, close_l=1856, icon=44, icon_t=30)
    downs = '<ondown condition="Control.IsVisible({flt})">{flt}</ondown>\n            <ondown>{first}</ondown>'.format(flt=FILTER, first=FIRST)
    return '''        <!-- top bar: a dark fade keeps it readable over the billboard -->
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>150</height><texture>top_fade.png</texture></control>
        <control type="image"><left>56</left><top>22</top><width>236</width><height>58</height><aspectratio>keep</aspectratio><texture>logo.png</texture><visible>String.IsEmpty(Window.Property(kids))</visible></control>
        <control type="label"><left>60</left><top>26</top><width>230</width><height>50</height><font>font36_title</font><textcolor>FFFFD640</textcolor><label>[B]KIDS[/B]</label><visible>!String.IsEmpty(Window.Property(kids))</visible></control>
        <control type="list" id="{tabs}">
            <left>330</left><top>24</top><width>{tabs_w}</width><height>56</height>
            <orientation>horizontal</orientation>
            {downs}
            <onright>{search}</onright>
            <onleft>noop</onleft>
            <onup>noop</onup>
            <!-- seven tabs with generous breathing room; the highlighted tab sits in a white pill, the open tab is bold white -->
            <itemlayout width="{tab_w}" height="56" condition="String.IsEmpty(Window.Property(kids))">
                <control type="label"><left>0</left><top>0</top><width>{pill_w}</width><height>56</height><font>font13</font><align>center</align><aligny>center</aligny><textcolor>FFB3B3B3</textcolor><label>$INFO[ListItem.Label]</label></control>
            </itemlayout>
            <focusedlayout width="{tab_w}" height="56" condition="String.IsEmpty(Window.Property(kids))">
                <control type="image"><left>0</left><top>6</top><width>{pill_w}</width><height>44</height><texture colordiffuse="FFFFFFFF" border="22">pill.png</texture><visible>Control.HasFocus({tabs})</visible><animation effect="zoom" start="90" end="100" time="120" center="auto">Visible</animation></control>
                <control type="label"><left>0</left><top>0</top><width>{pill_w}</width><height>56</height><font>font13</font><align>center</align><aligny>center</aligny><textcolor>FF141414</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>Control.HasFocus({tabs})</visible></control>
                <control type="label"><left>0</left><top>0</top><width>{pill_w}</width><height>56</height><font>font13</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>!Control.HasFocus({tabs})</visible></control>
            </focusedlayout>
            <itemlayout width="230" height="56" condition="!String.IsEmpty(Window.Property(kids))">
                <control type="label"><left>0</left><top>0</top><width>290</width><height>56</height><font>font13</font><aligny>center</aligny><textcolor>FFD6D6D6</textcolor><label>$INFO[ListItem.Label]</label></control>
            </itemlayout>
            <focusedlayout width="230" height="56" condition="!String.IsEmpty(Window.Property(kids))">
                <control type="image"><left>0</left><top>48</top><width>130</width><height>6</height><texture colordiffuse="FFFFD640" border="3">round.png</texture></control>
                <control type="label"><left>0</left><top>0</top><width>290</width><height>56</height><font>font13</font><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Label][/B]</label></control>
            </focusedlayout>
        </control>
        <control type="image"><left>{search_l}</left><top>{search_t}</top><width>{search_w}</width><height>{search_h}</height><texture colordiffuse="33FFFFFF" border="23">pill.png</texture><visible>!Control.HasFocus({search})</visible></control>
        <control type="image"><left>{search_l}</left><top>{search_t}</top><width>{search_w}</width><height>{search_h}</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture><visible>Control.HasFocus({search})</visible></control>
        <control type="image"><left>{search_i}</left><top>{search_it}</top><width>32</width><height>32</height><texture colordiffuse="FFD6D6D6">search-thin.png</texture><visible>!Control.HasFocus({search})</visible></control>
        <control type="image"><left>{search_i}</left><top>{search_it}</top><width>32</width><height>32</height><texture colordiffuse="FF141414">search-thin.png</texture><visible>Control.HasFocus({search})</visible></control>
        <control type="button" id="{search}">
            <left>{search_l}</left><top>{search_t}</top><width>{search_w}</width><height>{search_h}</height>
            <texturefocus>-</texturefocus>
            <texturenofocus>-</texturenofocus>
            <label>{search_label}</label>
            <font>font13</font>
            <textoffsetx>58</textoffsetx>
            <aligny>center</aligny>
            <textcolor>FFB3B3B3</textcolor>
            <focusedcolor>FF141414</focusedcolor>
            <onleft>{tabs}</onleft>
            <onright>{profile}</onright>
            {downs}
            <onup>noop</onup>
        </control>
        <control type="button" id="{profile}">
            <left>{profile_l}</left><top>{icon_t}</top><width>{icon}</width><height>{icon}</height>
            <texturefocus colordiffuse="FFE50914">person-thin.png</texturefocus>
            <texturenofocus colordiffuse="CCFFFFFF">person-thin.png</texturenofocus>
            <label></label>
            <onleft>{search}</onleft>
            <onright>{gear}</onright>
            <onup>noop</onup>
            {downs}
        </control>
        <control type="button" id="{gear}">
            <left>{gear_l}</left><top>{icon_t}</top><width>{icon}</width><height>{icon}</height>
            <texturefocus colordiffuse="FFE50914">gear-thin.png</texturefocus>
            <texturenofocus colordiffuse="CCFFFFFF">gear-thin.png</texturenofocus>
            <label></label>
            <onleft>{profile}</onleft>
            <onright>{close}</onright>
            <onup>noop</onup>
            {downs}
        </control>
        <control type="button" id="{close}">
            <left>{close_l}</left><top>{icon_t}</top><width>{icon}</width><height>{icon}</height>
            <texturefocus colordiffuse="FFE50914">close-thin.png</texturefocus>
            <texturenofocus colordiffuse="CCFFFFFF">close-thin.png</texturenofocus>
            <label></label>
            <onleft>{gear}</onleft>
            <onright>noop</onright>
            <onup>noop</onup>
            {downs}
        </control>
'''.format(tabs=TABS, downs=downs, search=SEARCH, profile=PROFILE, gear=GEAR, close=CLOSE, **bar)


def home_xml():
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<!-- Generated by tools/make_skin.py; edit that file, not this one. -->',
           '<window>',
           '    <defaultcontrol always="false">{0}</defaultcontrol>'.format(TABS),
           '    <controls>',
           '        <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height><texture>bg.png</texture></control>',
           '        <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height><texture>bg_kids.png</texture><visible>!String.IsEmpty(Window.Property(kids))</visible></control>',
           '        <control type="label"><left>60</left><top>240</top><width>1380</width><height>60</height><font>font13</font><textcolor>FFA3A3A3</textcolor>{0}<label>$INFO[Window.Property(status)]</label><visible>{1}</visible></control>'.format(SLIDE, TOP_BAR)]
    out += [billboard(n).rstrip('\n') for n in range(ROWS)]
    out.append(filter_strip().rstrip('\n'))
    # Rows start at 400 and run to the bottom of the 1080p frame so the active row sits prominently and the next
    # row's heading and poster tops peek in below, making the home screen scannable without wasted vertical gap.
    top, height = (430, 650) if TOUCH['on'] else (400, 680)
    out.append('''        <control type="grouplist" id="9000">
            <left>0</left><top>{top}</top><width>1920</width><height>{height}</height>
            {slide}
            <orientation>vertical</orientation>
            <itemgap>0</itemgap>
            <scrolltime tween="quadratic">250</scrolltime>
            <onup>{tabs}</onup>
            <onleft>noop</onleft>'''.format(slide=SLIDE, tabs=TABS, top=top, height=height))
    out += [row(n).rstrip('\n') for n in range(ROWS)]
    out.append('        </control>')
    out.append(top_bar().rstrip('\n'))
    out += ['    </controls>', '</window>', '']
    return '\n'.join(out)


# ---------- grid window ----------

def grid_xml():
    def poster(focused):
        parts = []
        if focused:
            parts.append('<control type="image"><left>-5</left><top>-5</top><width>210</width><height>310</height><texture border="14">round.png</texture><visible>Control.HasFocus(50)</visible></control>')
        parts += [
            '<control type="image"><left>0</left><top>0</top><width>200</width><height>300</height><texture colordiffuse="FF1E1E26" border="14">round.png</texture></control>',
            '<control type="textbox"><left>10</left><top>90</top><width>180</width><height>120</height><font>font13</font><align>center</align><textcolor>FFDDDDDD</textcolor><label>$INFO[ListItem.Label]</label><visible>String.IsEmpty(ListItem.Art(thumb))</visible></control>',
            '<control type="image"><left>0</left><top>0</top><width>200</width><height>300</height><aspectratio>scale</aspectratio><texture background="true" diffuse="mask_poster.png">$INFO[ListItem.Art(thumb)]</texture><visible>String.IsEmpty(ListItem.Property(logo)) + String.IsEmpty(ListItem.Property(wide))</visible></control>',
            '<control type="image"><left>0</left><top>94</top><width>200</width><height>112</height><aspectratio>scale</aspectratio><texture background="true">$INFO[ListItem.Art(thumb)]</texture><visible>!String.IsEmpty(ListItem.Property(wide))</visible></control>',
            '<control type="image"><left>16</left><top>50</top><width>168</width><height>200</height><aspectratio>keep</aspectratio><texture background="true">$INFO[ListItem.Art(thumb)]</texture><visible>!String.IsEmpty(ListItem.Property(logo))</visible></control>',
            '<control type="image"><left>8</left><top>8</top><width>96</width><height>28</height><texture colordiffuse="FFE50914" border="10">round.png</texture><visible>!String.IsEmpty(ListItem.Property(tag))</visible></control>',
            '<control type="label"><left>8</left><top>8</top><width>96</width><height>28</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Property(tag)][/B]</label><visible>!String.IsEmpty(ListItem.Property(tag))</visible></control>',
            '<control type="image"><left>8</left><top>262</top><width>84</width><height>28</height><texture colordiffuse="D0000000" border="14">round.png</texture><visible>!String.IsEmpty(ListItem.Property(rating))</visible></control>',
            '<control type="label"><left>8</left><top>262</top><width>84</width><height>28</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFF5C518</textcolor><label>[B]$INFO[ListItem.Property(rating)][/B]</label><visible>!String.IsEmpty(ListItem.Property(rating))</visible></control>',
            progress(10, 286, 180, 6),
            '<control type="label"><left>0</left><top>306</top><width>200</width><height>34</height><font>{f}</font><textcolor>{tc}</textcolor><label>{lb}</label><visible>!String.IsEmpty(ListItem.Property(logo)) | !String.IsEmpty(ListItem.Property(series))</visible></control>'.format(
                f='font13' if focused else 'font12', tc='FFFFFFFF' if focused else 'FFB8B8B8', lb='[B]$INFO[ListItem.Label][/B]' if focused else '$INFO[ListItem.Label]'),
        ]
        return indent('\n'.join(parts), 24)

    def wide(focused):
        parts = []
        if focused:
            parts.append('<control type="image"><left>-5</left><top>-5</top><width>360</width><height>207</height><texture border="14">round.png</texture><visible>Control.HasFocus(50)</visible></control>')
        parts += [
            '<control type="image"><left>0</left><top>0</top><width>350</width><height>197</height><texture colordiffuse="FF1E1E28" border="14">round.png</texture></control>',
            '<control type="image"><left>0</left><top>0</top><width>350</width><height>197</height><aspectratio>scale</aspectratio><texture background="true" diffuse="mask_wide.png">$INFO[ListItem.Art(thumb)]</texture></control>',
            progress(10, 183, 330, 6),
            '<control type="image"><left>276</left><top>157</top><width>66</width><height>26</height><texture colordiffuse="D0000000" border="10">round.png</texture><visible>!String.IsEmpty(ListItem.Property(length))</visible></control>',
            '<control type="label"><left>276</left><top>157</top><width>66</width><height>26</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>$INFO[ListItem.Property(length)]</label><visible>!String.IsEmpty(ListItem.Property(length))</visible></control>',
            '<control type="textbox"><left>10</left><top>60</top><width>330</width><height>90</height><font>font13</font><align>center</align><textcolor>FFFFFFFF</textcolor><label>$INFO[ListItem.Label]</label><visible>String.IsEmpty(ListItem.Art(thumb))</visible></control>',
            '<control type="label"><left>0</left><top>204</top><width>350</width><height>34</height><font>{f}</font><textcolor>{tc}</textcolor><label>{lb}</label></control>'.format(
                f='font13' if focused else 'font12', tc='FFFFFFFF' if focused else 'FFB8B8B8', lb='[B]$INFO[ListItem.Label][/B]' if focused else '$INFO[ListItem.Label]'),
        ]
        return indent('\n'.join(parts), 24)

    z = '<animation effect="zoom" start="100" end="106" time="160" center="auto" reversible="true" condition="Control.HasFocus(50)">Conditional</animation>'
    out = '''<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by tools/make_skin.py; edit that file, not this one. -->
<window>
    <defaultcontrol always="true">50</defaultcontrol>
    <controls>
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height><texture>bg.png</texture></control>
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height><texture>bg_kids.png</texture><visible>!String.IsEmpty(Window.Property(kids))</visible></control>
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>180</height><texture>top_fade.png</texture></control>
        <control type="label"><left>60</left><top>30</top><width>1100</width><height>60</height><font>font36_title</font><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[Window.Property(title)][/B]</label></control>
        <control type="label"><left>1180</left><top>30</top><width>360</width><height>60</height><font>font13</font><align>right</align><aligny>center</aligny><textcolor>FFA3A3A3</textcolor><label>$INFO[Window.Property(status)]</label></control>
        <!-- the highlighted title's facts and synopsis -->
        <control type="textbox"><left>60</left><top>92</top><width>1800</width><height>70</height><font>font12</font><textcolor>FFB3B3B3</textcolor><label>[COLOR FF46D369]$INFO[Container(50).ListItem.Property(meta)][/COLOR]  $INFO[Container(50).ListItem.Property(plot)]</label><visible>String.IsEmpty(Window.Property(mode))</visible></control>
        <control type="panel" id="50">
            <visible>String.IsEmpty(Window.Property(mode))</visible>
            <onleft>noop</onleft>
            <onright>noop</onright>
            <onup>noop</onup>
            <ondown>noop</ondown>
            <left>60</left><top>180</top><width>1810</width><height>900</height>
            <orientation>vertical</orientation>
            <scrolltime tween="quadratic">200</scrolltime>
            <!-- wide tiles: a listing of videos (Window.Property(wide) is set by the script) -->
            <itemlayout width="362" height="260" condition="!String.IsEmpty(Window.Property(wide))">
                <control type="group"><left>6</left><top>6</top>
{wide_item}
                </control>
            </itemlayout>
            <focusedlayout width="362" height="260" condition="!String.IsEmpty(Window.Property(wide))">
                <control type="group"><left>6</left><top>6</top>
                    {z}
{wide_focus}
                </control>
            </focusedlayout>
            <itemlayout width="224" height="330" condition="String.IsEmpty(Window.Property(wide))">
                <control type="group"><left>6</left><top>6</top>
{poster_item}
                </control>
            </itemlayout>
            <focusedlayout width="224" height="330" condition="String.IsEmpty(Window.Property(wide))">
                <control type="group"><left>6</left><top>6</top>
                    {z}
{poster_focus}
                </control>
            </focusedlayout>
        </control>
        <control type="list" id="51">
            <visible>!String.IsEmpty(Window.Property(mode))</visible>
            <left>60</left><top>120</top><width>1800</width><height>936</height>
            <orientation>vertical</orientation>
            <scrolltime tween="quadratic">160</scrolltime>
            <onleft>noop</onleft><onright>noop</onright><onup>noop</onup><ondown>noop</ondown>
            <itemlayout width="1800" height="72">
                <control type="image"><left>0</left><top>3</top><width>1800</width><height>66</height><texture colordiffuse="33000000" border="14">round.png</texture></control>
                <control type="label"><left>30</left><top>3</top><width>1150</width><height>66</height><font>font13</font><aligny>center</aligny><textcolor>FFE0E0E0</textcolor><label>$INFO[ListItem.Label]</label></control>
                <control type="label"><left>1190</left><top>3</top><width>580</width><height>66</height><font>font12</font><align>right</align><aligny>center</aligny><textcolor>FF46D369</textcolor><label>$INFO[ListItem.Property(meta)]</label></control>
            </itemlayout>
            <focusedlayout width="1800" height="72">
                <control type="image"><left>0</left><top>3</top><width>1800</width><height>66</height><texture colordiffuse="40FFFFFF" border="14">round.png</texture></control>
                <control type="image"><left>0</left><top>3</top><width>8</width><height>66</height><texture colordiffuse="FFE50914" border="3">round.png</texture></control>
                <control type="label"><left>30</left><top>3</top><width>1150</width><height>66</height><font>font13</font><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Label][/B]</label></control>
                <control type="label"><left>1190</left><top>3</top><width>580</width><height>66</height><font>font12</font><align>right</align><aligny>center</aligny><textcolor>FF46D369</textcolor><label>$INFO[ListItem.Property(meta)]</label></control>
            </focusedlayout>
        </control>
    </controls>
</window>
'''.format(z=z, wide_item=wide(False), wide_focus=wide(True), poster_item=poster(False), poster_focus=poster(True))
    if not TOUCH['on']:
        return out
    # Touch: bigger tiles, taller episode rows, and a Back button (a phone has no remote) with the title moved right.
    start = out.index('            <!-- wide tiles')
    end = out.index('        <control type="list" id="51">')
    out = out[:start] + scaled(out[start:end]) + out[end:].replace('height="72"', 'height="96"').replace('<height>66</height>', '<height>90</height>')
    back = '''        <control type="image"><left>40</left><top>31</top><width>150</width><height>48</height><texture colordiffuse="4DFFFFFF" border="23">pill.png</texture></control>
        <control type="button" id="{b}">
            <left>40</left><top>31</top><width>150</width><height>48</height>
            <texturefocus>-</texturefocus>
            <texturenofocus>-</texturenofocus>
            <label>‹  Back</label>
            <font>font13</font>
            <align>center</align>
            <aligny>center</aligny>
            <textcolor>FFFFFFFF</textcolor>
            <focusedcolor>FFFFFFFF</focusedcolor>
            <onleft>noop</onleft><onright>noop</onright><onup>noop</onup><ondown>50</ondown>
        </control>
'''.format(b=BACK)
    title = '<control type="label"><left>60</left><top>30</top><width>1100</width>'
    return out.replace(title, title.replace('<left>60</left>', '<left>210</left>').replace('1100', '950'), 1).replace(
        '        <!-- the highlighted title', back + '        <!-- the highlighted title', 1)


# ---------- title page ----------

PLAY_B, TRAILER_B, LIST_B, LIKE_B, DISLIKE_B = 301, 302, 303, 304, 305
SEASONS, EPISODES, ALIKE, LANGS = 310, 320, 330, 340


def pill_button(cid, left, width, label, nav):
    """A pill button: translucent when idle, white with dark text when highlighted."""
    return '''        <control type="image"><left>{l}</left><top>430</top><width>{w}</width><height>56</height><texture colordiffuse="4DFFFFFF" border="23">pill.png</texture><visible>!Control.HasFocus({c})</visible></control>
        <control type="image"><left>{l}</left><top>430</top><width>{w}</width><height>56</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture><visible>Control.HasFocus({c})</visible></control>
        <control type="button" id="{c}">
            <left>{l}</left><top>430</top><width>{w}</width><height>56</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <label>{lb}</label>
            <font>font13</font><align>center</align><aligny>center</aligny>
            <textcolor>FFFFFFFF</textcolor><focusedcolor>FF141414</focusedcolor>
            {nav}
        </control>
'''.format(l=left, w=width, c=cid, lb=label, nav=nav)


def episode_tile(focused):
    """A wide still with the episode number in a corner, its length, a watched tick and the progress bar; the
    episode's name sits under the still."""
    parts = []
    if focused:
        parts.append('<control type="image"><left>-5</left><top>-5</top><width>366</width><height>210</height><texture border="14">round.png</texture><visible>Control.HasFocus({r})</visible></control>'.format(r=EPISODES))
    parts += [
        '<control type="image"><left>0</left><top>0</top><width>356</width><height>200</height><texture colordiffuse="FF1E1E28" border="14">round.png</texture></control>',
        '<control type="image"><left>0</left><top>0</top><width>356</width><height>200</height><aspectratio>scale</aspectratio><texture background="true" diffuse="mask_wide.png">$INFO[ListItem.Art(thumb)]</texture></control>',
        '<control type="image"><left>0</left><top>0</top><width>356</width><height>200</height><texture colordiffuse="60000000" border="14">round.png</texture><visible>!String.IsEmpty(ListItem.Property(done))</visible></control>',
        '<control type="image"><left>8</left><top>8</top><width>52</width><height>30</height><texture colordiffuse="D0000000" border="10">round.png</texture></control>',
        '<control type="label"><left>8</left><top>8</top><width>52</width><height>30</height><font>font12</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Property(number)][/B]</label></control>',
        '<control type="label"><left>0</left><top>70</top><width>356</width><height>60</height><font>font36_title</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>✓</label><visible>!String.IsEmpty(ListItem.Property(done))</visible></control>',
        progress(10, 186, 336, 6),
        '<control type="image"><left>282</left><top>160</top><width>66</width><height>26</height><texture colordiffuse="D0000000" border="10">round.png</texture><visible>!String.IsEmpty(ListItem.Property(length))</visible></control>',
        '<control type="label"><left>282</left><top>160</top><width>66</width><height>26</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>$INFO[ListItem.Property(length)]</label><visible>!String.IsEmpty(ListItem.Property(length))</visible></control>',
        '<control type="label"><left>0</left><top>208</top><width>356</width><height>34</height><font>{f}</font><textcolor>{tc}</textcolor><label>{lb}</label></control>'.format(
            f='font13' if focused else 'font12', tc='FFFFFFFF' if focused else 'FFB8B8B8', lb='[B]$INFO[ListItem.Label][/B]' if focused else '$INFO[ListItem.Label]'),
    ]
    return '\n'.join(parts)


def title_xml():
    """A title's own page: backdrop, facts, synopsis and cast, a row of pill buttons (Play, Trailer, My List, Like,
    Dislike), language chips when the film is listed in more than one language, then a show's seasons and episodes
    or a movie's More like this row."""
    is_show, not_show = '!String.IsEmpty(Window.Property(show))', 'String.IsEmpty(Window.Property(show))'
    buttons = [
        (PLAY_B, 60, 260, '$INFO[Window.Property(play_label)]'),
        (TRAILER_B, 336, 200, 'Trailer'),
        (LIST_B, 552, 220, '$INFO[Window.Property(list_label)]'),
        (LIKE_B, 788, 160, '$INFO[Window.Property(like_label)]'),
        (DISLIKE_B, 964, 180, '$INFO[Window.Property(dislike_label)]'),
    ]
    rows = []
    for n, (cid, left, width, label) in enumerate(buttons):
        prev = buttons[n - 1][0] if n else cid
        nxt = buttons[n + 1][0] if n < len(buttons) - 1 else cid
        nav = ('<onleft>{p}</onleft><onright>{x}</onright><onup>{b}</onup>'
               '<ondown condition="!String.IsEmpty(Window.Property(langs))">{langs}</ondown>'
               '<ondown condition="{s}">{seasons}</ondown><ondown>{alike}</ondown>').format(
            p=prev, x=nxt, b=BACK if TOUCH['on'] else cid, s=is_show, seasons=SEASONS, alike=ALIKE, langs=LANGS)
        rows.append(pill_button(cid, left, width, label, nav))
    back = ''
    if TOUCH['on']:
        back = '''        <control type="image"><left>40</left><top>31</top><width>150</width><height>48</height><texture colordiffuse="4DFFFFFF" border="23">pill.png</texture></control>
        <control type="button" id="{b}">
            <left>40</left><top>31</top><width>150</width><height>48</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <label>‹  Back</label>
            <font>font13</font><align>center</align><aligny>center</aligny>
            <textcolor>FFFFFFFF</textcolor><focusedcolor>FFFFFFFF</focusedcolor>
            <onleft>noop</onleft><onright>noop</onright><onup>noop</onup><ondown>{p}</ondown>
        </control>
'''.format(b=BACK, p=PLAY_B)
    chosen, other = 'String.IsEqual(Window.Property(season),ListItem.Label)', '!String.IsEqual(Window.Property(season),ListItem.Label)'
    lang_on, lang_off = 'String.IsEqual(Window.Property(language),ListItem.Label)', '!String.IsEqual(Window.Property(language),ListItem.Label)'
    return '''<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by tools/make_skin.py; edit that file, not this one. -->
<window>
    <defaultcontrol always="true">{play}</defaultcontrol>
    <controls>
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height><texture>bg.png</texture></control>
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height><texture>bg_kids.png</texture><visible>!String.IsEmpty(Window.Property(kids))</visible></control>
        <!-- the backdrop fills the right of the screen; without one the poster stands in -->
        <control type="image"><left>640</left><top>0</top><width>1280</width><height>720</height><aspectratio>scale</aspectratio><texture background="true">$INFO[Window.Property(fanart)]</texture><fadetime>400</fadetime><visible>!String.IsEmpty(Window.Property(fanart))</visible></control>
        <control type="image"><left>640</left><top>0</top><width>700</width><height>720</height><texture>fade_left.png</texture></control>
        <control type="image"><left>640</left><top>300</top><width>1280</width><height>420</height><texture>fade_bottom.png</texture></control>
        <control type="image"><left>1520</left><top>96</top><width>250</width><height>360</height><aspectratio>keep</aspectratio><texture background="true" diffuse="mask_poster.png">$INFO[Window.Property(poster)]</texture><visible>String.IsEmpty(Window.Property(fanart)) + !String.IsEmpty(Window.Property(poster))</visible></control>
{back}        <control type="label"><left>60</left><top>96</top><width>1180</width><height>80</height><font>font52_title</font><textcolor>FFFFFFFF</textcolor><scroll>true</scroll><label>[B]$INFO[Window.Property(title)][/B]</label></control>
        <control type="label"><left>60</left><top>182</top><width>1180</width><height>36</height><font>font13</font><textcolor>FF46D369</textcolor><label>$INFO[Window.Property(meta)]</label></control>
        <control type="textbox"><left>60</left><top>226</top><width>900</width><height>130</height><font>font12</font><textcolor>FFE5E5E5</textcolor><autoscroll time="3000" delay="4000" repeat="8000">true</autoscroll><label>$INFO[Window.Property(plot)]</label></control>
        <control type="label"><left>60</left><top>366</top><width>1180</width><height>32</height><font>font10</font><textcolor>FFB3B3B3</textcolor><label>$INFO[Window.Property(cast)]</label></control>
        <control type="label"><left>60</left><top>396</top><width>1180</width><height>30</height><font>font10</font><textcolor>FF8C8C8C</textcolor><label>$INFO[Window.Property(status)]</label></control>
{buttons}
        <!-- language chips, only when this film is listed in more than one language; Down from Play lands here -->
        <control type="list" id="{langs}">
            <left>50</left><top>496</top><width>1820</width><height>52</height>
            <orientation>horizontal</orientation>
            <scrolltime tween="quadratic">150</scrolltime>
            <onleft>noop</onleft><onright>noop</onright><onup>{play}</onup>
            <ondown condition="{is_show}">{seasons}</ondown><ondown>{alike}</ondown>
            <visible>!String.IsEmpty(Window.Property(langs))</visible>
            <itemlayout width="230" height="52">
                <control type="image"><left>0</left><top>4</top><width>216</width><height>44</height><texture colordiffuse="33FFFFFF" border="22">pill.png</texture></control>
                <control type="label"><left>0</left><top>4</top><width>216</width><height>44</height><font>font12</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>{lang_on}</visible></control>
                <control type="label"><left>0</left><top>4</top><width>216</width><height>44</height><font>font12</font><align>center</align><aligny>center</aligny><textcolor>FFB3B3B3</textcolor><label>$INFO[ListItem.Label]</label><visible>{lang_off}</visible></control>
            </itemlayout>
            <focusedlayout width="230" height="52">
                <control type="image"><left>0</left><top>4</top><width>216</width><height>44</height><texture colordiffuse="FFFFFFFF" border="22">pill.png</texture><visible>Control.HasFocus({langs})</visible></control>
                <control type="image"><left>0</left><top>4</top><width>216</width><height>44</height><texture colordiffuse="33FFFFFF" border="22">pill.png</texture><visible>!Control.HasFocus({langs})</visible></control>
                <control type="label"><left>0</left><top>4</top><width>216</width><height>44</height><font>font12</font><align>center</align><aligny>center</aligny><textcolor>FF141414</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>Control.HasFocus({langs})</visible></control>
                <control type="label"><left>0</left><top>4</top><width>216</width><height>44</height><font>font12</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>!Control.HasFocus({langs})</visible></control>
            </focusedlayout>
        </control>
        <!-- seasons, episodes and similar titles drop down while the language chips are showing -->
        <control type="group">
            <animation effect="slide" end="0,56" time="0" condition="!String.IsEmpty(Window.Property(langs))">Conditional</animation>
        <!-- a show: its seasons as chips and the chosen season's episodes; a movie: titles like it -->
        <control type="label"><left>60</left><top>516</top><width>600</width><height>44</height><font>font30_title</font><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[Window.Property(section)][/B]</label></control>
        <control type="list" id="{seasons}">
            <left>420</left><top>516</top><width>1440</width><height>48</height>
            <orientation>horizontal</orientation>
            <scrolltime tween="quadratic">150</scrolltime>
            <onleft>noop</onleft><onright>noop</onright>
            <onup condition="!String.IsEmpty(Window.Property(langs))">{langs}</onup><onup>{play}</onup>
            <ondown>{episodes}</ondown>
            <visible>{is_show}</visible>
            <itemlayout width="190" height="48">
                <control type="image"><left>0</left><top>0</top><width>180</width><height>44</height><texture colordiffuse="33FFFFFF" border="22">pill.png</texture></control>
                <control type="label"><left>0</left><top>0</top><width>180</width><height>44</height><font>font12</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>{chosen}</visible></control>
                <control type="label"><left>0</left><top>0</top><width>180</width><height>44</height><font>font12</font><align>center</align><aligny>center</aligny><textcolor>FFB3B3B3</textcolor><label>$INFO[ListItem.Label]</label><visible>{other}</visible></control>
            </itemlayout>
            <focusedlayout width="190" height="48">
                <control type="image"><left>0</left><top>0</top><width>180</width><height>44</height><texture colordiffuse="FFFFFFFF" border="22">pill.png</texture><visible>Control.HasFocus({seasons})</visible></control>
                <control type="image"><left>0</left><top>0</top><width>180</width><height>44</height><texture colordiffuse="33FFFFFF" border="22">pill.png</texture><visible>!Control.HasFocus({seasons})</visible></control>
                <control type="label"><left>0</left><top>0</top><width>180</width><height>44</height><font>font12</font><align>center</align><aligny>center</aligny><textcolor>FF141414</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>Control.HasFocus({seasons})</visible></control>
                <control type="label"><left>0</left><top>0</top><width>180</width><height>44</height><font>font12</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>!Control.HasFocus({seasons})</visible></control>
            </focusedlayout>
        </control>
        <control type="list" id="{episodes}">
            <left>50</left><top>580</top><width>1870</width><height>260</height>
            <orientation>horizontal</orientation>
            <scrolltime tween="quadratic">200</scrolltime>
            <onleft>noop</onleft><onright>noop</onright><onup>{seasons}</onup><ondown>noop</ondown>
            <visible>{is_show}</visible>
            <itemlayout width="372" height="260">
                <control type="group"><left>10</left><top>14</top>
{episode_item}
                </control>
            </itemlayout>
            <focusedlayout width="372" height="260">
                <control type="group"><left>10</left><top>14</top>
                    {zoom}
{episode_focus}
                </control>
            </focusedlayout>
        </control>
        <!-- the highlighted episode's length and synopsis -->
        <control type="textbox"><left>60</left><top>850</top><width>1800</width><height>150</height><font>font12</font><textcolor>FFB3B3B3</textcolor><label>[B][COLOR FFFFFFFF]$INFO[Container({episodes}).ListItem.Label][/COLOR][/B]  [COLOR FF46D369]$INFO[Container({episodes}).ListItem.Property(meta)][/COLOR][CR]$INFO[Container({episodes}).ListItem.Property(plot)]</label><visible>{is_show}</visible></control>
        <control type="list" id="{alike}">
            <left>50</left><top>576</top><width>1870</width><height>330</height>
            <orientation>horizontal</orientation>
            <scrolltime tween="quadratic">200</scrolltime>
            <onleft>noop</onleft><onright>noop</onright>
            <onup condition="!String.IsEmpty(Window.Property(langs))">{langs}</onup><onup>{play}</onup>
            <ondown>noop</ondown>
            <visible>{not_show}</visible>
            <itemlayout width="216" height="330">
                <control type="group"><left>10</left><top>14</top>
{poster_item}
                </control>
            </itemlayout>
            <focusedlayout width="216" height="330">
                <control type="group"><left>10</left><top>14</top>
                    {zoom_alike}
{poster_focus}
                </control>
            </focusedlayout>
        </control>
        <control type="textbox"><left>60</left><top>920</top><width>1800</width><height>100</height><font>font12</font><textcolor>FFB3B3B3</textcolor><label>[B][COLOR FFFFFFFF]$INFO[Container({alike}).ListItem.Label][/COLOR][/B]  [COLOR FF46D369]$INFO[Container({alike}).ListItem.Property(meta)][/COLOR][CR]$INFO[Container({alike}).ListItem.Property(plot)]</label><visible>{not_show} + Control.HasFocus({alike})</visible></control>
        </control>
    </controls>
</window>
'''.format(play=PLAY_B, seasons=SEASONS, episodes=EPISODES, alike=ALIKE, langs=LANGS, is_show=is_show, not_show=not_show, back=back,
           buttons=''.join(rows).rstrip('\n'), chosen=chosen, other=other, lang_on=lang_on, lang_off=lang_off,
           zoom=zoom(EPISODES), zoom_alike=zoom(ALIKE),
           episode_item=indent(scaled(episode_tile(False)), 20), episode_focus=indent(scaled(episode_tile(True)), 20),
           poster_item=indent(scaled(poster_tile(ALIKE, False)), 20), poster_focus=indent(scaled(poster_tile(ALIKE, True)), 20))

def next_xml():
    """The Next episode overlay shown over the video in an episode's last minute: Up next and the episode's name,
    Watch credits, and a Next episode pill that fills from the left as its countdown runs (the script widens the
    fill image, id 302)."""
    return '''<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by tools/make_skin.py; edit that file, not this one. -->
<window>
    <defaultcontrol always="true">301</defaultcontrol>
    <animation effect="fade" start="0" end="100" time="250">WindowOpen</animation>
    <animation effect="fade" start="100" end="0" time="200">WindowClose</animation>
    <controls>
        <control type="image"><left>0</left><top>620</top><width>1920</width><height>460</height><texture colordiffuse="E0FFFFFF">fade_bottom.png</texture></control>
        <control type="label"><left>1160</left><top>842</top><width>700</width><height>34</height><font>font12</font><align>right</align><textcolor>FFB3B3B3</textcolor><label>Up next</label></control>
        <control type="label"><left>1060</left><top>874</top><width>800</width><height>44</height><font>font13</font><align>right</align><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[Window.Property(next)][/B]</label></control>
        <!-- Watch credits: closes the overlay and lets the episode play out -->
        <control type="image"><left>1250</left><top>938</top><width>270</width><height>64</height><texture colordiffuse="4DFFFFFF" border="23">pill.png</texture><visible>!Control.HasFocus(300)</visible></control>
        <control type="image"><left>1250</left><top>938</top><width>270</width><height>64</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture><visible>Control.HasFocus(300)</visible></control>
        <control type="button" id="300">
            <left>1250</left><top>938</top><width>270</width><height>64</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <label>Watch credits</label>
            <font>font13</font><align>center</align><aligny>center</aligny>
            <textcolor>FFFFFFFF</textcolor><focusedcolor>FF141414</focusedcolor>
            <onleft>300</onleft><onright>301</onright><onup>300</onup><ondown>300</ondown>
        </control>
        <!-- Next episode: the fill sweeps across while the countdown runs -->
        <control type="image"><left>1540</left><top>938</top><width>320</width><height>64</height><texture colordiffuse="4DFFFFFF" border="23">pill.png</texture><visible>!Control.HasFocus(301)</visible></control>
        <control type="image"><left>1540</left><top>938</top><width>320</width><height>64</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture><visible>Control.HasFocus(301)</visible></control>
        <control type="image" id="302"><left>1540</left><top>938</top><width>64</width><height>64</height><texture colordiffuse="4D000000" border="23">pill.png</texture></control>
        <control type="button" id="301">
            <left>1540</left><top>938</top><width>320</width><height>64</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <label>[B]$INFO[Window.Property(button)][/B]</label>
            <font>font13</font><align>center</align><aligny>center</aligny>
            <textcolor>FFFFFFFF</textcolor><focusedcolor>FF141414</focusedcolor>
            <onleft>300</onleft><onright>301</onright><onup>301</onup><ondown>301</ondown>
        </control>
    </controls>
</window>
'''


def search_xml():
    """Modern Netflix / Apple TV-style in-app Search overlay: compact 6x6 rounded keyboard + Voice Mode QR bridge
    on the left, instant live-as-you-type poster results on the right."""
    RES = 540
    KEYS = 520
    RECENT = 530
    return '''<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by tools/make_skin.py; edit that file, not this one. -->
<window>
    <defaultcontrol always="true">{keys}</defaultcontrol>
    <animation effect="fade" start="0" end="100" time="180">WindowOpen</animation>
    <animation effect="fade" start="100" end="0" time="150">WindowClose</animation>
    <controls>
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height><texture>bg.png</texture></control>

        <!-- Top search bar -->
        <control type="image"><left>70</left><top>36</top><width>1130</width><height>68</height><texture colordiffuse="26FFFFFF" border="23">pill.png</texture></control>
        <control type="image"><left>94</left><top>54</top><width>32</width><height>32</height><texture colordiffuse="FFD6D6D6">search-thin.png</texture></control>
        <control type="label">
            <left>142</left><top>36</top><width>1034</width><height>68</height>
            <font>font14</font><aligny>center</aligny><textcolor>FFFFFFFF</textcolor>
            <label>[B]$INFO[Window.Property(query)][/B][COLOR FFE50914]|[/COLOR]</label>
            <visible>!String.IsEmpty(Window.Property(query))</visible>
        </control>
        <control type="label">
            <left>142</left><top>36</top><width>1034</width><height>68</height>
            <font>font14</font><aligny>center</aligny><textcolor>FF8E8E98</textcolor>
            <label>$INFO[Window.Property(placeholder)]</label>
            <visible>String.IsEmpty(Window.Property(query))</visible>
        </control>

        <!-- Voice Mode pill (501): red glow when listening -->
        <control type="image"><left>1220</left><top>36</top><width>260</width><height>68</height><texture colordiffuse="33FFFFFF" border="23">pill.png</texture><visible>!Control.HasFocus(501) + String.IsEmpty(Window.Property(voice_active))</visible></control>
        <control type="image"><left>1220</left><top>36</top><width>260</width><height>68</height><texture colordiffuse="FFE50914" border="23">pill.png</texture><visible>!Control.HasFocus(501) + !String.IsEmpty(Window.Property(voice_active))</visible></control>
        <control type="image"><left>1220</left><top>36</top><width>260</width><height>68</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture><visible>Control.HasFocus(501)</visible></control>
        <control type="image"><left>1244</left><top>54</top><width>32</width><height>32</height><texture colordiffuse="FFFFFFFF">mic-thin.png</texture><visible>!Control.HasFocus(501)</visible></control>
        <control type="image"><left>1244</left><top>54</top><width>32</width><height>32</height><texture colordiffuse="FF141414">mic-thin.png</texture><visible>Control.HasFocus(501) + String.IsEmpty(Window.Property(voice_active))</visible></control>
        <control type="image"><left>1244</left><top>54</top><width>32</width><height>32</height><texture colordiffuse="FFE50914">mic-thin.png</texture><visible>Control.HasFocus(501) + !String.IsEmpty(Window.Property(voice_active))</visible></control>
        <control type="button" id="501">
            <left>1220</left><top>36</top><width>260</width><height>68</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <textoffsetx>64</textoffsetx>
            <label>$INFO[Window.Property(voice_label)]</label>
            <font>font13</font><align>left</align><aligny>center</aligny>
            <textcolor>FFFFFFFF</textcolor><focusedcolor>FF141414</focusedcolor>
            <onleft>501</onleft><onright>502</onright><onup>501</onup><ondown>510</ondown>
        </control>

        <!-- Search / Go pill (502) -->
        <control type="image"><left>1500</left><top>36</top><width>200</width><height>68</height><texture colordiffuse="33FFFFFF" border="23">pill.png</texture><visible>!Control.HasFocus(502)</visible></control>
        <control type="image"><left>1500</left><top>36</top><width>200</width><height>68</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture><visible>Control.HasFocus(502)</visible></control>
        <control type="button" id="502">
            <left>1500</left><top>36</top><width>200</width><height>68</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <label>[B]Search[/B]</label>
            <font>font13</font><align>center</align><aligny>center</aligny>
            <textcolor>FFFFFFFF</textcolor><focusedcolor>FF141414</focusedcolor>
            <onleft>501</onleft><onright>503</onright><onup>502</onup><ondown>{res}</ondown>
        </control>

        <!-- Close pill (503) -->
        <control type="image"><left>1720</left><top>36</top><width>68</width><height>68</height><texture colordiffuse="33FFFFFF" border="23">pill.png</texture><visible>!Control.HasFocus(503)</visible></control>
        <control type="image"><left>1720</left><top>36</top><width>68</width><height>68</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture><visible>Control.HasFocus(503)</visible></control>
        <control type="image"><left>1738</left><top>54</top><width>32</width><height>32</height><texture colordiffuse="FFD6D6D6">close-thin.png</texture><visible>!Control.HasFocus(503)</visible></control>
        <control type="image"><left>1738</left><top>54</top><width>32</width><height>32</height><texture colordiffuse="FF141414">close-thin.png</texture><visible>Control.HasFocus(503)</visible></control>
        <control type="button" id="503">
            <left>1720</left><top>36</top><width>68</width><height>68</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <onleft>502</onleft><onright>503</onright><onup>503</onup><ondown>{res}</ondown>
        </control>

        <!-- Left pane: Quick action row (Space 510, Backspace 511, Clear 512) -->
        <control type="image"><left>70</left><top>136</top><width>252</width><height>60</height><texture colordiffuse="26FFFFFF" border="12">key_bg.png</texture><visible>!Control.HasFocus(510)</visible></control>
        <control type="image"><left>70</left><top>136</top><width>252</width><height>60</height><texture colordiffuse="FFFFFFFF" border="12">key_bg.png</texture><visible>Control.HasFocus(510)</visible></control>
        <control type="button" id="510">
            <left>70</left><top>136</top><width>252</width><height>60</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <label>Space</label><font>font13</font><align>center</align><aligny>center</aligny>
            <textcolor>FFE8E8EC</textcolor><focusedcolor>FF141414</focusedcolor>
            <onleft>510</onleft><onright>511</onright><onup>501</onup><ondown>{keys}</ondown>
        </control>

        <control type="image"><left>332</left><top>136</top><width>142</width><height>60</height><texture colordiffuse="26FFFFFF" border="12">key_bg.png</texture><visible>!Control.HasFocus(511)</visible></control>
        <control type="image"><left>332</left><top>136</top><width>142</width><height>60</height><texture colordiffuse="FFFFFFFF" border="12">key_bg.png</texture><visible>Control.HasFocus(511)</visible></control>
        <control type="image"><left>387</left><top>150</top><width>32</width><height>32</height><texture colordiffuse="FFE8E8EC">backspace-thin.png</texture><visible>!Control.HasFocus(511)</visible></control>
        <control type="image"><left>387</left><top>150</top><width>32</width><height>32</height><texture colordiffuse="FF141414">backspace-thin.png</texture><visible>Control.HasFocus(511)</visible></control>
        <control type="button" id="511">
            <left>332</left><top>136</top><width>142</width><height>60</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <onleft>510</onleft><onright>512</onright><onup>501</onup><ondown>{keys}</ondown>
        </control>

        <control type="image"><left>484</left><top>136</top><width>142</width><height>60</height><texture colordiffuse="26FFFFFF" border="12">key_bg.png</texture><visible>!Control.HasFocus(512)</visible></control>
        <control type="image"><left>484</left><top>136</top><width>142</width><height>60</height><texture colordiffuse="FFFFFFFF" border="12">key_bg.png</texture><visible>Control.HasFocus(512)</visible></control>
        <control type="button" id="512">
            <left>484</left><top>136</top><width>142</width><height>60</height>
            <texturefocus>-</texturefocus><texturenofocus>-</texturenofocus>
            <label>Clear</label><font>font13</font><align>center</align><aligny>center</aligny>
            <textcolor>FFE8E8EC</textcolor><focusedcolor>FF141414</focusedcolor>
            <onleft>511</onleft><onright>{res}</onright><onup>501</onup><ondown>{keys}</ondown>
        </control>

        <!-- Modern 6x6 compact keyboard grid (520) -->
        <control type="panel" id="{keys}">
            <left>70</left><top>212</top><width>564</width><height>444</height>
            <orientation>vertical</orientation>
            <onleft>{keys}</onleft><onright>{res}</onright><onup>510</onup><ondown>{recent}</ondown>
            <visible>String.IsEmpty(Window.Property(voice_active))</visible>
            <itemlayout width="94" height="74">
                <control type="image"><left>4</left><top>4</top><width>86</width><height>66</height><texture colordiffuse="22FFFFFF" border="12">key_bg.png</texture></control>
                <control type="label"><left>4</left><top>4</top><width>86</width><height>66</height><font>font14</font><align>center</align><aligny>center</aligny><textcolor>FFF0F0F4</textcolor><label>[B]$INFO[ListItem.Label][/B]</label></control>
            </itemlayout>
            <focusedlayout width="94" height="74">
                <control type="image"><left>2</left><top>2</top><width>90</width><height>70</height><texture colordiffuse="FFFFFFFF" border="12">key_bg.png</texture><visible>Control.HasFocus({keys})</visible></control>
                <control type="image"><left>4</left><top>4</top><width>86</width><height>66</height><texture colordiffuse="22FFFFFF" border="12">key_bg.png</texture><visible>!Control.HasFocus({keys})</visible></control>
                <control type="label"><left>4</left><top>4</top><width>86</width><height>66</height><font>font14</font><align>center</align><aligny>center</aligny><textcolor>FF141414</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>Control.HasFocus({keys})</visible></control>
                <control type="label"><left>4</left><top>4</top><width>86</width><height>66</height><font>font14</font><align>center</align><aligny>center</aligny><textcolor>FFF0F0F4</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>!Control.HasFocus({keys})</visible></control>
            </focusedlayout>
        </control>

        <!-- Voice Mode QR card (replaces keyboard while Voice Mode is listening) -->
        <control type="group">
            <left>70</left><top>212</top><width>556</width><height>520</height>
            <visible>!String.IsEmpty(Window.Property(voice_active))</visible>
            <control type="image"><left>0</left><top>0</top><width>556</width><height>520</height><texture colordiffuse="FF1C1C24" border="14">round.png</texture></control>
            <control type="image"><left>148</left><top>36</top><width>260</width><height>260</height><texture colordiffuse="FFFFFFFF" border="14">round.png</texture></control>
            <control type="image"><left>162</left><top>50</top><width>232</width><height>232</height><aspectratio>keep</aspectratio><texture>$INFO[Window.Property(voice_qr)]</texture></control>
            <control type="label"><left>24</left><top>314</top><width>508</width><height>40</height><font>font14</font><align>center</align><textcolor>FFFFFFFF</textcolor><label>[B]Scan with your phone to speak[/B]</label></control>
            <control type="label"><left>24</left><top>360</top><width>508</width><height>36</height><font>font13</font><align>center</align><textcolor>FFE50914</textcolor><label>[B]$INFO[Window.Property(voice_short_url)][/B]</label></control>
            <control type="textbox"><left>32</left><top>408</top><width>492</width><height>92</height><font>font12</font><align>center</align><textcolor>FFB3B3BC</textcolor><label>$INFO[Window.Property(voice_status)]</label></control>
        </control>

        <!-- Recent searches strip (530) -->
        <control type="label"><left>74</left><top>684</top><width>550</width><height>32</height><font>font12</font><textcolor>FF8E8E98</textcolor><label>Recent searches</label><visible>String.IsEmpty(Window.Property(voice_active)) + !String.IsEmpty(Window.Property(has_recent))</visible></control>
        <control type="list" id="{recent}">
            <left>70</left><top>722</top><width>556</width><height>280</height>
            <orientation>vertical</orientation>
            <onleft>{recent}</onleft><onright>{res}</onright><onup>{keys}</onup><ondown>{recent}</ondown>
            <visible>String.IsEmpty(Window.Property(voice_active)) + !String.IsEmpty(Window.Property(has_recent))</visible>
            <itemlayout width="556" height="52">
                <control type="image"><left>0</left><top>4</top><width>556</width><height>44</height><texture colordiffuse="1FFFFFFF" border="22">pill.png</texture></control>
                <control type="label"><left>22</left><top>4</top><width>512</width><height>44</height><font>font12</font><aligny>center</aligny><textcolor>FFD6D6DE</textcolor><label>$INFO[ListItem.Label]</label></control>
            </itemlayout>
            <focusedlayout width="556" height="52">
                <control type="image"><left>0</left><top>4</top><width>556</width><height>44</height><texture colordiffuse="FFFFFFFF" border="22">pill.png</texture><visible>Control.HasFocus({recent})</visible></control>
                <control type="image"><left>0</left><top>4</top><width>556</width><height>44</height><texture colordiffuse="1FFFFFFF" border="22">pill.png</texture><visible>!Control.HasFocus({recent})</visible></control>
                <control type="label"><left>22</left><top>4</top><width>512</width><height>44</height><font>font12</font><aligny>center</aligny><textcolor>FF141414</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>Control.HasFocus({recent})</visible></control>
                <control type="label"><left>22</left><top>4</top><width>512</width><height>44</height><font>font12</font><aligny>center</aligny><textcolor>FFD6D6DE</textcolor><label>$INFO[ListItem.Label]</label><visible>!Control.HasFocus({recent})</visible></control>
            </focusedlayout>
        </control>

        <!-- Right pane: Instant live poster results (540) -->
        <control type="label">
            <left>684</left><top>136</top><width>1160</width><height>44</height>
            <font>font14</font><textcolor>FFFFFFFF</textcolor>
            <label>[B]$INFO[Window.Property(results_heading)][/B]</label>
        </control>
        <control type="label">
            <left>684</left><top>240</top><width>1100</width><height>80</height>
            <font>font13</font><textcolor>FF8E8E98</textcolor>
            <label>$INFO[Window.Property(results_empty)]</label>
            <visible>!String.IsEmpty(Window.Property(results_empty))</visible>
        </control>
        <control type="panel" id="{res}">
            <left>674</left><top>190</top><width>1180</width><height>680</height>
            <orientation>vertical</orientation>
            <onleft>{keys}</onleft><onright>{res}</onright><onup>502</onup><ondown>{res}</ondown>
            <itemlayout width="232" height="340">
                <control type="group"><left>16</left><top>16</top>
{poster_item}
                </control>
            </itemlayout>
            <focusedlayout width="232" height="340">
                <control type="group"><left>16</left><top>16</top>
                    {zoom}
{poster_focus}
                </control>
            </focusedlayout>
        </control>

        <!-- Focused result details strip at bottom right -->
        <control type="textbox">
            <left>684</left><top>894</top><width>1160</width><height>120</height>
            <font>font12</font><textcolor>FFB3B3B3</textcolor>
            <label>[B][COLOR FFFFFFFF]$INFO[Container({res}).ListItem.Label][/COLOR][/B]  [COLOR FF46D369]$INFO[Container({res}).ListItem.Property(meta)][/COLOR][CR]$INFO[Container({res}).ListItem.Property(plot)]</label>
            <visible>Control.HasFocus({res})</visible>
        </control>
    </controls>
</window>
'''.format(keys=KEYS, recent=RECENT, res=RES, zoom=zoom(RES),
           poster_item=indent(poster_tile(RES, False), 20),
           poster_focus=indent(poster_tile(RES, True), 20))


def main():
    make_images()
    with open(os.path.join(XML, 'script-stream-home.xml'), 'w', encoding='utf-8') as f:
        f.write(home_xml())
    with open(os.path.join(XML, 'script-stream-grid.xml'), 'w', encoding='utf-8') as f:
        f.write(grid_xml())
    with open(os.path.join(XML, 'script-stream-next.xml'), 'w', encoding='utf-8') as f:
        f.write(next_xml())
    with open(os.path.join(XML, 'script-stream-title.xml'), 'w', encoding='utf-8') as f:
        f.write(title_xml())
    with open(os.path.join(XML, 'script-stream-search.xml'), 'w', encoding='utf-8') as f:
        f.write(search_xml())
    TOUCH['on'] = True
    try:
        with open(os.path.join(XML, 'script-stream-home-touch.xml'), 'w', encoding='utf-8') as f:
            f.write(home_xml())
        with open(os.path.join(XML, 'script-stream-grid-touch.xml'), 'w', encoding='utf-8') as f:
            f.write(grid_xml())
        with open(os.path.join(XML, 'script-stream-title-touch.xml'), 'w', encoding='utf-8') as f:
            f.write(title_xml())
        with open(os.path.join(XML, 'script-stream-search-touch.xml'), 'w', encoding='utf-8') as f:
            f.write(search_xml())
    finally:
        TOUCH['on'] = False
    print('skin written')


if __name__ == '__main__':
    main()
