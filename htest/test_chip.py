"""to2610-switch 的整片测试，跑在交付的那份 .v 上，经五口顶层进出。

每个用例先复位。管理走 sw/ 里上板用的同一份代码。
"""
import pathlib
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Combine

import bench as B

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "sw"))
import spis  # noqa: E402
import switch as S  # noqa: E402


async def up(dut):
    cocotb.start_soon(Clock(dut.clock, B.PERIOD_NS, units="ns").start())
    dut.reset.value = 1
    b = B.Bench(dut)
    phys = [B.Phy(b, p) for p in range(S.PORTS)]
    spi = B.Spi(b)
    mdio = B.MdioPhy(b)
    cocotb.start_soon(b.run())
    await b.cycles(20)
    dut.reset.value = 0
    await b.cycles(10)
    hs = [B.Host(b, phys[i], i) for i in range(S.PORTS)]
    cocotb.start_soon(B.serve(hs, b))
    return b, phys, spi, hs, mdio


def clean(phys, hs):
    assert not any(p.bad_oe for p in phys), "RMII 的发送脚要一直驱动"
    assert not any(h.bad for h in hs), [h.bad for h in hs]


async def idle(b, phys, n=400):
    while any(p.busy() for p in phys):
        await b.cycles(16)
    await b.cycles(n)


@cocotb.test()
async def boots_forwarding(dut):
    """不碰管理口：复位之后就在转发，接上 PHY 就能 ping 通。"""
    b, phys, spi, hs, _ = await up(dut)
    assert await hs[0].ping(B.ip_of(1))
    assert await hs[6].ping(B.ip_of(3))
    clean(phys, hs)


@cocotb.test()
async def management(dut):
    b, phys, spi, hs, _ = await up(dut)
    assert await spi.do(spis.ident()) == b"SPIS"
    await spi.do(S.boot(50))
    assert await spi.do(spis.rd1(S.SW + S.TICK)) == 49_999_999
    assert await spi.do(spis.rd1(S.SW + S.AGETIME)) == 300
    assert await spi.do(spis.status()) == 0
    # 没有东西的地址回错，状态字 bit0 记下，读一次就清
    await spi.do(spis.rd1(0x2000_0000))
    assert await spi.do(spis.status()) == 1
    assert await spi.do(spis.status()) == 0


@cocotb.test()
async def flood_learn_forward(dut):
    """广播泛洪到除入口外的每个口、逐字节相同；学到源地址后单播只走那一个口。"""
    b, phys, spi, hs, _ = await up(dut)
    f = B.eth(B.BCAST, B.mac_of(0), 0x88B5, bytes(range(80)))
    phys[0].send(f)
    await idle(b, phys)
    for i in range(1, S.PORTS):
        assert hs[i].seen == [f], (i, hs[i].seen)
    assert hs[0].seen == []
    tab = await spi.do(S.table())
    assert (int.from_bytes(B.mac_of(0), "big"), 0) in [(m, p) for _, m, p in tab]

    for h in hs:
        h.seen.clear()
    u = B.eth(B.mac_of(0), B.mac_of(5), 0x88B5, b"unicast" * 10)
    phys[5].send(u)
    await idle(b, phys)
    assert hs[0].seen == [u]
    assert all(hs[i].seen == [] for i in range(1, S.PORTS)), [len(h.seen) for h in hs]
    clean(phys, hs)


@cocotb.test()
async def ping_pairs(dut):
    """四对主机同时互 ping。同时发的广播会互相占着出口，靠 ARP 重发走通——与真网络一样。"""
    b, phys, spi, hs, _ = await up(dut)
    got = [cocotb.start_soon(hs[a].ping(B.ip_of(z), seq=a)) for a, z in
           ((0, 1), (2, 3), (4, 5), (6, 7))]
    await Combine(*got)
    assert all(t.result() for t in got), [t.result() for t in got]
    clean(phys, hs)


@cocotb.test()
async def vlan(dut):
    """分成两个广播域：域内互通，跨域不通；撤掉隔离又通。"""
    b, phys, spi, hs, _ = await up(dut)
    await spi.do(S.isolate([[0, 1, 2, 3], [4, 5, 6, 7]]))
    f = B.eth(B.BCAST, B.mac_of(0), 0x88B5, b"vlan" * 16)
    phys[0].send(f)
    await idle(b, phys)
    assert [len(hs[i].seen) for i in range(S.PORTS)] == [0, 1, 1, 1, 0, 0, 0, 0]
    assert await hs[0].ping(B.ip_of(2))
    assert not await hs[0].ping(B.ip_of(5), limit=3000)
    await spi.do(S.isolate([]))
    assert await hs[0].ping(B.ip_of(5))
    clean(phys, hs)


