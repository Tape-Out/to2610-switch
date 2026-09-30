# to2610-switch

An eight-port 100 Mbps Ethernet switch chip for the ECOS 2610 shuttle. It has no core: it forwards from the moment reset is released, and an SPI host can manage it when there is one.

![maturity](https://img.shields.io/badge/maturity-simulated-yellow) ![license](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0%20OR%20MulanPSL--2.0-blue)

Assembled by [`xirang`](https://github.com/Tape-Out/xirang) from [`eswitch`](https://github.com/Tape-Out/eswitch) (eight ports, 32 learned addresses, port isolation), [`spis`](https://github.com/Tape-Out/spis) (SPI slave that masters the on-chip bus) and [`gpio`](https://github.com/Tape-Out/gpio) (six pins). There is no RTL of its own.

The board wiring (eight RMII PHYs with RJ45 jacks, the SPI management header, MDIO over GPIO), the chip tests and the limits are in [`docs/流片说明.md`](docs/流片说明.md); the tape-out report is generated from that file.

## Management

```console
$ python3 sw/switch.py --ftdi ftdi://ftdi:232h/1 boot        # ageing counted in real seconds
$ python3 sw/switch.py --spidev 0.0 table                     # learned addresses and their ports
$ python3 sw/switch.py --spidev 0.0 counters                  # received, sent and dropped frames per port
$ python3 sw/switch.py --spidev 0.0 isolate 0-3,4-7           # two broadcast domains
$ python3 sw/switch.py --spidev 0.0 mdio 1 2                  # read register 2 of the PHY at address 1
```

`sw/spis.py` is the SPI protocol and `sw/switch.py` the register map and the operations. The chip tests drive the chip through these same two files.

## Testing and tape-out

```console
$ ran test to2610-switch                  # the chip tests, on the Verilog file that goes to the shuttle
$ ran asic to2610-switch                  # to2610_switch.v, ecc at 50 MHz, report.json
$ ran asic to2610-switch --no-run         # only the Verilog file and ecc.toml
```

## License

任选其一：

- [MIT](LICENSE-MIT)
- [Apache 2.0](LICENSE-APACHE)
- [木兰宽松许可证 第2版](LICENSE-MULAN)

`SPDX-License-Identifier: MIT OR Apache-2.0 OR MulanPSL-2.0`

除非另行说明，你提交的贡献按上述三者同时授权，不附加其他条件。
