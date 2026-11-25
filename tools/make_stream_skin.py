#!/usr/bin/env python3
"""Generate the custom Kodi 21 skin `skin.stream` (Inter typography, Netflix-style Player OSD,
minimal red busy spinner, and dark Stream palette) using Estuary's base system dialogs."""
import math
import os
import shutil
import struct
import subprocess
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'skin.stream')
FONTS_SRC = os.path.join(ROOT, 'tools', 'fonts')
ESTUARY = '/Applications/Kodi.app/Contents/Resources/Kodi/addons/skin.estuary'
MAGICK = shutil.which('magick') or '/opt/homebrew/bin/magick'


def _write_png(path, width, height, pixel):
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend(pixel(x, y))
        rows.append(bytes(row))
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(b''.join(rows), 9)) + chunk(b'IEND', b''))


def _dist_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _dist_box_outline(px, py, x1, y1, x2, y2, r=0.0):
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    hx, hy = (x2 - x1) / 2.0 - r, (y2 - y1) / 2.0 - r
    qx, qy = abs(px - cx) - hx, abs(py - cy) - hy
    sd = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r
    return abs(sd)


def _render_sdf(path, size, dist_fn, half_w=2.8, color=(255, 255, 255)):
    def pixel(x, y):
        hits = 0
        for i in range(4):
            for j in range(4):
                if dist_fn(x + (i + 0.5) / 4.0, y + (j + 0.5) / 4.0) <= half_w:
                    hits += 1
        return (color[0], color[1], color[2], hits * 255 // 16)
    _write_png(path, size, size, pixel)


def render_svg(svg, out_path, width, height=None):
    height = height or width
    with tempfile.NamedTemporaryFile('w', suffix='.svg', delete=False, encoding='utf-8') as f:
        f.write(svg)
        tmp = f.name
    try:
        subprocess.run(
            [MAGICK, '-background', 'none', '-density', '384', tmp,
             '-resize', '{0}x{1}!'.format(width, height), 'PNG32:' + out_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    finally:
        os.unlink(tmp)


def make_osd_assets(media_dir):
    osd = os.path.join(media_dir, 'stream_osd')
    os.makedirs(osd, exist_ok=True)

    # Filled geometric icons (100% solid SVG fills)
    filled_icons = {
        'play.png': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#FFFFFF" d="M6.5 4.2c0-1 1.1-1.6 1.9-1.1l12.2 7.8c.8.5.8 1.7 0 2.2L8.4 20.9c-.8.5-1.9-.1-1.9-1.1V4.2z"/></svg>',
        'pause.png': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect fill="#FFFFFF" x="6" y="4" width="4.2" height="16" rx="1.4"/><rect fill="#FFFFFF" x="13.8" y="4" width="4.2" height="16" rx="1.4"/></svg>',
        'stop.png': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect fill="#FFFFFF" x="5" y="5" width="14" height="14" rx="2.5"/></svg>',
    }
    for name, svg in filled_icons.items():
        render_svg(svg, os.path.join(osd, name), 96)

    # Crisp 4x supersampled SDF stroke icons (96x96)
    # Back arrow
    _render_sdf(os.path.join(osd, 'back.png'), 96,
                lambda x, y: min(_dist_seg(x, y, 22, 48, 76, 48), _dist_seg(x, y, 48, 22, 22, 48), _dist_seg(x, y, 48, 74, 22, 48)), half_w=3.8)

    # Rewind 10s (counter-clockwise arc + arrow + "10")
    def d_rw10(x, y):
        ang = math.degrees(math.atan2(y - 48.0, x - 48.0))
        arc = abs(math.hypot(x - 48.0, y - 48.0) - 32.0) if not (-155 < ang < -75) else 999.0
        arr = min(_dist_seg(x, y, 22, 28, 36, 26), _dist_seg(x, y, 22, 28, 24, 14))
        one = _dist_seg(x, y, 40, 39, 40, 57)
        zero = _dist_box_outline(x, y, 49, 39, 60, 57, r=5.0)
        return min(arc, arr, one, zero)
    _render_sdf(os.path.join(osd, 'rw10.png'), 96, d_rw10, half_w=3.2)

    # Forward 10s (clockwise arc + arrow + "10")
    def d_ff10(x, y):
        ang = math.degrees(math.atan2(y - 48.0, x - 48.0))
        arc = abs(math.hypot(x - 48.0, y - 48.0) - 32.0) if not (-105 < ang < -25) else 999.0
        arr = min(_dist_seg(x, y, 74, 28, 60, 26), _dist_seg(x, y, 74, 28, 72, 14))
        one = _dist_seg(x, y, 40, 39, 40, 57)
        zero = _dist_box_outline(x, y, 49, 39, 60, 57, r=5.0)
        return min(arc, arr, one, zero)
    _render_sdf(os.path.join(osd, 'ff10.png'), 96, d_ff10, half_w=3.2)

    # Subtitles (CC box with text lines)
    def d_subs(x, y):
        box = _dist_box_outline(x, y, 12, 20, 84, 76, r=9.0)
        l1 = _dist_seg(x, y, 24, 52, 44, 52)
        l2 = _dist_seg(x, y, 52, 52, 72, 52)
        l3 = _dist_seg(x, y, 24, 64, 38, 64)
        l4 = _dist_seg(x, y, 46, 64, 72, 64)
        return min(box, l1, l2, l3, l4)
    _render_sdf(os.path.join(osd, 'subtitles.png'), 96, d_subs, half_w=3.4)

    # Audio (speaker + sound waves)
    def d_audio(x, y):
        spk = min(
            _dist_seg(x, y, 16, 38, 30, 38), _dist_seg(x, y, 16, 58, 30, 58), _dist_seg(x, y, 16, 38, 16, 58),
            _dist_seg(x, y, 30, 38, 48, 22), _dist_seg(x, y, 30, 58, 48, 74), _dist_seg(x, y, 48, 22, 48, 74),
        )
        ang = math.degrees(math.atan2(y - 48.0, x - 48.0))
        w1 = abs(math.hypot(x - 48.0, y - 48.0) - 18.0) if -48 <= ang <= 48 else 999.0
        w2 = abs(math.hypot(x - 48.0, y - 48.0) - 30.0) if -55 <= ang <= 55 else 999.0
        return min(spk, w1, w2)
    _render_sdf(os.path.join(osd, 'audio.png'), 96, d_audio, half_w=3.4)

    # Settings (crisp 8-tooth gear)
    def d_gear(x, y):
        r = math.hypot(x - 48.0, y - 48.0)
        inner = abs(r - 12.0)
        outer = abs(r - 24.0)
        teeth = 999.0
        for k in range(8):
            a = math.radians(k * 45.0)
            teeth = min(teeth, _dist_seg(x, y, 48 + 24 * math.cos(a), 48 + 24 * math.sin(a), 48 + 33 * math.cos(a), 48 + 33 * math.sin(a)))
        return min(inner, outer, teeth)
    _render_sdf(os.path.join(osd, 'settings.png'), 96, d_gear, half_w=3.4)

    # Netflix-red 270-degree spinner ring (128x128)
    def spinner_pixel(x, y):
        a_sum = 0
        for i in range(4):
            for j in range(4):
                sx, sy = x + (i + 0.5) / 4.0, y + (j + 0.5) / 4.0
                r = math.hypot(sx - 64.0, sy - 64.0)
                if abs(r - 48.0) <= 5.5:
                    ang = (math.degrees(math.atan2(sy - 64.0, sx - 64.0)) + 360.0) % 360.0
                    a_sum += 255 if ang <= 270.0 else 48
        return (0xE5, 0x09, 0x14, a_sum // 16)
    _write_png(os.path.join(osd, 'spinner_ring.png'), 128, 128, spinner_pixel)

    # Circles & pills for OSD transport controls
    render_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="48" fill="#FFFFFF"/></svg>',
               os.path.join(osd, 'circle_white.png'), 128)
    render_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="48" fill="#FFFFFF" fill-opacity="0.16" stroke="#FFFFFF" stroke-opacity="0.22" stroke-width="2"/></svg>',
               os.path.join(osd, 'circle_dark.png'), 128)
    render_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 64"><rect x="2" y="2" width="196" height="60" rx="30" fill="#FFFFFF"/></svg>',
               os.path.join(osd, 'pill_white.png'), 200, 64)
    render_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 64"><rect x="2" y="2" width="196" height="60" rx="30" fill="#FFFFFF" fill-opacity="0.14" stroke="#FFFFFF" stroke-opacity="0.20" stroke-width="2"/></svg>',
               os.path.join(osd, 'pill_dark.png'), 200, 64)

    # Seekbar textures (sleek Netflix Red)
    render_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 12"><rect x="0" y="2" width="40" height="8" rx="4" fill="#FFFFFF" fill-opacity="0.26"/></svg>',
               os.path.join(osd, 'bar_bg.png'), 40, 12)
    render_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 12"><rect x="0" y="2" width="40" height="8" rx="4" fill="#FFFFFF" fill-opacity="0.45"/></svg>',
               os.path.join(osd, 'bar_cache.png'), 40, 12)
    render_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 12"><rect x="0" y="2" width="40" height="8" rx="4" fill="#E50914"/></svg>',
               os.path.join(osd, 'bar_red.png'), 40, 12)
    render_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="11" fill="#E50914" stroke="#FFFFFF" stroke-width="3"/></svg>',
               os.path.join(osd, 'knob_red.png'), 32, 32)

    # Smooth cinema gradients for top & bottom player vignettes
    render_svg('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 240">
        <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#000000" stop-opacity="0.88"/>
            <stop offset="60%" stop-color="#000000" stop-opacity="0.35"/>
            <stop offset="100%" stop-color="#000000" stop-opacity="0"/>
        </linearGradient></defs><rect width="16" height="240" fill="url(#g)"/></svg>''',
        os.path.join(osd, 'fade_top.png'), 16, 240)
    render_svg('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 360">
        <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#000000" stop-opacity="0"/>
            <stop offset="40%" stop-color="#000000" stop-opacity="0.55"/>
            <stop offset="100%" stop-color="#000000" stop-opacity="0.94"/>
        </linearGradient></defs><rect width="16" height="360" fill="url(#g)"/></svg>''',
        os.path.join(osd, 'fade_bottom.png'), 16, 360)


