"""
rf_conformance_test.py

Automated RF conformance test: configures a signal generator and a spectrum
analyser over SCPI/LAN, runs a measurement, checks it against a spec limit,
calculates the compliance margin, and reports the result in both
human-readable and machine-readable form.

Low-level instrument communication (LAN connect, SCPI send/query, close) is
assumed to already exist elsewhere in the test framework; connect_instrument()
below is a placeholder for that so this module can be read and type-checked
on its own. SCPI command syntax is realistic but illustrative where the exact
command set for a specific instrument family isn't known.

v1.1 (post-submission review): see CHANGELOG.md for what changed and why.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterator, Optional, Protocol

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


class MeasurementError(RuntimeError):
    """Raised when a reading comes back but can't be trusted."""


# ---------------------------------------------------------------------------
# Instrument wrappers
# ---------------------------------------------------------------------------

# SCPI instruments return 9.91E37 when a value isn't available ("not a number").
SCPI_NAN = 9.91e37

# Anything outside this window is treated as a broken reading, not a result.
PLAUSIBLE_MIN_DBM = -200.0
PLAUSIBLE_MAX_DBM = 40.0

# Upper bound on error-queue reads, so a misbehaving instrument can't loop us forever.
MAX_ERROR_QUEUE_READS = 20


class ScpiInstrument:
    """Behaviour shared by the generator and the analyser."""

    def __init__(self, name: str, link: InstrumentLink) -> None:
        self.name = name
        self._link = link

    def reset(self) -> None:
        # Reset first, then clear, so anything the reset itself queues is cleared too.
        self._link.send("*RST")
        self._link.send("*CLS")

    def identify(self) -> str:
        return self._link.query("*IDN?").strip()

    def check_errors(self) -> None:
        """Drain the instrument's error queue and raise if anything was in it.

        A malformed SCPI command is often silently ignored rather than
        raising on the comms layer, so the error queue is checked explicitly
        after every configuration step. Several errors can stack up, so the
        queue is read until it reports 0, and every error is reported.
        """
        errors = []
        for _ in range(MAX_ERROR_QUEUE_READS):
            response = self._link.query("SYST:ERR?").strip()
            code = response.split(",", 1)[0].strip()  # e.g. '+0,"No error"'
            if code in ("0", "+0", "-0"):
                break
            errors.append(response)
        else:
            errors.append(f"(error queue not empty after {MAX_ERROR_QUEUE_READS} reads)")
        if errors:
            raise InstrumentError(f"{self.name} reported SCPI error(s): {'; '.join(errors)}")

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
    def configure(
        self,
        center_freq_hz: float,
        span_hz: float,
        rbw_hz: float,
        vbw_hz: float,
        ref_level_dbm: float,
    ) -> None:
        # Single-sweep mode, so INIT:IMM really means "one sweep, then stop".
        self._link.send("INIT:CONT OFF")
        self._link.send(f"SENS:FREQ:CENT {center_freq_hz:.1f}HZ")
        self._link.send(f"SENS:FREQ:SPAN {span_hz:.1f}HZ")
        self._link.send(f"SENS:BAND:RES {rbw_hz:.1f}HZ")
        self._link.send(f"SENS:BAND:VID {vbw_hz:.1f}HZ")
        # Reference level above the expected peak, input attenuation coupled to it,
        # so the analyser front end can't be overloaded.
        self._link.send(f"DISP:WIND:TRAC:Y:RLEV {ref_level_dbm:.2f}DBM")
        self._link.send("INP:ATT:AUTO ON")
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
        raw = self._link.query("CALC:MARK1:Y?").strip()
        self.check_errors()
        return _parse_power_reading(raw, self.name)


def _parse_power_reading(raw: str, source: str) -> float:
    """Turn the analyser's text reply into a number, or refuse it.

    A reading that isn't a number, is the SCPI "not a number" value, or is
    physically implausible is an instrument problem, not a measurement.
    Reporting it as a PASS or FAIL would be misleading, so it raises instead.
    """
    try:
        value = float(raw)
    except ValueError as exc:
        raise MeasurementError(f"{source} returned a non-numeric reading: {raw!r}") from exc
    if math.isnan(value) or math.isinf(value) or abs(value) >= SCPI_NAN:
        raise MeasurementError(f"{source} returned no valid reading: {raw!r}")
    if not (PLAUSIBLE_MIN_DBM <= value <= PLAUSIBLE_MAX_DBM):
        raise MeasurementError(
            f"{source} reading {value:.2f} dBm is outside the plausible range "
            f"[{PLAUSIBLE_MIN_DBM:.0f}, {PLAUSIBLE_MAX_DBM:.0f}] dBm"
        )
    return value


