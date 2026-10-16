"""The host prior has to change a decision, not merely exist.

The wiring runs main -> ctx -> runner -> discover -> deps -> graph, and every
link can be missing while all imports still succeed. These tests exercise the
two pure pieces that do the deciding, plus the repository method the prior is
computed from, so none of them touches the network.
"""
from __future__ import annotations

import datetime as _dt
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import graph as graph_svc  # noqa: E402
from app.repositories.local_repo import LocalRepo  # noqa: E402
from app.services import discovery as discovery_svc  # noqa: E402
from app.services import reputation as reputation_svc  # noqa: E402

STATE = {"entity": "acme", "fields": [], "queries": [], "goal": ""}
GOOD = {"url": "https://good.example/acme", "title": "Acme"}
BARREN = {"url": "https://barren.example/acme", "title": "Acme"}


class TestPriorReordersHosts:
    def test_textually_identical_hosts_rank_differently_once_known(self):
        assert (graph_svc._plan_relevance(GOOD, STATE)
                == graph_svc._plan_relevance(BARREN, STATE)), \
            "the two URLs and titles must be textually identical"
        prior = {"good.example": {"trust": 0.95, "known": True, "cells": 400},
                 "barren.example": {"trust": 0.02, "known": True, "cells": 380}}
        assert graph_svc._plan_relevance(GOOD, STATE, prior) > \
            graph_svc._plan_relevance(BARREN, STATE, prior)

    def test_an_unknown_host_scores_exactly_as_it_did_before(self):
        """Cold start must be unchanged, or every new instance is worse."""
        cand = {"url": "https://never-seen.example/acme", "title": "Acme"}
        assert (graph_svc._plan_relevance(cand, STATE)
                == graph_svc._plan_relevance(cand, STATE, {}))
        assert (graph_svc._plan_relevance(cand, STATE)
                == graph_svc._plan_relevance(
                    cand, STATE, {"other.example": {"trust": 1.0, "known": True,
                                                    "cells": 9}}))

    def test_a_known_barren_host_sits_below_an_unknown_one(self):
        """Knowing a host is useless is information; scoring it zero is not."""
        unknown = {"url": "https://new.example/acme", "title": "Acme"}
        prior = {"barren.example": {"trust": 0.0, "known": True, "cells": 300}}
        assert graph_svc._plan_relevance(BARREN, STATE, prior) < \
            graph_svc._plan_relevance(unknown, STATE, prior)

    def test_the_prior_cannot_drag_a_relevant_page_below_an_irrelevant_one(self):
        """Bounded on purpose: relevance is the harder constraint to recover."""
        strong = {"url": "https://x.example/indian-stocks-nse-listed-shares",
                  "title": "Indian stocks NSE listed shares"}
        weak = {"url": "https://y.example/blog", "title": "Blog"}
        state = {"entity": "indian stock", "fields": [], "queries": [], "goal": ""}
        prior = {"x.example": {"trust": 0.0, "known": True, "cells": 500}}
        assert graph_svc._plan_relevance(strong, state, prior) > \
            graph_svc._plan_relevance(weak, state, prior)

    def test_screen_node_reads_the_prior_through_deps(self):
        """`deps` is a namespace, not a typed object: a missing attribute is a
        crash in a deep node, not an import error."""
        import inspect
        src = inspect.getsource(graph_svc.screen_node)
        assert "host_prior" in src, \
            "screen_node sorts by a term it never reads"