ADDON_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<addon id="skin.stream" version="1.0.1" name="Stream" provider-name="yeshvantbhavnasi">
	<requires>
		<import addon="xbmc.gui" version="5.17.0"/>
	</requires>
	<extension point="xbmc.gui.skin" debugging="false">
		<res width="1920" height="1440" aspect="4:3" default="false" folder="xml" />
		<res width="1920" height="1280" aspect="3:2" default="false" folder="xml" />
		<res width="1920" height="1200" aspect="16:10" default="false" folder="xml" />
		<res width="2040" height="1080" aspect="17:9" default="false" folder="xml" />
		<res width="1920" height="1080" aspect="16:9" default="true" folder="xml" />
		<res width="2560" height="1080" aspect="21:9" default="false" folder="xml" />
		<res width="2338" height="1080" aspect="19.5:9" default="false" folder="xml" />
		<res width="2160" height="1080" aspect="18:9" default="false" folder="xml" />
	</extension>
	<extension point="xbmc.addon.metadata">
		<platform>all</platform>
		<summary lang="en_GB">Modern Netflix &amp; YouTube TV theme for Stream (Inter typography, custom Player OSD, dark cinema UI)</summary>
		<description lang="en_GB">Companion skin for the Stream add-on providing Inter typography, a clean Netflix-style video player OSD and seekbar, minimal red loading spinner, and a dark cinema palette across all Kodi dialogs.</description>
		<assets>
			<icon>resources/icon.png</icon>
			<fanart>resources/fanart.jpg</fanart>
		</assets>
	</extension>
