#!/usr/bin/env python3
"""
ccpatch.py - extract / inject / patch members of a Might & Magic: World of Xeen
.CC archive (CC file format, see https://xeen.fandom.com/wiki/CC_File_Format,
mirrors ScummVM engines/mm/shared/xeen/cc_archive.cpp).

Index: u16 count, then count*8 bytes encrypted with rotate-left-2 + running seed
(0xAC, +0x67 per byte). Entry = id(2 LE) offset(3 LE) size(2 LE) 0(1).
Member data is XOR-0x35 when the archive is "encoded" (XEEN.CC / INTRO.CC are).

Primary use: fix the World of Xeen MT-32 driver (ROLMUS, id 0x5084) fade bug —
the first volume ramp uses an uninitialised BX as `[bx+014D]`; we rewrite it to
the fixed channel-9 slot `[volbase+9]`. Length-preserving, so it's an in-place
byte patch (no repack, index unchanged).
"""
import sys, os, struct, argparse

XOR = 0x35

def decrypt_index(raw, count):
    raw = bytearray(raw)
    seed = 0xAC
    for i in range(count * 8):
        b = raw[i]
        raw[i] = (((b << 2) | (b >> 6)) + seed) & 0xFF
        seed = (seed + 0x67) & 0xFF
    return raw

def encrypt_index(plain):
    out = bytearray(len(plain))
    seed = 0xAC
    for i in range(len(plain)):
        b = (plain[i] - seed) & 0xFF
        out[i] = ((b >> 2) | (b << 6)) & 0xFF
        seed = (seed + 0x67) & 0xFF
    return out

def load(path):
    data = bytearray(open(path, 'rb').read())
    count = data[0] | (data[1] << 8)
    idx = decrypt_index(data[2:2 + count * 8], count)
    entries = []
    for k in range(count):
        e = idx[k * 8:k * 8 + 8]
        entries.append({
            'id':   e[0] | (e[1] << 8),
            'off':  e[2] | (e[3] << 8) | (e[4] << 16),
            'size': e[5] | (e[6] << 8),
            'pad':  e[7],
        })
    return data, count, entries

def find(entries, mid):
    hits = [e for e in entries if e['id'] == mid]
    if not hits:
        sys.exit(f"id 0x{mid:04X} not found")
    if len(hits) > 1:
        print(f"warning: {len(hits)} entries with id 0x{mid:04X}; using first", file=sys.stderr)
    return hits[0]

def member_bytes(data, ent, decode=True):
    blob = bytes(data[ent['off']:ent['off'] + ent['size']])
    if decode:
        blob = bytes(b ^ XOR for b in blob)
    return blob

# ---- ROLMUS fade-bug locator -------------------------------------------------
# The buggy "first ramp" is:  cmp byte [bx+disp],28 ; jl rel8 ; dec byte [bx+disp]
#   80 BF <disp16> 28   7C <rel8>   FE 8F <disp16>
# The SECOND ramp has the identical opcode pattern but is immediately preceded by
# `mov bx,0006` (BB 06 00) and is the legitimate one. We pick the occurrence that
# is NOT preceded by a bx-load and whose volume CC is sent on channel 9 (B4 B9).
def find_fade_bug(drv):
    cands = []
    i = 0
    n = len(drv)
    while i + 11 <= n:
        if (drv[i] == 0x80 and drv[i+1] == 0xBF and drv[i+4] == 0x28 and
                drv[i+5] == 0x7C and drv[i+7] == 0xFE and drv[i+8] == 0x8F and
                drv[i+2] == drv[i+9] and drv[i+3] == drv[i+10]):
            disp = drv[i+2] | (drv[i+3] << 8)
            # preceded by a bx-load?  BB xx xx (mov bx,imm16) ending at i, or 33 DB
            prev3 = drv[i-3:i]
            prev2 = drv[i-2:i]
            bx_loaded = (len(prev3) == 3 and prev3[0] == 0xBB) or (len(prev2) == 2 and prev2 == b'\x33\xDB')
            # channel-9 send (B4 B9) within the next ~0x20 bytes?
            ch9 = drv.find(b'\xB4\xB9', i, i + 0x40) != -1
            cands.append({'pos': i, 'disp': disp, 'bx_loaded': bx_loaded, 'ch9': ch9})
        i += 1
    return cands

def cmd_list(args):
    data, count, entries = load(args.cc)
    print(f"{args.cc}: {count} entries, filesize {len(data)}")
    for e in sorted(entries, key=lambda x: x['off']):
        print(f"  id=0x{e['id']:04X}  off=0x{e['off']:08X}  size={e['size']}")

def cmd_extract(args):
    data, count, entries = load(args.cc)
    ent = find(entries, int(args.id, 16))
    blob = member_bytes(data, ent, decode=not args.raw)
    open(args.out, 'wb').write(blob)
    print(f"wrote {len(blob)} bytes to {args.out} (id 0x{ent['id']:04X}, "
          f"off 0x{ent['off']:X}, {'raw' if args.raw else 'decoded'})")

