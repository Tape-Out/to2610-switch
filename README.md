# to2610-switch

An eight-port 10/100 Ethernet switch chip for the ECOS 2610 shuttle. It has no core: it forwards from the moment reset is released, and an SPI host can manage it when there is one.

![maturity](https://img.shields.io/badge/maturity-simulated-yellow) ![license](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0%20OR%20MulanPSL--2.0-blue)

Assembled by [`xirang`](https://github.com/Tape-Out/xirang) from [`eswitch`](https://github.com/Tape-Out/eswitch) (eight ports, 32 learned addresses, port isolation), [`spis`](https://github.com/Tape-Out/spis) (SPI slave that masters the on-chip bus) and [`gpio`](https://github.com/Tape-Out/gpio) (six pins). There is no RTL of its own.

## On the board

| Payload bits | Signal | Connects to |
|:--:|:--:|:--:|
| `7p` to `7p+2` | `TXD[1:0]`, `TX_EN` of port `p` | the RMII transmit pins of PHY `p` |
| `7p+3` to `7p+6` | `RXD[1:0]`, `CRS_DV`, `RX_ER` of port `p` | the RMII receive pins of PHY `p` |
| 56, 57, 58 | `SCK`, `CS_N`, `MOSI` | the management host, SPI mode 0 |
| 59 | `MISO` | the management host; driven only while `CS_N` is low |
| 60, 61 | GPIO 0, 1 | `MDC`, `MDIO` of the PHYs (bit-banged by the host tool) |
| 62 to 65 | GPIO 2 to 5 | free: PHY reset, LEDs, straps |

Ports `p` run from 0 to 7, so port 0 takes bits 0 to 6 and port 7 bits 49 to 55. Bit `n` of the payload is `user_io[n + 7]` of the MPC frame. The full table, bit by bit, is in `report.json` of `ran asic` and in the tape-out report.

- **Clock**: 50 MHz, and it must be the same 50 MHz as the RMII `REF_CLK` of all eight PHYs: one oscillator through a fan-out buffer to the chip and to each PHY in reference-clock-input mode.
- **PHYs**: any RMII 10/100 PHY, one per port, each with an RJ45 jack that has the magnetics built in. With strapped auto-negotiation the PHYs work without MDIO at all.
- **MDIO**: the eight PHYs share one MDIO bus, so they need distinct strapped addresses; MDIO needs a pull-up.
- **Management**: optional. A USB-to-SPI adapter (FT232H) or a Raspberry Pi on bits 56 to 59; SCK at most 6.25 MHz, one eighth of the clock.

After reset every port is enabled, learning is on and the ageing time is 300 "seconds" counted at 100 MHz, so 600 real seconds at 50 MHz until the host runs `boot`.

## Management

```console
$ python3 sw/switch.py --ftdi ftdi://ftdi:232h/1 boot        # ageing counted in real seconds
$ python3 sw/switch.py --spidev 0.0 table                     # learned addresses and their ports
$ python3 sw/switch.py --spidev 0.0 counters                  # received, sent and dropped frames per port
$ python3 sw/switch.py --spidev 0.0 isolate 0-3,4-7           # two broadcast domains
$ python3 sw/switch.py --spidev 0.0 mdio 1 2                  # read register 2 of the PHY at address 1
```

`sw/spis.py` is the SPI protocol and `sw/switch.py` the register map and the operations. The chip tests below drive the chip through these same two files.

## Testing

`ran test to2610-switch` runs the task `chip`: `ran asic` writes the single Verilog file that goes to the shuttle, and cocotb runs `htest/test_chip.py` on that file through the five frame ports. Signals are found in the payload through `report.json`, so the bit table in the report is tested too.

| Test | Checks |
|:--:|:--:|
| `boots_forwarding` | no management at all: two pairs of hosts ping each other right after reset |
| `management` | identify, `boot`, read back, bus error reported and cleared |
| `flood_learn_forward` | a broadcast reaches the seven other ports byte for byte; the source is learned; a unicast goes to that port only |
| `ping_pairs` | four pairs ping at once; colliding ARP broadcasts get through on retry |
| `vlan` | two isolated groups: pings inside a group work, across do not, and work again once the isolation is lifted |
| `ageing` | a learned address is gone after its ageing time, and a unicast to it floods again |
| `contention` | two frames for the same port at the same cycle: one arrives intact, the other is dropped and counted |
| `port_disable` | a disabled port neither forwards nor receives |
| `counters` | received and sent frame counts per port |
| `mdio` | MDIO over the GPIO pins: read a PHY ID, write and read back, no PHY reads as all ones |
| `gpio` | the spare pins as outputs and inputs |

## Tape-out

```console
$ ran asic to2610-switch                  # to2610_switch.v, ecc at 50 MHz, report.json
$ ran asic to2610-switch --no-run         # only the Verilog file and ecc.toml
```

## Limits

Forwarding is cut-through with no frame buffer: a frame whose output port is busy with another frame is dropped there and counted in `drops`, never interleaved. Two hosts that keep sending to the same port at full rate lose frames; TCP and ARP recover from it, as on any hub-like segment. There is no 802.1Q tagging, only port isolation, and no 1000 Mb/s.

## License

任选其一：

- [MIT](LICENSE-MIT)
- [Apache 2.0](LICENSE-APACHE)
- [木兰宽松许可证 第2版](LICENSE-MULAN)

`SPDX-License-Identifier: MIT OR Apache-2.0 OR MulanPSL-2.0`

除非另行说明，你提交的贡献按上述三者同时授权，不附加其他条件。