</addon>
'''

COLORS_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<colors>
	<color name="primary_background">FF0F0F0F</color>
	<color name="secondary_background">FF181818</color>
	<color name="dialog_tint">F2141414</color>
	<color name="background">FF0B0B0B</color>
	<color name="bg_image">FF909090</color>
	<color name="bg_overlay">24FFFFFF</color>
	<color name="black">FF000000</color>
	<color name="white">FFF5F5F5</color>
	<color name="grey">FFA8A8A8</color>
	<color name="blue">FFE50914</color>
	<color name="red">FFE50914</color>
	<color name="button_focus">FFE50914</color>
	<color name="button_alt_focus">80E50914</color>
	<color name="text_shadow">33000000</color>
	<color name="border_alpha">45FFFFFF</color>
	<color name="disabled">40FFFFFF</color>
	<color name="selected">FFE50914</color>
	<color name="invalid">FFFF3B30</color>
</colors>
'''

FONT_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<fonts>
	<fontset id="Default" idloc="15109">
		<!-- Modern Inter Typography -->
		<font><name>font10</name><filename>Inter-Medium.ttf</filename><size>22</size></font>
		<font><name>font12</name><filename>Inter-Regular.ttf</filename><size>25</size></font>
		<font><name>font13</name><filename>Inter-Medium.ttf</filename><size>29</size></font>
		<font><name>font14</name><filename>Inter-Medium.ttf</filename><size>33</size></font>
		<font><name>font23_narrow</name><filename>Inter-Regular.ttf</filename><size>23</size><linespacing>0.85</linespacing></font>
		<font><name>font25_narrow</name><filename>Inter-Regular.ttf</filename><size>25</size><linespacing>0.85</linespacing></font>
		<font><name>font27</name><filename>Inter-Medium.ttf</filename><size>27</size></font>
		<font><name>font27_narrow</name><filename>Inter-Regular.ttf</filename><size>27</size><linespacing>0.85</linespacing></font>
		<font><name>font32</name><filename>Inter-Medium.ttf</filename><size>32</size></font>
		<font><name>font37</name><filename>Inter-SemiBold.ttf</filename><size>37</size></font>
		<font><name>font45</name><filename>Inter-Bold.ttf</filename><size>45</size></font>
		<font><name>font60</name><filename>Inter-Black.ttf</filename><size>62</size></font>
		<font><name>font_clock</name><filename>Inter-SemiBold.ttf</filename><size>56</size></font>
		<!-- Title & Heading Fonts -->
		<font><name>font_flag</name><filename>Inter-Bold.ttf</filename><size>18</size></font>
		<font><name>font20_title</name><filename>Inter-SemiBold.ttf</filename><size>21</size></font>
		<font><name>font25_title</name><filename>Inter-SemiBold.ttf</filename><size>26</size></font>
		<font><name>font30_title</name><filename>Inter-Bold.ttf</filename><size>31</size></font>
		<font><name>font32_title</name><filename>Inter-Bold.ttf</filename><size>33</size></font>
		<font><name>font36_title</name><filename>Inter-Bold.ttf</filename><size>38</size></font>
		<font><name>font40_title</name><filename>Inter-Bold.ttf</filename><size>42</size></font>
		<font><name>font45_title</name><filename>Inter-ExtraBold.ttf</filename><size>46</size></font>
		<font><name>font52_title</name><filename>Inter-ExtraBold.ttf</filename><size>54</size></font>
		<font><name>font_MainMenu</name><filename>Inter-Bold.ttf</filename><size>56</size></font>
		<font><name>WeatherTemp</name><aspect>0.88</aspect><filename>Inter-Bold.ttf</filename><size>112</size></font>
		<font><name>Mono26</name><filename>Inter-Medium.ttf</filename><size>26</size></font>
	</fontset>