class TestBlockedHostExclusion:
    def test_a_reliably_refusing_host_is_excluded(self):
        prior = {"naukri.com": {"visits": 9, "successes": 0, "block_count": 7}}
        assert "naukri.com" in discovery_svc.blocked_hosts_from_prior(prior, set())

    def test_a_host_that_serves_us_despite_blocking_is_kept(self):
        """One bad day is not a refusal. Excluding a productive host on a
        transient 403 discards a source that has produced records before."""
        prior = {"mixed.example": {"visits": 10, "successes": 8, "block_count": 4}}
        assert discovery_svc.blocked_hosts_from_prior(prior, set()) == set()

    def test_a_single_403_never_writes_a_host_off(self):
        prior = {"once.example": {"visits": 1, "successes": 0, "block_count": 1}}
        assert discovery_svc.blocked_hosts_from_prior(prior, set()) == set()

    def test_an_already_excluded_host_is_not_re_reported(self):
        prior = {"naukri.com": {"visits": 9, "successes": 0, "block_count": 7}}
        assert discovery_svc.blocked_hosts_from_prior(prior, {"naukri.com"}) == set()

    def test_an_empty_prior_excludes_nothing(self):
        assert discovery_svc.blocked_hosts_from_prior({}, set()) == set()


class TestAdaptiveDomainBudget:
    """A barren host and a proven host shared one page allowance.

    `per_domain_cap` was a single number for every host, so a listicle that
    has never produced a record consumed the same budget a site with a 0.95
    verified-cell rate was given. The budget is finite, so that is a real
    trade rather than a tidiness issue.
    """

    @staticmethod
    def _plan(caps=None):
        traversal = {"max_pages_per_domain": 10}
        if caps:
            traversal["domain_caps"] = caps
        return {"goal": "t", "entity": "c", "max_pages": 20,
                "traversal": traversal,
                "fields": [], "search_queries": ["q"], "dedupe_keys": []}

    def test_a_proven_host_keeps_the_full_cap(self):
        from app.services import runner as runner_svc
        plan = self._plan({})
        runner_svc._apply_adaptive_domain_caps(
            plan, [{"url": "https://good.example/list"}],
            {"good.example": {"trust": 0.95, "known": True, "cells": 500}})
        assert plan["traversal"]["domain_caps"]["good.example"] == 10

    def test_a_barren_host_is_scaled_down_but_never_to_zero(self):
        from app.services import runner as runner_svc
        plan = self._plan({})
        runner_svc._apply_adaptive_domain_caps(
            plan, [{"url": "https://barren.example/list"}],
            {"barren.example": {"trust": 0.0, "known": True, "cells": 400}})
        cap = plan["traversal"]["domain_caps"]["barren.example"]
        assert 0 < cap < 10, cap
        assert cap >= runner_svc.CAP_FLOOR, \
            "not having tried it yet differs from having tried and failing"

    def test_an_unknown_host_is_left_alone(self):
        """Cold start must be byte-identical to the flat cap."""
        from app.services import runner as runner_svc
        plan = self._plan({})
        runner_svc._apply_adaptive_domain_caps(
            plan, [{"url": "https://new.example/list"}], {})
        assert "domain_caps" not in plan["traversal"]

    def test_www_is_normalised_so_the_cap_actually_applies(self):
        from app.services import runner as runner_svc
        plan = self._plan({})
        runner_svc._apply_adaptive_domain_caps(
            plan, [{"url": "https://www.good.example/list"}],
            {"good.example": {"trust": 0.9, "known": True}})
        assert "good.example" in plan["traversal"]["domain_caps"]

    def test_no_prior_leaves_the_plan_alone(self):
        from app.services import runner as runner_svc
        plan = self._plan({})
        before = dict(plan["traversal"])
        runner_svc._apply_adaptive_domain_caps(plan, [{"url": "https://x/"}], {})
        assert plan["traversal"] == before


