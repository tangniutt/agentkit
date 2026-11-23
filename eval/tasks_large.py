# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""Larger, multi-file eval tasks (M-20 token claim, change C-06).

The 21 tiny tasks are one 5-line file each: there the supervising AI fixing the bug itself is
cheapest. These tasks look more like real work: a small package of several ~60-120 line
modules where the fix spans 2-3 files and the host has to explore (repo_map, read the
failing test, read the modules) before it can edit. Same shape as eval/tasks.py, plus
`explore`: the host commands a direct-mode AI runs before editing (also replayed by the
offline scripted worker, where they cost worker tokens instead of host tokens).
"""

PYTEST = "{py} -m pytest -q -p no:cacheprovider tests"
HDR = "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"

# ---------------------------------------------------------------- ev-shop: inventory + pricing (2-file bug)

SHOP_MODELS = '''"""Domain objects for the shop package."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Item:
    sku: str
    name: str
    unit_price: float
    quantity: int = 0
    tags: List[str] = field(default_factory=list)

    def is_in_stock(self) -> bool:
        return self.quantity > 0

    def label(self) -> str:
        return f"{self.name} ({self.sku})"


@dataclass
class Customer:
    id: str
    name: str
    tier: str = "regular"  # regular | silver | gold
    email: Optional[str] = None

    def is_vip(self) -> bool:
        return self.tier in ("silver", "gold")


@dataclass
class OrderLine:
    sku: str
    quantity: int


@dataclass
class Order:
    customer: Customer
    lines: List[OrderLine] = field(default_factory=list)
    coupon: Optional[str] = None
    meta: Dict[str, str] = field(default_factory=dict)

    def add(self, sku: str, quantity: int = 1) -> "Order":
        for line in self.lines:
            if line.sku == sku:
                line.quantity += quantity
                return self
        self.lines.append(OrderLine(sku, quantity))
        return self

    def skus(self) -> List[str]:
        return [line.sku for line in self.lines]
'''

SHOP_INVENTORY = '''"""Stock keeping."""
from typing import Dict, Iterable, List

from shop.models import Item


class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self, items: Iterable[Item] = ()):
        self._items: Dict[str, Item] = {}
        for it in items:
            self.add(it)

    def add(self, item: Item) -> None:
        if item.sku in self._items:
            self._items[item.sku].quantity += item.quantity
        else:
            self._items[item.sku] = item

    def get(self, sku: str) -> Item:
        try:
            return self._items[sku]
        except KeyError:
            raise KeyError(f"unknown sku {sku}")

    def remove(self, sku: str, quantity: int) -> None:
        item = self.get(sku)
        if item.quantity < quantity:
            raise OutOfStock(f"{sku}: have {item.quantity}, need {quantity}")
        item.quantity -= quantity

    def in_stock(self) -> List[Item]:
        return [it for it in self._items.values() if it.is_in_stock()]

    def by_tag(self, tag: str) -> List[Item]:
        return [it for it in self._items.values() if tag in it.tags]

    def total_value(self) -> float:
        """Value of everything in stock: unit price times quantity, summed."""
        total = 0.0
        for it in self._items.values():
            total += it.unit_price
        return round(total, 2)

    def low_stock(self, threshold: int = 3) -> List[str]:
        return sorted(it.sku for it in self._items.values() if 0 < it.quantity <= threshold)

    def __len__(self) -> int:
        return len(self._items)
'''

SHOP_PRICING = '''"""Prices, discounts and order totals."""
from shop.inventory import Inventory
from shop.models import Customer, Order

TIER_DISCOUNT_PERCENT = {"regular": 0, "silver": 5, "gold": 10}
COUPONS = {"WELCOME": 15, "SPRING": 20}
TAX_RATE = 0.08


def apply_discount(amount: float, percent: float) -> float:
    """Reduce amount by `percent` percent (10 means 10%)."""
    if percent < 0 or percent > 100:
        raise ValueError("percent must be within 0..100")
    return round(amount * (1 - percent), 2)


def tier_discount(customer: Customer) -> float:
    return TIER_DISCOUNT_PERCENT.get(customer.tier, 0)


def coupon_discount(code) -> float:
    if not code:
        return 0
    return COUPONS.get(code.upper(), 0)


def subtotal(order: Order, inv: Inventory) -> float:
    return round(sum(inv.get(l.sku).unit_price * l.quantity for l in order.lines), 2)


def order_total(order: Order, inv: Inventory) -> float:
    """Subtotal, minus the larger of tier and coupon discount, plus tax."""
    sub = subtotal(order, inv)
    pct = max(tier_discount(order.customer), coupon_discount(order.coupon))
    net = apply_discount(sub, pct)
    return round(net * (1 + TAX_RATE), 2)


def checkout(order: Order, inv: Inventory) -> float:
    total = order_total(order, inv)
    for line in order.lines:
        inv.remove(line.sku, line.quantity)
    return total
