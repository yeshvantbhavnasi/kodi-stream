#!/usr/bin/env python3
"""Generate Stream's skin: the Home and Grid windows and the images they draw with.

The Home window has sixteen identical rows, each with its own billboard group and tile layouts, so the XML is
written from one template here rather than kept by hand. Run after changing anything below:

    python3 tools/make_skin.py

Layout notes (1080p coordinates):
  - Top bar 0-90: logo, tabs (pill highlight), search, profile, settings, close. A dark fade keeps it readable
    over the billboard. The genre strip (Movies and Shows) sits under it at 92 and slides everything else down 44.
  - Billboard 0-720: the highlighted title's TMDb backdrop fills the right of the screen behind a fade; the
    title, its facts, synopsis, cast and the Watch trailer pill sit on the left. Tiles without a backdrop fall
    back to their poster.
  - Rows from 500: poster tiles with rounded corners, a white rounded frame and a small zoom on the highlighted
    tile; YouTube rows use wide tiles; the Top 10 row draws Netflix-style big numerals beside the posters.
"""
import os
import struct
import zlib

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'script.stream', 'resources', 'skins', 'Default')
XML = os.path.join(ROOT, '1080i')
MEDIA = os.path.join(ROOT, 'media')

ROWS = 16
FIRST = 200
TABS, CLOSE, SEARCH, GEAR, PROFILE, TRAILER, FILTER = 100, 110, 120, 130, 140, 150, 160
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


def make_images():
    rounded(os.path.join(MEDIA, 'pill.png'), 48, 48, 24)                 # 9-slice with border="23"
    rounded(os.path.join(MEDIA, 'round.png'), 32, 32, 10)                # 9-slice with border="14": frames and badges
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
        '<control type="textbox"><left>{l}</left><top>90</top><width>180</width><height>120</height><font>font13</font><align>center</align><textcolor>FFDDDDDD</textcolor><label>$INFO[ListItem.Label]</label><visible>{c} + String.IsEmpty(ListItem.Art(thumb))</visible></control>'.format(l=o + 10, c=nocard),
        '<control type="image"><left>{l}</left><top>0</top><width>200</width><height>300</height><aspectratio>scale</aspectratio><texture background="true" diffuse="mask_poster.png">$INFO[ListItem.Art(thumb)]</texture><visible>{c} + String.IsEmpty(ListItem.Property(wide))</visible></control>'.format(l=o, c=nocard),
        '<control type="image"><left>{l}</left><top>94</top><width>200</width><height>112</height><aspectratio>scale</aspectratio><texture background="true">$INFO[ListItem.Art(thumb)]</texture><visible>{c} + !String.IsEmpty(ListItem.Property(wide))</visible></control>'.format(l=o, c=nocard),
        '<control type="image"><left>{l}</left><top>8</top><width>96</width><height>28</height><texture colordiffuse="FFE50914" border="10">round.png</texture><visible>{c} + !String.IsEmpty(ListItem.Property(tag))</visible></control>'.format(l=o + 8, c=nocard),
        '<control type="label"><left>{l}</left><top>8</top><width>96</width><height>28</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Property(tag)][/B]</label><visible>{c} + !String.IsEmpty(ListItem.Property(tag))</visible></control>'.format(l=o + 8, c=nocard),
        '<control type="image"><left>{l}</left><top>262</top><width>84</width><height>28</height><texture colordiffuse="D0000000" border="14">round.png</texture><visible>{c} + !String.IsEmpty(ListItem.Property(rating))</visible></control>'.format(l=o + 8, c=nocard),
        '<control type="label"><left>{l}</left><top>262</top><width>84</width><height>28</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFF5C518</textcolor><label>[B]$INFO[ListItem.Property(rating)][/B]</label><visible>{c} + !String.IsEmpty(ListItem.Property(rating))</visible></control>'.format(l=o + 8, c=nocard),
        progress(o + 10, 286, 180, 6, nocard + ' + '),
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


def indent(text, n):
    pad = ' ' * n
    return '\n'.join(pad + line for line in text.split('\n'))


