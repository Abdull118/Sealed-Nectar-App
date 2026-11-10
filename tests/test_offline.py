"""
Offline tests: no API key or network needed. A fake OpenAI client returns
canned JSON so the validation, verification, shuffling and CSV export can be
checked end to end. If the `anki` package is installed (pip install anki),
the CSVs are also imported into a throwaway Anki collection.

Run:  python -m pytest -q
"""

import csv
import io
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generator as g  # noqa: E402

# A short original summary written for testing (not text from the book).
SAMPLE = """\
When the leaders of Quraish met in Dar An-Nadwah and agreed to kill the Messenger of Allah,
Allah informed him of their plot and permitted him to emigrate. The Prophet (PBUH) went to the
house of Abu Bakr at noon, an hour at which he did not usually visit, and told him that
permission to leave had been granted. Abu Bakr asked to accompany him, and the Prophet (PBUH)
agreed. Abu Bakr had prepared two riding camels for this journey.

They hired Abdullah bin Uraiqit, a skilled guide who knew the desert roads, although he was
still a polytheist. They handed him the two camels and agreed to meet him at the Cave of Thawr
after three nights.

On the night of departure, Ali bin Abi Talib slept in the Prophet's bed. The Prophet (PBUH)
and Abu Bakr set off southward toward Yemen instead of north toward Yathrib, in order to
mislead the pursuers. After travelling about five miles they reached the mountain of Thawr, a
rugged and high mountain with a difficult path, and they hid in its cave.

They stayed in the cave for three nights: Friday, Saturday and Sunday. Abdullah, the son of
Abu Bakr, spent the nights with them and returned to Makkah before dawn. Amir bin Fuhairah, a
freed slave of Abu Bakr, grazed a flock of sheep near the cave and brought them milk.

Quraish offered a reward of one hundred camels for each of the two. The pursuers reached the
mouth of the cave. The Prophet (PBUH) replied: "What do you think of two, the third of whom is
Allah?" This teaches complete reliance upon Allah in the moment of greatest danger.

They reached Quba on Monday, the eighth of Rabi' Al-Awwal, and stayed there four days.
"""


def q(diff, focus, question, answer, distractors, excerpt, context="Context sentence.", place="Hijrah route"):
    return {"source_excerpt": excerpt, "place": place, "difficulty": diff, "focus": focus,
            "question": question, "correct_answer": answer, "distractors": distractors,
            "context": context}


E1 = q("Easy", "Historical Date", "For how many nights did they hide in the Cave of Thawr?",
       "Three", ["Two", "Five", "Seven"],
       "They stayed in the cave for three nights: Friday, Saturday and Sunday.")
M1 = q("Medium", "Key Event", "Why did they first head south toward Yemen?",
       "To mislead the pursuers", ["To meet the guide", "To avoid the heat", "To find water"],
       "set off southward toward Yemen instead of north toward Yathrib, in order to mislead the pursuers")
M2 = q("Medium", "Key Event", "Who brought milk to the cave?", "Amir bin Fuhairah",
       ["Abdullah bin Uraiqit", "Ali bin Abi Talib", "Abdullah bin Abu Bakr"],
       "Amir bin Fuhairah, a freed slave of Abu Bakr, grazed a flock of sheep near the cave")
# Hallucination: the excerpt and answer change "one hundred" to "two hundred".
M3 = q("Medium", "Key Event", "What reward did Quraish offer for each of the two?",
       "Two hundred camels", ["Fifty camels", "One thousand dirhams", "Ten horses"],
       "Quraish offered a reward of two hundred camels for each of the two.")
H1 = q("Hard", "Theological Lesson", "What lesson does the reply at the cave mouth teach?",
       "Complete reliance upon Allah in the moment of greatest danger",
       ["That hiding is better than fleeing", "That pursuers cannot climb mountains",
        "That a guide must always be a Muslim"],
       "This teaches complete reliance upon Allah in the moment of greatest danger.")
H2 = q("Hard", "Historical Date", "On which day of Rabi' Al-Awwal did they reach Quba?",
       "The eighth", ["The first", "The twelfth", "The fourth"],
       "They reached Quba on Monday, the eighth of Rabi' Al-Awwal, and stayed there four days.")
BAD = q("Easy", "Key Event", "Broken item?", "X", ["Y", "Y"], "nothing")   # 1 usable distractor