</fonts>
'''

DIALOG_BUSY_XML = '''<?xml version="1.0" encoding="utf-8"?>
<window>
	<animation effect="fade" start="0" end="100" time="220">WindowOpen</animation>
	<animation effect="fade" start="100" end="0" time="160">WindowClose</animation>
	<controls>
		<control type="group">
			<visible>!Window.IsActive(startup) + !Player.Caching</visible>
			<control type="image">
				<left>0</left><top>0</top><width>1920</width><height>1080</height>
				<texture>colors/black.png</texture>
				<colordiffuse>55000000</colordiffuse>
				<visible>!Window.IsVisible(fullscreenvideo)</visible>
			</control>
			<control type="image">
				<centerleft>50%</centerleft>
				<centertop>50%</centertop>
				<width>76</width>
				<height>76</height>
				<aspectratio>keep</aspectratio>
				<texture>stream_osd/spinner_ring.png</texture>
				<animation effect="rotate" start="0" end="-360" center="auto" time="900" loop="true" reversible="false" condition="true">Conditional</animation>
			</control>
		</control>
	</controls>
</window>
'''

DIALOG_SEEKBAR_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<window>
	<visible>Player.Seeking | Player.DisplayAfterSeek | [Player.Paused + !Player.Caching] | Player.Forwarding | Player.Rewinding | Player.ShowInfo | Window.IsActive(videoosd)</visible>
	<visible>Window.IsActive(fullscreenvideo)</visible>
	<visible>!Window.IsActive(sliderdialog) + !Window.IsActive(osdvideosettings) + !Window.IsActive(osdaudiosettings) + !Window.IsActive(osdsubtitlesettings)</visible>
	<animation effect="fade" start="0" end="100" time="180">VisibleChange</animation>
	<controls>
		<!-- Top & bottom cinema vignettes -->
		<control type="image">
			<left>0</left><top>0</top><width>1920</width><height>240</height>
			<texture>stream_osd/fade_top.png</texture>
		</control>
		<control type="image">
			<left>0</left><top>700</top><width>1920</width><height>380</height>
			<texture>stream_osd/fade_bottom.png</texture>
		</control>

		<!-- Top Header: Title + Show/Episode + Clock -->
		<control type="group">
			<left>72</left><top>44</top>
			<control type="label">
				<left>0</left><top>0</top><width>1320</width><height>52</height>
				<font>font40_title</font>
				<textcolor>FFFFFFFF</textcolor>
				<shadowcolor>88000000</shadowcolor>
				<label>$INFO[VideoPlayer.Title]</label>
			</control>
			<control type="label">
				<left>0</left><top>54</top><width>1320</width><height>34</height>
				<font>font13</font>
				<textcolor>FFB8B8C0</textcolor>
				<label>$INFO[VideoPlayer.TVShowTitle]$INFO[VideoPlayer.Season,  ·  S]$INFO[VideoPlayer.Episode,E]$INFO[VideoPlayer.Year,  ·  ]</label>
				<visible>!String.IsEmpty(VideoPlayer.TVShowTitle) | !String.IsEmpty(VideoPlayer.Year)</visible>
			</control>
		</control>

		<!-- Top Right: Current time & finish time -->
		<control type="group">
			<right>72</right><top>48</top><width>360</width><height>80</height>
			<control type="label">
				<left>0</left><top>0</top><width>360</width><height>40</height>
				<align>right</align>
				<font>font30_title</font>
				<textcolor>FFFFFFFF</textcolor>
				<label>$INFO[System.Time]</label>
			</control>
			<control type="label">
				<left>0</left><top>42</top><width>360</width><height>28</height>
				<align>right</align>
				<font>font12</font>
				<textcolor>FFAAAAAA</textcolor>
				<label>Ends at $INFO[Player.FinishTime]</label>
				<visible>!String.IsEmpty(Player.FinishTime)</visible>
			</control>
		</control>

		<!-- Center Status Badge (Paused / Buffering / Seeking) -->
		<control type="group">
			<centerleft>50%</centerleft><centertop>48%</centertop><width>120</width><height>120</height>
			<visible>Player.Paused + !Window.IsActive(videoosd)</visible>
			<control type="image">
				<left>0</left><top>0</top><width>120</width><height>120</height>
				<texture>stream_osd/circle_dark.png</texture>
			</control>
			<control type="image">
				<left>34</left><top>34</top><width>52</width><height>52</height>
				<aspectratio>keep</aspectratio>
				<texture>stream_osd/pause.png</texture>
			</control>
		</control>

		<!-- Sleek Netflix Seekbar & Timestamps -->
		<control type="group">
			<left>72</left><top>886</top><width>1776</width><height>72</height>
			<control type="label">
				<left>0</left><top>0</top><width>160</width><height>32</height>
				<font>font12</font>
				<textcolor>FFE5E5E5</textcolor>
				<label>$INFO[Player.Time]</label>
			</control>
			<control type="label">
				<right>0</right><top>0</top><width>180</width><height>32</height>
				<align>right</align>
				<font>font12</font>
				<textcolor>FFB8B8C0</textcolor>
				<label>-$INFO[Player.TimeRemaining]</label>
			</control>
			<!-- Buffer cache bar -->
			<control type="progress">
				<left>0</left><top>40</top><width>1776</width><height>10</height>
				<info>Player.ProgressCache</info>
				<texturebg border="4">stream_osd/bar_bg.png</texturebg>
				<midtexture border="4">stream_osd/bar_cache.png</midtexture>
			</control>
			<!-- Netflix Red progress bar -->
			<control type="progress" id="23">
				<left>0</left><top>40</top><width>1776</width><height>10</height>
				<info>Player.Progress</info>
				<texturebg colordiffuse="00FFFFFF">stream_osd/bar_bg.png</texturebg>
				<midtexture border="4">stream_osd/bar_red.png</midtexture>
			</control>
			<!-- Interactive seek slider when OSD is open -->
			<control type="slider" id="401">
				<left>0</left><top>29</top><width>1776</width><height>32</height>
				<texturesliderbar colordiffuse="00FFFFFF">stream_osd/bar_bg.png</texturesliderbar>
				<textureslidernib>stream_osd/knob_red.png</textureslidernib>
				<textureslidernibfocus>stream_osd/knob_red.png</textureslidernibfocus>
				<action>seek</action>
				<visible>Player.SeekEnabled</visible>
			</control>
		</control>
	</controls>
</window>
'''

