"""In-memory dependency tracking between sibling sub-tasks.

Pure data-structure operations — no I/O, no DB, no threading.
The Orchestrator composes this and wires it to dispatch/DB calls.
"""

import logging
from typing import Dict, List, Set

from core.models import ModelOutputError, Task

log = logging.getLogger(__name__)


class DependencyTracker:
    """Track which tasks are blocked on which prerequisites.

    Three maps:
      _pending_deps[task_id]  = set of dep IDs this task still waits on
      _reverse_deps[dep_id]   = set of task IDs waiting on dep_id
      _children_of[parent_id] = set of child task IDs
    """

    def __init__(self):
        self._pending_deps: Dict[str, Set[str]] = {}
        self._reverse_deps: Dict[str, Set[str]] = {}
        self._children_of: Dict[str, Set[str]] = {}

    @staticmethod
    def resolve_indices(child_ids: List[str], sub_tasks: List[dict]) -> List[List[str]]:
        """Resolve 0-based depends_on indices to real task IDs.

        Args:
            child_ids: ordered list of task IDs (same order as sub_tasks).
            sub_tasks: planner output dicts, each may have a ``depends_on``
                       list of 0-based integer indices.

        Returns:
            A list parallel to child_ids where each element is the resolved
            list of dependency task IDs.  Invalid entries (out-of-range,
            self-referencing, non-integer) are silently dropped and logged.
        """
        n = len(child_ids)
        result: List[List[str]] = []
        for idx, st in enumerate(sub_tasks):
            raw_deps = st.get("depends_on", [])
            resolved: List[str] = []
            for dep_idx in raw_deps:
                if isinstance(dep_idx, int) and 0 <= dep_idx < n and dep_idx != idx:
                    resolved.append(child_ids[dep_idx])
                else:
                    raise ModelOutputError(
                        f"Sub-task {idx} has invalid depends_on entry {dep_idx!r} "
                        f"(expected int in [0, {n}) excluding self)"
                    )
            result.append(resolved)
        return result

    def register(self, parent_id: str, children: List[Task]) -> None:
        """Register a batch of sub-tasks and their dependency structure."""
        child_ids: Set[str] = set()
        for child in children:
            child_ids.add(child.id)
            if child.depends_on:
                self._pending_deps[child.id] = set(child.depends_on)
                for dep_id in child.depends_on:
                    self._reverse_deps.setdefault(dep_id, set()).add(child.id)
        self._children_of[parent_id] = child_ids

    def is_blocked(self, task_id: str) -> bool:
        """Return True if task_id has unmet dependencies."""
        return task_id in self._pending_deps

    def on_completed(self, task_id: str) -> List[str]:
        """Process a task completion: remove it from all dependents' pending sets.

        Returns the list of task IDs that just became unblocked.
        """
        unblocked: List[str] = []
        waiting = self._reverse_deps.pop(task_id, set())
        for dep_task_id in waiting:
            pending = self._pending_deps[dep_task_id]
            pending.discard(task_id)
            if not pending:
                del self._pending_deps[dep_task_id]
                unblocked.append(dep_task_id)
        return unblocked

    def cleanup(self, task_id: str) -> None:
        """Remove task_id from all maps (called on cancel/fail).

        Does NOT auto-dispatch dependents — a cancelled/failed prerequisite
        means dependents stay blocked; the parent will be marked failed.
        """
        # Remove as a waiter
        removed_deps = self._pending_deps.pop(task_id, set())
        for dep_id in removed_deps:
            rev = self._reverse_deps.get(dep_id)
            if rev:
                rev.discard(task_id)
                if not rev:
                    del self._reverse_deps[dep_id]
        # Remove as a dependency (dependents stay blocked)
        self._reverse_deps.pop(task_id, None)

    def get_children(self, parent_id: str) -> Set[str]:
        """Return the set of child task IDs for a parent (empty set if none)."""
        return self._children_of.get(parent_id, set())
