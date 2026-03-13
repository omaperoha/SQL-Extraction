"""
sql_extractor.py
----------------
Scans a folder for .sql files and extracts all table and column references
into a single Excel (.xlsx) report.

Usage:
    python sql_extractor.py <folder_path>
    python sql_extractor.py --folder <folder_path>

Output:
    <folder_name>_columns_YYYYMMDD_HHMMSS.xlsx  (written inside the input folder)
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import sqlglot
import sqlglot.expressions as exp

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract all table and column references from .sql files in a folder."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=None,
        help="Path to folder containing .sql files (positional)",
    )
    parser.add_argument(
        "--folder",
        dest="folder_flag",
        default=None,
        help="Path to folder containing .sql files (named flag)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _make_row(col: exp.Column, clause: str, filename: str) -> dict:
    """Build one output row from a Column AST node."""
    return {
        "file": filename,
        "table_name": col.table or None,   # qualifier alias (e.g. "c" in c.customer_id)
        "column_name": col.name,            # just the column identifier
        "clause": clause,
    }


def _extract_columns_from_select(select_node: exp.Select, filename: str) -> list[dict]:
    """
    Extract all column references from a single SELECT node by inspecting
    each clause in isolation (clause-first approach).
    This avoids ambiguity when the same column name appears in multiple clauses.
    """
    rows: list[dict] = []

    # --- SELECT list (projection) ---
    for projection in select_node.expressions:
        if isinstance(projection, exp.Star):
            rows.append({"file": filename, "table_name": None, "column_name": "*", "clause": "SELECT"})
        elif isinstance(projection, exp.Dot) and isinstance(projection.expression, exp.Star):
            # table.* form
            table_alias = projection.this.name if hasattr(projection.this, "name") else None
            rows.append({"file": filename, "table_name": table_alias, "column_name": "*", "clause": "SELECT"})
        else:
            for col in projection.find_all(exp.Column):
                rows.append(_make_row(col, "SELECT", filename))

    # --- WHERE ---
    where = select_node.args.get("where")
    if where:
        for col in where.find_all(exp.Column):
            rows.append(_make_row(col, "WHERE", filename))

    # --- JOINs (ON conditions) ---
    for join in select_node.args.get("joins") or []:
        on_clause = join.args.get("on")
        if on_clause:
            for col in on_clause.find_all(exp.Column):
                rows.append(_make_row(col, "JOIN ON", filename))

    # --- GROUP BY ---
    group = select_node.args.get("group")
    if group:
        for col in group.find_all(exp.Column):
            rows.append(_make_row(col, "GROUP BY", filename))

    # --- HAVING ---
    having = select_node.args.get("having")
    if having:
        for col in having.find_all(exp.Column):
            rows.append(_make_row(col, "HAVING", filename))

    # --- ORDER BY ---
    order = select_node.args.get("order")
    if order:
        for col in order.find_all(exp.Column):
            rows.append(_make_row(col, "ORDER BY", filename))

    return rows


def _extract_tables(statement: exp.Expression) -> list[str]:
    """Return all unique table names referenced in the statement."""
    tables = []
    seen: set[str] = set()
    for table_node in statement.find_all(exp.Table):
        name = table_node.name
        if name and name not in seen:
            seen.add(name)
            tables.append(name)
    return tables


def extract_from_statement(statement: exp.Expression, filename: str) -> list[dict]:
    """
    Walk all SELECT nodes in the AST (including CTEs and subqueries)
    and collect column + table references.
    """
    rows: list[dict] = []

    # Collect columns from every SELECT node in the tree
    for select_node in statement.find_all(exp.Select):
        rows.extend(_extract_columns_from_select(select_node, filename))

    # Collect all table names and add a deduplicated "table reference" row
    # for tables that have no columns listed against them (e.g. in FROM only).
    # These are tracked separately and merged into the output to avoid losing
    # table-only references.
    for table_name in _extract_tables(statement):
        rows.append({
            "file": filename,
            "table_name": table_name,
            "column_name": None,
            "clause": "FROM/JOIN (table)",
        })

    return rows


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

def process_file(filepath: Path) -> tuple[list[dict], int, int]:
    """
    Parse one SQL file and return (rows, success_count, skip_count).
    Never raises; logs warnings on parse errors.
    """
    filename = filepath.name
    try:
        sql_text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning(f"Could not read {filename}: {e}")
        return [], 0, 1

    try:
        statements = sqlglot.parse(sql_text)   # list; handles multi-statement files
    except Exception as e:
        logger.warning(f"Could not parse {filename}: {e}")
        return [], 0, 1

    rows: list[dict] = []
    for stmt in statements:
        if stmt is None:
            continue
        rows.extend(extract_from_statement(stmt, filename))

    return rows, 1, 0


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(rows: list[dict], folder_path: Path) -> Path:
    """Write deduplicated results to an Excel file inside the input folder."""
    folder_name = folder_path.resolve().name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_name = f"{folder_name}_columns_{timestamp}.xlsx"
    output_path = folder_path.resolve() / output_name

    df = pd.DataFrame(rows, columns=["file", "table_name", "column_name", "clause"])
    df = df.drop_duplicates()
    df = df.sort_values(
        ["file", "clause", "table_name", "column_name"],
        na_position="last",
    ).reset_index(drop=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="SQL Columns")

        # Auto-fit column widths
        ws = writer.sheets["SQL Columns"]
        for col_cells in ws.columns:
            max_len = max(
                (len(str(cell.value)) if cell.value is not None else 0)
                for cell in col_cells
            )
            ws.column_dimensions[col_cells[0].column_letter].width = max_len + 4

    return output_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    folder_str = args.folder_flag or args.folder

    if not folder_str:
        logger.error(
            "No folder specified.\n"
            "Usage:  python sql_extractor.py <folder_path>\n"
            "    or  python sql_extractor.py --folder <folder_path>"
        )
        sys.exit(1)

    folder_path = Path(folder_str)
    if not folder_path.is_dir():
        logger.error(f"Not a directory: {folder_path}")
        sys.exit(1)

    sql_files = sorted(folder_path.glob("*.sql"))
    if not sql_files:
        logger.warning(f"No .sql files found in: {folder_path}")
        sys.exit(0)

    logger.info(f"Found {len(sql_files)} .sql file(s) in: {folder_path.resolve()}")

    all_rows: list[dict] = []
    total_success = 0
    total_skip = 0

    for sql_file in sql_files:
        rows, success, skip = process_file(sql_file)
        all_rows.extend(rows)
        total_success += success
        total_skip += skip
        if success:
            logger.info(f"  Parsed: {sql_file.name}  ({len(rows)} references)")

    logger.info(
        f"Done — processed {total_success} file(s), "
        f"skipped {total_skip} file(s) due to errors."
    )

    if not all_rows:
        logger.warning("No references found. No output file written.")
        sys.exit(0)

    output_path = write_output(all_rows, folder_path)
    logger.info(f"Output saved to: {output_path}")


if __name__ == "__main__":
    main()
