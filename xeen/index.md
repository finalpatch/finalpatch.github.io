# A Wild Register Appears: Hunting the 30-Year-Old World of Xeen MT-32 Crash

*Might & Magic IV & V: World of Xeen freezes and corrupts itself when you play it
with a Roland MT-32 — on emulators **and** on real hardware. Here's the months-long
hunt for the bug, and the six bytes that fix it.*

## The game that raised me

Might & Magic IV: Clouds of Xeen and V: Darkside of Xeen — together "World of
Xeen" — are core childhood memories for me. I spent countless afternoons mapping
dungeons on graph paper and grinding my party through Castleview.

Back then, almost nobody I knew had anything fancier than a Sound Blaster. The
music was fine, but it wasn't what the composers were hearing in the studio. The
*real* Xeen soundtrack was written for the Roland MT-32 / LAPC-1 (the CM-32L
sound module), and it sounds gorgeous — lush, warm, completely different from the
OPL FM synthesis of a Sound Blaster.

So years later, re-experiencing the game, I did the thing every audio nerd does:
I set it to **Roland MT-32** and sat back to enjoy the "intended" soundtrack.

And the game started falling apart.

## The symptoms

Within a few minutes — usually while wandering a town and talking to vendors —
something would go wrong:

- The music would slowly start playing the **wrong instruments**.
- Then the game would **freeze**, or **crash to DOS**, or die on an **illegal
  instruction**.
- Once, it wandered into the save routine and **corrupted my save file**.

Switch the music back to General MIDI / Sound Canvas, or to Sound Blaster, and
the game is rock solid forever. Only the MT-32 setting breaks it. And the longer
you play, and the busier the area (towns, shops, lots of music and sound-effect
changes), the faster it dies.

What's strange is how *few* reports of this exist online. My theory: almost
everybody plays Xeen with the default Sound Blaster setting, so almost nobody
ever pokes the MT-32 path hard enough to hit it.

## "It's just the emulator"... right?

My first assumption was the obvious one: it's an emulation bug. I could reproduce
it in DOSBox-pure (on Android/RetroArch), DOSBox Staging, and DOSBox-X, so surely
one of them was mis-emulating the MPU-401 or the timing.

Then I went looking, and found scattered accounts from people running the game on
**genuine MT-32 + CM-64L hardware on real 386/486 machines** describing the exact
same thing: instruments drift to the wrong patches, then it freezes, worst in
towns, never with a Sound Blaster.

That reframed everything. This was **not** an emulator bug. It's a real bug in
the game's own code, one that just happens to also fire under emulation. DOSBox
wasn't doing anything wrong — it was faithfully reproducing a 30-year-old crash.

## The graveyard of theories

This is the part nobody puts in the writeup, so I will: I was wrong, repeatedly,
for a long time.

- **MPU-401 timing / busy-wait latency.** The driver talks to the MPU by polling
  status bits in tight loops. I was sure the slow MT-32 was stalling those loops
  and opening a reentrancy window. I built a "spin detector" that logged whenever
  the driver actually busy-waited. Result: it only ever spun during the 14 ms
  power-on reset. During gameplay, the emulated I/O is effectively instant. Dead
  end.

- **Interrupt reentrancy.** The music driver runs as the timer interrupt handler.
  Maybe the timer was re-entering the driver while it was mid-update? I added a
  guard that blocked the timer from firing while executing in the driver's code
  segment. It barely ever triggered, and the crash still happened. Dead end.

- **Stack smashing.** A classic cause of "the program jumps to garbage" is an
  overwritten return address. I built a shadow return-address stack that pushed on
  every call/interrupt and checked on every return. Across **~29 million returns**
  it found exactly zero smashes. Trustworthy negative. Dead end.

- **Reading the whole driver.** I disassembled the entire MT-32 driver and
  convinced myself every memory write in it was *bounded* — masked to a channel
  number, or a fixed address. By that logic the driver *couldn't* be the thing
  scribbling memory. (Hold that thought.)

Every heuristic "this looks abnormal" detector I built drowned in false positives,
because legitimate code reuses the same segments, values, and stack slots
constantly. The lesson I kept re-learning: **stop guessing at what "looks wrong."
Anchor on a confirmed-corrupt value and watch it.**

## The breakthrough

The shift was to stop theorizing and look at the actual wreckage.

**1. Dump the driver after it dies.** When the game froze, I dumped the driver's
memory segment and compared it byte-for-byte against a clean copy. It was
*riddled* with changes — but the telling ones were in the **driver's own
machine code**:

