"""
rf_conformance_test.py

Automated RF conformance test: configures a signal generator and a spectrum
analyser over SCPI/LAN, runs a single measurement, checks it against a spec
limit, calculates the compliance margin, and reports the result in both
human-readable and machine-readable form.

Low-level instrument communication (LAN connect, SCPI send/query, close) is
assumed to already exist elsewhere in the test framework; connect_instrument()
below is a placeholder for that so this module can be read and type-checked
on its own. SCPI command syntax is realistic but illustrative where the exact
command set for a specific instrument family isn't known.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterator, Protocol

logger = logging.getLogger("rf_conformance_test")


# ---------------------------------------------------------------------------
# Low-level instrument I/O
#
# Assumed to already exist in the surrounding test framework. A Protocol is
# used here (rather than a concrete class) so the rest of the module can be
# type-checked and unit-tested against a fake, without caring whether the
# real implementation is VISA, raw sockets, or something else.
# ---------------------------------------------------------------------------

class InstrumentLink(Protocol):
    def send(self, scpi_command: str) -> None: ...
    def query(self, scpi_command: str) -> str: ...
    def close(self) -> None: ...


def connect_instrument(ip: str, port: int, timeout_s: float = 5.0) -> InstrumentLink:
    """Placeholder for the existing LAN/SCPI connection helper.

    Replace with the real comms-layer call (e.g. a VISA resource open or a
    raw TCP socket wrapped with SCPI framing). Kept here only so this module
    is runnable and reviewable on its own.
    """
    raise NotImplementedError("Provided by the existing instrument comms layer")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class InstrumentError(RuntimeError):
    """Raised when an instrument reports a SCPI error or fails to respond."""


# ---------------------------------------------------------------------------
# Instrument wrappers
# ---------------------------------------------------------------------------

class ScpiInstrument:
    """Behaviour shared by the generator and the analyser."""

    def __init__(self, name: str, link: InstrumentLink) -> None:
        self.name = name
        self._link = link

    def reset(self) -> None:
        self._link.send("*CLS")
        self._link.send("*RST")

    def identify(self) -> str:
        return self._link.query("*IDN?").strip()

    def check_errors(self) -> None:
        """Raise if the instrument's own error queue is non-empty.

        A malformed SCPI command is often silently ignored rather than
        raising on the comms layer, so the error queue is checked explicitly
        after every configuration step instead of only trusting exceptions.
        """
        response = self._link.query("SYST:ERR?").strip()
        code = response.split(",", 1)[0].strip()  # e.g. '+0,"No error"'
        if code not in ("0", "+0"):
            raise InstrumentError(f"{self.name} reported SCPI error: {response}")

    def close(self) -> None:
        self._link.close()


class SignalGenerator(ScpiInstrument):
    def configure(self, frequency_hz: float, power_dbm: float) -> None:
        self._link.send(f"SOUR:FREQ:FIXED {frequency_hz:.1f}HZ")
        self._link.send(f"SOUR:POW {power_dbm:.2f}DBM")
        self.check_errors()

    def output_on(self) -> None:
        self._link.send("OUTP ON")
        self.check_errors()

    def output_off(self) -> None:
        self._link.send("OUTP OFF")
        self.check_errors()


class SpectrumAnalyser(ScpiInstrument):
    def configure(self, center_freq_hz: float, span_hz: float, rbw_hz: float, vbw_hz: float) -> None:
        self._link.send(f"SENS:FREQ:CENT {center_freq_hz:.1f}HZ")
        self._link.send(f"SENS:FREQ:SPAN {span_hz:.1f}HZ")
        self._link.send(f"SENS:BAND:RES {rbw_hz:.1f}HZ")
        self._link.send(f"SENS:BAND:VID {vbw_hz:.1f}HZ")
        self.check_errors()

    def measure_peak_power_dbm(self) -> float:
        """Trigger a single sweep and read back the marker peak power.

        If the real analyser API already exposes the measured value
        directly (per the brief's assumption), this is the only method
        that would need to change; everything downstream of it is unaware
        of how the number was obtained.
        """
        self._link.send("INIT:IMM")
        self._link.query("*OPC?")  # block until the sweep completes
        self._link.send("CALC:MARK1:MAX")
        value = self._link.query("CALC:MARK1:Y?").strip()
        self.check_errors()
        return float(value)


# ---------------------------------------------------------------------------
# Test configuration and results
# ---------------------------------------------------------------------------

class LimitType(str, Enum):
    UPPER = "upper"  # measured value must be <= limit
    LOWER = "lower"  # measured value must be >= limit


@dataclass(frozen=True)
class TestConfig:
    generator_ip: str
    generator_port: int
    analyser_ip: str
    analyser_port: int
    test_frequency_hz: float
    test_power_dbm: float
    analyser_span_hz: float
    analyser_rbw_hz: float
    analyser_vbw_hz: float
    spec_limit_dbm: float
    limit_type: LimitType = LimitType.UPPER
    test_name: str = "RF Conformance Test"


@dataclass(frozen=True)
class TestResult:
    test_name: str
    timestamp_utc: str
    measured_dbm: float
    limit_dbm: float
    limit_type: str
    margin_db: float
    passed: bool

    def as_human_readable(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"{self.test_name}\n"
            f"  Timestamp      : {self.timestamp_utc}\n"
            f"  Measured       : {self.measured_dbm:.2f} dBm\n"
            f"  Limit ({self.limit_type:<5s}) : {self.limit_dbm:.2f} dBm\n"
            f"  Margin         : {self.margin_db:+.2f} dB\n"
            f"  Result         : {verdict}\n"
        )

    def as_machine_readable(self) -> dict:
        return asdict(self)


def _compute_margin(measured_dbm: float, limit_dbm: float, limit_type: LimitType) -> tuple[float, bool]:
    """Positive margin always means pass, with that much headroom."""
    if limit_type == LimitType.UPPER:
        margin = limit_dbm - measured_dbm
    else:
        margin = measured_dbm - limit_dbm
    return margin, margin >= 0.0


# ---------------------------------------------------------------------------
# Instrument sessions (connect + guaranteed cleanup)
#
# Each instrument gets its own context manager. Python's `with` statement
# runs __exit__ on any exception, so if configuration or the measurement
# fails partway through, the generator output still gets turned off and
# both connections still close, without tracking that manually at every
# call site in the test logic itself.
# ---------------------------------------------------------------------------

@contextmanager
def signal_generator_session(ip: str, port: int) -> Iterator[SignalGenerator]:
    link = connect_instrument(ip, port)
    generator = SignalGenerator("Signal Generator", link)
    try:
        generator.reset()
        yield generator
    finally:
        try:
            generator.output_off()
        except Exception:
            logger.warning("Could not confirm generator output was turned off", exc_info=True)
        try:
            generator.close()
        except Exception:
            logger.warning("Failed to close signal generator connection", exc_info=True)


@contextmanager
def spectrum_analyser_session(ip: str, port: int) -> Iterator[SpectrumAnalyser]:
    link = connect_instrument(ip, port)
    analyser = SpectrumAnalyser("Spectrum Analyser", link)
    try:
        analyser.reset()
        yield analyser
    finally:
        try:
            analyser.close()
        except Exception:
            logger.warning("Failed to close spectrum analyser connection", exc_info=True)


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

def run_conformance_test(config: TestConfig) -> TestResult:
    logger.info("Starting %s", config.test_name)

    with signal_generator_session(config.generator_ip, config.generator_port) as generator, \
         spectrum_analyser_session(config.analyser_ip, config.analyser_port) as analyser:

        generator.configure(config.test_frequency_hz, config.test_power_dbm)
        analyser.configure(
            center_freq_hz=config.test_frequency_hz,
            span_hz=config.analyser_span_hz,
            rbw_hz=config.analyser_rbw_hz,
            vbw_hz=config.analyser_vbw_hz,
        )

        generator.output_on()
        measured_dbm = analyser.measure_peak_power_dbm()

    margin_db, passed = _compute_margin(measured_dbm, config.spec_limit_dbm, config.limit_type)

    return TestResult(
        test_name=config.test_name,
        timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        measured_dbm=measured_dbm,
        limit_dbm=config.spec_limit_dbm,
        limit_type=config.limit_type.value,
        margin_db=margin_db,
        passed=passed,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_reports(result: TestResult, json_path: str = "rf_test_result.json") -> None:
    print(result.as_human_readable())
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result.as_machine_readable(), f, indent=2)
    logger.info("Machine-readable report written to %s", json_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = TestConfig(
        generator_ip="192.168.1.10",
        generator_port=5025,
        analyser_ip="192.168.1.11",
        analyser_port=5025,
        test_frequency_hz=2_400_000_000.0,
        test_power_dbm=-10.0,
        analyser_span_hz=10_000_000.0,
        analyser_rbw_hz=100_000.0,
        analyser_vbw_hz=100_000.0,
        spec_limit_dbm=-20.0,
        limit_type=LimitType.UPPER,
        test_name="2.4 GHz Spurious Emission Conformance Test",
    )

    try:
        result = run_conformance_test(config)
    except Exception:
        logger.error("Test aborted, see traceback above", exc_info=True)
        return 1

    write_reports(result)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
