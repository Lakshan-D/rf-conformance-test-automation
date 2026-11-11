"""
demo_simulated_bench.py

Run the conformance test against a pretend bench, so you can see exactly
what the script sends to the instruments and what it reports, with no
hardware. Usage:

    python demo_simulated_bench.py            # all scenarios
    python demo_simulated_bench.py harmonic   # one scenario

The fake generator "transmits" a carrier plus a weaker 2nd harmonic. The
fake analyser returns whatever power sits at the frequency it is tuned to,
minus the cable loss, plus a little random noise, just like a real bench.
"""

from __future__ import annotations

import logging
import random
import sys

import rf_conformance_test as rct

CARRIER_HZ = 2.4e9
HARMONIC_LEVEL_DBC = -38.0  # harmonic sits 38 dB below the carrier
CABLE_LOSS_DB = 1.5


class SimulatedBench:
    """Shared state between the fake generator and the fake analyser."""

    def __init__(self, noise_db=0.2, broken_reading=None, crash_on=None):
        self.freq_hz = 0.0
        self.power_dbm = -100.0
        self.rf_on = False
        self.centre_hz = 0.0
        self.noise_db = noise_db
        self.broken_reading = broken_reading
        self.crash_on = crash_on

    def power_at(self, freq_hz):
        """What a perfect probe would see at the DUT output, at this frequency."""
        if not self.rf_on:
            return -95.0  # noise floor
        if abs(freq_hz - self.freq_hz) < 1e6:
            return self.power_dbm
        if abs(freq_hz - 2 * self.freq_hz) < 1e6:
            return self.power_dbm + HARMONIC_LEVEL_DBC
        return -95.0


class SimLink:
    def __init__(self, name, bench):
        self.name = name
        self.bench = bench

    def _show(self, arrow, text):
        print(f"      {self.name:<9} {arrow} {text}")

    def send(self, cmd):
        self._show("<-", cmd)
        b = self.bench
        if b.crash_on and cmd.startswith(b.crash_on):
            raise ConnectionError(f"LAN link to {self.name} dropped during {cmd}")
        if cmd.startswith("SOUR:FREQ:FIXED"):
            b.freq_hz = float(cmd.split()[1].rstrip("HZ"))
        elif cmd.startswith("SOUR:POW"):
            b.power_dbm = float(cmd.split()[1].rstrip("DBM"))
        elif cmd == "OUTP ON":
            b.rf_on = True
        elif cmd == "OUTP OFF":
            b.rf_on = False
        elif cmd.startswith("SENS:FREQ:CENT"):
            b.centre_hz = float(cmd.split()[1].rstrip("HZ"))

    def query(self, cmd):
        b = self.bench
        if cmd == "SYST:ERR?":
            reply = '+0,"No error"'
        elif cmd == "*OPC?":
            reply = "1"
        elif cmd == "CALC:MARK1:Y?":
            if b.broken_reading:
                reply = b.broken_reading
            else:
                level = b.power_at(b.centre_hz) - CABLE_LOSS_DB + random.gauss(0, b.noise_db)
                reply = f"{level:.3f}"
        else:
            reply = ""
        if cmd != "SYST:ERR?":  # error checks are constant noise, hide them
            self._show("->", f"{cmd}  =>  {reply}")
        return reply

    def close(self):
        self._show("x ", "connection closed")


def base_config(**overrides):
    cfg = dict(
        generator_ip="gen", generator_port=5025,
        analyser_ip="analyser", analyser_port=5025,
        test_frequency_hz=CARRIER_HZ, test_power_dbm=-10.0,
        analyser_span_hz=10e6, analyser_rbw_hz=100e3, analyser_vbw_hz=100e3,
        path_loss_db=CABLE_LOSS_DB, settle_time_s=0.0,
        num_sweeps=3, measurement_uncertainty_db=1.0,
    )
    cfg.update(overrides)
    return rct.TestConfig(**cfg)


SCENARIOS = {
    "harmonic": (
        "Spurious emission at the 2nd harmonic (4.8 GHz), limit -30 dBm. Expect PASS.",
        {},
        dict(measurement_freq_hz=2 * CARRIER_HZ, spec_limit_dbm=-30.0,
             test_name="Spurious emission, 2nd harmonic"),
    ),
    "v1_bug": (
        "The v1.0 mistake: spurious limit, but analyser tuned to the carrier. Expect FAIL.",
        {},
        dict(measurement_freq_hz=None, spec_limit_dbm=-20.0,
             test_name="Spurious emission (wrongly measured on carrier)"),
    ),
    "output_power": (
        "Output power on the carrier, must be at least -11 dBm (LOWER limit). Expect PASS.",
        {},
        dict(measurement_freq_hz=None, spec_limit_dbm=-11.0, limit_type=rct.LimitType.LOWER,
             test_name="Output power"),
    ),
    "marginal": (
        "Harmonic limit set to -48.5 dBm, right next to the real level. Expect marginal.",
        {},
        dict(measurement_freq_hz=2 * CARRIER_HZ, spec_limit_dbm=-48.5,
             test_name="Spurious emission, tight limit"),
    ),
    "bad_reading": (
        "Analyser returns 9.91E37 (no valid data). Expect abort, RF still turned off.",
        dict(broken_reading="9.91E37"),
        dict(measurement_freq_hz=2 * CARRIER_HZ, spec_limit_dbm=-30.0, num_sweeps=1,
             test_name="Bad reading"),
    ),
    "crash": (
        "LAN drops mid-sweep. Expect abort, RF still turned off, both links closed.",
        dict(crash_on="INIT:IMM"),
        dict(measurement_freq_hz=2 * CARRIER_HZ, spec_limit_dbm=-30.0, num_sweeps=1,
             test_name="Crash mid-test"),
    ),
}


def run(name):
    description, bench_kwargs, cfg_kwargs = SCENARIOS[name]
    print("=" * 72)
    print(f"SCENARIO: {name}\n  {description}\n")
    bench = SimulatedBench(**bench_kwargs)
    links = {"gen": SimLink("generator", bench), "analyser": SimLink("analyser", bench)}
    rct.connect_instrument = lambda ip, port, timeout_s=5.0: links[ip]
    print("  SCPI traffic (<- sent to instrument, -> reply):")
    try:
        result = rct.run_conformance_test(base_config(**cfg_kwargs))
    except Exception as exc:
        print(f"\n  TEST ABORTED: {type(exc).__name__}: {exc}")
        print(f"  RF left on after abort? {bench.rf_on}\n")
        return
    print("\n  REPORT:")
    print("  " + result.as_human_readable().replace("\n", "\n  "))


def main():
    random.seed(1)
    logging.basicConfig(level=logging.CRITICAL)
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        run(name)


if __name__ == "__main__":
    main()
