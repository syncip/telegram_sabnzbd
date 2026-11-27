"""'Arbeitest du noch?' is answered right away, without the AI."""

from arrbot import busy


def test_status_questions():
    for q in ("Arbeitest du noch?", "arbeitest du da noch", "Bist du noch dran?", "Läuft das noch?",
              "hängst du?", "Wie lange dauert das noch?", "status", "Was machst du gerade?"):
        assert busy.is_status_question(q), q
    for q in ("Lad Dune", "Arbeitest du noch an Dune und lad dann Alien?", "Was läuft heute im Kino?"):
        assert not busy.is_status_question(q), q
    assert busy.is_status_question("?", busy=True) and not busy.is_status_question("?", busy=False)


def test_status_text():
    assert "frei" in busy.status_text(None, None, None, None, 100)
    t = busy.status_text("Zeig mir die Duplikate", 10.0, "admin_duplicates", 40.0, 200.0)
    assert "„Zeig mir die Duplikate“ (seit 3 Min. 10 s)" in t and "Duplikate – seit 2 Min. 40 s" in t
    assert "warte auf die KI" in busy.status_text("x", 0.0, None, None, 5.0)
