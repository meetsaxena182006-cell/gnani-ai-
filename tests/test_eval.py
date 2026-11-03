import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("run_eval", Path(__file__).parent.parent / "eval" / "run_eval.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_wer():
    assert ev.wer("toll pe teen sau diye", "toll pe teen sau diye") == 0
    assert ev.wer("toll pe teen sau diye", "tol pe teen sau diye") == 0.2
    assert ev.wer("a b", "") == 1.0


def test_amounts():
    assert ev.amounts_in("₹4,500 ka diesel, 48 litre") == {4500, 48}
    assert ev.amounts_in("₹४,५००") == {4500}
