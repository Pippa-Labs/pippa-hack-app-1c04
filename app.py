"""pre-tty perfect: a pre-drinks shopping list for a night out.

Given a group size, a budget, a leaving time and where they are setting off
from, it works out what to buy (drinks, water, snacks, and food if they have
not eaten) at typical UK supermarket prices, keeps the alcohol to a sensible
amount for the time before they leave, and lists the nearest shops that sell
drinks, from OpenStreetMap.

It is told only what Pippa pulls out of the message. If the person mentions
what they have drunk recently, how well they handle drink or what they do not
like, those arrive as inputs and change the list; if not, the list is planned
on ordinary defaults and the advice says which ones it assumed.

Prices are typical, not live: there is no free public API for supermarket
prices, so they come from the table below and the output says so.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    Result,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

#: Nominatim refuses a request without a descriptive User-Agent (docs/API_APPS.md).
USER_AGENT = "pippa-agent-app-pre-tty-perfect/0.1 (https://github.com/Pippa-Labs)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search?"
OVERPASS_URL = "https://overpass-api.de/api/interpreter?"
SHOP_RADIUS_M = 1000
SHOPS_SHOWN = 5

DEFAULT_TIMEZONE = "Europe/London"
#: Hours of pres planned for when no leaving time is given.
DEFAULT_HOURS = 2.0

#: UK Chief Medical Officers' low-risk guideline: no more than 14 units a week.
WEEKLY_GUIDELINE_UNITS = 14.0
#: Units per person planned for the pres itself, before the night out proper.
PRES_UNITS_CAP = 4.5
LOW_TOLERANCE_UNITS_CAP = 2.5
CLOSE_TO_GUIDELINE_UNITS_CAP = 2.0
#: Drinks this strong or stronger (wine, prosecco, spirits) are "strong".
STRONG_ABV = 11.0


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    pence: int          # price of one pack
    servings: int       # drinks (or people fed) per pack
    units_each: float   # UK alcohol units per serving
    abv: float
    tags: frozenset[str]
    tasty: int          # 1 to 5
    needs_mixer: bool = False

    @property
    def pence_per_serving(self) -> float:
        return self.pence / self.servings


def _item(key, label, pence, servings, units_each, abv, tags, tasty, needs_mixer=False):
    return Item(key, label, pence, servings, units_each, abv,
                frozenset(tags.split()), tasty, needs_mixer)


#: Typical UK supermarket prices, November 2026. Estimates, not live prices.
ALCOHOL = [
    _item("lager", "Lager, 4 x 440ml cans (4%)", 500, 4, 1.8, 4.0,
          "beer lager", 2),
    _item("ipa", "Craft IPA, 4 x 330ml cans (5.5%)", 750, 4, 1.8, 5.5,
          "beer ipa ale craft", 4),
    _item("cider", "Cider, 4 x 500ml cans (4.5%)", 550, 4, 2.3, 4.5,
          "cider", 3),
    _item("fruit_cider", "Fruit cider, 4 x 330ml bottles (4%)", 600, 4, 1.3, 4.0,
          "cider fruit", 4),
    _item("seltzer", "Hard seltzer, 4 x 330ml cans (4%)", 650, 4, 1.3, 4.0,
          "seltzer", 3),
    _item("gin_tonic", "Canned gin and tonic, 4 x 250ml (5%)", 750, 4, 1.25, 5.0,
          "gin spirits spirit premix tins", 4),
    _item("wine", "White or rosé wine, 750ml bottle (12%)", 650, 4, 2.25, 12.0,
          "wine rose rosé white", 3),
    _item("prosecco", "Prosecco, 750ml bottle (11%)", 750, 6, 1.4, 11.0,
          "wine prosecco fizz sparkling bubbly", 4),
    _item("vodka", "Vodka, 35cl bottle (37.5%)", 1000, 14, 0.94, 37.5,
          "vodka spirits spirit shots", 2, needs_mixer=True),
]

ALCOHOL_FREE = [
    _item("af_lager", "Alcohol-free lager, 4 x 330ml (0.0%)", 450, 4, 0.0, 0.0,
          "beer lager", 3),
    _item("af_fizz", "Alcohol-free sparkling wine, 750ml", 400, 5, 0.0, 0.0,
          "wine fizz sparkling", 3),
]

SOFT_DRINK = _item("soft", "Fizzy soft drink, 2L bottle", 180, 8, 0.0, 0.0, "soft", 2)
MIXER = _item("mixer", "Lemonade or cola mixer, 2L bottle", 125, 8, 0.0, 0.0, "mixer", 2)
WATER = _item("water", "Water, 2L bottle", 60, 4, 0.0, 0.0, "water", 1)
FOOD = _item("pizza", "Large frozen pizza", 350, 2, 0.0, 0.0, "food", 4)
SNACK_CHEAP = _item("crisps", "Sharing bag of crisps", 175, 3, 0.0, 0.0, "snack", 3)
SNACK_TASTY = _item("dip", "Tortilla chips and salsa", 300, 4, 0.0, 0.0, "snack", 4)

PRICES_NOTE = ("Prices are typical UK supermarket prices, not live ones, so "
               "the total is an estimate.")

SHOP_KINDS = {
    "alcohol": "off-licence",
    "supermarket": "supermarket",
    "convenience": "convenience store",
}


class BudgetTooSmall(AgentAppError):
    user_message = "That budget is too small to buy anything for pres."
    retryable = False


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # ── The one seam to the network ──────────────────────────────────────────

    def _fetch(self, url: str, timeout: float = 6.0) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    # ── The handler ──────────────────────────────────────────────────────────

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "pres.plan":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        inputs = call.inputs
        advice: list[str] = []

        group = inputs.get("group_size")
        if not group or group < 1:
            group = 1
            advice.append("Planned for one person: say how many are coming "
                          "for a group list.")
        group = min(int(group), 30)

        budget = inputs.get("budget")
        budget_pence = None
        if budget is not None:
            budget_pence = round(float(budget) * 100)
            if budget_pence < WATER.pence + SOFT_DRINK.pence:
                error = BudgetTooSmall(f"budget {budget!r} too small")
                error.user_message = (f"A budget of £{budget:.2f} is too small to "
                                      "buy anything useful for pres.")
                raise error

        priority = (inputs.get("priority") or "").strip().lower()
        if priority not in ("cheap", "tasty"):
            priority = "balanced"
        tolerance = (inputs.get("tolerance") or "").strip().lower()
        dislikes = set(re.findall(r"[a-zé&]+", (inputs.get("dislikes") or "").lower()))
        has_eaten = inputs.get("has_eaten")
        units_recent = inputs.get("units_last_7_days")

        hours = self._hours_until_leaving(call, advice)
        drinks_each = 1 if hours is not None and hours <= 1 else min(
            4, max(1, math.ceil(hours if hours is not None else DEFAULT_HOURS)))

        # ── How much alcohol, and how strong ──
        cap = PRES_UNITS_CAP
        allow_strong = "strong" not in dislikes
        if tolerance == "low":
            cap = LOW_TOLERANCE_UNITS_CAP
            allow_strong = False
            advice.append("Kept to fewer, lower-strength drinks, since you want "
                          "to take it easy.")
        if units_recent is not None:
            units_recent = max(0.0, float(units_recent))
            if units_recent >= WEEKLY_GUIDELINE_UNITS:
                cap = 0.0
                advice.append(
                    f"You have had about {units_recent:g} units in the last 7 days, "
                    f"over the UK low-risk guideline of {WEEKLY_GUIDELINE_UNITS:g} "
                    "a week, so this list is alcohol-free. Worth giving it a rest.")
            elif units_recent >= 10:
                cap = min(cap, CLOSE_TO_GUIDELINE_UNITS_CAP)
                allow_strong = False
                advice.append(
                    f"You have had about {units_recent:g} units in the last 7 days, "
                    f"close to the {WEEKLY_GUIDELINE_UNITS:g}-unit weekly guideline, "
                    "so the list keeps it light and skips wine and spirits.")
            elif units_recent >= 8:
                allow_strong = False
                advice.append(
                    f"You have had about {units_recent:g} units in the last 7 days, "
                    "so the list skips wine and spirits.")

        # ── Spend: water, food, snacks, then drinks ──
        basket: dict[str, list] = {}   # key -> [item, packs]
        left = budget_pence if budget_pence is not None else 10 ** 9

        def buy(item: Item, packs: int) -> int:
            nonlocal left
            affordable = min(packs, left // item.pence)
            if affordable > 0:
                basket.setdefault(item.key, [item, 0])[1] += affordable
                left -= affordable * item.pence
            return affordable

        buy(WATER, math.ceil(group / WATER.servings))
        if has_eaten is False:
            fed = buy(FOOD, math.ceil(group / FOOD.servings))
            if fed:
                advice.append("You have not eaten, so food is on the list. "
                              "Eat before you start drinking.")
        elif has_eaten is None:
            advice.append("If you have not eaten, have a proper meal first: "
                          "the list only has snacks.")

        target = group * drinks_each
        units_cap_total = group * cap

        # A pack much bigger than the round (a 35cl bottle for two drinks)
        # is mostly waste, so only packs that the drinks planned can use.
        alcoholic = [i for i in ALCOHOL
                     if not (i.tags & dislikes)
                     and (allow_strong or i.abv < STRONG_ABV)
                     and i.servings <= max(target, 4)]
        alcohol_servings = {}
        units_total = 0.0
        capped = False
        if units_cap_total > 0 and alcoholic:
            top = self._rank(alcoholic, priority, target)[:2]
            alcohol_servings, units_total, spent_left = self._allocate(
                top[:1], target, units_cap_total, left)
            # A second kind of drink only when there are enough drinks to
            # use up a pack of each; otherwise one kind, no half-full packs.
            if len(top) == 2 and sum(alcohol_servings.values()) >= \
                    top[0].servings + top[1].servings:
                alcohol_servings, units_total, spent_left = self._allocate(
                    top, target, units_cap_total, left)
            left = spent_left
            got = sum(alcohol_servings.values())
            capped = got < target and units_total + min(
                i.units_each for i in top) > units_cap_total + 1e-9
        elif cap > 0 and not alcoholic:
            advice.append("Nothing on my list avoids everything you dislike, "
                          "so the drinks are alcohol-free.")

        got = sum(alcohol_servings.values())
        fill_servings = {}
        if got < target:
            # Alcohol-free versions when the whole list is alcohol-free,
            # otherwise plain soft drinks; soft drinks if those do not fit.
            fill = [SOFT_DRINK]
            if cap == 0 or not alcoholic:
                fill = self._rank([i for i in ALCOHOL_FREE
                                   if not (i.tags & dislikes)], priority,
                                  target)[:1] + fill
            for item in fill:
                need = target - got - sum(fill_servings.values())
                if need <= 0:
                    break
                more, _, left = self._allocate([item], need, 0.0, left)
                fill_servings.update(more)
            if capped and sum(fill_servings.values()):
                advice.append(f"Capped at about {cap:g} units each before going out; "
                              "soft drinks make up the rest.")

        # _allocate has already paid for these packs (and one mixer).
        for chosen in (alcohol_servings, fill_servings):
            for item, servings in chosen.items():
                basket.setdefault(item.key, [item, 0])[1] += math.ceil(
                    servings / item.servings)
                if item.needs_mixer:
                    basket.setdefault(MIXER.key, [MIXER, 0])[1] += 1
                    buy(MIXER, math.ceil(servings / MIXER.servings) - 1)

        # Snacks last, from what is left: drinks matter more on a tight budget.
        snack = SNACK_CHEAP if priority == "cheap" or budget_pence is not None \
            and left < 2 * SNACK_TASTY.pence else SNACK_TASTY
        buy(snack, math.ceil(group / snack.servings))

        drinks_got = got + sum(fill_servings.values())
        if drinks_got < target and budget_pence is not None:
            advice.append(f"£{budget_pence / 100:.2f} only stretches to "
                          f"{drinks_got} drinks for {group} "
                          f"{'person' if group == 1 else 'people'}.")
        advice.append("Drink a glass of water between drinks, and keep some "
                      "money for getting home.")

        total = sum(item.pence * packs for item, packs in basket.values())
        lines = [f"{packs} x {item.label}: £{item.pence * packs / 100:.2f}"
                 for item, packs in basket.values()]

        data = {
            "shopping_list": lines,
            "estimated_total": round(total / 100, 2),
            "per_person_cost": round(total / group / 100, 2),
            "group_size": group,
            "alcohol_units_per_person": round(units_total / group, 1),
            "advice": advice,
            "prices_note": PRICES_NOTE,
        }
        if budget_pence is not None:
            data["budget"] = round(budget_pence / 100, 2)
        if hours is not None:
            data["hours_until_leaving"] = hours

        place = (inputs.get("start_place") or "").strip() or \
            (inputs.get("destination") or "").strip()
        if place:
            data["shops_searched_near"] = place
            data["shops"] = self._shops(place, advice)
        else:
            advice.insert(0, "Say where you are setting off from and I will "
                             "list shops nearby.")

        return Result(data=data, narrative=self._sentence(data))

    # ── Planning helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _rank(items: list[Item], priority: str, target: int) -> list[Item]:
        """Best first. "Cheap" means cheapest to cover the whole round, so a
        bottle bought for its last two glasses counts at its full price."""
        def cover(i: Item) -> int:
            return math.ceil(target / i.servings) * i.pence
        if priority == "cheap":
            key = lambda i: (cover(i), -i.tasty, i.key)
        elif priority == "tasty":
            key = lambda i: (-i.tasty, cover(i), i.key)
        else:
            key = lambda i: (-i.tasty / cover(i), i.key)
        return sorted(items, key=key)

    @staticmethod
    def _allocate(items, target, units_cap, left):
        """Hand out drinks one serving at a time, alternating between items.

        A new pack is paid for only when the last one is used up. A serving
        is skipped if it would go over the units cap (0 means no alcohol
        limit applies, because the items have none) or over the budget.
        """
        servings: dict[Item, int] = {}
        units = 0.0
        mixer_bought = False
        while sum(servings.values()) < target:
            added = False
            for item in items:
                if sum(servings.values()) >= target:
                    break
                if item.units_each and units + item.units_each > units_cap + 1e-9:
                    continue
                count = servings.get(item, 0)
                cost = item.pence if count % item.servings == 0 else 0
                if item.needs_mixer and not mixer_bought:
                    cost += MIXER.pence
                if cost > left:
                    continue
                left -= cost
                mixer_bought = mixer_bought or item.needs_mixer
                servings[item] = count + 1
                units += item.units_each
                added = True
            if not added:
                break
        return servings, units, left

    @staticmethod
    def _hours_until_leaving(call: Call, advice: list[str]):
        text = (call.inputs.get("leaving_time") or "").strip().lower()
        if not text:
            advice.append(f"No leaving time given, so I planned for about "
                          f"{DEFAULT_HOURS:g} hours of pres.")
            return None
        clock = App._parse_clock(text)
        if clock is None:
            advice.append(f"I could not read the leaving time {text!r}, so I "
                          f"planned for about {DEFAULT_HOURS:g} hours of pres.")
            return None
        zone = ZoneInfo(call.inputs.get("timezone") or DEFAULT_TIMEZONE)
        now = call.context.now.astimezone(zone)
        leave = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if leave < now - timedelta(hours=1):
            leave += timedelta(days=1)
        hours = max(0.0, (leave - now).total_seconds() / 3600)
        if hours <= 0.25:
            advice.append("You are due to leave about now, so this is a quick "
                          "one-drink list.")
        return round(hours, 1)

    @staticmethod
    def _parse_clock(text: str):
        if text in ("midnight", "12am"):
            return (0, 0)
        match = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", text)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        meridiem = match.group(3)
        if hour > 23 or minute > 59:
            return None
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif meridiem is None and 1 <= hour <= 11:
            hour += 12           # "leaving at 9" means the evening
        elif meridiem is None and hour == 12:
            hour = 0             # "leaving at 12" means midnight
        return (hour, minute)

    # ── Shops, from OpenStreetMap ────────────────────────────────────────────

    def _shops(self, place: str, advice: list[str]) -> list[str]:
        """Up to five shops selling drinks near `place`, closest first.

        Never fails the plan: if the lookup goes wrong the list is still
        useful, so the advice says what happened and the shops are empty.
        """
        try:
            found = json.loads(self._fetch(
                NOMINATIM_URL + urlencode({"q": place, "format": "jsonv2", "limit": 1}),
                timeout=5.0))
            if not found:
                advice.append(f"I could not find {place} on the map, so no shops "
                              "are listed.")
                return []
            lat, lon = float(found[0]["lat"]), float(found[0]["lon"])
            query = (
                f'[out:json][timeout:7];'
                f'nwr(around:{SHOP_RADIUS_M},{lat},{lon})'
                f'[shop~"^(alcohol|supermarket|convenience)$"];'
                f'out center 40;'
            )
            elements = json.loads(self._fetch(
                OVERPASS_URL + urlencode({"data": query}), timeout=9.0)
            ).get("elements") or []
        except urllib.error.HTTPError:
            advice.append("The shop lookup is busy just now, so no shops are "
                          "listed. Try again shortly.")
            return []
        except (TimeoutError, urllib.error.URLError, OSError, ValueError,
                KeyError, IndexError, TypeError):
            advice.append("I could not look up shops just now, so none are listed.")
            return []

        shops = []
        for element in elements:
            tags = element.get("tags") or {}
            point = element if "lat" in element else element.get("center") or {}
            if "lat" not in point or "lon" not in point:
                continue
            kind = SHOP_KINDS.get(tags.get("shop"), "shop")
            name = tags.get("name") or tags.get("brand") or f"Unnamed {kind}"
            metres = _distance_m(lat, lon, float(point["lat"]), float(point["lon"]))
            line = f"{name} ({kind}), {_round_distance(metres)} away"
            if tags.get("opening_hours"):
                line += f", hours {tags['opening_hours']}"
            shops.append((metres, line))
        shops.sort()
        if not shops:
            advice.append(f"I found no shops selling drinks within "
                          f"{SHOP_RADIUS_M // 1000} km of {place}.")
        return [line for _, line in shops[:SHOPS_SHOWN]]

    @staticmethod
    def _sentence(data: dict) -> str:
        people = data["group_size"]
        text = (f"pre-tty perfect list for {people} "
                f"{'person' if people == 1 else 'people'}: about "
                f"£{data['estimated_total']:.2f} "
                f"(£{data['per_person_cost']:.2f} each)")
        if "budget" in data:
            text += f" from a £{data['budget']:.2f} budget"
        text += f", around {data['alcohol_units_per_person']:g} units each."
        if data.get("shops"):
            text += f" Closest shop: {data['shops'][0]}."
        return text


def _distance_m(lat1, lon1, lat2, lon2) -> float:
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _round_distance(metres: float) -> str:
    if metres < 1000:
        return f"{int(round(metres, -1))} m"
    return f"{metres / 1000:.1f} km"