'''

SHOP_REPORT = '''"""Plain-text reports."""
from shop.inventory import Inventory


def stock_report(inv: Inventory) -> str:
    rows = [f"{it.sku:<8} {it.name:<20} {it.quantity:>4}" for it in sorted(inv.in_stock(), key=lambda i: i.sku)]
    rows.append(f"value: {inv.total_value():.2f}")
    low = inv.low_stock()
    if low:
        rows.append("low: " + ", ".join(low))
    return "\\n".join(rows)
'''

SHOP_TEST = HDR + '''from shop.inventory import Inventory
from shop.models import Customer, Item, Order
from shop.pricing import apply_discount, checkout, order_total


def inv():
    return Inventory([Item("A1", "pen", 1.50, 10), Item("B2", "pad", 4.00, 2), Item("C3", "ink", 12.00, 0)])


def test_total_value_counts_quantity():
    assert inv().total_value() == 23.00


def test_apply_discount_is_percent():
    assert apply_discount(200.0, 10) == 180.0
    assert apply_discount(50.0, 0) == 50.0


def test_order_total_gold_customer():
    o = Order(Customer("c1", "Ann", tier="gold")).add("A1", 4).add("B2", 1)
    assert order_total(o, inv()) == 9.72


def test_checkout_removes_stock():
    i = inv()
    checkout(Order(Customer("c2", "Bo"), coupon="welcome").add("B2", 2), i)
    assert i.get("B2").quantity == 0
'''

# ---------------------------------------------------------------- ev-csv: parser + stats (2-file bug)

CSV_PARSER = '''"""A tiny CSV reader (no quoting support needed for our data)."""
from typing import Dict, List


class ParseError(ValueError):
    pass


def split_line(line: str, sep: str = ",") -> List[str]:
    return [c.strip() for c in line.split(sep)]


def parse(text: str, sep: str = ",") -> List[Dict[str, str]]:
    """Header line + data lines -> list of dicts. Blank lines and # comments are ignored."""
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        return []
    header = split_line(lines[0], sep)
    if len(set(header)) != len(header):
        raise ParseError("duplicate column names")
    rows = []
    for n, line in enumerate(lines[1:-1], start=2):
        cells = split_line(line, sep)
        if len(cells) != len(header):
            raise ParseError(f"line {n}: expected {len(header)} cells, got {len(cells)}")
        rows.append(dict(zip(header, cells)))
    return rows


def column(rows: List[Dict[str, str]], name: str) -> List[str]:
    return [r[name] for r in rows]


def numeric(values: List[str]) -> List[float]:
    out = []
    for v in values:
        try:
            out.append(float(v))
        except ValueError:
            raise ParseError(f"not a number: {v!r}")
    return out
'''

CSV_STATS = '''"""Descriptive statistics on lists of floats."""
from typing import Dict, List


def mean(xs: List[float]) -> float:
    if not xs:
        raise ValueError("mean of empty list")
    return sum(xs) / len(xs)


def median(xs: List[float]) -> float:
    if not xs:
        raise ValueError("median of empty list")
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid]


def variance(xs: List[float]) -> float:
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def summary(xs: List[float]) -> Dict[str, float]:
    return {"n": len(xs), "mean": round(mean(xs), 3), "median": median(xs), "min": min(xs), "max": max(xs),
            "var": round(variance(xs), 3)}
'''

CSV_CLI = '''"""python -m csvstats.cli FILE COLUMN"""
import json
import sys