CLOZE = {"cards": [
    {"source_excerpt": "They stayed in the cave for three nights: Friday, Saturday and Sunday.",
     "category": "Rest Stop", "place": "Hijrah route",
     "text": "The Prophet (PBUH) and Abu Bakr hid in the {{c2::Cave of Thawr}} for {{c3::three}} nights.",
     "extra": "Food and news reached them each night."},
    {"source_excerpt": "After travelling about five miles they reached the mountain of Thawr, a rugged and high mountain",
     "category": "Terrain", "place": "Hijrah route",
     "text": "Thawr is a {{c1::rugged and high}} mountain reached after about {{c2::five}} miles.",
     "extra": "They went south first."},
    {"source_excerpt": "They hired Abdullah bin Uraiqit, a skilled guide who knew the desert roads",
     "category": "Figure", "place": "Hijrah route",
     "text": "Their guide was {{c1::Abdullah bin Uraiqit}}.",
     "extra": "He was not yet Muslim."},
    # Hallucinated term: Quba stay changed to fourteen days.
    {"source_excerpt": "They reached Quba on Monday, the eighth of Rabi' Al-Awwal, and stayed there four days.",
     "category": "Rest Stop", "place": "Madinah",
     "text": "At {{c1::Quba}} they stayed for {{c2::fourteen}} days.",
     "extra": "First stop near Yathrib."},
    {"source_excerpt": "x", "category": "Location", "text": "Broken {{c1::cloze card.", "extra": ""},
    {"source_excerpt": "They hired Abdullah bin Uraiqit, a skilled guide who knew the desert roads",
     "category": "Figure", "text": "Their guide was {{c1::Abdullah bin Uraiqit}}.", "extra": "dup"},
]}


class FakeClient:
    """Mimics client.responses.create(...) and records every request."""

    def __init__(self, replies: dict[str, list[dict]]):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        name = kw["text"]["format"]["name"]
        payload = self.replies[name].pop(0)
        return SimpleNamespace(output_text=json.dumps(payload), status="completed", output=[])


def make_client():
    # 7 questions for a maximum of 5: one invalid, one Hard too many
    reply = {"questions": [E1, M1, BAD, M2, M3, H1, H2]}
    return FakeClient({"mcq_set": [reply], "cloze_set": [CLOZE]})


@pytest.fixture(scope="module")
def result():
    client = make_client()
    res = g.run_generation(client, "test-model", g.clean_text(SAMPLE), "The Hijrah", 5, 15)
    res.client = client
    return res


# ---- counts and mix -------------------------------------------------------

def test_difficulty_split():
    assert {n: tuple(g.difficulty_split(n).values()) for n in g.ALLOWED_MCQ_COUNTS} == {
        5: (1, 3, 1), 10: (3, 4, 3), 15: (4, 7, 4), 20: (6, 8, 6)}
    with pytest.raises(ValueError):
        g.difficulty_split(7)


def test_suggested_count_is_increment_of_5():
    assert [g.suggest_mcq_count(w) for w in (2000, 3200, 6000, 9000)] == [5, 10, 15, 20]


# ---- MCQ pipeline ---------------------------------------------------------

def test_prompt_contents(result):
    first = result.client.calls[0] if result.client.calls[0]["text"]["format"]["name"] == "mcq_set" \
        else result.client.calls[1]
    assert first["instructions"] == g.SYSTEM_PROMPT
    msg = first["input"][0]["content"]
    assert "up to 5 multiple-choice" in msg and "1 Hard" in msg and "<source_text>" in msg
    assert first["text"]["format"]["strict"] is True


def test_one_request_trimmed_to_max(result):
    names = [c["text"]["format"]["name"] for c in result.client.calls]
    assert names.count("mcq_set") == 1                # no retries: counts are maximums
    df = result.mcq_review
    assert len(df) == 5
    assert Counter(df["Difficulty"]) == {"Easy": 1, "Medium": 3, "Hard": 1}
    assert list(df["Difficulty"]) == ["Easy", "Medium", "Medium", "Medium", "Hard"]


def test_options_shuffled_and_answer_letter_correct(result):
    df = result.mcq_review
    answers = {E1["question"]: "Three", M1["question"]: "To mislead the pursuers",
               M2["question"]: "Amir bin Fuhairah", M3["question"]: "Two hundred camels",
               H1["question"]: H1["correct_answer"]}
    for r in df.to_dict("records"):
        assert r[r["Answer"]] == answers[r["Question"]]
        assert len({r["A"], r["B"], r["C"], r["D"]}) == 4
    assert max(Counter(df["Answer"]).values()) <= 2   # 5 answers spread over 4 letters


def test_hallucinated_mcq_flagged(result):
    df = result.mcq_review.set_index("Question")
    assert not df.loc[M3["question"], "Verified"]
    assert "not word-for-word" in df.loc[M3["question"], "Check notes"]
    assert df.loc[E1["question"], "Verified"] and df.loc[H1["question"], "Verified"]


# ---- cloze pipeline -------------------------------------------------------

