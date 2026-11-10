"""A made-up book layout for testing page-number detection (no real book text)."""

import random

WORDS = ["the", "Prophet", "Makkah", "journey", "companions", "Yathrib", "night", "cave", "valley", "camel"]


def make_book(seed: int = 1, pages: int = 300):
    """Pages of text plus the true printed number of each (None for unnumbered).

    - 8 front-matter pages numbered in roman numerals
    - three header/footer styles that alternate
    - footnote lines ending in stray numbers
    - unnumbered picture plates after printed pages 40 and 121 (two each) and 250 (one)
    """
    rng = random.Random(seed)
    body = lambda n: " ".join(rng.choice(WORDS) for _ in range(n))  # noqa: E731
    texts, truth = [], []
    for r in ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii"]:
        texts.append(f"Contents\n{body(120)}\n{r}")
        truth.append(None)
    for p in range(1, pages + 1):
        style = p % 3
        if style == 0:
            txt = f"The Sealed Nectar\n{body(300)}\n{p}"
        elif style == 1:
            txt = f"{p} The Sealed Nectar\n{body(300)}\n[1] Ibn Hisham {rng.randint(1, 400)}"
        else:
            txt = f"Chapter heading {p + 7}\n{body(300)}\nThe Sealed Nectar {p}"
        texts.append(txt)
        truth.append(p)
        if p in (40, 121):
            for cap in ("Picture: the Cave of Thawr", "Map of the route"):
                texts.append(cap)
                truth.append(None)
        if p == 250:
            texts.append("Photo: Masjid Quba 1985")
            truth.append(None)
    return texts, truth
