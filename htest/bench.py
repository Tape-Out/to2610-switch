"""整片测试台：一切都经五口顶层的 io_in / io_out / io_oe 进出，与板子上看到的一样。

每根信号在 payload 的哪一位，从 `ran asic` 写的 report.json 里查，不在这里另写一份：
位表错了，测试就先错。一个协程在下降沿驱动、上升沿采样，所有口共用这两次等待。
"""
import json
import os
import pathlib
import struct
import zlib

from cocotb.triggers import FallingEdge, RisingEdge

REPORT = json.loads(pathlib.Path(os.environ["CHIP_REPORT"]).read_text(encoding="utf-8"))
PERIOD_NS = 1000 / REPORT["mhz"]


def _index() -> dict[str, dict[str, int]]:
    """{"sw0_pins_tx_0_txd[1]": {"out": 位}, ...}：每个设计端口位接在 payload 哪一位、哪个方向。"""
    got: dict[str, dict[str, int]] = {}
    for r in REPORT["pads"]["bits"]:
        for role in ("in", "out", "oe"):
            if r[role]:
                got.setdefault(r[role], {})[role] = r["bit"]
    return got


PIN = _index()


def bit(sig: str, role: str) -> int:
    return PIN[sig][role]


def fcs(b: bytes) -> bytes:
    return struct.pack("<I", zlib.crc32(b))


def _bits(v) -> int:
    return int("".join(c if c in "01" else "0" for c in v.binstr), 2)


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.inv = 0
        self.ports: list["Phy"] = []
        self.watch = []                 # 每个上升沿回调 f(io_out, io_oe)
        self.cycle = 0
        dut.io_in.value = 0

    def set(self, b: int, v: int) -> None:
        self.inv = (self.inv | (1 << b)) if v else (self.inv & ~(1 << b))

    def out(self) -> tuple[int, int]:
        """逐位读：复位前后有几位是 X，不能因此把整根向量当 0，常数 1 的使能位照样是 1。"""
        return _bits(self.dut.io_out.value), _bits(self.dut.io_oe.value)

    async def run(self):
        while True:
            await FallingEdge(self.dut.clock)
            for p in self.ports:
                p.drive()
            self.dut.io_in.value = self.inv
            await RisingEdge(self.dut.clock)
            self.cycle += 1
            o, e = self.out()
            for p in self.ports:
                p.sample(o, e)
            for f in self.watch:
                f(o, e)

    async def cycles(self, n: int):
        for _ in range(n):
            await RisingEdge(self.dut.clock)


class Phy:
    """一个 RMII 口的对端：像 PHY 一样发给芯片、从芯片收。帧带着 FCS 上线。"""

    def __init__(self, bench: Bench, port: int):
        self.b, self.port = bench, port
        pre = f"sw0_pins_rx_{port}_"
        self.rxd = [bit(f"{pre}rxd[{i}]", "in") for i in range(2)]
        self.crs = bit(f"{pre}crs_dv[0]", "in")
        self.er = bit(f"{pre}rx_er[0]", "in")
        pre = f"sw0_pins_tx_{port}_"
        self.txd = [bit(f"{pre}txd[{i}]", "out") for i in range(2)]
        self.en = bit(f"{pre}tx_en[0]", "out")
        self.q: list[int | None] = []   # 待发的双比特，None 是载波空闲
        self.cur: list[int] | None = None
        self.frames: list[tuple[bytes, bool]] = []   # 收到的 (帧不含 FCS, FCS 对不对)
        self.bad_oe = False
        bench.ports.append(self)

    def send(self, frame: bytes, gap: int = 48) -> None:
        raw = b"\x55" * 7 + b"\xd5" + frame + fcs(frame)
        for byte in raw:
            for k in range(4):
                self.q.append((byte >> (2 * k)) & 3)
        self.q += [None] * gap

    def busy(self) -> bool:
        return bool(self.q)

    def drive(self):
        d = self.q.pop(0) if self.q else None
        for i, b in enumerate(self.rxd):
            self.b.set(b, 0 if d is None else (d >> i) & 1)
        self.b.set(self.crs, d is not None)
        self.b.set(self.er, 0)

    def sample(self, o: int, e: int):
        if not all((e >> b) & 1 for b in (*self.txd, self.en)):
            self.bad_oe = True
        if (o >> self.en) & 1:
            if self.cur is None:
                self.cur = []
            self.cur.append(((o >> self.txd[0]) & 1) | (((o >> self.txd[1]) & 1) << 1))
        elif self.cur is not None:
            d, self.cur = self.cur, None
            raw = bytes(sum(d[i + k] << (2 * k) for k in range(4)) for i in range(0, len(d) - 3, 4))
            if not raw.startswith(b"\x55" * 7 + b"\xd5"):
                self.frames.append((raw, False))
                return
            body = raw[8:]
            self.frames.append((body[:-4], len(body) >= 4 and fcs(body[:-4]) == body[-4:]))

    def take(self) -> list[tuple[bytes, bool]]:
        f, self.frames = self.frames, []
        return f