@cocotb.test()
async def ageing(dut):
    """「一秒」改成 100 拍、老化 10 秒：学到的地址 1000 拍后没了，发给它的单播又泛洪。"""
    b, phys, spi, hs, _ = await up(dut)
    await spi.do(spis.wr(S.SW + S.AGETIME, 10))
    await spi.do(spis.wr(S.SW + S.TICK, 99))
    phys[3].send(B.eth(B.BCAST, B.mac_of(3), 0x88B5, b"hello"))
    await idle(b, phys, 50)
    mac3 = int.from_bytes(B.mac_of(3), "big")
    assert mac3 in [m for _, m, _ in await spi.do(S.table())]
    await b.cycles(1600)
    assert mac3 not in [m for _, m, _ in await spi.do(S.table())]
    for h in hs:
        h.seen.clear()
    phys[0].send(B.eth(B.mac_of(3), B.mac_of(0), 0x88B5, b"where are you"))
    await idle(b, phys)
    assert all(len(hs[i].seen) == 1 for i in range(1, S.PORTS)), [len(h.seen) for h in hs]
    clean(phys, hs)


@cocotb.test()
async def contention(dut):
    """两个口同一拍发往同一个口：一帧完整送到，另一帧整帧丢掉并记数，不交错成坏帧。"""
    b, phys, spi, hs, _ = await up(dut)
    phys[0].send(B.eth(B.BCAST, B.mac_of(0), 0x88B5, b"learn me"))
    await idle(b, phys)
    for h in hs:
        h.seen.clear()
    a = B.eth(B.mac_of(0), B.mac_of(1), 0x88B5, b"A" * 64)
    z = B.eth(B.mac_of(0), B.mac_of(2), 0x88B5, b"Z" * 64)
    phys[1].send(a)
    phys[2].send(z)
    await idle(b, phys)
    assert len(hs[0].seen) == 1 and hs[0].seen[0] in (a, z), hs[0].seen
    drops = [d for _, _, d in await spi.do(S.counters())]
    assert sorted(drops[1:3]) == [0, 1] and sum(drops) == 1, drops
    phys[1].send(a)
    await idle(b, phys)
    phys[2].send(z)
    await idle(b, phys)
    assert hs[0].seen[1:] == [a, z]
    clean(phys, hs)


@cocotb.test()
async def port_disable(dut):
    """关掉 7 号口：它收的不转，发给它的也不送。"""
    b, phys, spi, hs, _ = await up(dut)
    await spi.do(S.enable(range(7)))
    phys[7].send(B.eth(B.BCAST, B.mac_of(7), 0x88B5, b"from seven"))
    phys[0].send(B.eth(B.BCAST, B.mac_of(0), 0x88B5, b"from zero"))
    await idle(b, phys)
    assert hs[7].seen == []
    assert all(len(hs[i].seen) == 1 and b"zero" in hs[i].seen[0] for i in range(1, 7))
    await spi.do(S.enable(range(8)))
    assert await hs[7].ping(B.ip_of(0))
    clean(phys, hs)


@cocotb.test()
async def counters(dut):
    b, phys, spi, hs, _ = await up(dut)
    for k in range(3):
        phys[4].send(B.eth(B.BCAST, B.mac_of(4), 0x88B5, bytes([k]) * 40))
    await idle(b, phys)
    c = await spi.do(S.counters())
    assert c[4][0] == 3 and all(c[i][1] == 3 for i in range(S.PORTS) if i != 4), c
    assert c[4][1] == 0


@cocotb.test()
async def mdio(dut):
    """经 GPIO 拨出 MDIO：读 PHY 的标识寄存器、写一个再读回；没有 PHY 的地址读出全 1。"""
    b, phys, spi, hs, phy = await up(dut)
    assert await spi.do(S.mdio_read(1, 2)) == 0x0007
    await spi.do(S.mdio_write(1, 4, 0x01E1))
    assert await spi.do(S.mdio_read(1, 4)) == 0x01E1
    assert phy.regs[4] == 0x01E1
    assert await spi.do(S.mdio_read(5, 2)) == 0xFFFF


@cocotb.test()
async def gpio(dut):
    """余下四根 GPIO：出方向时驱到焊盘上，入方向时读得回外面给的电平。"""
    b, phys, spi, hs, _ = await up(dut)
    await spi.do(spis.wr(S.GPIO + S.DIR, 0b001100))
    await spi.do(spis.wr(S.GPIO + S.DOUT, 0b000100))
    await b.cycles(8)
    o, e = b.out()
    pin = lambda i, r: B.bit(f"gpio0_pins_gpio_{r}[{i}]", {"out": "out", "dir": "oe", "in": "in"}[r])  # noqa: E731
    assert (e >> pin(2, "dir")) & 1 and (e >> pin(3, "dir")) & 1
    assert not (e >> pin(4, "dir")) & 1
    assert (o >> pin(2, "out")) & 1 and not (o >> pin(3, "out")) & 1
    b.set(pin(4, "in"), 1)
    b.set(pin(5, "in"), 0)
    await b.cycles(8)
    assert (await spi.do(spis.rd1(S.GPIO + S.DIN)) >> 4) & 0b11 == 0b01
