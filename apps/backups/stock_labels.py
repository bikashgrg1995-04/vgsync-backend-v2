"""
Repair stock numbers that the old system cut in the wrong place.

The old system took the first 15 characters of "36030072 Spacer - 4.057 mm" as
the stock number and kept the rest as the name, so a part with the code
36030072 came out as stock no "36030072 Spacer" and name "- 4.057 mm", and
sometimes the cut fell inside a word ("32010067 FRICTI" + "ON PLATE").

``fix_label`` puts the text back together: the leading digits are the real
stock number and everything else is the name. Where the cut fell inside a word
it is joined without a space, judged from the words found in the other names.
"""

import re
from collections import Counter

CODE = re.compile(r"^(\d{6,}[0-9A-Za-z\-/.]*) (\S.*)$")
CONNECTORS = {"for", "with", "and", "of", "rh", "lh", "to", "in"}


def is_split(stock_no):
    return bool(CODE.match(str(stock_no or "").strip()))


def _blank(text):
    text = str(text or "").strip()

    return "" if text.lower() == "nan" else text


def vocabulary(labels):
    """Words (3+ letters) seen in names that were not split, lower case."""
    words = Counter()

    for stock_no, name in labels:
        if not is_split(stock_no):
            words.update(re.findall(r"[A-Za-z]{3,}", _blank(name).lower()))

    return words


def fix_label(stock_no, name, words):
    """Return (stock_no, name) with a split label put back together."""
    no, name = str(stock_no or "").strip(), _blank(name)
    m = CODE.match(no)

    if not m:
        return no, name

    code, tail = m.group(1), m.group(2).strip()

    if not name:
        return code, tail

    if name[0] in ",.;:)]":
        return code, tail + name

    if not name[0].isalnum() or not tail[-1].isalnum():
        return code, f"{tail} {name}"

    frag = re.search(r"[A-Za-z]+$", tail)
    first = re.match(r"[A-Za-z]+", name)

    if not frag or not first:
        return code, f"{tail} {name}"

    a, b = frag.group().lower(), first.group().lower()

    if len(no) < 15 or (len(a) > 2 and a in words and b in words and a + b not in words):
        return code, f"{tail} {name}"  # the cut fell on a space

    if len(a) > 2 and a not in words and a + b not in words and b in CONNECTORS:
        return code, f"{tail} {name}"  # "COMMAN" + "FOR ..." is two words

    return code, tail + name