def test_cloze_validation(result):
    df = result.cloze_review
    texts = list(df["Text"])
    assert len(df) == 4                               # broken + duplicate removed
    assert texts[0].startswith("The Prophet (PBUH) and Abu Bakr hid in the {{c1::Cave of Thawr}}")
    assert "{{c2::three}}" in texts[0]                # renumbered c2,c3 -> c1,c2
    by_text = df.set_index("Text")
    assert by_text.loc[texts[0], "Verified"]
    hallucinated = by_text.loc["At {{c1::Quba}} they stayed for {{c2::fourteen}} days."]
    assert not hallucinated["Verified"] and "fourteen" in hallucinated["Check notes"]
    assert any("removed as invalid" in w for w in result.warnings)


def test_check_cloze_unit():
    assert g.check_cloze("No deletion here")[0] is None
    assert g.check_cloze("{{c1::a}} {{c1::b}}")[0] is None              # nothing left to read
    fixed, answers, _ = g.check_cloze("He left at {{c3::noon::time}} with {{c3::Abu Bakr}} by camel.")
    assert fixed == "He left at {{c1::noon::time}} with {{c1::Abu Bakr}} by camel." and answers == ["noon", "Abu Bakr"]


# ---- CSV export -----------------------------------------------------------

def _parse(csv_text):
    header = [l for l in csv_text.splitlines() if l.startswith("#")]
    body = "\n".join(l for l in csv_text.splitlines() if not l.startswith("#"))
    return header, list(csv.reader(io.StringIO(body)))


def test_mcq_csv(result):
    notes = g.build_mcq_notes(result.mcq_review, "The Hijrah", 14)
    header, rows = _parse(g.anki_csv(notes, "Basic"))
    assert header == ["#separator:Comma", "#html:true", "#notetype:Basic",
                      "#columns:Front,Back,Tags,Deck", "#tags column:3", "#deck column:4"]
    assert len(rows) == 5 and all(len(r) == 4 for r in rows)
    front, back, tags, deck = rows[0]
    assert "<br><br><b>A.</b>" in front and "<b>D.</b>" in front
    assert back.startswith("<b>") and "Source:" in back
    assert {"SealedNectar", "Chapter::The_Hijrah", "Difficulty::Easy", "Day::014"} <= set(tags.split())
    assert deck == g.DEFAULT_MCQ_DECK
    assert sum("Needs_Review" in r[2] for r in rows) == 1


def test_cloze_csv(result):
    notes = g.build_cloze_notes(result.cloze_review, "The Hijrah")
    header, rows = _parse(g.anki_csv(notes, "Cloze"))
    assert "#notetype:Cloze" in header and "#columns:Text,Back Extra,Tags,Deck" in header
    assert all("Chapter::The_Hijrah" in r[2].split() for r in rows)
    route = [r for r in rows if "Place::Hijrah_route" in r[2].split()]
    assert len(route) == 3 and all("Hijrah_Trek" in r[2].split() for r in route)
    quba = [r for r in rows if "Quba" in r[0]][0]
    assert "Place::Madinah" in quba[2].split() and "Hijrah_Trek" not in quba[2].split()
    assert all("{{c1::" in r[0] for r in rows)


def test_unticked_rows_are_dropped(result):
    df = result.mcq_review.copy()
    df.loc[0, "Keep"] = False
    assert len(g.build_mcq_notes(df, "X")) == 4


# ---- real Anki import (optional) -----------------------------------------

def test_real_anki_import(result, tmp_path):
    anki = pytest.importorskip("anki.collection")
    col = anki.Collection(str(tmp_path / "test.anki2"))
    try:
        paths = g.save_outputs(
            tmp_path, "The Hijrah", 14,
            g.anki_csv(g.build_mcq_notes(result.mcq_review, "The Hijrah", 14), "Basic"),
            g.anki_csv(g.build_cloze_notes(result.cloze_review, "The Hijrah", 14), "Cloze"))
        for key in ("mcq", "cloze"):
            meta = col.get_csv_metadata(str(paths[key]), None)
            col.import_csv(anki.ImportCsvRequest(path=str(paths[key]), metadata=meta))

        mcq_ids = col.find_notes('"deck:Sealed Nectar::MCQ"')
        cloze_ids = col.find_notes('"deck:Sealed Nectar::Hijrah Cloze"')
        assert len(mcq_ids) == 5 and len(cloze_ids) == 4
        n = col.get_note(mcq_ids[0])
        assert n.note_type()["name"] == "Basic" and "<b>A.</b>" in n["Front"]
        assert "Difficulty::Easy" in n.tags or any(t.startswith("Difficulty::") for t in n.tags)
        c = col.get_note(cloze_ids[0])
        assert c.note_type()["name"] == "Cloze" and "{{c1::" in c["Text"] and "Source:" in c["Back Extra"]
        assert any("Hijrah_Trek" in col.get_note(i).tags for i in cloze_ids)
        # 2 deletions -> 2 cards, so the cloze deck has more cards than notes
        assert len(col.find_cards('"deck:Sealed Nectar::Hijrah Cloze"')) > len(cloze_ids)
        assert len(col.find_notes("tag:Needs_Review")) == 2
    finally:
        col.close()
