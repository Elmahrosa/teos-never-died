from dataclasses import dataclass, field
from typing import Dict, Tuple


@dataclass
class Detector:
    same_tool_limit: int = 5
    total_tool_limit: int = 10
    _same_tool_counts: Dict[str, int] = field(default_factory=dict)
    _total_count: int = 0
    _fired_countdown: int = 0

    def check(self, tool_name: str) -> Tuple[bool, str]:
        # If disabled, always return False
        if self.same_tool_limit <= 0 or self.total_tool_limit <= 0:
            return (False, "")

        # If we're in the fired countdown period, decrement and return True
        if self._fired_countdown > 0:
            self._fired_countdown -= 1
            return (True, f"fired countdown: {self._fired_countdown + 1}")

        # Increment counters
        self._same_tool_counts[tool_name] = self._same_tool_counts.get(tool_name, 0) + 1
        self._total_count += 1

        # Check if we hit either limit
        same_tool_count = self._same_tool_counts[tool_name]
        total_count = self._total_count

        if same_tool_count == self.same_tool_limit:
            self._fired_countdown = 2  # Stay fired for 2 more calls after this one
            return (True, f"same_tool_limit reached: {same_tool_count}")
        if total_count == self.total_tool_limit:
            self._fired_countdown = 2  # Stay fired for 2 more calls after this one
            return (True, f"total_tool_limit reached: {total_count}")

        return (False, "")

    def reset(self) -> None:
        self._same_tool_counts.clear()
        self._total_count = 0
        self._fired_countdown = 0


@dataclass
class Budget:
    general_calls: int = 100
    restricted_calls: int = 20
    _general_consumed: int = 0
    _restricted_consumed: int = 0

    def remaining(self, kind: str) -> int:
        if kind == "general":
            if self.general_calls <= 0:
                return 0
            return self.general_calls - self._general_consumed
        elif kind == "restricted":
            if self.restricted_calls <= 0:
                return 0
            return self.restricted_calls - self._restricted_consumed
        else:
            raise ValueError(f"Unknown kind: {kind}")

    def consume(self, kind: str) -> None:
        if kind == "general":
            if self.general_calls > 0:
                self._general_consumed += 1
        elif kind == "restricted":
            if self.restricted_calls > 0:
                self._restricted_consumed += 1
        else:
            raise ValueError(f"Unknown kind: {kind}")

    def approaching(self, kind: str, margin: int = 3) -> bool:
        remaining = self.remaining(kind)
        if self.general_calls <= 0 and kind == "general":
            return False
        if self.restricted_calls <= 0 and kind == "restricted":
            return False
        return 0 <= remaining <= margin