def cmd_locate(args):
    data, count, entries = load(args.cc)
    ent = find(entries, int(args.id, 16))
    drv = member_bytes(data, ent, decode=True)
    print(f"id 0x{ent['id']:04X} size {ent['size']} head {drv[:6].hex()}")
    for c in find_fade_bug(drv):
        tag = []
        tag.append("bx-preset" if c['bx_loaded'] else "BX-UNINIT")
        tag.append("ch9(B9)" if c['ch9'] else "chN(B1+bl)")
        note = ""
        if not c['bx_loaded'] and c['ch9']:
            note = f"  <-- BUGGY; fix -> [0x{c['disp']+8:04X}] (channel 8)"
        print(f"  fade ramp @0x{c['pos']:04X}  disp=0x{c['disp']:04X}  [{' '.join(tag)}]{note}")

def apply_fix(drv):
    """Return (patched_drv, info). Patches the buggy BX-uninitialised first fade
    ramp so its three [bx+volbase] references address the fixed slot of the channel
    it transmits on (status 0xB9 -> channel 0xB9-0xB1 = 8 -> [volbase+8])."""
    cands = [c for c in find_fade_bug(drv) if not c['bx_loaded'] and c['ch9']]
    if not cands:
        raise SystemExit("no unpatched BX-uninitialised B9 fade ramp found "
                         "(already patched, or layout not recognised)")
    if len(cands) > 1:
        raise SystemExit(f"ambiguous: {len(cands)} candidate ramps {[hex(c['pos']) for c in cands]}")
    c = cands[0]
    p = c['pos']; volbase = c['disp']
    drv = bytearray(drv)
    # channel is taken from the hard-coded controller status (mov ah,B9 == B4 B9)
    bpos = drv.find(b'\xB4\xB9', p, p + 0x40)
    if bpos < 0:
        raise SystemExit("could not find the B9 channel-status send in the ramp")
    channel = drv[bpos + 1] - 0xB1               # 0xB9 -> 8
    nd = volbase + channel                       # fixed volume slot for that channel
    lo, hi = nd & 0xFF, (nd >> 8) & 0xFF
    # the volume send: mov ah,[bx+volbase] = 8A A7 <volbase>
    sp = drv.find(bytes([0x8A, 0xA7, volbase & 0xFF, (volbase >> 8) & 0xFF]), p, p + 0x40)
    if sp < 0:
        raise SystemExit("could not find the volume send (mov ah,[bx+volbase])")
    # cmp byte [bx+volbase],28 -> cmp byte [nd],28   (BF->3E)
    assert drv[p] == 0x80 and drv[p+1] == 0xBF
    drv[p+1] = 0x3E; drv[p+2] = lo; drv[p+3] = hi
    # dec byte [bx+volbase]    -> dec byte [nd]       (8F->0E)
    assert drv[p+7] == 0xFE and drv[p+8] == 0x8F
    drv[p+8] = 0x0E; drv[p+9] = lo; drv[p+10] = hi
    # mov ah,[bx+volbase]      -> mov ah,[nd]         (A7->26)
    drv[sp+1] = 0x26; drv[sp+2] = lo; drv[sp+3] = hi
    return bytes(drv), {'pos': p, 'volbase': volbase, 'channel': channel,
                        'newdisp': nd, 'cmp': p, 'dec': p+7, 'send': sp}

def cmd_patch(args):
    data, count, entries = load(args.cc)
    ent = find(entries, int(args.id, 16))
    drv = member_bytes(data, ent, decode=True)
    patched, info = apply_fix(drv)
    print(f"patching ramp @0x{info['pos']:04X} (channel {info['channel']}): "
          f"cmp@0x{info['cmp']:04X} dec@0x{info['dec']:04X} send@0x{info['send']:04X}  "
          f"[bx+0x{info['volbase']:04X}] -> [0x{info['newdisp']:04X}]")
    # re-encode and splice back in place (size unchanged -> index/offsets unchanged)
    enc = bytes(b ^ XOR for b in patched)
    assert len(enc) == ent['size']
    out = bytearray(data)
    out[ent['off']:ent['off'] + ent['size']] = enc
    if args.dry_run:
        print("dry-run: not writing")
        return
    outpath = args.out or (args.cc + ".patched")
    open(outpath, 'wb').write(out)
    print(f"wrote patched archive -> {outpath}")

def main():
    ap = argparse.ArgumentParser(description="World of Xeen .CC extract/patch tool")
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('list');    s.add_argument('cc'); s.set_defaults(fn=cmd_list)
    s = sub.add_parser('extract'); s.add_argument('cc'); s.add_argument('id'); s.add_argument('out'); s.add_argument('--raw', action='store_true'); s.set_defaults(fn=cmd_extract)
    s = sub.add_parser('locate');  s.add_argument('cc'); s.add_argument('id'); s.set_defaults(fn=cmd_locate)
    s = sub.add_parser('patch');   s.add_argument('cc'); s.add_argument('id'); s.add_argument('--out'); s.add_argument('--dry-run', action='store_true'); s.set_defaults(fn=cmd_patch)
    args = ap.parse_args()
    args.fn(args)

if __name__ == '__main__':
    main()
