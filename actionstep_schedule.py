"""Actionstep schedule export -> printable per-day column grid.

Pure data transformation: no Flask, no AI, no persistence. The caller supplies
the firm's config (which calendars map to which column, and the time window);
everything else is derived from the uploaded CSV.

Firm config shape (stored in firms.config["actionstep_schedule"]):

    {
      "window_start": "08:00",
      "window_end": "17:00",
      "columns": [
        {"label": "Zach",
         "calendars": [
           {"name": "Zach's Calendar", "color": "#bfdbfe"},
           {"name": "Zach's Dripping Springs Calendar", "color": "#bbf7d0"}
         ]}
      ]
    }

Calendar names match exactly. Colors are assigned once at config-save time
(see assign_colors) so a given calendar keeps its color across every run.
"""

import csv
import datetime
import io
import re

REQUIRED_HEADERS = ["Calendar Name", "Appointment Title", "Start", "End"]

# Guards a shared web worker against an accidental full-history export. A normal
# few-weeks export is under a thousand rows.
MAX_ROWS = 20000

_DATE_FORMATS = ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S")

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Ordered, print-friendly fills. Slots are handed out in order and never
# reshuffled, so adding a calendar later cannot change existing colors.
PALETTE = [
    "#bfdbfe", "#fde68a", "#bbf7d0", "#fecaca", "#ddd6fe", "#fed7aa",
    "#a5f3fc", "#fbcfe8", "#d9f99d", "#c7d2fe", "#99f6e4", "#f5d0fe",
    "#fef08a", "#d1fae5", "#e9d5ff", "#e2e8f0",
]


class ScheduleError(Exception):
    """Raised for problems the user can fix (wrong file, missing columns)."""


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _as_text(value):
    """Stripped string for string input, empty otherwise.

    Config is hand-edited JSON, so a field can hold any type. Coercing here
    turns what would be an AttributeError deep in the call stack into a plain
    validation message.
    """
    return value.strip() if isinstance(value, str) else ""


def parse_hhmm(value, field):
    text = _as_text(value)
    try:
        hour, minute = text.split(":")
        result = datetime.time(int(hour), int(minute))
    except ValueError:
        raise ScheduleError(f"{field} must be a 24-hour time like 08:00 (got {value!r})")
    return result


def iter_config_calendars(config):
    """Yield (column_index, column, calendar_entry) for every configured calendar.

    Skips malformed entries so callers cannot crash on hand-edited JSON;
    validate_config is what reports those to the admin.
    """
    columns = config.get("columns")
    if not isinstance(columns, list):
        return
    for col_index, column in enumerate(columns):
        if not isinstance(column, dict):
            continue
        calendars = column.get("calendars")
        if not isinstance(calendars, list):
            continue
        for entry in calendars:
            if isinstance(entry, dict) and _as_text(entry.get("name")):
                yield col_index, column, entry


def assign_colors(config):
    """Fill in any missing calendar colors from PALETTE, in place.

    Existing colors are left untouched so saved assignments stay stable. Slots
    already in use are skipped, so a newly added calendar takes the next free
    color rather than duplicating one.
    """
    used = set()
    for _, _, entry in iter_config_calendars(config):
        color = _as_text(entry.get("color"))
        if color:
            entry["color"] = color
            used.add(color.lower())

    available = [c for c in PALETTE if c.lower() not in used]
    next_slot = 0
    for _, _, entry in iter_config_calendars(config):
        if _as_text(entry.get("color")):
            continue
        if next_slot < len(available):
            entry["color"] = available[next_slot]
            next_slot += 1
        else:
            # More calendars than palette slots: wrap round rather than fail.
            entry["color"] = PALETTE[next_slot % len(PALETTE)]
            next_slot += 1
    return config