class Spi:
    """SPI 主机：SCK 取主频的八分之一，是 spis 的上限。跑的是 sw/ 里的同一份操作。"""

    HALF = 4

    def __init__(self, bench: Bench):
        self.b = bench
        self.sck = bit("mgmt_pins_sck[0]", "in")
        self.cs = bit("mgmt_pins_cs_n[0]", "in")
        self.mosi = bit("mgmt_pins_mosi[0]", "in")
        self.miso = bit("mgmt_pins_miso[0]", "out")
        assert PIN["mgmt_pins_miso[0]"]["out"] == PIN["mgmt_pins_miso_oe[0]"]["oe"]
        bench.set(self.cs, 1)

    async def xfer(self, tx: bytes) -> bytes:
        b = self.b
        b.set(self.cs, 0)
        await b.cycles(6)
        rx = bytearray()
        for byte in tx:
            got = 0
            for i in range(7, -1, -1):
                b.set(self.mosi, (byte >> i) & 1)
                await b.cycles(self.HALF)
                b.set(self.sck, 1)
                await RisingEdge(b.dut.clock)
                o, e = b.out()
                assert (e >> self.miso) & 1, "cs_n 低着时 miso 要驱动"
                got = (got << 1) | ((o >> self.miso) & 1)
                await b.cycles(self.HALF - 1)
                b.set(self.sck, 0)
            rx.append(got)
        await b.cycles(8)
        b.set(self.cs, 1)
        await b.cycles(24)
        return bytes(rx)

    async def do(self, op):
        try:
            tx = next(op)
            while True:
                tx = op.send(await self.xfer(tx))
        except StopIteration as done:
            return done.value


# ---------------------------------------------------------------- 主机

def mac_of(i: int) -> bytes:
    return bytes([0x02, 0, 0, 0, 0, 0x10 + i])


def ip_of(i: int) -> bytes:
    return bytes([10, 0, 0, 10 + i])


BCAST = b"\xff" * 6


def csum(b: bytes) -> int:
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def eth(dst: bytes, src: bytes, typ: int, payload: bytes) -> bytes:
    f = dst + src + struct.pack("!H", typ) + payload
    return f + bytes(max(0, 60 - len(f)))


def arp(op: int, sha: bytes, spa: bytes, tha: bytes, tpa: bytes) -> bytes:
    return struct.pack("!HHBBH", 1, 0x0800, 6, 4, op) + sha + spa + tha + tpa


def ipv4(src: bytes, dst: bytes, proto: int, payload: bytes, ident: int = 1) -> bytes:
    h = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident, 0, 64, proto, 0, src, dst)
    return h[:10] + struct.pack("!H", csum(h)) + h[12:] + payload


def icmp_echo(typ: int, ident: int, seq: int, data: bytes) -> bytes:
    h = struct.pack("!BBHHH", typ, 0, 0, ident, seq) + data
    return h[:2] + struct.pack("!H", csum(h)) + h[4:]


