"""Fixed four-day A–D timetable rotation used by this school."""

SCHEDULE_BLOCKS = (
    ("A", "Block A"),
    ("B", "Block B"),
    ("C", "Block C"),
    ("D", "Block D"),
)

# Each block meets once per rotation day. Values are (rotation_day, period).
BLOCK_ROTATION = {
    "A": ((1, 1), (2, 3), (3, 2), (4, 4)),
    "B": ((1, 2), (2, 4), (3, 1), (4, 3)),
    "C": ((1, 3), (2, 1), (3, 4), (4, 2)),
    "D": ((1, 4), (2, 2), (3, 3), (4, 1)),
}