def row(n):
    rid = FIRST + n
    down = rid + 1 if n < ROWS - 1 else 'noop'
    return '''            <control type="group" id="{gid}">
                <height>440</height>
                <visible>!String.IsEmpty(Window.Property(row{n}.title))</visible>
                <control type="label"><left>60</left><top>8</top><width>1700</width><height>44</height><font>font30_title</font><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[Window.Property(row{n}.title)][/B]</label></control>
                <control type="list" id="{rid}">
                    <onup>{trailer}</onup>
                    <onleft>{trailer}</onleft>
                    <ondown>{down}</ondown>
                    <left>50</left><top>56</top><width>1870</width><height>330</height>
                    <orientation>horizontal</orientation>
                    <scrolltime tween="quadratic">200</scrolltime>{layouts}
                </control>
            </control>
'''.format(gid=300 + n, n=n, rid=rid, trailer=TRAILER, down=down, layouts=layouts(n, rid))


# ---------- billboard ----------

def billboard(n):
    rid = FIRST + n
    c = 'Container({0}).ListItem'.format(rid)
    shown = 'Control.HasFocus({r}) | [Control.HasFocus({t}) + String.IsEqual(Window.Property(hero),{r})]'.format(r=rid, t=TRAILER)
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
            <control type="image"><left>1520</left><top>96</top><width>250</width><height>360</height><aspectratio>keep</aspectratio><texture background="true" diffuse="mask_poster.png">$INFO[{c}.Art(thumb)]</texture><visible>{no_art} | !String.IsEmpty(Window.Property(kids))</visible></control>
            <control type="label"><left>60</left><top>146</top><width>1100</width><height>70</height><font>font52_title</font><textcolor>FFFFFFFF</textcolor><scroll>true</scroll><label>[B]$INFO[{c}.Label][/B]</label></control>
            <control type="label"><left>60</left><top>226</top><width>1100</width><height>36</height><font>font13</font><textcolor>FF46D369</textcolor><label>$INFO[{c}.Property(meta)]</label></control>
            <control type="textbox"><left>60</left><top>268</top><width>880</width><height>104</height><font>font12</font><textcolor>FFE5E5E5</textcolor><label>$INFO[{c}.Property(plot)]</label></control>
            <control type="label"><left>60</left><top>382</top><width>1100</width><height>32</height><font>font10</font><textcolor>FFB3B3B3</textcolor><label>$INFO[{c}.Property(cast)]</label></control>
            <control type="label"><left>60</left><top>432</top><width>1100</width><height>30</height><font>font10</font><textcolor>FF8C8C8C</textcolor><label>$INFO[{c}.Property(hint)]</label><visible>String.IsEmpty({c}.Property(trailer))</visible></control>
            <control type="label"><left>320</left><top>432</top><width>1100</width><height>30</height><font>font10</font><textcolor>FF8C8C8C</textcolor><label>$INFO[{c}.Property(hint)]</label><visible>!String.IsEmpty({c}.Property(trailer))</visible></control>
        </control>
'''.format(slide=SLIDE, shown=shown, c=c, has_art=has_art, no_art=no_art)


def trailer_button():
    rows_with_trailer = ' | '.join('[Control.HasFocus({r}) + !String.IsEmpty(Container({r}).ListItem.Property(trailer))]'.format(r=FIRST + n) for n in range(ROWS))
    shown = rows_with_trailer + ' | [[{0}] + String.IsEmpty(Window.Property(status)) + !String.IsEmpty(Container({1}).ListItem.Property(trailer))]'.format(TOP_BAR, FIRST)
    ups = ['<onup condition="[Control.HasFocus({f}) | String.IsEqual(Window.Property(hero),{f})] + Control.IsVisible({flt})">{flt}</onup>'.format(f=FIRST, flt=FILTER)]
    ups += ['<onup condition="Control.HasFocus({r})">{up}</onup>'.format(r=FIRST + n, up=TABS if n == 0 else FIRST + n - 1) for n in range(ROWS)]
    ups += ['<onup condition="String.IsEqual(Window.Property(hero),{r})">{up}</onup>'.format(r=FIRST + n, up=TABS if n == 0 else FIRST + n - 1) for n in range(ROWS)]
    ups.append('<onup>{0}</onup>'.format(TABS))
    downs = ['<ondown condition="String.IsEqual(Window.Property(hero),{r})">{r}</ondown>'.format(r=FIRST + n) for n in range(ROWS)]
    downs.append('<ondown>{0}</ondown>'.format(TRAILER))
    return '''        <!-- Watch trailer: shown for movies and shows (dimmed while the top bar has focus). Up from any tile reaches it (Kodi skips the hidden button and follows its own Up to the row above or the tabs); Left from a row's first tile reaches it too -->
        <control type="image"><left>60</left><top>422</top><width>240</width><height>48</height><texture colordiffuse="4DFFFFFF" border="23">pill.png</texture>{slide}<visible>{shown}</visible></control>
        <control type="image"><left>60</left><top>422</top><width>240</width><height>48</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture>{slide}<visible>Control.HasFocus({t})</visible></control>
        <control type="button" id="{t}">
            <left>60</left><top>422</top><width>240</width><height>48</height>
            {slide}
            <texturefocus>-</texturefocus>
            <texturenofocus>-</texturenofocus>
            <label>Watch trailer</label>
            <font>font13</font>
            <align>center</align>
            <aligny>center</aligny>
            <textcolor>FFFFFFFF</textcolor>
            <focusedcolor>FF141414</focusedcolor>
            <onleft>{t}</onleft>
            <onright>{t}</onright>
            {ups}
            {downs}
            <visible>Control.HasFocus({t}) | {shown}</visible>
        </control>
