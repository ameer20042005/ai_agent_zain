# -*- coding: utf-8 -*-
import pytest

from app.agent.amounts import parse_amounts


@pytest.mark.parametrize("text, expected", [
    ("دز 50 الف لأحمد", 50_000),
    ("حول خمسين الف", 50_000),
    ("ربع مليون", 250_000),
    ("نص مليون", 500_000),
    ("مليون ونص", 1_500_000),
    ("مليون وخمسمية الف", 1_500_000),
    ("مية وخمسين الف", 150_000),
    ("خمس تلاف", 5_000),
    ("خمسطعش الف", 15_000),
    ("تلثمية الف", 300_000),
    ("الفين", 2_000),
    ("٢٥٠٠٠ دينار", 25_000),
    ("50,000", 50_000),
    ("25الف", 25_000),
    ("2.5 مليون", 2_500_000),
])
def test_clear_amounts(text, expected):
    mentions = parse_amounts(text)
    assert [(m.value, m.issue) for m in mentions] == [(expected, None)]


@pytest.mark.parametrize("text, issue", [
    ("دز خمسين لأحمد", "maybe_thousands"),      # خمسين = 50 أو 50 الف؟
    ("حول 100 دولار", "usd"),
    ("دز ورقة لعلي", "slang_unit"),
    ("انطيه ورقتين", "slang_unit"),
    ("حول كل رصيدي", "all_balance"),
    ("دز -5000", "negative"),
    ("ناقص 5000", "negative"),
])
def test_ambiguous_amounts_are_flagged_not_guessed(text, issue):
    mentions = parse_amounts(text)
    assert mentions and mentions[0].issue == issue and mentions[0].value is None


def test_two_amounts_of_same_order_are_not_merged():
    # خطأ حقيقي لقيناه بمجموعة الاختبار: كان يطلع 50,000 مبلغ واحد.
    assert [m.value for m in parse_amounts("دز 20 الف و 30 الف")] == [20_000, 30_000]


def test_phone_numbers_are_not_amounts():
    assert [m.value for m in parse_amounts("حول 25 الف على 07701234567")] == [25_000]
    assert [m.value for m in parse_amounts("حول 25 الف على 0770-123-4567")] == [25_000]


def test_small_number_words_that_are_names_are_ignored():
    assert parse_amounts("ست زينب") == []
