"""Tests for Pres Planner, run by the build through LocalRunner.

The network is stubbed: `_fetch` is replaced on the instance with canned
Nominatim and Overpass replies, so nothing here reaches the internet.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner

from app import App, BudgetTooSmall

# 19:00 in London (GMT in November), so "9pm" is two hours away.
NOW = datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc)

GEOCODE = json.dumps([{"lat": "53.4450", "lon": "-2.2190"}]).encode()
SHOPS = json.dumps({"elements": [
    {"type": "node", "lat": 53.4480, "lon": -2.2190,
     "tags": {"shop": "supermarket", "name": "Far Supermarket"}},
    {"type": "node", "lat": 53.4452, "lon": -2.2190,
     "tags": {"shop": "alcohol", "name": "Corner Offie",
              "opening_hours": "Mo-Su 10:00-23:00"}},
    {"type": "way", "center": {"lat": 53.4460, "lon": -2.2190},
     "tags": {"shop": "convenience", "brand": "Co-op"}},
    {"type": "node", "tags": {"shop": "alcohol"}},   # no position: skipped
]}).encode()

STRONG_WORDS = ("wine", "Prosecco", "Vodka")


def fake_fetch(geocode=GEOCODE, shops=SHOPS):
    def fetch(url, timeout=6.0):
        if "nominatim" in url:
            return geocode
        if "overpass" in url:
            return shops
        raise AssertionError(f"unexpected url {url}")
    return fetch


def run(inputs, *, explicit=False, fetch=None):
    app = App()
    app._fetch = fetch or fake_fetch()
    return LocalRunner(app).call(
        "pres.plan", inputs, granted_secrets={}, now=NOW, request_id="t1",
        explicit=explicit,
    ).result


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.app_1c04"
    assert [o.id for o in app.manifest.outcomes] == ["pres.plan"]


def test_a_group_plan_fits_the_budget_and_lists_shops():
    data = run({"group_size": 4, "budget": 40, "priority": "cheap",
                "leaving_time": "9pm", "start_place": "Fallowfield"}).data
    assert data["estimated_total"] <= 40
    assert data["budget"] == 40
    assert data["group_size"] == 4
    assert data["hours_until_leaving"] == 2.0
    assert 0 < data["alcohol_units_per_person"] <= 4.5
    assert any("Water" in line for line in data["shopping_list"])
    assert data["per_person_cost"] == round(data["estimated_total"] / 4, 2)
    # Closest first; the shop with no position is skipped.
    assert data["shops"][0].startswith("Corner Offie (off-licence), 20 m away")
    assert "Mo-Su 10:00-23:00" in data["shops"][0]
    assert data["shops"][1].startswith("Co-op (convenience store)")
    assert len(data["shops"]) == 3
    assert data["shops_searched_near"] == "Fallowfield"


def test_the_total_is_the_sum_of_the_lines():
    data = run({"group_size": 5, "budget": 60}).data
    pence = sum(round(float(line.rsplit("£", 1)[1]) * 100)
                for line in data["shopping_list"])
    assert pence == round(data["estimated_total"] * 100)


def test_a_tight_budget_is_never_exceeded():
    for budget in (5, 8, 12, 20):
        data = run({"group_size": 4, "budget": budget}).data
        assert data["estimated_total"] <= budget


def test_over_the_weekly_guideline_the_list_is_alcohol_free():
    data = run({"group_size": 2, "units_last_7_days": 18}).data
    assert data["alcohol_units_per_person"] == 0
    assert any("14" in line for line in data["advice"])
    assert not any(word in line for line in data["shopping_list"]
                   for word in ("Lager, 4", "Cider", "Vodka", "IPA"))


def test_close_to_the_guideline_skips_strong_drinks():
    data = run({"group_size": 2, "units_last_7_days": 11}).data
    assert 0 < data["alcohol_units_per_person"] <= 2.0
    assert not any(word in line for line in data["shopping_list"]
                   for word in STRONG_WORDS)


def test_low_tolerance_means_fewer_and_weaker_drinks():
    data = run({"group_size": 3, "tolerance": "low", "leaving_time": "11pm"}).data
    assert data["alcohol_units_per_person"] <= 2.5
    assert not any(word in line for line in data["shopping_list"]
                   for word in STRONG_WORDS)


def test_disliked_drinks_are_left_out():
    data = run({"group_size": 4, "dislikes": "wine, cider",
                "priority": "tasty"}).data
    lines = " ".join(data["shopping_list"]).lower()
    assert "wine" not in lines and "prosecco" not in lines and "cider" not in lines


def test_not_having_eaten_adds_food():
    hungry = run({"group_size": 4, "has_eaten": False}).data
    fed = run({"group_size": 4, "has_eaten": True}).data
    assert any("pizza" in line for line in hungry["shopping_list"])
    assert not any("pizza" in line for line in fed["shopping_list"])


def test_spirits_come_with_a_mixer():
    everything_else = "beer cider wine seltzer gin"
    lines = " ".join(run({"group_size": 8, "dislikes": everything_else})
                     .data["shopping_list"])
    assert "Vodka" in lines and "mixer" in lines


def test_a_small_group_gets_no_big_bottles():
    """A 35cl bottle or a bottle of prosecco for two drinks is mostly waste."""
    for priority in ("cheap", "tasty", ""):
        lines = " ".join(run({"group_size": 1, "priority": priority}).data["shopping_list"])
        assert "Vodka" not in lines and "Prosecco" not in lines


def test_drinks_come_before_snacks_on_a_tight_budget():
    data = run({"group_size": 4, "budget": 5}).data
    assert any("soft drink" in line for line in data["shopping_list"])


def test_missing_details_are_assumed_and_said():
    data = run({}).data
    assert data["group_size"] == 1
    assert "hours_until_leaving" not in data
    assert "shops" not in data
    advice = " ".join(data["advice"])
    assert "one person" in advice and "leaving time" in advice
    assert "setting off" in advice


def test_an_unreadable_leaving_time_is_said():
    data = run({"leaving_time": "after the match"}).data
    assert "hours_until_leaving" not in data
    assert any("could not read the leaving time" in a for a in data["advice"])


def test_leaving_time_formats():
    assert App._parse_clock("9pm") == (21, 0)
    assert App._parse_clock("21:30") == (21, 30)
    assert App._parse_clock("9.30") == (21, 30)
    assert App._parse_clock("12am") == (0, 0)
    assert App._parse_clock("midnight") == (0, 0)
    assert App._parse_clock("25:00") is None


def test_leaving_after_midnight_counts_forward():
    data = run({"leaving_time": "1am"}).data
    assert data["hours_until_leaving"] == 6.0


def test_a_place_not_on_the_map_still_gives_a_list():
    data = run({"start_place": "Narnia"}, fetch=fake_fetch(geocode=b"[]")).data
    assert data["shops"] == []
    assert any("could not find Narnia" in a for a in data["advice"])
    assert data["shopping_list"]


def test_a_shop_lookup_timeout_still_gives_a_list():
    def slow(url, timeout=6.0):
        raise TimeoutError("timed out")
    data = run({"start_place": "Leeds", "group_size": 2}, fetch=slow).data
    assert data["shops"] == []
    assert any("could not look up shops" in a for a in data["advice"])


def test_a_busy_shop_lookup_still_gives_a_list():
    def busy(url, timeout=6.0):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
    data = run({"start_place": "Leeds"}, fetch=busy).data
    assert data["shops"] == []
    assert any("busy" in a for a in data["advice"])


def test_the_destination_is_used_when_there_is_no_start():
    data = run({"destination": "Deansgate"}).data
    assert data["shops_searched_near"] == "Deansgate"


def test_a_tiny_budget_is_refused_plainly():
    with pytest.raises(BudgetTooSmall) as failure:
        run({"budget": 1})
    assert "£1.00" in failure.value.user_message


def test_picked_by_name_it_answers_the_same():
    inputs = {"group_size": 3, "budget": 30}
    assert run(inputs, explicit=True).data == run(inputs).data
