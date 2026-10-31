"""Key server check (no real key posted): `BSPM_KEYS_PORT=1 python test_keys.py`.
An error in the window must come back as an error, not kill the server; a dropped
connection must reconnect (2026-10-31: BrokenPipe on every p-lock release)."""
import os
import keys

calls = []


def fake(down):
    calls.append(down)
    if len(calls) == 1:
        raise ValueError("boom")


keys._direct_shift, keys._direct_can = fake, lambda: True
os.environ["BSPM_KEYS_PORT"] = str(keys.serve())
try:
    keys.shift(True)
    raise SystemExit("error not reported")
except RuntimeError as e:
    assert "boom" in str(e), e
keys.shift(True); keys.shift(False)
assert calls == [True, True, False]             # the server survived its error
keys._conn.close()                              # connection gone
keys.shift(False)
assert calls[-1] is False and len(calls) == 4   # reconnected and served
assert keys.can_hold_keys()
print("key server ok")
