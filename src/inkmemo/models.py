from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class DueKind(StrEnum):
    OVERDUE = "overdue"
    TODAY = "today"
    UPCOMING = "upcoming"
    UNDATED = "undated"


@dataclass(frozen=True)
class MergeRange:
    row_from: int
    row_to: int
    col_from: int
    col_to: int

    def contains(self, row: int, col: int) -> bool:
        return self.row_from <= row <= self.row_to and self.col_from <= col <= self.col_to


@dataclass
class Grid:
    values: list[list[object]]
    merges: list[MergeRange]
    document_title: str
    sheet_name: str
    fetched_at: datetime


@dataclass
class Task:
    task_id: str
    product: str
    content: str
    due_text: str
    due_date: date | None
    due_kind: DueKind
    owner: str
    counterpart: str
    source_row: int
    missing_fields: list[str] = field(default_factory=list)
    is_near: bool = False


@dataclass(frozen=True)
class Weather:
    city: str
    condition: str
    icon: str
    temperature: int
    low: int
    high: int

    @classmethod
    def unavailable(cls, city: str) -> Weather:
        return cls(city=city, condition="--", icon="cloud", temperature=0, low=0, high=0)

    @property
    def available(self) -> bool:
        return self.condition != "--"


@dataclass(frozen=True)
class Battery:
    millivolts: int | None
    segments: int = 0
    low: bool = False

    @property
    def display(self) -> str:
        if self.millivolts is None:
            return "电量 --"
        return f"{self.millivolts / 1000:.2f}V"


@dataclass
class LayoutResult:
    layout: str
    png: bytes
    black: bytes
    red: bytes
    display_hash: str


@dataclass
class SyncResult:
    ok: bool
    stage: str
    message: str
    task_count: int = 0
    layout: str | None = None
    sent: bool = False
