"""The two read paths must answer one question the same way.

On the live dataset, "companies whose founders column is filled" returned 14
through Ask and 50 through Query, and nothing on screen said which was right.
The Ask number was correct: 36 of those 50 rows held `["NA"]` and no founder.

Two causes, both fixed in `services/sqlq.py`:

* the prompt told the model to test `IS NOT NULL AND <> ''`, which counts the
  literal NA as a value — the product's own `is_placeholder` says otherwise;
* even the right predicate failed on array fields, because the relation reads
  `founders` as the text `'["NA"]'` while the agent renders the list by joining
  its elements and sees `NA`. Two representations of one cell.

The relation now carries `<field>__filled` and `<field>__num`, built from the
same `normalizer` vocabulary the Python path uses, so the two cannot drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import sqlq  # noqa: E402
from app.services.normalizer import absence_words, is_placeholder  # noqa: E402

FIELDS = [{"name": "company_name"}, {"name": "founders"},
          {"name": "funding_amount"}, {"name": "location"}]


@pytest.fixture(scope="module")
def relation():
    sql, cols = sqlq.relation_sql("11111111-1111-1111-1111-111111111111", FIELDS)
    return sql, cols


class TestColumnsExist:
    def test_filled_and_num_accompany_each_field(self, relation):
        _sql, cols = relation
        for name in ("company_name", "founders", "funding_amount", "location"):
            assert f"{name}__filled" in cols, name
            assert f"{name}__num" in cols, name
            assert f"{name}__status" in cols, name
            assert name in cols, name

    def test_the_prompt_offers_the_companion_columns(self):
        prompt = sqlq.build_prompt("how many companies have founders",
                                   FIELDS, 142)
        assert "__filled" in prompt
        assert "__num" in prompt
        # The instruction that produced the wrong answer must be gone.
        assert "Treat an empty string as missing" not in prompt
        assert 'CAST(\\"batch_number\\" AS integer)' not in prompt

    def test_the_absence_vocabulary_is_shared_not_duplicated(self):
        """Two copies of this list is how the two paths came to disagree.

        Checked against the *generated* predicate, not the source text: the
        words live in `normalizer` and are imported, so their absence from
        `sqlq.py` is correct and their absence from the SQL is not.
        """
        pred = sqlq._filled_predicate('"x"')
        for word in sorted(absence_words()):
            assert f"'{word}'" in pred, f"{word!r} missing from the SQL predicate"


class TestFilledPredicate:
    def test_an_absence_word_is_not_filled(self):
        pred = sqlq._filled_predicate('"x"')
        assert "'na'" in pred and "'unknown'" in pred
        # The inner-text form is what catches a serialised array.
        assert "btrim(btrim(" in pred, (
            "a single trim cannot see through ['[\"NA\"]']")
        assert pred.count("NOT IN") == 2, (
            "the value and its inner text must both be tested")

    def test_a_serialised_na_is_not_filled(self):
        """The exact live case: 36 rows stored as the list ``[\"NA\"]``."""
        assert is_placeholder("NA")
        # The agent renders a list by joining, so it sees `NA` and refuses it.
        assert is_placeholder(", ".join(["NA"]))
        # The relation reads the same cell as text, brackets and quotes intact.
        assert not is_placeholder('["NA"]'), (
            "this is why the SQL needs the inner-text form, not just the "
            "Python check")

    def test_punctuation_only_is_not_filled(self):
        for junk in ("-", "—", "–", "?", "…", '""'):
            assert is_placeholder(junk)
        pred_value = sqlq._filled_predicate('"x"')
        assert "'[A-Za-z0-9]'" in pred_value, (
            "a cell with no alphanumeric character is not a value")

    def test_a_real_value_is_filled(self):
        for real in ("David Silver", "Yann LeCun, Saining Xie", "$1.1b"):
            assert not is_placeholder(real)


class TestNumericPredicate:
    def test_unparseable_values_yield_null_not_zero(self):
        """A comparator reading `unknown` as 0 matches every row against a
        positive threshold."""
        pred = sqlq._numeric_predicate('"x"')
        assert "ELSE NULL END" in pred
        assert "CAST(" in pred
        for suffix, factor in (("billion|bn|b", "1e9"), ("million|mn|m", "1e6"),
                               ("thousand|k", "1e3")):
            assert suffix in pred, suffix
            assert factor in pred, factor

    def test_a_funding_round_is_not_a_number(self):
        """`Series B` is a round. Reading it as a number is worse than NULL."""
        assert not any(c.isdigit() for c in "Series B")
        pred = sqlq._numeric_predicate('"x"')
        # The guard requires a digit right after the optional symbol.
        assert "[0-9]" in pred


class TestGrammarStillHolds:
    def test_the_new_columns_are_usable_by_the_model(self, relation):
        sql, cols = relation
        for q in (
            'SELECT count(*) FROM records WHERE "founders__filled" = true',
            'SELECT * FROM records WHERE "funding_amount__num" > 1000000',
        ):
            assert sqlq.validate(q, cols), q

    def test_the_old_way_still_validates(self, relation):
        """It parses; it is simply no longer what the prompt recommends."""
        _sql, cols = relation
        q = 'SELECT count(*) FROM records WHERE "founders" IS NOT NULL'
        assert sqlq.validate(q, cols)

    def test_a_write_is_still_refused(self, relation):
        _sql, cols = relation
        with pytest.raises(ValueError):
            sqlq.validate('DELETE FROM records', cols)