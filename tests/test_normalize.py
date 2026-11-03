"""Tests for src.normalize: norm() and strip_feat()."""
from src.normalize import norm, strip_feat


def test_norm_lower_and_trim():
    assert norm("  Hello WORLD  ") == "hello world"


def test_norm_punct_collapse():
    assert norm("Hello,   World!!!") == "hello world"
    assert norm("rock'n'roll -- legend...") == "rock n roll legend"
    assert norm("a\tb\n  c") == "a b c"
    assert norm("") == ""


def test_norm_cyrillic_preserved():
    assert norm("Привет,  Мир!") == "привет мир"


def test_norm_yo_to_e_flag():
    assert norm("Ёжик") == "ёжик"  # default: ё preserved
    assert norm("Ёжик", yo_to_e=True) == "ежик"
    assert norm("ПОДЪЁМ", yo_to_e=True) == "подъем"


def test_strip_feat_basic():
    title, feat = strip_feat("Halo feat. Beyoncé")
    assert title == "Halo"
    assert "feat" in feat.lower()


def test_strip_feat_parens_and_ft():
    title, feat = strip_feat("Song (ft. Some Artist)")
    assert title == "Song"
    assert feat != ""
    title2, feat2 = strip_feat("Track Featuring Guest")
    assert title2 == "Track"
    assert feat2 != ""


def test_strip_feat_none():
    assert strip_feat("Hello") == ("Hello", "")
    assert strip_feat("  Hello  ") == ("Hello", "")
