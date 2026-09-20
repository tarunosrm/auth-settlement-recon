import random

from authgen.distributions import decline_code, draw_amount, draw_hour


def test_amounts_are_ints_in_clamped_range():
    rng = random.Random(1)
    for _ in range(500):
        a = draw_amount(rng, median_minor=3500, sigma=0.7)
        assert isinstance(a, int) and 100 <= a <= 3500 * 40


def test_hour_draw_stays_in_range_and_favors_evening():
    rng = random.Random(2)
    hours = [draw_hour(rng) for _ in range(2000)]
    assert all(0 <= h <= 23 for h in hours)
    evening = sum(1 for h in hours if 18 <= h <= 21)
    night = sum(1 for h in hours if 2 <= h <= 5)
    assert evening > night * 3  # diurnal shape actually holds


def test_decline_codes_are_valid_iso_style():
    rng = random.Random(3)
    for _ in range(50):
     assert decline_code(rng) in {"05", "51", "91", "57", "65"}
