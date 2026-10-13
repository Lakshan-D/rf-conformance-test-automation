# RF Conformance Test Automation

A small automated RF conformance test: a signal generator and a spectrum analyser are configured and driven remotely over SCPI/LAN, a measurement is taken, checked against a spec limit, and reported with a compliance margin in both human-readable and machine-readable form.

Written for a technical challenge (AccelerComm, Graduate System Test Engineer). Brief: configure the instruments, run a measurement, compare it against a limit, calculate the margin, report PASS/FAIL in both forms, and handle errors and instrument cleanup properly.

## What's here

- `rf_conformance_test.py` — the solution.
- `test_rf_conformance_test.py` — unit tests against a fake instrument link (no real hardware needed to run them).

## Approach

Three layers: a thin SCPI wrapper shared by both instruments (reset, identify, read the error queue), instrument-specific classes for the generator and the analyser, and a top-level test function that only deals with conformance-test logic (configure, measure, compare, report). Keeping those separate means the SCPI command set can change without touching the pass/fail logic, and the test logic can be unit tested against a fake instrument instead of real hardware.

Instrument lifecycle uses Python context managers for both the generator and the analyser rather than manual try/finally blocks scattered through the test function. A `with` statement already guarantees its exit code runs on any exception, so if configuration or the measurement fails partway through, the generator output still gets turned off and both connections still close, without tracking that manually at every call site. The generator's cleanup explicitly turns the output off before closing, since leaving an RF source live after a failed test causes real problems on a shared bench.

The compliance margin is calculated as limit minus measured for an upper limit, and measured minus limit for a lower limit, so a positive margin always means pass with that much headroom, and a negative margin always means fail by that amount, regardless of which direction the spec runs.

Errors are checked two ways: after every SCPI configuration call, the instrument's own error queue (`SYST:ERR?`) is queried, since an instrument can silently ignore a malformed command rather than raising anything on the comms layer; and any Python-level exception (a timeout, a dropped connection, a bad value) propagates up through the context managers, so cleanup still runs before the exception reaches the caller. Retries were left out deliberately: a failed measurement should fail loudly and visibly rather than quietly retrying and masking an intermittent fault.

Reporting is split into a human-readable block (printed for whoever is watching the test run) and a machine-readable JSON file (for a CI pipeline or a results database), both built from the same `TestResult` dataclass so the two views can't drift out of sync.

## Assumptions

The brief states that instrument communication functions already exist and that the measured value is available from the analyser API, so `connect_instrument()` is treated as the existing low-level comms layer, and `measure_peak_power_dbm()` is written as a thin wrapper over a realistic single-sweep-plus-marker-peak SCPI sequence (`INIT:IMM`, `*OPC?`, `CALC:MARK1:MAX`, `CALC:MARK1:Y?`). If the real analyser API already returns the measured value directly, that one method is the only thing that would need to change; everything downstream of it stays the same.

A single measurement per run is assumed rather than averaging over several sweeps, since the brief didn't specify a repeat count, and a single-limit, single-frequency test rather than a frequency sweep against a limit line. Extending to a sweep would mean looping the measure-compare step and collecting a list of `TestResult` objects rather than changing the core design.

## Running the tests

```bash
pip install pytest
pytest test_rf_conformance_test.py -v
```

Or without pytest:

```bash
python test_rf_conformance_test.py
```

The tests exercise the module against a fake `InstrumentLink`: a pass case, a fail case, and a case where the analyser's configure step raises partway through the test, to confirm both instruments still get closed and the generator output still gets turned off even when the test fails midway.

## AI-assisted development

Claude helped scaffold the module structure and talk through a couple of edge cases while writing this, in particular the margin-sign logic for upper versus lower limits, and whether to use a context manager or a manual try/finally for cleanup. The design decisions and the final code are mine; I reviewed and adjusted what it suggested rather than taking it as given.

## Author

Lakshan Divakar — [linkedin.com/in/lakshan-d](https://linkedin.com/in/lakshan-d)