class Host:
    """一台接在某个口上的机器：答 ARP、答 ping，也能自己去 ping 别人。"""

    def __init__(self, bench: Bench, phy: Phy, i: int):
        self.b, self.phy, self.i = bench, phy, i
        self.mac, self.ip = mac_of(i), ip_of(i)
        self.arp: dict[bytes, bytes] = {}
        self.replies: list[tuple[bytes, int, bytes]] = []
        self.seen: list[bytes] = []
        self.bad = 0

    def poll(self):
        for f, ok in self.phy.take():
            if not ok:
                self.bad += 1
                continue
            self.seen.append(f)
            dst, src, typ = f[:6], f[6:12], struct.unpack("!H", f[12:14])[0]
            if dst not in (self.mac, BCAST):
                continue
            p = f[14:]
            if typ == 0x0806:
                op = struct.unpack("!H", p[6:8])[0]
                sha, spa, tpa = p[8:14], p[14:18], p[24:28]
                self.arp[spa] = sha
                if op == 1 and tpa == self.ip:
                    self.phy.send(eth(sha, self.mac, 0x0806, arp(2, self.mac, self.ip, sha, spa)))
            elif typ == 0x0800 and p[9] == 1 and p[16:20] == self.ip and csum(p[:20]) == 0:
                ic = p[20:struct.unpack("!H", p[2:4])[0]]
                if csum(ic) != 0:
                    self.bad += 1
                    continue
                if ic[0] == 8:
                    ident, seq = struct.unpack("!HH", ic[4:8])
                    self.phy.send(eth(src, self.mac, 0x0800, ipv4(
                        self.ip, p[12:16], 1, icmp_echo(0, ident, seq, ic[8:]))))
                elif ic[0] == 0:
                    self.replies.append((p[12:16], struct.unpack("!H", ic[6:8])[0], ic[8:]))

    async def ping(self, ip: bytes, seq: int = 1, data: bytes = b"to2610-switch",
                   limit: int = 20000, tries: int = 4) -> bool:
        """ARP 没答就重发，按主机号错开，免得几台同时发的广播一次次撞在一起。"""
        per = limit // tries
        for _ in range(tries):
            if ip not in self.arp:
                self.phy.send(eth(BCAST, self.mac, 0x0806, arp(1, self.mac, self.ip, bytes(6), ip)))
                for _ in range(per + 97 * self.i):
                    await RisingEdge(self.b.dut.clock)
                    if ip in self.arp:
                        break
                else:
                    continue
            self.phy.send(eth(self.arp[ip], self.mac, 0x0800,
                              ipv4(self.ip, ip, 1, icmp_echo(8, 0x2610, seq, data))))
            for _ in range(per):
                await RisingEdge(self.b.dut.clock)
                if (ip, seq, data) in self.replies:
                    return True
        return False


async def serve(hosts: list[Host], bench: Bench):
    while True:
        await RisingEdge(bench.dut.clock)
        for h in hosts:
            h.poll()


class MdioPhy:
    """GPIO 第 0、1 位上的一颗 PHY：802.3 第 22 条的读写。线上没人驱动时上拉成 1。

    输出在 MDC 上升沿之后换：读操作的 TA 第二位驱 0，其后十六位是数据，再放开。
    """

    def __init__(self, bench: Bench, addr: int = 1):
        self.b, self.addr = bench, addr
        self.mdc_o = bit("gpio0_pins_gpio_out[0]", "out")
        self.mdc_e = bit("gpio0_pins_gpio_dir[0]", "oe")
        self.io_i = bit("gpio0_pins_gpio_in[1]", "in")
        self.io_o = bit("gpio0_pins_gpio_out[1]", "out")
        self.io_e = bit("gpio0_pins_gpio_dir[1]", "oe")
        self.regs = {2: 0x0007, 3: 0xC0F1}
        self.prev, self.ones, self.hdr, self.drive = 0, 0, None, None
        bench.watch.append(self.edge)

    def edge(self, o: int, e: int):
        mdc = (o >> self.mdc_o) & 1 if (e >> self.mdc_e) & 1 else 0
        host = (o >> self.io_o) & 1 if (e >> self.io_e) & 1 else None
        line = host if host is not None else (1 if self.drive is None else self.drive)
        if mdc and not self.prev:
            self.rise(line)
            line = host if host is not None else (1 if self.drive is None else self.drive)
        self.prev = mdc
        self.b.set(self.io_i, line)

    def rise(self, v: int):
        if self.hdr is None:
            if v == 1:
                self.ones += 1
            else:
                if self.ones >= 32:
                    self.hdr = [0]
                self.ones = 0
            return
        self.hdr.append(v)
        n, h = len(self.hdr), self.hdr
        if n < 14:
            return
        op = h[2] << 1 | h[3]
        phy = int("".join(map(str, h[4:9])), 2)
        reg = int("".join(map(str, h[9:14])), 2)
        if phy != self.addr or op not in (0b01, 0b10):
            self.hdr, self.drive = None, None
        elif op == 0b10:
            if n == 15:
                self.drive = 0
            elif 16 <= n <= 31:
                self.drive = (self.regs.get(reg, 0) >> (31 - n)) & 1
            elif n >= 32:
                self.hdr, self.drive = None, None
        elif n == 32:
            self.regs[reg] = int("".join(map(str, h[16:32])), 2)
            self.hdr = None
