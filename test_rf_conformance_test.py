"""Unit tests for rf_conformance_test.py, run against a fake InstrumentLink (no real hardware needed). Run with pytest, or directly with `python test_rf_conformance_test.py`."""
import rf_conformance_test as rf


class FakeLink:
    def __init__(self, peak_value="-25.3"):
        self.peak_value = peak_value
        self.closed = False
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)
        if cmd == "RAISE_ON_SEND":
            raise RuntimeError("simulated comms failure")

    def query(self, cmd):
        if cmd == "SYST:ERR?":
            return '+0,"No error"'
        if cmd == "CALC:MARK1:Y?":
            return self.peak_value
        if cmd == "*OPC?":
            return "1"
        return ""

    def close(self):
        self.closed = True


def test_pass_case(monkeypatch):
    links = {"gen": FakeLink(), "an": FakeLink(peak_value="-25.3")}

    def fake_connect(ip, port, timeout_s=5.0):
        return links["gen"] if port == 5025 and ip.endswith(".10") else links["an"]

    monkeypatch.setattr(rf, "connect_instrument", fake_connect)

    config = rf.TestConfig(
        generator_ip="192.168.1.10", generator_port=5025,
        analyser_ip="192.168.1.11", analyser_port=5025,
        test_frequency_hz=2_400_000_000.0, test_power_dbm=-10.0,
        analyser_span_hz=10_000_000.0, analyser_rbw_hz=100_000.0, analyser_vbw_hz=100_000.0,
        spec_limit_dbm=-20.0, limit_type=rf.LimitType.UPPER,
    )
    result = rf.run_conformance_test(config)
    assert result.passed is True
    assert abs(result.margin_db - 5.3) < 1e-9
    assert links["gen"].closed and links["an"].closed
    assert "OUTP OFF" in links["gen"].sent


def test_fail_case(monkeypatch):
    links = {"gen": FakeLink(), "an": FakeLink(peak_value="-15.0")}

    def fake_connect(ip, port, timeout_s=5.0):
        return links["gen"] if ip.endswith(".10") else links["an"]

    monkeypatch.setattr(rf, "connect_instrument", fake_connect)

    config = rf.TestConfig(
        generator_ip="192.168.1.10", generator_port=5025,
        analyser_ip="192.168.1.11", analyser_port=5025,
        test_frequency_hz=2_400_000_000.0, test_power_dbm=-10.0,
        analyser_span_hz=10_000_000.0, analyser_rbw_hz=100_000.0, analyser_vbw_hz=100_000.0,
        spec_limit_dbm=-20.0, limit_type=rf.LimitType.UPPER,
    )
    result = rf.run_conformance_test(config)
    assert result.passed is False
    assert result.margin_db < 0


def test_cleanup_runs_on_mid_test_failure(monkeypatch):
    gen = FakeLink()
    an = FakeLink()

    def fake_connect(ip, port, timeout_s=5.0):
        return gen if ip.endswith(".10") else an

    monkeypatch.setattr(rf, "connect_instrument", fake_connect)

    # Force the analyser's configure step to blow up after the generator
    # session has already started, to prove cleanup still runs.
    def broken_configure(self, **kwargs):
        raise rf.InstrumentError("simulated analyser fault")

    monkeypatch.setattr(rf.SpectrumAnalyser, "configure", broken_configure)

    config = rf.TestConfig(
        generator_ip="192.168.1.10", generator_port=5025,
        analyser_ip="192.168.1.11", analyser_port=5025,
        test_frequency_hz=2_400_000_000.0, test_power_dbm=-10.0,
        analyser_span_hz=10_000_000.0, analyser_rbw_hz=100_000.0, analyser_vbw_hz=100_000.0,
        spec_limit_dbm=-20.0, limit_type=rf.LimitType.UPPER,
    )

    try:
        rf.run_conformance_test(config)
        assert False, "expected InstrumentError to propagate"
    except rf.InstrumentError:
        pass

    assert gen.closed, "generator must still be closed after a mid-test failure"
    assert an.closed, "analyser must still be closed after a mid-test failure"
    assert "OUTP OFF" in gen.sent, "generator output must still be turned off after a mid-test failure"


if __name__ == "__main__":
    import types

    class MonkeyPatch:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    mp = MonkeyPatch()
    test_pass_case(mp)
    test_fail_case(mp)
    test_cleanup_runs_on_mid_test_failure(mp)
    print("all smoke tests passed")