from csvstats.parser import column, numeric, parse
from csvstats.stats import summary


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: cli FILE COLUMN", file=sys.stderr)
        return 2
    with open(argv[0]) as f:
        rows = parse(f.read())
    print(json.dumps(summary(numeric(column(rows, argv[1])))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

CSV_TEST = HDR + '''from csvstats.parser import column, numeric, parse
from csvstats.stats import median, summary

DATA = "name,score\\n# comment\\nann,3\\nbo,1\\n\\ncy,4\\ndi,2\\n"


def test_parse_keeps_every_row():
    rows = parse(DATA)
    assert [r["name"] for r in rows] == ["ann", "bo", "cy", "di"]


def test_median_even_and_odd():
    assert median([3, 1, 4, 2]) == 2.5
    assert median([5, 1, 3]) == 3


def test_summary():
    s = summary(numeric(column(parse(DATA), "score")))
    assert s["n"] == 4 and s["median"] == 2.5 and s["mean"] == 2.5
'''

# ---------------------------------------------------------------- ev-rename: API rename across 3 modules

USERS_STORE = '''"""In-memory user store."""
from typing import Dict, List, Optional

_USERS: Dict[int, dict] = {}


def reset() -> None:
    _USERS.clear()


def create_user(uid: int, name: str, email: str) -> dict:
    if uid in _USERS:
        raise ValueError(f"user {uid} exists")
    _USERS[uid] = {"id": uid, "name": name, "email": email, "active": True}
    return _USERS[uid]


def get_user(uid: int) -> Optional[dict]:
    return _USERS.get(uid)


def deactivate(uid: int) -> None:
    u = get_user(uid)
    if u is None:
        raise KeyError(uid)
    u["active"] = False


def active_users() -> List[dict]:
    return [u for u in _USERS.values() if u["active"]]
'''

USERS_API = '''"""HTTP-ish handlers (framework-free)."""
from users.store import create_user, get_user


def handle_get(uid: int) -> tuple:
    u = get_user(uid)
    if u is None:
        return 404, {"error": "not found"}
    return 200, {"id": u["id"], "name": u["name"]}


def handle_create(payload: dict) -> tuple:
    try:
        u = create_user(int(payload["id"]), payload["name"], payload.get("email", ""))
    except (KeyError, ValueError) as e:
        return 400, {"error": str(e)}
    return 201, {"id": u["id"]}
'''

USERS_EMAILS = '''"""Outgoing e-mail texts."""
from users.store import get_user


def welcome_text(uid: int) -> str:
    u = get_user(uid)
    if u is None:
        raise KeyError(uid)
    return f"Hello {u['name']}, welcome aboard!"
'''

USERS_TEST = HDR + '''import users.store as store
from users.api import handle_get
from users.emails import welcome_text
from users.store import create_user, fetch_user


def setup_function():
    store.reset()


def test_fetch_user_is_the_new_name():
    create_user(1, "Ann", "ann@example.invalid")
    assert fetch_user(1)["name"] == "Ann"
    assert fetch_user(2) is None
    assert not hasattr(store, "get_user"), "the old name must be gone"


def test_callers_use_new_name():
    create_user(7, "Bo", "")
    assert handle_get(7) == (200, {"id": 7, "name": "Bo"})
    assert welcome_text(7) == "Hello Bo, welcome aboard!"
'''


def _large(id, goal, files, test_path, test, fix, explore):
    f = dict(files)
    f[test_path] = test
    f[".gitignore"] = "__pycache__/\n"
    return {"id": id, "goal": goal, "files": f, "check": PYTEST, "fix": fix, "explore": explore, "size": "large"}


TASKS_LARGE = [
    _large("ev-shop", "The shop package's tests fail: inventory value ignores quantities and discounts are applied wrongly. "
           "Fix the shop package so tests/test_shop.py passes.",
           {"shop/__init__.py": "", "shop/models.py": SHOP_MODELS, "shop/inventory.py": SHOP_INVENTORY,
            "shop/pricing.py": SHOP_PRICING, "shop/report.py": SHOP_REPORT},
           "tests/test_shop.py", SHOP_TEST,
           [("shop/inventory.py", "total += it.unit_price\n", "total += it.unit_price * it.quantity\n"),
            ("shop/pricing.py", "amount * (1 - percent)", "amount * (1 - percent / 100)")],
           [["repo_map"], ["read", "tests/test_shop.py"], ["read", "shop/models.py"]]),
    _large("ev-csv", "csvstats drops the last data row and computes the median wrong for even-sized inputs. "
           "Fix the package so tests/test_csvstats.py passes.",
           {"csvstats/__init__.py": "", "csvstats/parser.py": CSV_PARSER, "csvstats/stats.py": CSV_STATS,
            "csvstats/cli.py": CSV_CLI},
           "tests/test_csvstats.py", CSV_TEST,
           [("csvstats/parser.py", "enumerate(lines[1:-1], start=2)", "enumerate(lines[1:], start=2)"),
            ("csvstats/stats.py", "    mid = len(s) // 2\n    return s[mid]\n",
             "    mid = len(s) // 2\n    if len(s) % 2 == 0:\n        return (s[mid - 1] + s[mid]) / 2\n    return s[mid]\n")],
           [["repo_map"], ["read", "tests/test_csvstats.py"], ["read", "csvstats/cli.py"]]),
    _large("ev-rename", "Rename users.store.get_user to fetch_user (remove the old name) and update every caller in the "
           "users package so tests/test_users.py passes.",
           {"users/__init__.py": "", "users/store.py": USERS_STORE, "users/api.py": USERS_API, "users/emails.py": USERS_EMAILS},
           "tests/test_users.py", USERS_TEST,
           [("users/store.py", "def get_user(uid: int)", "def fetch_user(uid: int)"),
            ("users/store.py", "    u = get_user(uid)\n", "    u = fetch_user(uid)\n"),
            ("users/api.py", "import create_user, get_user", "import create_user, fetch_user"),
            ("users/api.py", "    u = get_user(uid)\n", "    u = fetch_user(uid)\n"),
            ("users/emails.py", "import get_user", "import fetch_user"),
            ("users/emails.py", "    u = get_user(uid)\n", "    u = fetch_user(uid)\n")],
           [["repo_map"], ["read", "tests/test_users.py"], ["search", "get_user"]]),
]