def validate_config(config):
    """Return a list of human-readable problems with a firm's tool config."""
    errors = []
    if not isinstance(config, dict):
        return ["Actionstep schedule settings must be a JSON object"]
    if not config:
        return ["Actionstep schedule settings are required when the tool is enabled"]

    try:
        start = parse_hhmm(config.get("window_start"), "window_start")
        end = parse_hhmm(config.get("window_end"), "window_end")
        if start >= end:
            errors.append("Actionstep schedule window_start must be earlier than window_end")
    except ScheduleError as e:
        errors.append(str(e))

    columns = config.get("columns")
    if not isinstance(columns, list) or not columns:
        errors.append("Actionstep schedule needs a non-empty 'columns' array")
        return errors

    seen = {}
    for i, column in enumerate(columns, start=1):
        if not isinstance(column, dict):
            errors.append(f"Actionstep schedule column {i} must be an object")
            continue
        label = _as_text(column.get("label"))
        if not label:
            errors.append(f"Actionstep schedule column {i} is missing a 'label'")
        calendars = column.get("calendars")
        if not isinstance(calendars, list) or not calendars:
            errors.append(
                f"Actionstep schedule column {i} needs a non-empty 'calendars' array"
            )
            continue
        for entry in calendars:
            name = _as_text(entry.get("name")) if isinstance(entry, dict) else ""
            if not name:
                errors.append(
                    f"Actionstep schedule column {i} has a calendar without a 'name'"
                )
                continue
            color = _as_text(entry.get("color"))
            if color and not _HEX_COLOR.match(color):
                errors.append(
                    f"Calendar {name!r} has an invalid color {color!r} "
                    "— use a hex value like #bfdbfe"
                )
            if name in seen:
                errors.append(
                    f"Calendar {name!r} is mapped to more than one column "
                    f"({seen[name]} and {label or i})"
                )
            else:
                seen[name] = label or i
    return errors


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------

