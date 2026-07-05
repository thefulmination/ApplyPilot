from __future__ import annotations

import argparse
import csv
import sqlite3
import sys

from applypilot.scoring.scorer import REQUIREMENTS_MARKER_RE

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def _rows(db_path: str, cut: int):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(
            """
            SELECT url, title, site, fit_score, length(full_description) AS desc_len,
                   full_description
              FROM jobs
             WHERE fit_score IS NOT NULL
               AND full_description IS NOT NULL
               AND length(full_description) > ?
             ORDER BY url
            """,
            (cut,),
        ):
            match = REQUIREMENTS_MARKER_RE.search(row["full_description"] or "")
            if match and match.start() >= cut:
                yield row
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="List scored jobs whose requirements section was beyond the old description cut.")
    parser.add_argument("--db", required=True, help="SQLite DB backup path. Opened read-only.")
    parser.add_argument("--cut", type=int, default=6000)
    parser.add_argument("--format", choices=("csv", "urls"), default="csv")
    args = parser.parse_args(argv)

    rows = list(_rows(args.db, args.cut))
    if args.format == "csv":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["url", "title", "site", "fit_score", "desc_len"])
        for row in rows:
            writer.writerow([row["url"], row["title"], row["site"], row["fit_score"], row["desc_len"]])
    else:
        for row in rows:
            print(row["url"])
    print(f"{len(rows)} rows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
