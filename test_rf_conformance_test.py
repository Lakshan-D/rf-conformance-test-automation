"""Unit tests for rf_conformance_test.py, using a fake instrument link.

No hardware needed: FakeLink records every SCPI command and returns canned
replies, so the test logic, cleanup and reporting can be checked in CI.
"""

from __future__ import annotations

import json

import pytest

import rf_conformance_test as rct


class FakeLink:
    """Stands in for a real LAN/SCPI connection."""

    def __init__(self, name, log, readings=None, errors=None, fail_on=None):
        self.name = name
        self.log = log                      # shared list, so order across instruments is visible
        self.readings = list(readings or [])
        self.errors = list(errors or [])    # queued SYST:ERR? replies before "+0"
        self.fail_on = fail_on              # command that raises, to simulate a crash
        self.closed = False

    def send(self, cmd):
        self.log.append((self.name, cmd))
        if self.fail_on and cmd.startswith(self.fail_on):
            raise ConnectionError(f"link dropped on {cmd}")

    def query(self, cmd):
        self.log.append((self.name, cmd))
        if cmd == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else '+0,"No error"'
        if cmd == "CALC:MARK1:Y?":
            return self.readings.pop(0)
        if cmd == "*OPC?":
            return "1"
        if cmd == "*IDN?":
            return f"Fake,{self.name},0,1.0"
        return ""

    def close(self):
        self.log.append((self.name, "CLOSE"))
        self.closed = True


def make_config(**overrides):
    base = dict(
        generator_ip="gen",
        generator_port=5025,
        analyser_ip="sa",
        analyser_port=5025,
        test_frequency_hz=2.4e9,
        test_power_dbm=-10.0,
        analyser_span_hz=10e6,
        analyser_rbw_hz=100e3,
        analyser_vbw_hz=100e3,
        spec_limit_dbm=-30.0,
        measurement_freq_hz=4.8e9,
        settle_time_s=0.0,
    )
    base.update(overrides)
    return rct.TestConfig(**base)


@pytest.fixture
def bench(monkeypatch):
    """Patch connect_instrument so each IP gets a FakeLink. Returns (links, log)."""
    log = []
    links = {}

    def setup(sa_readings, gen_kwargs=None, sa_kwargs=None):
        links["gen"] = FakeLink("gen", log, **(gen_kwargs or {}))
        links["sa"] = FakeLink("sa", log, readings=sa_readings, **(sa_kwargs or {}))
        monkeypatch.setattr(rct, "connect_instrument", lambda ip, port, timeout_s=5.0: links[ip])
        return links, log

    return setup


# --- verdicts -----------------------------------------------------------------

def test_pass_with_margin(bench):
    bench(["-45.0"])
    result = rct.run_conformance_test(make_config())
    assert result.passed
    assert result.margin_db == pytest.approx(15.0)
    assert result.measured_freq_hz == 4.8e9


def test_fail_with_negative_margin(bench):
    bench(["-25.0"])
    result = rct.run_conformance_test(make_config())
    assert not result.passed
    assert result.margin_db == pytest.approx(-5.0)


def test_lower_limit(bench):
    bench(["-8.0"])
    result = rct.run_conformance_test(
        make_config(spec_limit_dbm=-10.0, limit_type=rct.LimitType.LOWER, measurement_freq_hz=None)
    )
    assert result.passed
    assert result.margin_db == pytest.approx(2.0)
    assert result.measured_freq_hz == 2.4e9  # None means "measure on the carrier"


def test_path_loss_is_added_back(bench):
    bench(["-32.0"])
    result = rct.run_conformance_test(make_config(path_loss_db=3.0))
    assert result.measured_dbm == pytest.approx(-29.0)
    assert not result.passed


def test_marginal_flag(bench):
    bench(["-30.5"])
    result = rct.run_conformance_test(make_config(measurement_uncertainty_db=1.0))
    assert result.passed and result.marginal


def test_averaging_is_done_in_linear_power(bench):
    bench(["-10.0", "-20.0"])
    result = rct.run_conformance_test(make_config(num_sweeps=2, spec_limit_dbm=0.0))
    # mean of 0.1 mW and 0.01 mW = 0.055 mW = -12.60 dBm (not -15)
    assert result.measured_dbm == pytest.approx(-12.596, abs=1e-3)


# --- analyser looks where we think it looks ------------------------------------

def test_analyser_centred_on_measurement_frequency(bench):
    _, log = bench(["-45.0"])
    rct.run_conformance_test(make_config())
    assert ("sa", "SENS:FREQ:CENT 4800000000.0HZ") in log
    assert ("gen", "SOUR:FREQ:FIXED 2400000000.0HZ") in log
    assert ("sa", "INIT:CONT OFF") in log


# --- bad readings are errors, not results --------------------------------------

@pytest.mark.parametrize("raw", ["9.91E37", "nan", "garbage", "120.0"])
def test_implausible_reading_aborts(bench, raw):
    links, _ = bench([raw])
    with pytest.raises(rct.MeasurementError):
        rct.run_conformance_test(make_config())
    assert links["gen"].closed and links["sa"].closed


# --- error queue ---------------------------------------------------------------

def test_all_queued_errors_reported(bench):
    bench(["-45.0"], gen_kwargs={"errors": ['-113,"Undefined header"', '-222,"Data out of range"']})
    with pytest.raises(rct.InstrumentError) as exc:
        rct.run_conformance_test(make_config())
    assert "-113" in str(exc.value) and "-222" in str(exc.value)


# --- cleanup -------------------------------------------------------------------

def test_rf_off_happens_before_anything_closes(bench):
    _, log = bench(["-45.0"])
    rct.run_conformance_test(make_config())
    off = log.index(("gen", "OUTP OFF"))
    assert off < log.index(("gen", "CLOSE"))
    assert off < log.index(("sa", "CLOSE"))


def test_crash_mid_test_still_turns_rf_off_and_closes(bench):
    links, log = bench(["-45.0"], sa_kwargs={"fail_on": "INIT:IMM"})
    with pytest.raises(ConnectionError):
        rct.run_conformance_test(make_config())
    assert ("gen", "OUTP ON") in log
    assert ("gen", "OUTP OFF") in log
    assert links["gen"].closed and links["sa"].closed


def test_reset_then_clear(bench):
    _, log = bench(["-45.0"])
    rct.run_conformance_test(make_config())
    gen_cmds = [c for n, c in log if n == "gen"]
    assert gen_cmds.index("*RST") < gen_cmds.index("*CLS")


# --- reporting -----------------------------------------------------------------

def test_reports(bench, tmp_path, capsys):
    bench(["-45.0"])
    result = rct.run_conformance_test(make_config())
    path = tmp_path / "out.json"
    rct.write_reports(result, str(path))
    data = json.loads(path.read_text())
    assert data["passed"] is True and data["margin_db"] == pytest.approx(15.0)
    assert "PASS" in capsys.readouterr().out