def decode_csv(raw):
    """Decode an Actionstep export. Exports are Windows-1252, not UTF-8."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ScheduleError("Could not read the file's text encoding.")


def _parse_dt(value):
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def read_rows(raw):
    """Parse the export into appointment dicts. Raises ScheduleError on bad files."""
    text = decode_csv(raw)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ScheduleError("The CSV appears to be empty.")

    headers = {(h or "").strip() for h in reader.fieldnames}
    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        raise ScheduleError(
            "This does not look like an Actionstep Appointments export. "
            "Missing column(s): " + ", ".join(missing)
        )

    appointments = []
    unreadable = 0
    for row in reader:
        start = _parse_dt(row.get("Start"))
        end = _parse_dt(row.get("End"))
        if start is None:
            unreadable += 1
            continue
        if end is None or end < start:
            end = start
        appointments.append({
            "calendar": (row.get("Calendar Name") or "").strip(),
            "title": " ".join((row.get("Appointment Title") or "").split()),
            "start": start,
            "end": end,
            "all_day": (row.get("All Day Flag") or "").strip().upper() == "T",
        })
        if len(appointments) > MAX_ROWS:
            raise ScheduleError(
                f"This export has more than {MAX_ROWS:,} appointments. "
                "Export a shorter date range and try again."
            )
    return appointments, unreadable


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _fmt_time(value):
    """12-hour label without platform-specific strftime padding flags."""
    hour = value.hour % 12 or 12
    suffix = "am" if value.hour < 12 else "pm"
    return f"{hour}:{value.minute:02d} {suffix}"


def _assign_lanes(blocks):
    """Give each block a lane within its overlap cluster.

    Blocks that do not overlap anything keep the column's full width; only the
    overlapping span is subdivided. Mutates and returns the list.
    """
    blocks.sort(key=lambda b: (b["start"], b["end"]))

    cluster = []
    cluster_end = None
    clusters = []
    for block in blocks:
        if cluster and block["start"] >= cluster_end:
            clusters.append(cluster)
            cluster = []
            cluster_end = None
        cluster.append(block)
        cluster_end = max(cluster_end or block["end"], block["end"])
    if cluster:
        clusters.append(cluster)

    for group in clusters:
        lane_ends = []
        for block in group:
            placed = False
            for lane, end in enumerate(lane_ends):
                if block["start"] >= end:
                    block["lane"] = lane
                    lane_ends[lane] = block["end"]
                    placed = True
                    break
            if not placed:
                block["lane"] = len(lane_ends)
                lane_ends.append(block["end"])
        width = len(lane_ends)
        for block in group:
            block["lanes"] = width
    return blocks


def build_schedule(raw, config):
    """Turn an export plus firm config into printable per-day page data."""
    errors = validate_config(config)
    if errors:
        raise ScheduleError(errors[0])

    window_start = parse_hhmm(config.get("window_start"), "window_start")
    window_end = parse_hhmm(config.get("window_end"), "window_end")

    # Colors normally come from the saved config. The positional fallback keeps
    # output deterministic if one is ever missing, without mutating the caller's
    # (cached, shared) config dict, and guarantees no color is ever null
    # downstream.
    calendar_lookup = {}
    for position, (col_index, _, entry) in enumerate(iter_config_calendars(config)):
        name = _as_text(entry.get("name"))
        color = _as_text(entry.get("color")) or PALETTE[position % len(PALETTE)]
        calendar_lookup[name] = (col_index, color)

    appointments, unreadable = read_rows(raw)
    if unreadable and not appointments:
        raise ScheduleError(
            f"None of the {unreadable:,} rows had a readable Start date. "
            "Expected a format like 9/15/2026 14:30."
        )

    columns_config = config["columns"]
    span_minutes = (
        window_end.hour * 60 + window_end.minute
        - window_start.hour * 60 - window_start.minute
    )

    days = {}
    mapped_counts = {name: 0 for name in calendar_lookup}
    unmapped = {}
    for appt in appointments:
        mapping = calendar_lookup.get(appt["calendar"])
        if mapping is None:
            if appt["calendar"]:
                unmapped[appt["calendar"]] = unmapped.get(appt["calendar"], 0) + 1
            continue
        mapped_counts[appt["calendar"]] += 1
        col_index, color = mapping
        day = appt["start"].date()
        bucket = days.setdefault(day, [[] for _ in columns_config])
        bucket[col_index].append((appt, color))

    pages = []
    for day in sorted(days):
        page_columns = []
        for col_index, column_config in enumerate(columns_config):
            blocks = []
            before = []
            after = []
            for appt, color in days[day][col_index]:
                start, end = appt["start"], appt["end"]
                item = {
                    "title": appt["title"] or "(no title)",
                    "color": color,
                    "calendar": appt["calendar"],
                    "time_label": _fmt_time(start)
                    if start == end
                    else f"{_fmt_time(start)}\u2013{_fmt_time(end)}",
                }

                if appt["all_day"]:
                    item["reason"] = "all day"
                    before.append(item)
                    continue
                if start == end:
                    item["reason"] = "no duration"
                    before.append(item)
                    continue
                if start.time() < window_start:
                    item["reason"] = "before window"
                    before.append(item)
                    continue
                if start.time() >= window_end:
                    item["reason"] = "after window"
                    after.append(item)
                    continue

                # Runs past the window (or past midnight): clamp to the grid.
                limit = datetime.datetime.combine(day, window_end)
                clamped_end = min(end, limit)
                offset = (
                    start.hour * 60 + start.minute
                    - window_start.hour * 60 - window_start.minute
                )
                duration = max(
                    int((clamped_end - start).total_seconds() // 60), 1
                )
                item.update({
                    "start": start,
                    "end": clamped_end,
                    "top_pct": round(offset * 100 / span_minutes, 4),
                    "height_pct": round(duration * 100 / span_minutes, 4),
                    "truncated": clamped_end < end,
                })
                blocks.append(item)

            _assign_lanes(blocks)
            for block in blocks:
                block.pop("start", None)
                block.pop("end", None)

            page_columns.append({
                "label": column_config.get("label") or "",
                "blocks": blocks,
                "before": before,
                "after": after,
            })

        pages.append({
            "date": day.isoformat(),
            "date_label": day.strftime("%A, %B ") + str(day.day) + day.strftime(", %Y"),
            "columns": page_columns,
        })

    return {
        "pages": pages,
        "hours": _hour_marks(window_start, window_end, span_minutes),
        "window": {
            "start": window_start.strftime("%H:%M"),
            "end": window_end.strftime("%H:%M"),
        },
        "calendars": [
            {"name": name, "count": mapped_counts[name], "included": True}
            for name in calendar_lookup
        ] + [
            {"name": name, "count": count, "included": False}
            for name, count in sorted(unmapped.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "unreadable_rows": unreadable,
        "appointment_count": sum(
            len(c["blocks"]) + len(c["before"]) + len(c["after"])
            for page in pages for c in page["columns"]
        ),
    }


def _hour_marks(window_start, window_end, span_minutes):
    """Hour gridlines as percentage offsets down the grid."""
    marks = []
    hour = window_start.hour
    if window_start.minute:
        hour += 1
    while hour < window_end.hour or (hour == window_end.hour and window_end.minute):
        offset = hour * 60 - (window_start.hour * 60 + window_start.minute)
        if offset >= 0:
            label = f"{hour % 12 or 12} {'am' if hour < 12 else 'pm'}"
            marks.append({"label": label, "top_pct": round(offset * 100 / span_minutes, 4)})
        hour += 1
    return marks
