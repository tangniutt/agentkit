# Copyright 2026 tangniutt and contributors
# SPDX-License-Identifier: Apache-2.0
"""M-20 eval set: small, self-contained buggy repos with a check command.

Each task: id, goal, files {path: content}, check (with {py} = the Python interpreter),
fix [(path, old, new)] = the reference solution (used by the offline scripted model and
by the direct-mode baseline). Checks only need the standard library + pytest.
"""

PYTEST = "{py} -m pytest -q -p no:cacheprovider tests"
UNITTEST = "{py} -m unittest discover -q -s tests -t ."
HDR = "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"


def _t(id, goal, src_path, src, test, fix, check=PYTEST, extra=None):
    files = {src_path: src, "tests/test_" + id.replace("-", "_") + ".py": HDR + test, ".gitignore": "__pycache__/\n"}
    if check == UNITTEST:
        files["tests/__init__.py"] = ""
    files.update(extra or {})
    return {"id": id, "goal": goal, "files": files, "check": check, "fix": fix}


TASKS = [
    _t("ev-add", "Fix add() in calc.py so it returns the sum.", "calc.py",
       "def add(a, b):\n    return a - b\n",
       "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n    assert add(-1, 1) == 0\n",
       [("calc.py", "return a - b", "return a + b")]),
    _t("ev-max", "Fix biggest() in stats.py: it must return the largest element.", "stats.py",
       "def biggest(xs):\n    best = xs[0]\n    for x in xs:\n        if x < best:\n            best = x\n    return best\n",
       "from stats import biggest\n\ndef test_biggest():\n    assert biggest([3, 9, 2]) == 9\n    assert biggest([-5, -2]) == -2\n",
       [("stats.py", "if x < best:", "if x > best:")]),
    _t("ev-rev", "Fix reverse_words() in text.py: reverse the order of words, not characters.", "text.py",
       "def reverse_words(s):\n    return s[::-1]\n",
       "from text import reverse_words\n\ndef test_rev():\n    assert reverse_words('a bc d') == 'd bc a'\n",
       [("text.py", "return s[::-1]", "return ' '.join(reversed(s.split()))")]),
    _t("ev-pal", "Fix is_palindrome() in pal.py: ignore case and non-alphanumerics.", "pal.py",
       "def is_palindrome(s):\n    return s == s[::-1]\n",
       "from pal import is_palindrome\n\ndef test_pal():\n    assert is_palindrome('A man, a plan, a canal: Panama')\n    assert not is_palindrome('abc')\n",
       [("pal.py", "    return s == s[::-1]", "    t = [c.lower() for c in s if c.isalnum()]\n    return t == t[::-1]")]),
    _t("ev-fizz", "Fix fizzbuzz() in fb.py: multiples of 15 must give 'FizzBuzz'.", "fb.py",
       "def fizzbuzz(n):\n    if n % 3 == 0:\n        return 'Fizz'\n    if n % 5 == 0:\n        return 'Buzz'\n    if n % 15 == 0:\n        return 'FizzBuzz'\n    return str(n)\n",
       "from fb import fizzbuzz\n\ndef test_fb():\n    assert [fizzbuzz(i) for i in (3, 5, 15, 7)] == ['Fizz', 'Buzz', 'FizzBuzz', '7']\n",
       [("fb.py", "    if n % 3 == 0:\n        return 'Fizz'", "    if n % 15 == 0:\n        return 'FizzBuzz'\n    if n % 3 == 0:\n        return 'Fizz'")]),
    _t("ev-fib", "Fix fib() in fib.py: fib(0)=0, fib(1)=1, fib(10)=55.", "fib.py",
       "def fib(n):\n    a, b = 1, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
       "from fib import fib\n\ndef test_fib():\n    assert [fib(i) for i in (0, 1, 2, 10)] == [0, 1, 1, 55]\n",
       [("fib.py", "a, b = 1, 1", "a, b = 0, 1")]),
    _t("ev-temp", "Fix c_to_f() in temp.py (Celsius to Fahrenheit).", "temp.py",
       "def c_to_f(c):\n    return c * 5 / 9 + 32\n",
       "from temp import c_to_f\n\ndef test_temp():\n    assert c_to_f(100) == 212\n    assert c_to_f(0) == 32\n",
       [("temp.py", "c * 5 / 9 + 32", "c * 9 / 5 + 32")]),
    _t("ev-wc", "Fix word_count() in wc.py: count words separated by any whitespace.", "wc.py",
       "def word_count(s):\n    return len(s.split(' '))\n",
       "from wc import word_count\n\ndef test_wc():\n    assert word_count('a  b\\tc\\n') == 3\n    assert word_count('') == 0\n",
       [("wc.py", "s.split(' ')", "s.split()")]),
    _t("ev-dedupe", "Fix dedupe() in seq.py: remove duplicates but keep first-seen order.", "seq.py",
       "def dedupe(xs):\n    return list(set(xs))\n",
       "from seq import dedupe\n\ndef test_dedupe():\n    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]\n",
       [("seq.py", "return list(set(xs))", "return list(dict.fromkeys(xs))")]),
    _t("ev-clamp", "Fix clamp() in num.py: clamp x into [lo, hi].", "num.py",
       "def clamp(x, lo, hi):\n    return max(hi, min(lo, x))\n",
       "from num import clamp\n\ndef test_clamp():\n    assert clamp(5, 0, 3) == 3\n    assert clamp(-1, 0, 3) == 0\n    assert clamp(2, 0, 3) == 2\n",
       [("num.py", "max(hi, min(lo, x))", "max(lo, min(hi, x))")]),
    _t("ev-fact", "Fix factorial() in fact.py: factorial(0) must be 1.", "fact.py",
       "def factorial(n):\n    if n == 1:\n        return 1\n    return n * factorial(n - 1)\n",
       "from fact import factorial\n\ndef test_fact():\n    assert factorial(0) == 1\n    assert factorial(5) == 120\n",
       [("fact.py", "if n == 1:", "if n <= 1:")]),
    _t("ev-prime", "Fix is_prime() in prime.py: 0 and 1 are not prime.", "prime.py",
       "def is_prime(n):\n    for d in range(2, int(n ** 0.5) + 1):\n        if n % d == 0:\n            return False\n    return True\n",
       "from prime import is_prime\n\ndef test_prime():\n    assert [n for n in range(12) if is_prime(n)] == [2, 3, 5, 7, 11]\n",
       [("prime.py", "def is_prime(n):\n", "def is_prime(n):\n    if n < 2:\n        return False\n")]),
    _t("ev-merge", "Fix merge() in dicts.py: values from b must win and inputs must not be mutated.", "dicts.py",
       "def merge(a, b):\n    b.update(a)\n    return b\n",
       "from dicts import merge\n\ndef test_merge():\n    a, b = {'x': 1, 'y': 1}, {'y': 2}\n    assert merge(a, b) == {'x': 1, 'y': 2}\n    assert b == {'y': 2}\n",
       [("dicts.py", "    b.update(a)\n    return b", "    out = dict(a)\n    out.update(b)\n    return out")]),
    _t("ev-flat", "Fix flatten() in flat.py: flatten arbitrarily nested lists.", "flat.py",
       "def flatten(xs):\n    out = []\n    for x in xs:\n        if isinstance(x, list):\n            out.extend(x)\n        else:\n            out.append(x)\n    return out\n",
       "from flat import flatten\n\ndef test_flat():\n    assert flatten([1, [2, [3, [4]]], 5]) == [1, 2, 3, 4, 5]\n",
       [("flat.py", "out.extend(x)", "out.extend(flatten(x))")]),
    _t("ev-bsearch", "Fix bsearch() in bs.py: it misses the last element.", "bs.py",
       "def bsearch(xs, t):\n    lo, hi = 0, len(xs) - 1\n    while lo < hi:\n        mid = (lo + hi) // 2\n        if xs[mid] == t:\n            return mid\n        if xs[mid] < t:\n            lo = mid + 1\n        else:\n            hi = mid - 1\n    return -1\n",
       "from bs import bsearch\n\ndef test_bs():\n    xs = [1, 3, 5, 7]\n    assert [bsearch(xs, t) for t in xs] == [0, 1, 2, 3]\n    assert bsearch(xs, 4) == -1\n",
       [("bs.py", "while lo < hi:", "while lo <= hi:")]),
    _t("ev-leap", "Fix is_leap() in cal.py (Gregorian rules: /4, not /100 unless /400).", "cal.py",
       "def is_leap(y):\n    return y % 4 == 0\n",
       "import unittest\nfrom cal import is_leap\n\n\nclass T(unittest.TestCase):\n    def test_leap(self):\n        self.assertTrue(is_leap(2024) and is_leap(2000))\n        self.assertFalse(is_leap(1900) or is_leap(2023))\n",
       [("cal.py", "return y % 4 == 0", "return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)")], check=UNITTEST),
    _t("ev-chunk", "Fix chunks() in chunk.py: the last partial chunk is dropped.", "chunk.py",
       "def chunks(xs, n):\n    return [xs[i:i + n] for i in range(0, len(xs) - n + 1, n)]\n",
       "import unittest\nfrom chunk import chunks\n\n\nclass T(unittest.TestCase):\n    def test_chunks(self):\n        self.assertEqual(chunks([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])\n",
       [("chunk.py", "range(0, len(xs) - n + 1, n)", "range(0, len(xs), n)")], check=UNITTEST),
    _t("ev-div", "Fix safe_div() in div.py: return None when dividing by zero.", "div.py",
       "def safe_div(a, b):\n    return a / b\n",
       "from div import safe_div\n\ndef test_div():\n    assert safe_div(6, 3) == 2\n    assert safe_div(1, 0) is None\n",
       [("div.py", "    return a / b", "    if b == 0:\n        return None\n    return a / b")]),
    _t("ev-title", "Fix title_case() in title.py: capitalize each word, lower the rest.", "title.py",
       "def title_case(s):\n    return s.capitalize()\n",
       "from title import title_case\n\ndef test_title():\n    assert title_case('hELLO wORLD') == 'Hello World'\n",
       [("title.py", "return s.capitalize()", "return ' '.join(w.capitalize() for w in s.split(' '))")]),
    _t("ev-config", "Set the default port in settings.py to 8080 (the check script expects it).", "settings.py",
       "PORT = 80\nHOST = 'localhost'\n",
       "from settings import PORT\n\ndef test_port():\n    assert PORT == 8080\n",
       [("settings.py", "PORT = 80\n", "PORT = 8080\n")], check="sh check.sh",
       extra={"check.sh": "#!/bin/sh\nset -e\n{py} -m pytest -q -p no:cacheprovider tests\n"}),
    _t("ev-evens", "Fix sum_evens() in ev.py: sum only the even numbers.", "ev.py",
       "def sum_evens(xs):\n    return sum(x for x in xs if x % 2)\n",
       "from ev import sum_evens\n\ndef test_evens():\n    assert sum_evens([1, 2, 3, 4]) == 6\n",
       [("ev.py", "if x % 2)", "if x % 2 == 0)")]),
]