def _average_dbm(readings_dbm: list[float]) -> float:
    """Average power readings in linear units (mW), then convert back to dBm.

    Averaging dBm values directly is wrong: -10 dBm and -20 dBm do not
    average to -15 dBm of power.
    """
    mean_mw = sum(10 ** (r / 10.0) for r in readings_dbm) / len(readings_dbm)
    return 10.0 * math.log10(mean_mw)


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
    test_frequency_hz: float          # where the generator transmits (the carrier)
    test_power_dbm: float
    analyser_span_hz: float
    analyser_rbw_hz: float
    analyser_vbw_hz: float
    spec_limit_dbm: float
    limit_type: LimitType = LimitType.UPPER
    test_name: str = "RF Conformance Test"
    # Where the analyser looks. None = on the carrier (e.g. an output power test).
    # For a spurious emission test, set this away from the carrier, e.g. a harmonic.
    measurement_freq_hz: Optional[float] = None
    analyser_ref_level_dbm: float = 0.0
    # Calibrated cable/attenuator loss between the DUT and the analyser, in dB.
    # Added back to the reading so the result refers to the DUT's port.
    path_loss_db: float = 0.0
    settle_time_s: float = 0.2        # wait after RF on, before measuring
    num_sweeps: int = 1               # readings averaged per result
    # Expanded measurement uncertainty. A margin smaller than this is flagged
    # as marginal: a pass or fail that the bench can't really tell apart.
    measurement_uncertainty_db: float = 0.0

    @property
    def analyser_center_hz(self) -> float:
        if self.measurement_freq_hz is None:
            return self.test_frequency_hz
        return self.measurement_freq_hz


@dataclass(frozen=True)
class TestResult:
    test_name: str
    timestamp_utc: str
    carrier_freq_hz: float
    measured_freq_hz: float
    measured_dbm: float
    limit_dbm: float
    limit_type: str
    margin_db: float
    passed: bool
    marginal: bool
    num_sweeps: int
    path_loss_db: float

    def as_human_readable(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        if self.marginal:
            verdict += " (marginal: within measurement uncertainty)"
        return (
            f"{self.test_name}\n"
            f"   Timestamp      : {self.timestamp_utc}\n"
            f"   Carrier        : {self.carrier_freq_hz / 1e9:.4f} GHz\n"
            f"   Measured at    : {self.measured_freq_hz / 1e9:.4f} GHz\n"
            f"   Measured       : {self.measured_dbm:.2f} dBm "
            f"({self.num_sweeps} sweep(s), path loss {self.path_loss_db:.2f} dB applied)\n"
            f"   Limit ({self.limit_type:<5s}) : {self.limit_dbm:.2f} dBm\n"
            f"   Margin         : {self.margin_db:+.2f} dB\n"
            f"   Result         : {verdict}\n"
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
    if config.num_sweeps < 1:
        raise ValueError("num_sweeps must be at least 1")
    logger.info("Starting %s", config.test_name)

    # The analyser is opened first on purpose: `with` blocks exit in reverse
    # order, so the generator's cleanup (RF off) is the first thing that runs.
    with spectrum_analyser_session(config.analyser_ip, config.analyser_port) as analyser, \
         signal_generator_session(config.generator_ip, config.generator_port) as generator:

        generator.configure(config.test_frequency_hz, config.test_power_dbm)
        analyser.configure(
            center_freq_hz=config.analyser_center_hz,
            span_hz=config.analyser_span_hz,
            rbw_hz=config.analyser_rbw_hz,
            vbw_hz=config.analyser_vbw_hz,
            ref_level_dbm=config.analyser_ref_level_dbm,
        )

        generator.output_on()
        time.sleep(config.settle_time_s)  # let the source settle before trusting a reading

        readings = [analyser.measure_peak_power_dbm() for _ in range(config.num_sweeps)]

    measured_dbm = _average_dbm(readings) + config.path_loss_db
    margin_db, passed = _compute_margin(measured_dbm, config.spec_limit_dbm, config.limit_type)
    marginal = abs(margin_db) < config.measurement_uncertainty_db

    return TestResult(
        test_name=config.test_name,
        timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        carrier_freq_hz=config.test_frequency_hz,
        measured_freq_hz=config.analyser_center_hz,
        measured_dbm=measured_dbm,
        limit_dbm=config.spec_limit_dbm,
        limit_type=config.limit_type.value,
        margin_db=margin_db,
        passed=passed,
        marginal=marginal,
        num_sweeps=config.num_sweeps,
        path_loss_db=config.path_loss_db,
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

    carrier_hz = 2_400_000_000.0

    # Spurious emission check at the 2nd harmonic: the generator transmits at
    # 2.4 GHz, and the analyser looks at 4.8 GHz, away from the carrier.
    # Limit and loss values are illustrative.
    config = TestConfig(
        generator_ip="192.168.1.10",
        generator_port=5025,
        analyser_ip="192.168.1.11",
        analyser_port=5025,
        test_frequency_hz=carrier_hz,
        test_power_dbm=-10.0,
        measurement_freq_hz=2 * carrier_hz,
        analyser_span_hz=10_000_000.0,
        analyser_rbw_hz=100_000.0,
        analyser_vbw_hz=100_000.0,
        analyser_ref_level_dbm=0.0,
        spec_limit_dbm=-30.0,
        limit_type=LimitType.UPPER,
        path_loss_db=1.5,
        num_sweeps=5,
        measurement_uncertainty_db=1.0,
        test_name="2.4 GHz Spurious Emission Test (2nd harmonic, 4.8 GHz)",
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