VIDEO_OSD_XML = '''<?xml version="1.0" encoding="utf-8"?>
<window>
	<onload condition="!Player.PauseEnabled">SetFocus(603)</onload>
	<defaultcontrol always="true">602</defaultcontrol>
	<animation effect="fade" start="0" end="100" time="160">WindowOpen</animation>
	<animation effect="fade" start="100" end="0" time="140">WindowClose</animation>
	<controls>
		<!-- Bottom Transport & Utility Bar -->
		<control type="group" id="200">
			<left>72</left><top>962</top><width>1776</width><height>84</height>
			<visible>![Window.IsVisible(SliderDialog) | Window.IsVisible(osdaudiosettings) | Window.IsVisible(osdvideosettings) | Window.IsVisible(osdsubtitlesettings)]</visible>

			<!-- Left: Back button -->
			<control type="button" id="600">
				<left>0</left><top>10</top><width>64</width><height>64</height>
				<texturefocus>stream_osd/circle_white.png</texturefocus>
				<texturenofocus>stream_osd/circle_dark.png</texturenofocus>
				<onclick>Dialog.Close(videoosd)</onclick>
				<onright>601</onright>
			</control>
			<control type="image">
				<left>18</left><top>28</top><width>28</width><height>28</height>
				<aspectratio>keep</aspectratio>
				<texture colordiffuse="FF141414">stream_osd/back.png</texture>
				<visible>Control.HasFocus(600)</visible>
			</control>
			<control type="image">
				<left>18</left><top>28</top><width>28</width><height>28</height>
				<aspectratio>keep</aspectratio>
				<texture>stream_osd/back.png</texture>
				<visible>!Control.HasFocus(600)</visible>
			</control>

			<!-- Center Transport: Rewind 10s, Play/Pause, Forward 10s -->
			<control type="group">
				<left>756</left><top>0</top><width>264</width><height>84</height>

				<!-- Rewind 10s -->
				<control type="button" id="601">
					<left>0</left><top>10</top><width>64</width><height>64</height>
					<texturefocus>stream_osd/circle_white.png</texturefocus>
					<texturenofocus>stream_osd/circle_dark.png</texturenofocus>
					<onclick>Seek(-10)</onclick>
					<onleft>600</onleft>
					<onright>602</onright>
				</control>
				<control type="image">
					<left>16</left><top>26</top><width>32</width><height>32</height>
					<aspectratio>keep</aspectratio>
					<texture colordiffuse="FF141414">stream_osd/rw10.png</texture>
					<visible>Control.HasFocus(601)</visible>
				</control>
				<control type="image">
					<left>16</left><top>26</top><width>32</width><height>32</height>
					<aspectratio>keep</aspectratio>
					<texture>stream_osd/rw10.png</texture>
					<visible>!Control.HasFocus(601)</visible>
				</control>

				<!-- Play / Pause (prominent center button) -->
				<control type="button" id="602">
					<left>94</left><top>4</top><width>76</width><height>76</height>
					<texturefocus>stream_osd/circle_white.png</texturefocus>
					<texturenofocus>stream_osd/circle_dark.png</texturenofocus>
					<onclick>PlayerControl(Play)</onclick>
					<onleft>601</onleft>
					<onright>603</onright>
				</control>
				<control type="image">
					<left>114</left><top>24</top><width>36</width><height>36</height>
					<aspectratio>keep</aspectratio>
					<texture colordiffuse="FF141414">stream_osd/play.png</texture>
					<visible>Player.Paused + Control.HasFocus(602)</visible>
				</control>
				<control type="image">
					<left>114</left><top>24</top><width>36</width><height>36</height>
					<aspectratio>keep</aspectratio>
					<texture>stream_osd/play.png</texture>
					<visible>Player.Paused + !Control.HasFocus(602)</visible>
				</control>
				<control type="image">
					<left>114</left><top>24</top><width>36</width><height>36</height>
					<aspectratio>keep</aspectratio>
					<texture colordiffuse="FF141414">stream_osd/pause.png</texture>
					<visible>!Player.Paused + Control.HasFocus(602)</visible>
				</control>
				<control type="image">
					<left>114</left><top>24</top><width>36</width><height>36</height>
					<aspectratio>keep</aspectratio>
					<texture>stream_osd/pause.png</texture>
					<visible>!Player.Paused + !Control.HasFocus(602)</visible>
				</control>

				<!-- Forward 10s -->
				<control type="button" id="603">
					<left>200</left><top>10</top><width>64</width><height>64</height>
					<texturefocus>stream_osd/circle_white.png</texturefocus>
					<texturenofocus>stream_osd/circle_dark.png</texturenofocus>
					<onclick>Seek(10)</onclick>
					<onleft>602</onleft>
					<onright>604</onright>
				</control>
				<control type="image">
					<left>216</left><top>26</top><width>32</width><height>32</height>
					<aspectratio>keep</aspectratio>
					<texture colordiffuse="FF141414">stream_osd/ff10.png</texture>
					<visible>Control.HasFocus(603)</visible>
				</control>
				<control type="image">
					<left>216</left><top>26</top><width>32</width><height>32</height>
					<aspectratio>keep</aspectratio>
					<texture>stream_osd/ff10.png</texture>
					<visible>!Control.HasFocus(603)</visible>
				</control>
			</control>

			<!-- Right Utility Controls: Subtitles, Audio, Video Settings, Stop -->
			<control type="group">
				<right>0</right><top>10</top><width>316</width><height>64</height>

				<!-- Subtitles -->
				<control type="button" id="604">
					<left>0</left><top>0</top><width>64</width><height>64</height>
					<texturefocus>stream_osd/circle_white.png</texturefocus>
					<texturenofocus>stream_osd/circle_dark.png</texturenofocus>
					<onclick>ActivateWindow(osdsubtitlesettings)</onclick>
					<onleft>603</onleft>
					<onright>605</onright>
				</control>
				<control type="image">
					<left>17</left><top>17</top><width>30</width><height>30</height>
					<aspectratio>keep</aspectratio>
					<texture colordiffuse="FF141414">stream_osd/subtitles.png</texture>
					<visible>Control.HasFocus(604)</visible>
				</control>
				<control type="image">
					<left>17</left><top>17</top><width>30</width><height>30</height>
					<aspectratio>keep</aspectratio>
					<texture>stream_osd/subtitles.png</texture>
					<visible>!Control.HasFocus(604)</visible>
				</control>

				<!-- Audio -->
				<control type="button" id="605">
					<left>84</left><top>0</top><width>64</width><height>64</height>
					<texturefocus>stream_osd/circle_white.png</texturefocus>
					<texturenofocus>stream_osd/circle_dark.png</texturenofocus>
					<onclick>ActivateWindow(osdaudiosettings)</onclick>
					<onleft>604</onleft>
					<onright>606</onright>
				</control>
				<control type="image">
					<left>101</left><top>17</top><width>30</width><height>30</height>
					<aspectratio>keep</aspectratio>
					<texture colordiffuse="FF141414">stream_osd/audio.png</texture>
					<visible>Control.HasFocus(605)</visible>
				</control>
				<control type="image">
					<left>101</left><top>17</top><width>30</width><height>30</height>
					<aspectratio>keep</aspectratio>
					<texture>stream_osd/audio.png</texture>
					<visible>!Control.HasFocus(605)</visible>
				</control>

				<!-- Video Settings -->
				<control type="button" id="606">
					<left>168</left><top>0</top><width>64</width><height>64</height>
					<texturefocus>stream_osd/circle_white.png</texturefocus>
					<texturenofocus>stream_osd/circle_dark.png</texturenofocus>
					<onclick>ActivateWindow(osdvideosettings)</onclick>
					<onleft>605</onleft>
					<onright>607</onright>
				</control>
				<control type="image">
					<left>185</left><top>17</top><width>30</width><height>30</height>
					<aspectratio>keep</aspectratio>
					<texture colordiffuse="FF141414">stream_osd/settings.png</texture>
					<visible>Control.HasFocus(606)</visible>
				</control>
				<control type="image">
					<left>185</left><top>17</top><width>30</width><height>30</height>
					<aspectratio>keep</aspectratio>
					<texture>stream_osd/settings.png</texture>
					<visible>!Control.HasFocus(606)</visible>
				</control>

				<!-- Stop -->
				<control type="button" id="607">
					<left>252</left><top>0</top><width>64</width><height>64</height>
					<texturefocus>stream_osd/circle_white.png</texturefocus>
					<texturenofocus>stream_osd/circle_dark.png</texturenofocus>
					<onclick>PlayerControl(Stop)</onclick>
					<onleft>606</onleft>
				</control>
				<control type="image">
					<left>270</left><top>18</top><width>28</width><height>28</height>
					<aspectratio>keep</aspectratio>
					<texture colordiffuse="FF141414">stream_osd/stop.png</texture>
					<visible>Control.HasFocus(607)</visible>
				</control>
				<control type="image">
					<left>270</left><top>18</top><width>28</width><height>28</height>
					<aspectratio>keep</aspectratio>
					<texture>stream_osd/stop.png</texture>
					<visible>!Control.HasFocus(607)</visible>
				</control>
			</control>
		</control>
	</controls>
</window>
'''


