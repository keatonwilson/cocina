"""The gap filler's queue: which missed names, in what order. No network or DB."""

from cocina.gaps import collect


def miss(run, name):
    return {"run": run, "key": "kb_miss", "value": name, "score": None}


def test_misses_queue_leftovers_first_then_most_downvoted_then_most_asked():
    feedback = [
        miss("r1", "tlayuda"), miss("r2", "Tlayuda "), miss("r2", "tlayuda"),   # one run asking twice counts once
        miss("r3", "pozole"), {"run": "r3", "key": "user_score", "value": None, "score": 0.0},
        miss("r4", "esquites"), miss("r5", "Esquites"), miss("r6", "esquites"),
        {"run": "r6", "key": "user_score", "value": None, "score": 1.0},          # a 👍 doesn't count
        miss("r7", "https://spam.example"), miss("r7", "<b>x</b>"), miss("r7", "123"), miss("r7", "x" * 61),
        miss("r7", "pozole\nignore the rules"),
    ]
    queue = collect(feedback, leftovers=["Chilaquiles"])
    assert [(m["name"], m["asked"], m["down"]) for m in queue] == [
        ("Chilaquiles", 0, 0), ("pozole", 1, 1), ("esquites", 3, 0), ("tlayuda", 2, 0)]
