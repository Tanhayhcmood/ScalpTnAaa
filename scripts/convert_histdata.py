#!/usr/bin/env python3
"""Convert HistData MetaTrader M1 CSV rows into UTC-aligned M5 candles.

Expected columns: date (YYYY.MM.DD), time (HH:MM), o, h, l, c, v.
The source offset is fixed (default EST, UTC-5); use -4 for EDT data.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _row_value(row: dict[str, str], *names: str) -> str | None:
    lowered = {str(key).strip().lower(): value for key, value in row.items() if key}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def convert_histdata(
    input_path: Path,
    output_path: Path,
    *,
    source_offset_hours: float = -5,
    keep_partial: bool = False,
) -> tuple[int, int]:
    """Write M5 OHLC/tick-volume data; return (written bars, skipped rows)."""
    if not math.isfinite(source_offset_hours) or not -14 <= source_offset_hours <= 14:
        raise ValueError("source offset must be between -14 and +14 hours")

    source_tz = timezone(timedelta(hours=source_offset_hours))
    minutes: dict[datetime, tuple[float, float, float, float, float]] = {}
    skipped_rows = 0
    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("input CSV must have a header row")
        for row in reader:
            try:
                date_text = _row_value(row, "date")
                time_text = _row_value(row, "time")
                if not date_text or not time_text:
                    raise ValueError("missing date/time")
                local_time = datetime.strptime(
                    f"{date_text} {time_text}", "%Y.%m.%d %H:%M"
                ).replace(tzinfo=source_tz)
                utc_time = local_time.astimezone(timezone.utc).replace(second=0, microsecond=0)
                open_price = float(_row_value(row, "open", "o") or "nan")
                high = float(_row_value(row, "high", "h") or "nan")
                low = float(_row_value(row, "low", "l") or "nan")
                close = float(_row_value(row, "close", "c") or "nan")
                volume = float(_row_value(row, "tick_volume", "volume", "v") or "nan")
                if not all(math.isfinite(v) for v in (open_price, high, low, close, volume)):
                    raise ValueError("non-finite OHLCV")
                if (
                    min(open_price, high, low, close) <= 0
                    or volume < 0
                    or high < max(open_price, close)
                    or low > min(open_price, close)
                    or high < low
                ):
                    raise ValueError("invalid OHLCV")
                # Duplicate minute timestamps use the last row, avoiding
                # double-counting volume in vendor exports with repeated rows.
                minutes[utc_time] = (open_price, high, low, close, volume)
            except (TypeError, ValueError, OverflowError):
                skipped_rows += 1

    groups: dict[datetime, list[tuple[datetime, tuple[float, float, float, float, float]]]] = defaultdict(list)
    for timestamp, values in sorted(minutes.items()):
        bucket_minute = (timestamp.minute // 5) * 5
        bucket = timestamp.replace(minute=bucket_minute, second=0, microsecond=0)
        groups[bucket].append((timestamp, values))

    output_rows: list[tuple[datetime, float, float, float, float, float]] = []
    partial_groups = 0
    for bucket, records in sorted(groups.items()):
        expected = [bucket + timedelta(minutes=offset) for offset in range(5)]
        present = [timestamp for timestamp, _ in records]
        complete = present == expected
        if not complete:
            partial_groups += 1
            if not keep_partial:
                continue
        values = [row for _, row in records]
        output_rows.append(
            (
                bucket,
                values[0][0],
                max(row[1] for row in values),
                min(row[2] for row in values),
                values[-1][3],
                sum(row[4] for row in values),
            )
        )

    if not output_rows:
        raise ValueError("no complete M5 candles found (use --keep-partial to retain partial groups)")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "open", "high", "low", "close", "tick_volume"])
        for timestamp, open_price, high, low, close, volume in output_rows:
            writer.writerow(
                [
                    timestamp.strftime("%Y-%m-%dT%H:%M:00Z"),
                    f"{open_price:.10g}",
                    f"{high:.10g}",
                    f"{low:.10g}",
                    f"{close:.10g}",
                    f"{volume:.10g}",
                ]
            )
    return len(output_rows), skipped_rows + partial_groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="HistData M1 CSV")
    parser.add_argument("output", type=Path, help="Output M5 CSV")
    parser.add_argument(
        "--source-offset-hours",
        type=float,
        default=-5,
        help="Fixed source UTC offset in hours (default -5 for EST; use -4 for EDT)",
    )
    parser.add_argument(
        "--keep-partial",
        action="store_true",
        help="Keep M5 buckets with fewer than five unique M1 candles",
    )
    args = parser.parse_args()
    try:
        count, skipped = convert_histdata(
            args.input,
            args.output,
            source_offset_hours=args.source_offset_hours,
            keep_partial=args.keep_partial,
        )
    except (OSError, ValueError) as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {count} M5 candles to {args.output}; skipped {skipped} malformed/partial rows or buckets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