def build_skin():
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    # Copy Kodi 21 Estuary base so every fallback Kodi dialog/settings screen works seamlessly
    for sub in ('xml', 'media', 'extras', 'playlists'):
        shutil.copytree(os.path.join(ESTUARY, sub), os.path.join(OUT, sub))
    os.makedirs(os.path.join(OUT, 'language'), exist_ok=True)
    shutil.copytree(
        os.path.join(ESTUARY, 'language', 'resource.language.en_gb'),
        os.path.join(OUT, 'language', 'resource.language.en_gb'),
    )
    os.makedirs(os.path.join(OUT, 'colors'), exist_ok=True)
    os.makedirs(os.path.join(OUT, 'fonts'), exist_ok=True)
    os.makedirs(os.path.join(OUT, 'resources'), exist_ok=True)

    # Copy Inter static TTFs into skin.stream/fonts/
    for fn in os.listdir(FONTS_SRC):
        if fn.endswith('.ttf'):
            shutil.copy2(os.path.join(FONTS_SRC, fn), os.path.join(OUT, 'fonts', fn))

    # Copy Stream icon & fanart
    shutil.copy2(os.path.join(ROOT, 'script.stream', 'resources', 'icon-s.png'), os.path.join(OUT, 'resources', 'icon.png'))
    shutil.copy2(os.path.join(ROOT, 'script.stream', 'resources', 'fanart-stream.jpg'), os.path.join(OUT, 'resources', 'fanart.jpg'))

    # Render crisp vector OSD & spinner PNG assets
    make_osd_assets(os.path.join(OUT, 'media'))

    # Write Stream skin overrides
    open(os.path.join(OUT, 'addon.xml'), 'w', encoding='utf-8').write(ADDON_XML)
    open(os.path.join(OUT, 'colors', 'defaults.xml'), 'w', encoding='utf-8').write(COLORS_XML)
    open(os.path.join(OUT, 'xml', 'Font.xml'), 'w', encoding='utf-8').write(FONT_XML)
    open(os.path.join(OUT, 'xml', 'DialogBusy.xml'), 'w', encoding='utf-8').write(DIALOG_BUSY_XML)
    open(os.path.join(OUT, 'xml', 'DialogSeekBar.xml'), 'w', encoding='utf-8').write(DIALOG_SEEKBAR_XML)
    open(os.path.join(OUT, 'xml', 'VideoOSD.xml'), 'w', encoding='utf-8').write(VIDEO_OSD_XML)

    print('Built skin.stream at', OUT)


if __name__ == '__main__':
    build_skin()
