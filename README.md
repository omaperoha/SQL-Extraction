# SQL Column Extractor

A Python command-line tool that scans a folder of `.sql` files and produces a single
Excel report listing every **table** and **column reference** found across all files,
along with the SQL clause where each reference appears.

## What it does

For each `.sql` file in a folder, the script parses the SQL and extracts:

- Every column referenced in `SELECT`, `WHERE`, `JOIN ON`, `GROUP BY`, `HAVING`, and `ORDER BY`
- Every table name referenced in `FROM` or `JOIN` clauses

Results are deduplicated and written to a single `.xlsx` file for easy review in Excel.

## Requirements

- Python 3.8 or higher
- The three libraries listed in `requirements.txt`:
  - `sqlglot` — SQL parser
  - `pandas` — data handling
  - `openpyxl` — Excel writer

## Quick Start

```bash
# 1. Install dependencies (one time only)
pip install -r requirements.txt

# 2. Run against a folder of .sql files
python sql_extractor.py "C:\path\to\your\sql_folder"
```

The output Excel file is created inside the same folder as your `.sql` files.

## Output Format

| Column | Description |
|--------|-------------|
| `file` | Source `.sql` filename |
| `table_name` | Table alias or qualifier (e.g. `c` from `c.customer_id`) |
| `column_name` | Column identifier (`*` for `SELECT *`) |
| `clause` | SQL clause: `SELECT`, `WHERE`, `JOIN ON`, `GROUP BY`, `HAVING`, `ORDER BY`, `FROM/JOIN (table)` |

## First time on Windows?

See [HOW_TO_RUN.md](HOW_TO_RUN.md) for a step-by-step guide including how to install
Python and run the script from Command Prompt.

## Notes

- Multi-statement SQL files (separated by `;`) are fully supported
- CTEs, subqueries, UNION, and window functions are all handled
- Files that cannot be parsed are skipped with a warning; processing continues
- Only `.sql` files in the **top level** of the specified folder are scanned (no subfolders)
