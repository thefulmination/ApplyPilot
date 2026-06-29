"""The Chrome-slot derivation that lets multiple apply workers share ONE machine
without their browsers colliding (slot keys the profile + CDP port + per-run logs)."""
from applypilot.fleet.apply_worker_main import _chrome_slot


def test_chrome_slot_auto_derives_from_worker_id():
    assert _chrome_slot("home-0") == 0
    assert _chrome_slot("home-1") == 1
    assert _chrome_slot("home-2") == 2


def test_chrome_slot_non_numeric_falls_back_to_zero():
    assert _chrome_slot("homebox") == 0
    assert _chrome_slot("") == 0
    assert _chrome_slot(None) == 0


def test_chrome_slot_caps_0_to_9():
    assert _chrome_slot("home-15") == 5   # 15 % 10


def test_chrome_slot_explicit_override_wins():
    assert _chrome_slot("home-1", 3) == 3
    assert _chrome_slot("home-1", 12) == 2  # override is capped too
