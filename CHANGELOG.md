# Changelog

## v1.1 (post-submission review)

I went back through the submitted version line by line and fixed the things I'd want fixed before this ran on a real bench. The structure is the same: same classes, same session context managers, same reporting.

### The main fix: the demo measured the wrong thing

v1.0 called the demo a "2.4 GHz Spurious Emission" test, but it centred the analyser on the 2.4 GHz carrier itself. So it measured the carrier (about -10 dBm) against a -20 dBm spurious limit and failed by 10 dB every time. That's an output power measurement with a spurious label on it.

- Added `measurement_freq_hz` to `TestConfig`. The generator still transmits at `test_frequency_hz`, but the analyser now centres on `measurement_freq_hz`.
- `None` (default) means "measure on the carrier", which is the right setting for an output power test.
- The demo now looks at the 2nd harmonic (4.8 GHz), which is where a spurious emission actually shows up.
- The report and JSON now show both the carrier frequency and the frequency that was measured, so nobody has to guess.

### Robustness

- **Bad readings are rejected.** SCPI returns `9.91E37` when there's no valid value. v1.0 would have turned that into a number and given a verdict. Non-numeric, NaN, `9.91E37` and physically implausible readings now raise `MeasurementError`, so the test aborts instead of reporting a fake PASS or FAIL.
- **Whole error queue is read.** `check_errors()` now reads `SYST:ERR?` until it returns 0 and reports every error, not just the first one. It's capped at 20 reads, so a broken instrument can't loop forever.
- **`*RST` then `*CLS`.** The order was the wrong way round. Reset first, then clear, so anything the reset queues gets cleared too.
- **`INIT:CONT OFF`** before `INIT:IMM`, so the analyser does exactly one sweep and stops.
- **Settle time** after `OUTP ON` (`settle_time_s`, default 0.2 s), so the source has settled before the first reading.
- **RF off runs first in cleanup.** The analyser session is now opened first. `with` exits in reverse order, so the generator's RF off is the first thing that happens if anything fails.
- **Reference level and attenuation** are set on the analyser (`analyser_ref_level_dbm`, auto attenuation), so the front end isn't overloaded.

### Measurement quality

- **Path loss.** `path_loss_db` adds back the calibrated cable/attenuator loss, so the result refers to the DUT's port, not the analyser's input.
- **Averaging.** `num_sweeps` takes several readings and averages them in linear power (mW), not in dB.
- **Marginal flag.** `measurement_uncertainty_db` marks any result whose margin is smaller than the bench uncertainty as `marginal`. A pass by 0.3 dB on a 1 dB bench isn't a confident pass.

### Tests

`test_rf_conformance_test.py` runs against a fake instrument link (no hardware):

- pass, fail, lower limit, path loss, averaging, marginal
- analyser centred on the measurement frequency, not the carrier
- `9.91E37`, NaN, text and out-of-range readings all abort and still clean up
- multiple queued SCPI errors are all reported
- RF off happens before either connection closes
- a crash mid-sweep still turns RF off and closes both instruments

Run with `python -m pytest`.

## v1.0

Original submission for the AccelerComm technical challenge.