- The jump table at the top of the driver had wrong targets.
- A configuration word holding the **MIDI port address (`0x0330`)** had been
  overwritten with garbage (`0x00DF`). *That* explained a baffling earlier
  symptom — when this happens, the driver starts polling a port that isn't the
  MPU, every byte times out, and the whole thing grinds to a crawl.

So the driver wasn't just a victim — **something was modifying the driver's code
as it ran.** The "all writes are bounded" analysis was correct... for the
*pristine* code. Once a single instruction byte flips, the rules are off.

**2. Catch the self-modification live.** One changed byte stood out. At offset
`071A`, a harmless 5-byte compare instruction:

```
83 3E 65 01 01    cmp word [0165], 1
```

had become a 3-byte instruction (one byte decremented), which **re-aligned the
instruction stream** so that the bytes after it now decoded as:

```
01 01             add [bx+di], ax     ; a write to an arbitrary address!
```

A bounded compare had mutated into an **unbounded store**, executed on every
note — a self-amplifying corruption engine. That single byte was the lynchpin.
Since nothing in the driver legitimately writes to its own code, I set a
**hardware write-breakpoint on that exact byte** and let the game run until it
fired.

(This is where DOSBox-X earns its keep: its built-in debugger — memory dumps,
disassembler, and especially hardware write-watchpoints — was the single most
important tool in this whole hunt. None of this would have been catchable without
it.)

**3. The culprit, red-handed.** The breakpoint stopped on:

```
8A7F:09B3   FE 8F 4D 01    dec byte [bx+014D]      ; with BX = 0x05CD
```

`0x05CD + 0x014D = 0x071A`. There it was. The driver itself was decrementing one
of its own code bytes — and `BX` was a junk value, `0x05CD`, that had no business
being a volume-table index.

## The root cause: one uninitialized register

The instruction lives in the music **fade-out routine**. To fade the music, the
driver walks its per-channel volume table and ramps each entry down. It does this
in two passes:

```
; ----- first ramp: the channel it transmits as 0xB9 -----
mov  [0025], 1
cmp  byte [bx+014D], 28      ; <-- BX is never set here!
jl   skip
dec  byte [bx+014D]          ; <-- the wild write
...send that channel's volume...

; ----- second ramp: channels 6..0 -----
mov  bx, 0006                ; <-- BX correctly initialized here
loop: cmp byte [bx+014D], 28
      dec byte [bx+014D]
      ...
      dec bx
      jns loop
```

Look at the two passes. The **second** one carefully sets `bx` before using it.
The **first** one **never initializes `bx` at all.** It just trusts whatever
happens to be in the register.

Most of the time, you get lucky: `bx` holds some small leftover value, the index
lands inside the volume table, and the worst that happens is the wrong channel's
volume gets nudged. Nobody notices.

But here's the kicker — *when* does this fade code run? It runs inside the timer
interrupt, specifically on a "delay tick," the moments between notes when the
interpreter has nothing to play. On those ticks the driver **skips its note
interpreter entirely**, so nothing ever loads `bx` with a channel number. `bx` is
simply **whatever the main game code was holding when the timer fired** — an
arbitrary value.

So every fade, on every idle tick, the driver does `dec byte [bx + 0x14D]` with a
random `bx`. If the game happened to be holding a large value like `0x05CD`, that
decrement lands **outside the volume table, inside the driver's own code.**

And that's the whole disaster, in one line of buggy code:

1. The fade routine decrements a random byte in the driver (here, code at `071A`).
2. That byte turns a `cmp` into a self-replicating `add [bx+di], ax`.
3. The amplifier sprays the rest of the driver — the jump table, the saved
   registers in the init routine, the MIDI port address.
4. The next time the game calls into the now-smashed driver (or the driver
   returns through a smashed return address), the CPU jumps into garbage —
   freeze, illegal instruction, crash to DOS, or a wild write into a save buffer.

Every symptom I'd chased — wrong instruments, the slow grind, the freezes, the
varied crash sites — falls out of this one uninitialized register.

### Why MT-32 only, and why towns

- It's a bug in the **MT-32 driver's code**, not in the music data (the note data
  is identical across all sound devices). The Sound Blaster and Sound Canvas paths
  are *different driver code* that doesn't have this flaw.
- **Towns and vendors** trigger lots of music and sound-effect transitions, and
  every transition kicks off a fade — more fades, more rolls of the dice.
- And because it's a genuine logic bug, it crashes **real Roland hardware** too.

