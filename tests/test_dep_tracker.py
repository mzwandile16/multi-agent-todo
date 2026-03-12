"""Tests for core/dep_tracker.py: pure in-memory dependency tracking."""

import pytest

from core.dep_tracker import DependencyTracker
from core.models import ModelOutputError, Task


@pytest.fixture
def tracker():
    return DependencyTracker()


def _children(parent_id, specs):
    """Build a list of Task objects from (title, depends_on_indices) specs.

    deps are given as indices into the list; resolved to real IDs after creation.
    """
    tasks = [Task(title=title, parent_id=parent_id) for title, _ in specs]
    for i, (_, dep_indices) in enumerate(specs):
        tasks[i].depends_on = [tasks[j].id for j in dep_indices]
    return tasks


class TestRegisterAndIsBlocked:

    def test_no_deps_not_blocked(self, tracker):
        children = _children("p", [("A", []), ("B", [])])
        tracker.register("p", children)
        assert not tracker.is_blocked(children[0].id)
        assert not tracker.is_blocked(children[1].id)

    def test_with_deps_blocked(self, tracker):
        children = _children("p", [("A", []), ("B", [0])])
        tracker.register("p", children)
        assert not tracker.is_blocked(children[0].id)
        assert tracker.is_blocked(children[1].id)

    def test_unknown_task_not_blocked(self, tracker):
        assert not tracker.is_blocked("nonexistent")

    def test_children_registered(self, tracker):
        children = _children("p", [("A", []), ("B", [0])])
        tracker.register("p", children)
        assert tracker.get_children("p") == {children[0].id, children[1].id}

    def test_get_children_empty(self, tracker):
        assert tracker.get_children("no_parent") == set()


class TestOnCompleted:

    def test_single_dep_unblocks(self, tracker):
        # B depends on A
        children = _children("p", [("A", []), ("B", [0])])
        tracker.register("p", children)
        unblocked = tracker.on_completed(children[0].id)
        assert unblocked == [children[1].id]
        assert not tracker.is_blocked(children[1].id)

    def test_multi_dep_partial_completion(self, tracker):
        # C depends on A and B
        children = _children("p", [("A", []), ("B", []), ("C", [0, 1])])
        tracker.register("p", children)

        # Complete A — C still blocked (waiting on B)
        unblocked = tracker.on_completed(children[0].id)
        assert unblocked == []
        assert tracker.is_blocked(children[2].id)

        # Complete B — C now unblocked
        unblocked = tracker.on_completed(children[1].id)
        assert unblocked == [children[2].id]
        assert not tracker.is_blocked(children[2].id)

    def test_chain_dependency(self, tracker):
        # A → B → C (chain)
        children = _children("p", [("A", []), ("B", [0]), ("C", [1])])
        tracker.register("p", children)

        assert not tracker.is_blocked(children[0].id)
        assert tracker.is_blocked(children[1].id)
        assert tracker.is_blocked(children[2].id)

        # Complete A → unblocks B
        unblocked = tracker.on_completed(children[0].id)
        assert unblocked == [children[1].id]

        # Complete B → unblocks C
        unblocked = tracker.on_completed(children[1].id)
        assert unblocked == [children[2].id]

    def test_diamond_dependency(self, tracker):
        # A, B independent; C depends on both; D depends on C
        children = _children("p", [
            ("A", []),      # 0
            ("B", []),      # 1
            ("C", [0, 1]),  # 2
            ("D", [2]),     # 3
        ])
        tracker.register("p", children)

        tracker.on_completed(children[0].id)  # C still blocked
        assert tracker.is_blocked(children[2].id)
        unblocked = tracker.on_completed(children[1].id)  # C unblocked
        assert children[2].id in unblocked
        assert tracker.is_blocked(children[3].id)

        unblocked = tracker.on_completed(children[2].id)  # D unblocked
        assert children[3].id in unblocked

    def test_no_deps_on_completed_is_noop(self, tracker):
        children = _children("p", [("A", []), ("B", [])])
        tracker.register("p", children)
        unblocked = tracker.on_completed(children[0].id)
        assert unblocked == []

    def test_maps_clean_after_full_completion(self, tracker):
        children = _children("p", [("A", []), ("B", [0]), ("C", [1])])
        tracker.register("p", children)
        tracker.on_completed(children[0].id)
        tracker.on_completed(children[1].id)
        tracker.on_completed(children[2].id)
        assert len(tracker._pending_deps) == 0
        assert len(tracker._reverse_deps) == 0