class TestLocalAdapterLocking:
    """The local store deadlocked on first use of its read-modify-write helpers.

    Each takes `self._lock` and then calls `_scan`, which takes it again. A
    plain `threading.Lock` is not reentrant, so `note_source_outcome`,
    `note_source_cells` and `note_field_verified` each hung forever on first
    call. It was invisible because the local adapter is opt-in dev storage and
    the Postgres adapter has no lock, so the default path never tripped it.
    """

    @pytest.mark.parametrize("method,args,kwargs", [
        ("note_source_outcome", ("h.example",), {"ok": True}),
        ("note_source_cells", ("h.example",), {"cells": 1, "verified_cells": 1}),
    ])
    def test_read_modify_write_helpers_do_not_deadlock(self, method, args, kwargs):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalRepo(root=tmp)
            done = threading.Event()

            def _call():
                try:
                    getattr(repo, method)(*args, **kwargs)
                finally:
                    done.set()

            t = threading.Thread(target=_call, daemon=True)
            t.start()
            # A deadlock here is permanent, so wait on the thread rather than
            # letting the suite hang: a hang is a much worse failure mode to
            # debug than an explicit failure here.
            assert done.wait(timeout=10), (
                f"{method} deadlocked: it re-enters a non-reentrant lock "
                f"through _scan")
            t.join(timeout=1)

    def test_field_verified_does_not_deadlock(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalRepo(root=tmp)
            done = threading.Event()

            def _call():
                try:
                    repo.note_field_verified("d1", "name", 1, 2)
                finally:
                    done.set()

            t = threading.Thread(target=_call, daemon=True)
            t.start()
            assert done.wait(timeout=10), "note_field_verified deadlocked"
            t.join(timeout=1)

    def test_the_lock_is_reentrant(self):
        import _thread
        with tempfile.TemporaryDirectory() as tmp:
            lock = LocalRepo(root=tmp)._lock
        assert isinstance(lock, _thread.RLock), (
            "a plain Lock re-entering through _scan deadlocks")


class TestThePriorIsComputedFromRealData:
    def test_cell_attribution_does_not_count_a_visit(self):
        """A cell is not a request. Charging a visit would inflate the
        denominator of proof = verified/cells and shrink every score."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalRepo(root=tmp)
            repo.note_source_outcome("x.example", ok=True)
            repo.note_source_cells("x.example", cells=10, verified_cells=9)
            row = (repo.get_source_reputation(["x.example"]) or [{}])[0]
        assert row["visits"] == 1, "a cell must not look like a fetch"
        assert row["cells"] == 10
        assert row["verified_cells"] == 9

    def test_cell_attribution_is_additive(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalRepo(root=tmp)
            repo.note_source_cells("y.example", cells=5, verified_cells=4)
            repo.note_source_cells("y.example", cells=3, verified_cells=1)
            row = (repo.get_source_reputation(["y.example"]) or [{}])[0]
        assert row["cells"] == 8 and row["verified_cells"] == 5
        assert row["visits"] == 0

    def test_attributing_zero_cells_is_a_no_op(self):
        """Otherwise a dataset of empty rows inserts a zeroed host row that
        then reads as "known" to the prior."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalRepo(root=tmp)
            repo.note_source_cells("z.example", cells=0, verified_cells=0)
            assert repo.get_source_reputation() == []

    def test_trust_sees_a_proof_ratio_at_all(self):
        """The point of A1: with `cells` at 0, proof was 0 for every host and
        the whole decay/block/confidence machinery had nothing to act on."""
        now = _dt.datetime.now(_dt.UTC)
        base = {"host": "h", "visits": 10, "successes": 10, "block_count": 0,
                "first_seen": now, "last_seen": now, "last_blocked_at": None}
        proven = reputation_svc.trust(
            {**base, "cells": 100, "verified_cells": 95,
             "last_success_at": now}, now)
        bare = reputation_svc.trust(
            {**base, "cells": 0, "verified_cells": 0,
             "last_success_at": now}, now)
        assert proven["score"] > bare["score"], (
            "a host with 95% proven cells must outscore an identical host with "
            "no cell history at all")
        assert proven["terms"]["proof"] > 0.9

    def test_the_prior_reader_reports_a_cold_start_on_an_empty_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalRepo(root=tmp)
            rows = repo.get_source_reputation()
        assert rows == []
        assert reputation_svc.trust({**rows[0]}, ) if rows else True