'''.format(slide=SLIDE, shown=shown, t=TRAILER, ups='\n            '.join(ups), downs='\n            '.join(downs))


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
            <itemlayout width="178" height="40">
                <control type="image"><left>10</left><top>3</top><width>164</width><height>34</height><texture colordiffuse="2EFFFFFF" border="17">pill.png</texture><visible>{other}</visible></control>
                <control type="image"><left>10</left><top>3</top><width>164</width><height>34</height><texture colordiffuse="80FFFFFF" border="17">pill.png</texture><visible>{chosen}</visible></control>
                <control type="label"><left>10</left><top>3</top><width>164</width><height>34</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFE0E0E0</textcolor><label>$INFO[ListItem.Label]</label></control>
            </itemlayout>
            <focusedlayout width="178" height="40">
                <control type="image"><left>10</left><top>3</top><width>164</width><height>34</height><texture colordiffuse="2EFFFFFF" border="17">pill.png</texture><visible>!Control.HasFocus({f}) + {other}</visible></control>
                <control type="image"><left>10</left><top>3</top><width>164</width><height>34</height><texture colordiffuse="80FFFFFF" border="17">pill.png</texture><visible>!Control.HasFocus({f}) + {chosen}</visible></control>
                <control type="image"><left>10</left><top>3</top><width>164</width><height>34</height><texture colordiffuse="FFFFFFFF" border="17">pill.png</texture><visible>Control.HasFocus({f})</visible></control>
                <control type="label"><left>10</left><top>3</top><width>164</width><height>34</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FFE0E0E0</textcolor><label>$INFO[ListItem.Label]</label><visible>!Control.HasFocus({f})</visible></control>
                <control type="label"><left>10</left><top>3</top><width>164</width><height>34</height><font>font10</font><align>center</align><aligny>center</aligny><textcolor>FF141414</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>Control.HasFocus({f})</visible></control>
            </focusedlayout>
        </control>
'''.format(f=f, tabs=TABS, first=FIRST, chosen=chosen, other=other)