class TestCleanup:

    def test_cleanup_waiter(self, tracker):
        """Cancelling a blocked task removes it cleanly from all maps."""
        children = _children("p", [("A", []), ("B", [0])])
        tracker.register("p", children)

        tracker.cleanup(children[1].id)  # Cancel B
        assert not tracker.is_blocked(children[1].id)
        # A's reverse_deps entry for B should also be gone
        assert children[1].id not in tracker._reverse_deps.get(children[0].id, set())

    def test_cleanup_dependency(self, tracker):
        """Cancelling a prerequisite removes its reverse_deps entry.
        Dependents stay blocked (no auto-dispatch)."""
        children = _children("p", [("A", []), ("B", [0])])
        tracker.register("p", children)

        tracker.cleanup(children[0].id)  # Cancel A (the dep)
        # B is still in _pending_deps (waiting on a cancelled task)
        assert tracker.is_blocked(children[1].id)

    def test_cleanup_nonexistent_is_noop(self, tracker):
        tracker.cleanup("does_not_exist")  # should not raise

    def test_cleanup_clears_both_roles(self, tracker):
        """Task that is both a waiter and a dependency gets fully cleaned."""
        # A → B → C : cancel B
        children = _children("p", [("A", []), ("B", [0]), ("C", [1])])
        tracker.register("p", children)

        tracker.cleanup(children[1].id)  # Cancel B
        assert not tracker.is_blocked(children[1].id)
        # C still blocked (its dep B was cancelled, not completed)
        assert tracker.is_blocked(children[2].id)
        # A's reverse entry for B should be gone
        assert children[1].id not in tracker._reverse_deps.get(children[0].id, set())


class TestResolveIndices:
    """Test the static resolve_indices method that converts 0-based planner
    output indices into real task IDs, filtering invalid entries."""

    def test_valid_indices(self):
        ids = ["id_A", "id_B", "id_C"]
        sub_tasks = [
            {"depends_on": []},
            {"depends_on": [0]},
            {"depends_on": [0, 1]},
        ]
        result = DependencyTracker.resolve_indices(ids, sub_tasks)
        assert result == [[], ["id_A"], ["id_A", "id_B"]]

    def test_no_depends_on_key(self):
        ids = ["id_A", "id_B"]
        sub_tasks = [{"title": "A"}, {"title": "B"}]
        result = DependencyTracker.resolve_indices(ids, sub_tasks)
        assert result == [[], []]

    def test_self_reference_raises(self):
        ids = ["id_A", "id_B"]
        sub_tasks = [
            {"depends_on": [0]},  # self-ref
            {"depends_on": [1]},  # self-ref
        ]
        with pytest.raises(ModelOutputError, match="self"):
            DependencyTracker.resolve_indices(ids, sub_tasks)

    def test_out_of_range_raises(self):
        ids = ["id_A", "id_B"]
        with pytest.raises(ModelOutputError):
            DependencyTracker.resolve_indices(ids, [{"depends_on": [5]}, {"depends_on": []}])
        with pytest.raises(ModelOutputError):
            DependencyTracker.resolve_indices(ids, [{"depends_on": [-1]}, {"depends_on": []}])

    def test_non_integer_raises(self):
        ids = ["id_A", "id_B"]
        with pytest.raises(ModelOutputError):
            DependencyTracker.resolve_indices(ids, [{"depends_on": ["0"]}, {"depends_on": []}])
        with pytest.raises(ModelOutputError):
            DependencyTracker.resolve_indices(ids, [{"depends_on": [0.5]}, {"depends_on": []}])

    def test_mixed_valid_then_invalid_raises(self):
        """Even if some entries are valid, first invalid entry raises."""
        ids = ["id_A", "id_B", "id_C"]
        sub_tasks = [
            {"depends_on": []},
            {"depends_on": [0, 99]},  # 0 valid, 99 OOB
            {"depends_on": [0, 1]},
        ]
        with pytest.raises(ModelOutputError):
            DependencyTracker.resolve_indices(ids, sub_tasks)

    def test_empty_sub_tasks(self):
        result = DependencyTracker.resolve_indices([], [])
        assert result == []

    def test_single_task_no_deps(self):
        result = DependencyTracker.resolve_indices(["id_A"], [{"depends_on": []}])
        assert result == [[]]

    def test_bool_true_treated_as_int(self):
        """In Python, bool is a subclass of int: True == 1, False == 0.
        This is a realistic model output quirk."""
        ids = ["id_A", "id_B", "id_C"]
        sub_tasks = [
            {"depends_on": []},
            {"depends_on": []},
            {"depends_on": [True]},  # True is int(1)
        ]
        result = DependencyTracker.resolve_indices(ids, sub_tasks)
        # isinstance(True, int) is True, so True resolves to index 1 = id_B
        assert result[2] == ["id_B"]


class TestMultipleParents:
    """Verify independent parent groups don't interfere."""

    def test_two_parent_groups(self, tracker):
        g1 = _children("p1", [("A", []), ("B", [0])])
        g2 = _children("p2", [("X", []), ("Y", [0])])
        tracker.register("p1", g1)
        tracker.register("p2", g2)

        assert tracker.get_children("p1") == {g1[0].id, g1[1].id}
        assert tracker.get_children("p2") == {g2[0].id, g2[1].id}

        # Completing A in p1 should not affect p2
        unblocked = tracker.on_completed(g1[0].id)
        assert unblocked == [g1[1].id]
        assert tracker.is_blocked(g2[1].id)

        # Complete X in p2
        unblocked = tracker.on_completed(g2[0].id)
        assert unblocked == [g2[1].id]
