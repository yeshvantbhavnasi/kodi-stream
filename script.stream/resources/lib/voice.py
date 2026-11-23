# Voice search bridge + pure-Python QR code PNG generator (zero external dependencies).
# Connects Kodi's Search screen to Stream's HTTPS voice remote (https://recs.senapati.si/voice/<code>),
# so any phone or browser in the room can dictate in English, Telugu, or Hindi directly into the TV.
import os
import struct
import zlib

import requests
import xbmcaddon
import xbmcvfs

USER_AGENT = 'StreamKodi/1.8 (VoiceSearch)'


def _addon():
    return xbmcaddon.Addon('script.stream')


def service_url():
    return (_addon().getSetting('recs_url') or 'https://recs.senapati.si').strip().rstrip('/')


def _headers():
    return {
        'X-Stream-Key': _addon().getSetting('recs_key').strip(),
        'User-Agent': USER_AGENT,
    }


def start_session():
    """Create a short-lived voice session on the recs service. Returns (code, url, qr_path) or raises."""
    base = service_url()
    if not base:
        raise RuntimeError('Recommendation service URL is not configured')
    r = requests.post(base + '/voice/session', json={}, headers=_headers(), timeout=10)
    r.raise_for_status()
    data = r.json()
    code = data['code']
    url = data.get('url') or (base + '/voice/' + code)
    profile_dir = xbmcvfs.translatePath(_addon().getAddonInfo('profile'))
    os.makedirs(profile_dir, exist_ok=True)
    qr_path = os.path.join(profile_dir, 'voice_qr_{0}.png'.format(code))
    make_qr_png(url, qr_path, scale=6, border=3)
    return code, url, qr_path


def poll_session(code):
    """Poll the voice session for live/final transcript. Returns dict or None on error."""
    base = service_url()
    if not base or not code:
        return None
    r = requests.get(base + '/voice/poll', params={'code': code}, headers=_headers(), timeout=6)
    if r.status_code != 200:
        return None
    return r.json()


# ---------- Pure-Python QR Code (Byte mode, Level L, Versions 1-6) ----------

# (total_codewords, ec_codewords_per_block, data_codewords) for Level L (1 block for V1-V5, 2 blocks for V6)
_VER_SPEC = {
    1: (26, 7, 19, []),
    2: (44, 10, 34, [6, 18]),
    3: (70, 15, 55, [6, 22]),
    4: (100, 20, 80, [6, 26]),
    5: (134, 26, 108, [6, 30]),
}

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_gen(n):
    g = [1]
    for i in range(n):
        nxt = [0] * (len(g) + 1)
        a = _GF_EXP[i]
        for j, c in enumerate(g):
            nxt[j] ^= c
            nxt[j + 1] ^= _gf_mul(c, a)
        g = nxt
    return g


def _rs_encode(data, ec_len):
    gen = _rs_gen(ec_len)
    rem = [0] * ec_len
    for b in data:
        factor = b ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            for j in range(ec_len):
                rem[j] ^= _gf_mul(gen[j + 1], factor)
    return rem


def _format_bits(ec_bits, mask):
    # Level L = 01
    data = (ec_bits << 3) | mask
    rem = data << 10
    for i in range(14, 9, -1):
        if rem & (1 << i):
            rem ^= 0x537 << (i - 10)
    return ((data << 10) | rem) ^ 0x5412


def _encode_qr(text):
    raw = text.encode('utf-8')
    ver = None
    for v in sorted(_VER_SPEC):
        # Byte mode header: 4 bits mode + 8 bits length = 12 bits; +4 terminator -> 2 bytes overhead
        if len(raw) + 2 <= _VER_SPEC[v][2]:
            ver = v
            break
    if ver is None:
        raise ValueError('QR payload too long: %d bytes' % len(raw))
    total_cw, ec_cw, data_cw, align = _VER_SPEC[ver]
    bits = []

    def push(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    push(0b0100, 4)        # byte mode
    push(len(raw), 8)      # char count (V1-V9)
    for b in raw:
        push(b, 8)
    push(0, min(4, data_cw * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    codewords = []
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        codewords.append(v)
    pad = (0xEC, 0x11)
    pi = 0
    while len(codewords) < data_cw:
        codewords.append(pad[pi % 2])
        pi += 1
    codewords += _rs_encode(codewords, ec_cw)

    size = 17 + 4 * ver
    grid = [[None] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def set_func(r, c, val):
        if 0 <= r < size and 0 <= c < size:
            grid[r][c] = 1 if val else 0
            func[r][c] = True

    def finder(r0, c0):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= c < size:
                    on = (0 <= dr <= 6 and 0 <= dc <= 6 and
                          (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)))
                    set_func(r, c, on)

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # Alignment patterns
    for r0 in align:
        for c0 in align:
            if func[r0][c0]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    on = max(abs(dr), abs(dc)) != 1
                    set_func(r0 + dr, c0 + dc, on)

    # Timing patterns
    for i in range(8, size - 8):
        if not func[6][i]:
            set_func(6, i, i % 2 == 0)
        if not func[i][6]:
            set_func(i, 6, i % 2 == 0)

    # Reserve format info + dark module
    for i in range(9):
        if not func[8][i]:
            set_func(8, i, False)
        if not func[i][8]:
            set_func(i, 8, False)
    for i in range(8):
        set_func(8, size - 1 - i, False)
        set_func(size - 1 - i, 8, False)
    set_func(size - 8, 8, True)

    # Place data bits
    data_bits = [(cw >> i) & 1 for cw in codewords for i in range(7, -1, -1)]
    bi = 0
    up = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not func[r][c]:
                    grid[r][c] = data_bits[bi] if bi < len(data_bits) else 0
                    bi += 1
        up = not up
        col -= 2

    # Apply mask 0 ((r + c) % 2 == 0)
    mask = 0
    for r in range(size):
        for c in range(size):
            if not func[r][c] and ((r + c) % 2 == 0):
                grid[r][c] ^= 1

    # Write format bits (Level L = 01)
    fmt = _format_bits(0b01, mask)
    for i in range(6):
        b = (fmt >> i) & 1
        grid[i][8] = b
        grid[8][size - 1 - i] = b
    grid[7][8] = (fmt >> 6) & 1
    grid[8][size - 7] = (fmt >> 6) & 1
    grid[8][8] = (fmt >> 7) & 1
    grid[8][size - 8] = (fmt >> 7) & 1
    grid[8][7] = (fmt >> 8) & 1
    grid[size - 7][8] = (fmt >> 8) & 1
    for i in range(9, 15):
        b = (fmt >> i) & 1
        grid[8][14 - i] = b
        grid[size - 15 + i][8] = b

    return grid


def make_qr_png(text, path, scale=6, border=3):
    """Write a crisp black-on-white QR code PNG for `text` to `path`."""
    grid = _encode_qr(text)
    n = len(grid)
    w = (n + border * 2) * scale
    raw = bytearray()
    white = b'\xff\xff\xff\xff'
    black = b'\x14\x14\x14\xff'
    for py in range(w):
        raw.append(0)
        gy = py // scale - border
        for px in range(w):
            gx = px // scale - border
            dark = 0 <= gy < n and 0 <= gx < n and grid[gy][gx] == 1
            raw.extend(black if dark else white)

    def chunk(kind, data):
        body = kind + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)

    out = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, w, 8, 6, 0, 0, 0))
    out += chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(out)
    return path
