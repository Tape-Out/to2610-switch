"""to2610-switch 的管理：寄存器地址、上电之后的一步、查表、计数、端口隔离、经 GPIO 走 MDIO。

复位后交换机就在转发，不接管理口也能用；接了管理口才用得上这里。地址照 `eswitch` 与
`gpio` 的 regmap.yaml 与本仓 ip.yaml 的 addr。

    python3 sw/switch.py --ftdi ftdi://ftdi:232h/1 boot
    python3 sw/switch.py --spidev 0.0 table
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import spis  # noqa: E402

SW, GPIO = 0x1000_0000, 0x1000_1000
PORTS, ENTRIES = 8, 32            # 照本仓 ip.yaml 的 ports 与 macEntries

CTRL, PORTEN, AGETIME, TICK = 0x00, 0x04, 0x10, 0x14
MACLO = 0x100                       # 每条 8 字节：低 32 位，再是 {有效, 口号, 高 16 位}
RXCNT, TXCNT, DROPS, ISOL = 0x300, 0x340, 0x380, 0x3C0
DOUT, DIN, DIR = 0x00, 0x04, 0x08
MDC, MDIO = 0, 1                    # GPIO 的第 0、1 位


def boot(mhz: float = 50, agetime: int = 300):
    """「一秒」复位时按 100 MHz 算，改成这颗芯片的主频，老化才按真秒走。"""
    yield from spis.wr(SW + AGETIME, agetime)
    yield from spis.wr(SW + TICK, round(mhz * 1_000_000) - 1)


def table():
    """学到的地址：[(槽, MAC, 口)]。整张表一次突发读完。"""
    w = yield from spis.rd(SW + MACLO, 2 * ENTRIES)
    return [(i, ((w[2 * i + 1] & 0xFFFF) << 32) | w[2 * i], (w[2 * i + 1] >> 16) & 0x7FFF)
            for i in range(ENTRIES) if w[2 * i + 1] >> 31]


def counters():
    """每口的收、发、丢。"""
    rx = yield from spis.rd(SW + RXCNT, PORTS)
    tx = yield from spis.rd(SW + TXCNT, PORTS)
    dr = yield from spis.rd(SW + DROPS, PORTS)
    return list(zip(rx, tx, dr))


def isolate(groups):
    """分广播域：`[[0, 1, 2, 3], [4, 5, 6, 7]]`。组外的口一律隔开，空列表即全通。"""
    full = (1 << PORTS) - 1
    mask = [0] * PORTS
    for g in groups:
        m = sum(1 << p for p in g)
        for p in g:
            mask[p] = full & ~m
    yield from spis.wr(SW + ISOL, *mask)


def enable(ports):
    yield from spis.wr(SW + PORTEN, sum(1 << p for p in ports))


def _bits(bits):
    for b in bits:
        yield from spis.wr(GPIO + DOUT, b << MDIO)
        yield from spis.wr(GPIO + DOUT, (b << MDIO) | (1 << MDC))
    yield from spis.wr(GPIO + DOUT, 0)


def _tick():
    yield from spis.wr(GPIO + DOUT, 1 << MDC)
    yield from spis.wr(GPIO + DOUT, 0)


def _head(op: int, phy: int, reg: int) -> list[int]:
    # 802.3 第 22 条：32 个 1 的前导、ST=01、OP、PHYAD、REGAD
    return ([1] * 32 + [0, 1] + [op >> 1, op & 1]
            + [(phy >> i) & 1 for i in range(4, -1, -1)]
            + [(reg >> i) & 1 for i in range(4, -1, -1)])


def mdio_read(phy: int, reg: int):
    """PHY 在 MDC 上升沿之后换位，所以每个上升沿之后读一次。"""
    yield from spis.wr(GPIO + DIR, (1 << MDC) | (1 << MDIO))
    yield from _bits(_head(0b10, phy, reg))
    yield from spis.wr(GPIO + DIR, 1 << MDC)
    yield from _tick()
    v = 0
    for _ in range(16):
        yield from _tick()
        d = yield from spis.rd1(GPIO + DIN)
        v = (v << 1) | ((d >> MDIO) & 1)
    yield from _tick()
    return v


def mdio_write(phy: int, reg: int, val: int):
    yield from spis.wr(GPIO + DIR, (1 << MDC) | (1 << MDIO))
    yield from _bits(_head(0b01, phy, reg) + [1, 0] + [(val >> i) & 1 for i in range(15, -1, -1)])
    yield from spis.wr(GPIO + DIR, 1 << MDC)


def _groups(s: str):
    out = []
    for g in s.split(","):
        a, _, b = g.partition("-")
        out.append(list(range(int(a), int(b or a) + 1)))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="to2610-switch 管理")
    link = ap.add_mutually_exclusive_group(required=True)
    link.add_argument("--spidev", help="总线.片选，如 0.0")
    link.add_argument("--ftdi", help="pyftdi 的地址，如 ftdi://ftdi:232h/1")
    ap.add_argument("--hz", type=int, default=2_000_000, help="SCK，不超过芯片主频的八分之一")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("boot")
    b.add_argument("--mhz", type=float, default=50)
    sub.add_parser("ident")
    sub.add_parser("table")
    sub.add_parser("counters")
    i = sub.add_parser("isolate", help="如 0-3,4-7；给空串即全通")
    i.add_argument("groups")
    m = sub.add_parser("mdio")
    m.add_argument("phy", type=int)
    m.add_argument("reg", type=int)
    m.add_argument("val", nargs="?", type=lambda x: int(x, 0))
    a = ap.parse_args(argv)
    if a.spidev:
        bus, _, dev = a.spidev.partition(".")
        x = spis.spidev(int(bus), int(dev or 0), a.hz)
    else:
        x = spis.ftdi(a.ftdi, a.hz)
    run = lambda op: spis.run(op, x)  # noqa: E731
    if a.cmd == "boot":
        run(boot(a.mhz))
    elif a.cmd == "ident":
        print(run(spis.ident()).decode(errors="replace"))
    elif a.cmd == "table":
        for slot, mac, port in run(table()):
            print(f"{slot:3} {mac:012x} {port}")
    elif a.cmd == "counters":
        for p, (r, t, d) in enumerate(run(counters())):
            print(f"{p}  rx {r:10}  tx {t:10}  drop {d:10}")
    elif a.cmd == "isolate":
        run(isolate(_groups(a.groups) if a.groups else []))
    elif a.cmd == "mdio":
        if a.val is None:
            print(f"0x{run(mdio_read(a.phy, a.reg)):04x}")
        else:
            run(mdio_write(a.phy, a.reg, a.val))
    return 0


if __name__ == "__main__":
    sys.exit(main())