def top_bar():
    return '''        <!-- top bar: a dark fade keeps it readable over the billboard -->
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>150</height><texture>top_fade.png</texture></control>
        <control type="image"><left>56</left><top>22</top><width>236</width><height>58</height><aspectratio>keep</aspectratio><texture>logo.png</texture><visible>String.IsEmpty(Window.Property(kids))</visible></control>
        <control type="label"><left>60</left><top>26</top><width>230</width><height>50</height><font>font36_title</font><textcolor>FFFFD640</textcolor><label>[B]KIDS[/B]</label><visible>!String.IsEmpty(Window.Property(kids))</visible></control>
        <control type="list" id="{tabs}">
            <left>330</left><top>24</top><width>1150</width><height>56</height>
            <orientation>horizontal</orientation>
            <ondown>{flt}</ondown>
            <onright>{search}</onright>
            <onleft>noop</onleft>
            <onup>noop</onup>
            <!-- nine tabs share 1150px, so each is 127 wide; the highlighted tab sits in a white pill, the open tab is bold white -->
            <itemlayout width="127" height="56" condition="String.IsEmpty(Window.Property(kids))">
                <control type="label"><left>0</left><top>0</top><width>124</width><height>56</height><font>font13</font><align>center</align><aligny>center</aligny><textcolor>FFB3B3B3</textcolor><label>$INFO[ListItem.Label]</label></control>
            </itemlayout>
            <focusedlayout width="127" height="56" condition="String.IsEmpty(Window.Property(kids))">
                <control type="image"><left>0</left><top>6</top><width>124</width><height>44</height><texture colordiffuse="FFFFFFFF" border="22">pill.png</texture><visible>Control.HasFocus({tabs})</visible><animation effect="zoom" start="90" end="100" time="120" center="auto">Visible</animation></control>
                <control type="label"><left>0</left><top>0</top><width>124</width><height>56</height><font>font13</font><align>center</align><aligny>center</aligny><textcolor>FF141414</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>Control.HasFocus({tabs})</visible></control>
                <control type="label"><left>0</left><top>0</top><width>124</width><height>56</height><font>font13</font><align>center</align><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Label][/B]</label><visible>!Control.HasFocus({tabs})</visible></control>
            </focusedlayout>
            <itemlayout width="230" height="56" condition="!String.IsEmpty(Window.Property(kids))">
                <control type="label"><left>0</left><top>0</top><width>290</width><height>56</height><font>font13</font><aligny>center</aligny><textcolor>FFD6D6D6</textcolor><label>$INFO[ListItem.Label]</label></control>
            </itemlayout>
            <focusedlayout width="230" height="56" condition="!String.IsEmpty(Window.Property(kids))">
                <control type="image"><left>0</left><top>48</top><width>130</width><height>6</height><texture colordiffuse="FFFFD640" border="3">round.png</texture></control>
                <control type="label"><left>0</left><top>0</top><width>290</width><height>56</height><font>font13</font><aligny>center</aligny><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[ListItem.Label][/B]</label></control>
            </focusedlayout>
        </control>
        <control type="image"><left>1490</left><top>28</top><width>240</width><height>48</height><texture colordiffuse="33FFFFFF" border="23">pill.png</texture><visible>!Control.HasFocus({search})</visible></control>
        <control type="image"><left>1490</left><top>28</top><width>240</width><height>48</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture><visible>Control.HasFocus({search})</visible></control>
        <control type="image"><left>1506</left><top>36</top><width>32</width><height>32</height><texture colordiffuse="FFD6D6D6">search-thin.png</texture><visible>!Control.HasFocus({search})</visible></control>
        <control type="image"><left>1506</left><top>36</top><width>32</width><height>32</height><texture colordiffuse="FF141414">search-thin.png</texture><visible>Control.HasFocus({search})</visible></control>
        <control type="button" id="{search}">
            <left>1490</left><top>28</top><width>240</width><height>48</height>
            <texturefocus>-</texturefocus>
            <texturenofocus>-</texturenofocus>
            <label>Search</label>
            <font>font13</font>
            <textoffsetx>58</textoffsetx>
            <aligny>center</aligny>
            <textcolor>FFB3B3B3</textcolor>
            <focusedcolor>FF141414</focusedcolor>
            <onleft>{tabs}</onleft>
            <onright>{profile}</onright>
            <ondown>{flt}</ondown>
            <onup>noop</onup>
        </control>
        <control type="button" id="{profile}">
            <left>1746</left><top>30</top><width>44</width><height>44</height>
            <texturefocus colordiffuse="FFE50914">person-thin.png</texturefocus>
            <texturenofocus colordiffuse="CCFFFFFF">person-thin.png</texturenofocus>
            <label></label>
            <onleft>{search}</onleft>
            <onright>{gear}</onright>
            <onup>noop</onup>
            <ondown>{flt}</ondown>
        </control>
        <control type="button" id="{gear}">
            <left>1800</left><top>30</top><width>44</width><height>44</height>
            <texturefocus colordiffuse="FFE50914">gear-thin.png</texturefocus>
            <texturenofocus colordiffuse="CCFFFFFF">gear-thin.png</texturenofocus>
            <label></label>
            <onleft>{profile}</onleft>
            <onright>{close}</onright>
            <onup>noop</onup>
            <ondown>{flt}</ondown>
        </control>
        <control type="button" id="{close}">
            <left>1856</left><top>30</top><width>44</width><height>44</height>
            <texturefocus colordiffuse="FFE50914">close-thin.png</texturefocus>
            <texturenofocus colordiffuse="CCFFFFFF">close-thin.png</texturenofocus>
            <label></label>
            <onleft>{gear}</onleft>
            <onright>noop</onright>
            <onup>noop</onup>
            <ondown>{flt}</ondown>
        </control>
'''.format(tabs=TABS, flt=FILTER, search=SEARCH, profile=PROFILE, gear=GEAR, close=CLOSE)


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
    out.append(trailer_button().rstrip('\n'))
    out.append(filter_strip().rstrip('\n'))
    # 510 tall: one row plus the next row's title. Kodi keeps the highlighted row fully inside this box, so when moving
    # down the empty space under the previous row's posters peeks in, not the posters themselves.
    out.append('''        <control type="grouplist" id="9000">
            <left>0</left><top>500</top><width>1920</width><height>510</height>
            {slide}
            <orientation>vertical</orientation>
            <itemgap>6</itemgap>
            <scrolltime tween="quadratic">250</scrolltime>
            <onup>{tabs}</onup>
            <onleft>{trailer}</onleft>'''.format(slide=SLIDE, tabs=TABS, trailer=TRAILER))
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
    return '''<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by tools/make_skin.py; edit that file, not this one. -->
<window>
    <defaultcontrol always="true">50</defaultcontrol>
    <controls>
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height><texture>bg.png</texture></control>
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>1080</height><texture>bg_kids.png</texture><visible>!String.IsEmpty(Window.Property(kids))</visible></control>
        <control type="image"><left>0</left><top>0</top><width>1920</width><height>180</height><texture>top_fade.png</texture></control>
        <control type="label"><left>60</left><top>30</top><width>1100</width><height>60</height><font>font36_title</font><textcolor>FFFFFFFF</textcolor><label>[B]$INFO[Window.Property(title)][/B]</label></control>
        <control type="label"><left>1180</left><top>30</top><width>360</width><height>60</height><font>font13</font><align>right</align><aligny>center</aligny><textcolor>FFA3A3A3</textcolor><label>$INFO[Window.Property(status)]</label></control>
        <!-- Watch trailer for the highlighted movie or show; Up from the top row reaches it -->
        <control type="image"><left>1600</left><top>31</top><width>260</width><height>48</height><texture colordiffuse="4DFFFFFF" border="23">pill.png</texture><visible>!String.IsEmpty(Container(50).ListItem.Property(trailer)) + String.IsEmpty(Window.Property(mode))</visible></control>
        <control type="image"><left>1600</left><top>31</top><width>260</width><height>48</height><texture colordiffuse="FFFFFFFF" border="23">pill.png</texture><visible>Control.HasFocus(150)</visible></control>
        <control type="button" id="150">
            <left>1600</left><top>31</top><width>260</width><height>48</height>
            <texturefocus>-</texturefocus>
            <texturenofocus>-</texturenofocus>
            <label>Watch trailer</label>
            <font>font13</font>
            <align>center</align>
            <aligny>center</aligny>
            <textcolor>FFFFFFFF</textcolor>
            <focusedcolor>FF141414</focusedcolor>
            <onleft>150</onleft>
            <onright>150</onright>
            <onup>150</onup>
            <ondown>50</ondown>
            <visible>Control.HasFocus(150) | [!String.IsEmpty(Container(50).ListItem.Property(trailer)) + String.IsEmpty(Window.Property(mode))]</visible>
        </control>
        <control type="textbox"><left>60</left><top>92</top><width>1800</width><height>70</height><font>font12</font><textcolor>FFB3B3B3</textcolor><label>[COLOR FF46D369]$INFO[Container(50).ListItem.Property(meta)][/COLOR]  $INFO[Container(50).ListItem.Property(plot)]</label><visible>String.IsEmpty(Window.Property(mode))</visible></control>
        <control type="panel" id="50">
            <visible>String.IsEmpty(Window.Property(mode))</visible>
            <onleft>noop</onleft>
            <onright>noop</onright>
            <onup>150</onup>
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


def main():
    make_images()
    with open(os.path.join(XML, 'script-stream-home.xml'), 'w', encoding='utf-8') as f:
        f.write(home_xml())
    with open(os.path.join(XML, 'script-stream-grid.xml'), 'w', encoding='utf-8') as f:
        f.write(grid_xml())
    with open(os.path.join(XML, 'script-stream-next.xml'), 'w', encoding='utf-8') as f:
        f.write(next_xml())
    print('skin written')


if __name__ == '__main__':
    main()
