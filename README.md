# RF Conformance Test Automation

A small automated RF conformance test: a signal generator and a spectrum analyser are configured and driven remotely over SCPI/LAN, a measurement is taken, checked against a spec limit, and reported with a compliance margin in both human-readable and machine-readable form.

Written for a technical challenge (AccelerComm, Graduate System Test Engineer). Brief: configure the instruments, run a measurement, compare it against a limit, calculate the margin, report PASS/FAIL in both forms, and handle errors and instrument cleanup properly.

## What's here

- `rf_conformance_test.py`: the solution.
- `test_rf_conformance_test.py`: unit tests against a fake instrument link (no real hardware needed to run them).
- `demo_simulated_bench.py`: runs the test against a simulated bench and prints every SCPI command and reply, so you can watch it work with no hardware.
- `CHANGELOG.md`: what changed in v1.1 after a post-submission review, and why.

## v1.1: post-submission review

After submitting, I went back through the code line by line and found a real mistake. The demo was labelled a spurious emission test, but the analyser was centred on the 2.4 GHz carrier itself, so it was really measuring output power against a spurious limit and failed by 10 dB every time.

v1.1 adds `measurement_freq_hz`, so the generator transmits on the carrier and the analyser can look somewhere else. Leave it as `None` for an output power test (measure on the carrier), or set it away from the carrier for a spurious test. The demo now checks the 2nd harmonic at 4.8 GHz.

| Test | `measurement_freq_hz` | Limit type |
|---|---|---|
| Output power | `None` (measure on the carrier) | usually `LOWER` |
| Spurious emission | away from the carrier, e.g. 2nd harmonic | `UPPER` |

The same review also added a plausibility check on readings (so `9.91E37` aborts instead of producing a verdict), a full error-queue drain, `*RST` before `*CLS`, single-sweep mode, a settle time after RF on, path loss correction, averaging in linear power, and a marginal flag for results inside the measurement uncertainty. Details are in [CHANGELOG.md](CHANGELOG.md). The original submission is the first commit.

## Approach

Three layers: a thin SCPI wrapper shared by both instruments (reset, identify, read the error queue), instrument-specific classes for the generator and the analyser, and a top-level test function that only deals with conformance-test logic (configure, measure, compare, report). Keeping those separate means the SCPI command set can change without touching the pass/fail logic, and the test logic can be unit tested against a fake instrument instead of real hardware.

Instrument lifecycle uses Python context managers for both the generator and the analyser rather than manual try/finally blocks scattered through the test function. A `with` statement already guarantees its exit code runs on any exception, so if configuration or the measurement fails partway through, the generator output still gets turned off and both connections still close, without tracking that manually at every call site. The generator's cleanup explicitly turns the output off before closing, since leaving an RF source live after a failed test causes real problems on a shared bench. The analyser session is opened first, so (because `with` blocks exit in reverse order) turning RF off is the very first cleanup step.

The compliance margin is calculated as limit minus measured for an upper limit, and measured minus limit for a lower limit, so a positive margin always means pass with that much headroom, and a negative margin always means fail by that amount, regardless of which direction the spec runs.

Errors are checked two ways: after every SCPI configuration call, the instrument's own error queue (`SYST:ERR?`) is queried, since an instrument can silently ignore a malformed command rather than raising anything on the comms layer; and any Python-level exception (a timeout, a dropped connection, a bad value) propagates up through the context managers, so cleanup still runs before the exception reaches the caller. Retries were left out deliberately: a failed measurement should fail loudly and visibly rather than quietly retrying and masking an intermittent fault.

Reporting is split into a human-readable block (printed for whoever is watching the test run) and a machine-readable JSON file (for a CI pipeline or a results database), both built from the same `TestResult` dataclass so the two views can't drift out of sync.

## Assumptions

The brief states that instrument communication functions already exist and that the measured value is available from the analyser API, so `connect_instrument()` is treated as the existing low-level comms layer, and `measure_peak_power_dbm()` is written as a thin wrapper over a realistic single-sweep-plus-marker-peak SCPI sequence (`INIT:IMM`, `*OPC?`, `CALC:MARK1:MAX`, `CALC:MARK1:Y?`). If the real analyser API already returns the measured value directly, that one method is the only thing that would need to change; everything downstream of it stays the same.

One sweep per run is the default (`num_sweeps=1`), since the brief didn't specify a repeat count; v1.1 can average several sweeps in linear power. The test is single-limit, single-frequency rather than a frequency sweep against a limit line. Extending to a sweep would mean looping the measure-compare step and collecting a list of `TestResult` objects rather than changing the core design.

## Setup

Everything runs inside a virtual environment. The module itself only uses the standard library; pytest is the only dependency.

Windows (Command Prompt):

```bat
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements-dev.txt
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

## Simulated bench demo

```bash
python demo_simulated_bench.py            # all scenarios
python demo_simulated_bench.py harmonic   # one scenario
```

The simulated generator transmits a carrier plus a weaker 2nd harmonic, and the simulated analyser returns the power at whatever frequency it is tuned to, minus cable loss, plus a little noise. Scenarios: `harmonic` (spurious test, PASS), `v1_bug` (the v1.0 mistake, analyser on the carrier, FAIL), `output_power` (lower limit, marginal PASS), `marginal` (marginal FAIL), `bad_reading` (`9.91E37`, abort with RF off), `crash` (link drops mid-sweep, abort with RF off and both links closed).

## Running the tests

```bash
python -m pytest -v
```

The tests exercise the module against a fake `InstrumentLink`: pass and fail cases, upper and lower limits, path loss, averaging, the marginal flag, the analyser being centred on the measurement frequency rather than the carrier, bad readings (`9.91E37`, NaN, text, out of range) aborting the test, multiple queued SCPI errors all being reported, RF being turned off before either connection closes, and a crash mid-sweep still leaving RF off and both instruments closed.

## AI-assisted development

This project was developed with AI assistance (Claude). After submitting, I reviewed the code line by line, found that the demo was measuring the carrier instead of a spurious emission, and fixed that along with several robustness issues in v1.1.

## Author

Lakshan Divakar, [linkedin.com/in/lakshan-d](https://linkedin.com/in/lakshan-d)