## The fix: six bytes

That first ramp always talks to one fixed channel — you can see it hard-code the
`0xB9` controller status. One small gotcha bit me here: in this driver, channels
are numbered from `0xB1`, so `0xB9` is **channel 8**, not 9. (I got this wrong on
my first attempt — read "B9" as "channel 9" — and only caught it by
cross-referencing ScummVM's reimplementation of the same driver. The off-by-one
still *stopped the crash*, because the only wild *write* was now bounded — but it
faded the wrong channel. Reverse engineering stays humbling right to the end.)

Channel 8's volume lives at a fixed table slot, `0x014D + 8 = 0x0155`. The ramp
touches that volume in three places — a compare, a decrement, and the volume it
transmits — so the fix points all three at the fixed slot instead of routing them
through the uninitialized register:

```
cmp byte [bx+014D], 28   ->   cmp byte [0155], 28
dec byte [bx+014D]       ->   dec byte [0155]
mov ah,  [bx+014D]       ->   mov ah,  [0155]
```

That's six bytes, all **length-preserving** — same instruction sizes, so nothing
else in the binary moves. The uninitialized register is never consulted again, so
it can never go wild.

## Getting the fix into the game files

The driver (resource ID `0x5084`, internally "ROLMUS") is packed inside the
game's `.CC` archive — a simple format with an encrypted index and XOR-obfuscated
members ([format reference](https://xeen.fandom.com/wiki/CC_File_Format)). I wrote
a small Python tool (with Claude Code) that:

1. Decrypts the archive index and locates the driver resource.
2. De-obfuscates the member (a simple XOR).
3. **Finds the buggy ramp by pattern** rather than a hardcoded offset — it looks
   for the channel-8 fade ramp that runs through an uninitialized register — then
   verifies and applies the six-byte fix.
4. Re-obfuscates and splices it back in place. Because the patch is
   length-preserving, the archive index never changes — it's a pure in-place
   patch.

And here's the wrinkle that explains *why* this bug exists at all. "World of
Xeen" isn't one game — it's two. Might & Magic IV: Clouds of Xeen (1992) ships
`XEEN.CC`; Might & Magic V: Darkside of Xeen (1993) ships `INTRO.CC` and `DARK.cc`.
Install V on top of IV and they fuse into the combined World of Xeen, and the 
**newer (1993) driver from `INTRO.CC` is the one that loads and stays resident.**

So I pulled the ROLMUS driver out of *both* archives and compared them. The 1992
(Clouds) build doesn't fade channel 8 at all — it goes straight to the
channels-6..0 loop, and has no uninitialized-register ramp to get wrong. The 1993
(Darkside) build is 246 bytes larger and **adds** the channel-8 fade ramp — and
ships it with the uninitialized `bx`.

In other words, this isn't some dusty unreachable code path. It's a **regression**:
someone added a feature (fade channel 8 on the way out) in the 1993 rewrite, got
the register setup wrong, and that's the build the combined game everyone plays
actually runs. The 1992 game was fine. My patch tool inspects each archive and
only touches the vulnerable 1993 build.

Patched, verified, and the game now plays the MT-32 soundtrack for hours without a
hiccup — the way it was meant to sound, finally without the self-destruct.

## Closing thoughts

This was a thirty-year-old bug hiding behind an audio option almost nobody chose,
in a code path that only misfires when an interrupt catches the CPU holding the
wrong value. It crashed real hardware in 1993 and it crashes emulators today, for
the same reason.

If you want to play World of Xeen the way the composers intended — on an MT-32 or
with Munt — you no longer have to choose between the good soundtrack and a stable
game.

---

**Downloads**

- **Pre-patched `INTRO.CC`** — drop-in replacement; back up your original first.
  + [English Version](INTRO.CC.zip)
  + [Chinese Version](INTRO.CC.chinese.zip)
- **`ccpatch.py`** — the CC extract/patch tool, if you'd rather patch your own copy
  or inspect the change yourself. Run `python3 ccpatch.py patch INTRO.CC 0x5084`.
  + [Download](ccpatch.py)

**Notes & credits.** The detective work was done in **DOSBox-X's** debugger
(memory watchpoints were the hero). The patch tool was written with **Claude
Code**. Cross-referenced against **ScummVM's** MM engine reimplementation, which
was invaluable for confirming the driver's channel/volume semantics. Reproduced on
DOSBox-pure, DOSBox-Staging, and DOSBox-X — and, per period reports, on real
Roland hardware.
