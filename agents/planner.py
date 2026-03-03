"""Planner agent: scans TODOs, breaks down complex tasks into sub-tasks."""

import json
import logging
import os
import re
import subprocess
from typing import List, Tuple

from agents.base import BaseAgent
from agents.prompts import (
    analyzer_todo,
    planner_analyze_and_split,
    planner_plan_task,
    planner_decompose_task,
)
from core.models import AgentRun, Task, TaskPriority, TaskSource, TodoItem, TodoItemStatus
from core.opencode_client import OpenCodeClient

log = logging.getLogger(__name__)


class PlannerAgent(BaseAgent):
    agent_type = "planner"

    def __init__(self, model: str, client: OpenCodeClient):
        super().__init__(model, client)

    def scan_todos(self, repo_path: str, extensions: str = "java,cpp,h,py,go",
                   subdir: str = "", limit: int = 0) -> List[dict]:
        """Scan the repository (or a subdirectory) for TODO/FIXME comments.

        Args:
            repo_path: absolute path to the repository root.
            extensions: comma-separated file extensions to include.
            subdir: relative subdirectory within repo_path to restrict the scan
                    (empty string means the whole repo).
            limit: maximum number of results to return (0 = no limit).

        Returns list of {file, line, text} dicts.
        """
        scan_root = os.path.join(repo_path, subdir) if subdir else repo_path
        scan_root = os.path.normpath(scan_root)

        todos = []
        ext_list = extensions.split(",")
        include_args = []
        for ext in ext_list:
            include_args.extend(["--include", f"*.{ext.strip()}"])

        try:
            result = subprocess.run(
                ["grep", "-rn", "--no-messages"] + include_args +
                ["-E", r"(TODO|FIXME|HACK|XXX)\b", scan_root],
                capture_output=True, text=True, timeout=60,
            )
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                # Format: file:line:text
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    todos.append({
                        "file": parts[0],
                        "line": int(parts[1]),
                        "text": parts[2].strip(),
                    })
        except Exception as e:
            log.error("Failed to scan TODOs: %s", e)

        if limit > 0:
            todos = todos[:limit]
        log.info("Found %d TODOs in %s (subdir=%r limit=%d)", len(todos), repo_path, subdir, limit)
        return todos

    def create_tasks_from_todos(
        self, todos: List[dict], max_tasks: int = 20
    ) -> List[Task]:
        """Convert raw TODO items into Task objects."""
        tasks = []
        for item in todos[:max_tasks]:
            rel_path = item["file"]
            text = item["text"]
            # Strip the TODO/FIXME prefix to get the description
            desc = re.sub(r"^.*?(TODO|FIXME|HACK|XXX)\s*:?\s*", "", text)
            if len(desc) < 5:
                continue
            task = Task(
                title=f"TODO: {desc[:80]}",
                description=f"Resolve TODO at {rel_path}:{item['line']}\n\n"
                           f"Original comment: {text}",
                priority=TaskPriority.MEDIUM,
                source=TaskSource.TODO_SCAN,
                file_path=rel_path,
                line_number=item["line"],
            )
            tasks.append(task)
        return tasks

    def analyze_todo(self, item: TodoItem, repo_path: str) -> Tuple[AgentRun, float, float, str]:
        """Run the analyzer on a single TodoItem.
        Returns (agent_run, feasibility_score, difficulty_score, note).
        """
        import time as _time
        log.info(
            "Analyzer starting for todo [%s]: file=%s:%d desc=%r",
            item.id, item.file_path, item.line_number, item.description[:80],
        )
        prompt = analyzer_todo(
            file_path=item.file_path,
            line_number=item.line_number,
            raw_text=item.raw_text,
            description=item.description,
            repo_path=repo_path,
        )
        log.debug("Analyzer prompt length: %d chars (todo=%s)", len(prompt), item.id)

        t0 = _time.time()
        run = self.run(prompt, repo_path)
        elapsed = _time.time() - t0
        text = self.get_text(run)

        log.debug(
            "Analyzer raw output for todo [%s] (%.1fs, exit=%d):\n%s",
            item.id, elapsed, run.exit_code, text[:500],
        )

        feasibility = -1.0
        difficulty = -1.0
        note = ""
        try:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group())
                feasibility = float(data.get("feasibility_score", -1))
                difficulty = float(data.get("difficulty_score", -1))
                note = str(data.get("note", ""))
            else:
                log.warning("Analyzer output for todo [%s] contained no JSON object", item.id)
                note = text[:300]
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(
                "Failed to parse analyzer output for todo [%s]: %s\nRaw text: %s",
                item.id, e, text[:300],
            )
            note = text[:300]

        log.info(
            "Analyzer done for todo [%s]: feasibility=%.1f difficulty=%.1f note=%r (%.1fs)",
            item.id, feasibility, difficulty, note[:80], elapsed,
        )
        return run, feasibility, difficulty, note

    def analyze_and_split(
        self, title: str, description: str, repo_path: str
    ) -> Tuple[AgentRun, bool, str, List[dict], str]:
        """Unified entry-point: assess complexity, decide atomicity, produce plan or sub-tasks.
        Returns (agent_run, is_split, plan_text, sub_tasks_list, complexity).
        plan_text is set only when is_split=False.
        sub_tasks_list is set only when is_split=True.
        complexity is one of: very_complex / complex / medium / simple (empty string on parse failure).
        """
        prompt = planner_analyze_and_split(
            title=title,
            description=description,
            repo_path=repo_path,
        )
        run = self.run(prompt, repo_path)
        text = self.get_text(run)

        is_split = False
        plan_text = ""
        sub_tasks: List[dict] = []
        complexity = ""
        try:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group())
                complexity = str(data.get("complexity", ""))
                is_split = bool(data.get("split", False))
                if is_split:
                    sub_tasks = data.get("sub_tasks", [])
                else:
                    plan_text = data.get("plan", text)
        except (json.JSONDecodeError, AttributeError) as e:
            log.warning("Failed to parse analyze_and_split output: %s", e)
            plan_text = text
        return run, is_split, plan_text, sub_tasks, complexity

    def plan_task(self, task: Task, repo_path: str) -> Tuple[AgentRun, str]:
        """Use opencode to create a detailed implementation plan for a task.
        Returns (agent_run, plan_text).
        """
        prompt = planner_plan_task(
            title=task.title,
            description=task.description,
            file_path=task.file_path,
            line_number=task.line_number,
            repo_path=repo_path,
        )

        run = self.run(prompt, repo_path, task_id=task.id)
        plan_text = self.get_text(run)
        return run, plan_text

    def decompose_complex_task(
        self, description: str, repo_path: str
    ) -> Tuple[AgentRun, List[dict]]:
        """Break a complex task description into sub-tasks.
        Returns (agent_run, list of {title, description} dicts).
        """
        prompt = planner_decompose_task(
            description=description,
            repo_path=repo_path,
        )

        run = self.run(prompt, repo_path)
        text = self.get_text(run)

        # Try to parse JSON from the response
        sub_tasks = []
        try:
            # Find JSON array in response
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                sub_tasks = json.loads(match.group())
        except (json.JSONDecodeError, AttributeError) as e:
            log.warning("Failed to parse planner output as JSON: %s", e)

        return run, sub_tasks
