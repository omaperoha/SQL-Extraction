"""
sql_extractor.py
----------------
Scans a folder for .sql files and produces a column-level lineage Excel report.

For each SELECT-list projection the script extracts:
  - Database / Schema / Source Table  (fully resolved from qualified table names)
  - View Name                         (from CREATE VIEW, or blank for plain SELECT)
  - View Column Name                  (the output alias)
  - Formula/Transformation Flag       (Yes/No)
  - Formula                           (the SQL expression, if transformed)
  - Source Column                     (the underlying column name)
  - Join Ordinal Sequence             (1 = FROM table, 2 = first JOIN, …)
  - Join Type                         (FROM / INNER JOIN / LEFT OUTER JOIN / …)

CTE columns are traced back to their ultimate physical source table(s).
Multi-source expressions (e.g. CONCAT(a, b)) produce one output row per source column.

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

# SQL data type keywords that may appear as Column nodes inside CAST/CONVERT
# expressions — these should never be treated as real column references.
_SQL_TYPE_KEYWORDS = {
    "datetime", "datetime2", "date", "time", "smalldatetime",
    "varchar", "nvarchar", "char", "nchar", "text", "ntext",
    "int", "integer", "bigint", "smallint", "tinyint",
    "float", "real", "decimal", "numeric", "money", "smallmoney",
    "bit", "binary", "varbinary", "uniqueidentifier", "xml",
    "image", "geography", "geometry",
}

OUTPUT_COLUMNS = [
    "Database",
    "Schema",
    "View Name",
    "View Column Name",
    "Formula/Transformation Flag",
    "Formula",
    "Source Table",
    "Source Column",
    "Join Ordinal Sequence",
    "Join Type",
    "Join Flag",
    "Join Condition",
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract column lineage from .sql files into an Excel report."
    )
    parser.add_argument("folder", nargs="?", default=None,
                        help="Path to folder containing .sql files (positional)")
    parser.add_argument("--folder", dest="folder_flag", default=None,
                        help="Path to folder containing .sql files (named flag)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# View name
# ---------------------------------------------------------------------------

def get_view_name(statement: exp.Expression) -> str | None:
    """Return the view/object name from CREATE VIEW, or None for plain SELECT."""
    if isinstance(statement, exp.Create):
        table = statement.find(exp.Table)
        if table:
            return table.name
    return None


# ---------------------------------------------------------------------------
# Table registry  (alias → metadata)
# ---------------------------------------------------------------------------

def _make_table_info(table_node: exp.Table, ordinal: int, join_type: str) -> dict:
    name = table_node.name
    schema = table_node.db or None        # sqlglot: db = schema
    database = table_node.catalog or None  # sqlglot: catalog = database
    source_table = f"{schema}.{name}" if schema else name
    return {
        "database": database,
        "schema": schema,
        "table": name,
        "source_table": source_table,
        "ordinal": ordinal,
        "join_type": join_type,
    }


def _get_join_type(join_node: exp.Join) -> str:
    parts = []
    side = join_node.args.get("side") or ""
    kind = join_node.args.get("kind") or ""
    if side:
        parts.append(str(side).upper())
    if kind:
        parts.append(str(kind).upper())
    parts.append("JOIN")
    return " ".join(parts)


def _table_from_clause_expr(expr: exp.Expression) -> exp.Table | None:
    """Extract the exp.Table from a FROM/JOIN expression, skipping subqueries."""
    if isinstance(expr, exp.Table):
        return expr
    if isinstance(expr, exp.Alias) and isinstance(expr.this, exp.Table):
        return expr.this
    return None


def build_table_registry(select_node: exp.Select) -> dict:
    """
    Build {alias_or_name: table_info} for the direct FROM + JOIN tables
    of a single SELECT node (does not descend into subqueries).
    Note: sqlglot v25+ stores FROM as args["from_"] (with underscore).
    """
    registry: dict = {}
    ordinal = 0

    # sqlglot >= 25 uses "from_" as the key; fall back to "from" for safety
    from_clause = select_node.args.get("from_") or select_node.args.get("from")
    if from_clause:
        table = _table_from_clause_expr(from_clause.this) if from_clause.this else None
        if table:
            ordinal += 1
            key = table.alias or table.name
            registry[key] = _make_table_info(table, ordinal, "FROM")

    for join in select_node.args.get("joins") or []:
        table = _table_from_clause_expr(join.this) if join.this else None
        if table:
            ordinal += 1
            key = table.alias or table.name
            registry[key] = _make_table_info(table, ordinal, _get_join_type(join))

    return registry


# ---------------------------------------------------------------------------
# CTE registry
# ---------------------------------------------------------------------------

def build_cte_registry(statement: exp.Expression) -> dict:
    """
    Build {cte_name: {select: exp.Select, table_registry: dict}}
    for all CTEs defined in the statement.
    """
    cte_registry: dict = {}
    # sqlglot >= 25 uses "with_" as the key; fall back to "with" for safety
    with_node = statement.args.get("with_") or statement.args.get("with")
    if not with_node:
        return cte_registry

    for cte in (with_node.expressions or []):
        cte_name = cte.alias
        body = cte.this
        # Unwrap Subquery wrapper if present
        if isinstance(body, exp.Subquery):
            body = body.this
        if isinstance(body, (exp.Select, exp.Union)):
            # For UNION inside a CTE, take first branch for projection lookup
            sel = body if isinstance(body, exp.Select) else _first_select(body)
            if sel:
                cte_registry[cte_name] = {
                    "select": sel,
                    "table_registry": build_table_registry(sel),
                }
    return cte_registry


def _first_select(node: exp.Expression) -> exp.Select | None:
    """Return the first exp.Select in a Union tree."""
    if isinstance(node, exp.Select):
        return node
    if isinstance(node, exp.Union):
        return _first_select(node.this)
    return None


# ---------------------------------------------------------------------------
# CTE-aware column resolution
# ---------------------------------------------------------------------------

def _lookup(registry: dict, key: str | None) -> dict | None:
    """Case-insensitive dict lookup."""
    if key is None:
        return None
    direct = registry.get(key)
    if direct:
        return direct
    key_low = key.lower()
    for k, v in registry.items():
        if k.lower() == key_low:
            return v
    return None


def resolve_to_physical(
    col_name: str,
    table_alias: str | None,
    local_table_reg: dict,
    cte_registry: dict,
    depth: int = 0,
) -> list[dict]:
    """
    Recursively trace a column reference through CTEs until a physical table
    is reached.  Returns a list of resolved dicts (one per physical source).
    """
    if depth > 15:
        return [_unresolved(table_alias, col_name)]

    table_info = _lookup(local_table_reg, table_alias)
    if table_info is None:
        # No qualifier — try to find a single-table registry
        if len(local_table_reg) == 1:
            table_info = next(iter(local_table_reg.values()))
        else:
            return [_unresolved(table_alias, col_name)]

    table_name = table_info["table"]
    cte_info = _lookup(cte_registry, table_name)

    if cte_info:
        # This table reference is a CTE — look up the matching projection
        cte_sel: exp.Select = cte_info["select"]
        cte_table_reg: dict = cte_info["table_registry"]

        for proj in cte_sel.expressions:
            proj_alias: str | None = None
            proj_expr = proj

            if isinstance(proj, exp.Alias):
                proj_alias = proj.alias
                proj_expr = proj.this
            elif isinstance(proj, exp.Column):
                proj_alias = proj.name

            if proj_alias and proj_alias.lower() == col_name.lower():
                # Found the matching CTE output column — recurse into its expression
                source_cols = (
                    [proj_expr] if isinstance(proj_expr, exp.Column)
                    else list(proj_expr.find_all(exp.Column))
                )
                results: list[dict] = []
                for src_col in source_cols:
                    results.extend(
                        resolve_to_physical(
                            src_col.name, src_col.table,
                            cte_table_reg, cte_registry,
                            depth + 1,
                        )
                    )
                return results if results else [_unresolved(table_name, col_name)]

        # Column not found by alias in CTE projections
        return [_unresolved(table_name, col_name)]

    # Physical table
    return [{
        "database": table_info["database"],
        "schema": table_info["schema"],
        "source_table": table_info["source_table"],
        "source_column": col_name,
        "ordinal": table_info["ordinal"],
        "join_type": table_info["join_type"],
    }]


def _unresolved(table_alias: str | None, col_name: str) -> dict:
    return {
        "database": None,
        "schema": None,
        "source_table": table_alias or None,
        "source_column": col_name,
        "ordinal": None,
        "join_type": None,
    }


# ---------------------------------------------------------------------------
# SELECT projection processing
# ---------------------------------------------------------------------------

def _expand_aliases_in_text(text: str, table_reg: dict) -> str:
    """Replace table aliases with full table names in SQL text (e.g. F. → FactClaims_MAIN.)."""
    import re
    # Sort aliases longest-first to avoid partial replacements
    for alias, info in sorted(table_reg.items(), key=lambda x: -len(x[0])):
        full = info["source_table"]
        # Unbracketed:  F.col  →  FactClaims_MAIN.col
        text = re.sub(
            r'\b' + re.escape(alias) + r'(?=\s*\.)',
            full, text, flags=re.IGNORECASE,
        )
        # Bracketed:  [F].[col]  →  FactClaims_MAIN.[col]
        text = re.sub(
            r'\[' + re.escape(alias) + r'\](?=\s*\.)',
            full, text, flags=re.IGNORECASE,
        )
    return text


def _build_join_condition_map(select_node: exp.Select, table_reg: dict) -> dict:
    """
    Scan all JOIN ON clauses in a SELECT and return a dict that maps each
    column reference to the ON condition text it appears in.

    Table aliases in the condition text are expanded to full table names
    using the table registry (e.g. F.col → FactClaims_MAIN.col).

    Returns {(table_alias_lower, col_name_lower): on_clause_sql, …}
    A column that appears in multiple ON clauses gets its conditions
    joined with ' ; '.
    """
    jc_map: dict[tuple[str, str], str] = {}
    for join in select_node.args.get("joins") or []:
        on_clause = join.args.get("on")
        if not on_clause:
            continue
        on_text = _expand_aliases_in_text(on_clause.sql(dialect="tsql"), table_reg)
        for col in on_clause.find_all(exp.Column):
            key = ((col.table or "").lower(), col.name.lower())
            if key in jc_map:
                jc_map[key] = jc_map[key] + " ; " + on_text
            else:
                jc_map[key] = on_text
    return jc_map


def _make_row(view_name, view_col_name, is_transformed, formula, resolution,
              join_flag=False, join_condition=None) -> dict:
    return {
        "Database": resolution["database"],
        "Schema": resolution["schema"],
        "View Name": view_name,
        "View Column Name": view_col_name,
        "Formula/Transformation Flag": "Yes" if is_transformed else "No",
        "Formula": formula if is_transformed else None,
        "Source Table": resolution["source_table"],
        "Source Column": resolution["source_column"],
        "Join Ordinal Sequence": resolution["ordinal"],
        "Join Type": resolution["join_type"],
        "Join Flag": join_flag,
        "Join Condition": join_condition,
    }


def process_select(
    select_node: exp.Select,
    view_name: str | None,
    cte_registry: dict,
) -> list[dict]:
    """Process one SELECT node and return lineage rows for its projection list."""
    rows: list[dict] = []
    table_reg = build_table_registry(select_node)
    jc_map = _build_join_condition_map(select_node, table_reg)

    for projection in select_node.expressions:
        # --- Determine output alias and source expression ---
        if isinstance(projection, exp.Alias):
            view_col_name = projection.alias
            expr = projection.this
        elif isinstance(projection, exp.Column):
            view_col_name = projection.name
            expr = projection
        elif isinstance(projection, exp.Star):
            rows.append(_make_row(view_name, "*", False, None,
                                  _unresolved(None, "*")))
            continue
        else:
            # Unaliased expression (function, literal, etc.)
            view_col_name = projection.sql(dialect="tsql")
            expr = projection

        # --- Plain column vs. transformed expression ---
        if isinstance(expr, exp.Column):
            is_transformed = False
            formula = None
            source_cols = [expr]
        else:
            is_transformed = True
            formula = expr.sql(dialect="tsql")
            # Filter out SQL data type keywords that sqlglot may surface as Column nodes
            # inside CAST/CONVERT expressions (e.g. CONVERT(DATETIME, ...) → "DATETIME" col)
            source_cols = [
                c for c in expr.find_all(exp.Column)
                if c.name.lower() not in _SQL_TYPE_KEYWORDS
            ]

        # --- No column references (GETDATE(), literal, etc.) ---
        if not source_cols:
            rows.append(_make_row(view_name, view_col_name, is_transformed, formula,
                                  _unresolved(None, None)))
            continue

        # --- Resolve each source column through CTEs to physical table ---
        for col in source_cols:
            # Check if this column reference appears in any JOIN ON condition
            jc_key = ((col.table or "").lower(), col.name.lower())
            join_condition = jc_map.get(jc_key)
            join_flag = join_condition is not None

            for res in resolve_to_physical(col.name, col.table or None,
                                           table_reg, cte_registry):
                rows.append(_make_row(view_name, view_col_name, is_transformed,
                                      formula, res,
                                      join_flag=join_flag,
                                      join_condition=join_condition))

    return rows


# ---------------------------------------------------------------------------
# Statement-level extraction
# ---------------------------------------------------------------------------

def _iter_main_selects(query_body: exp.Expression):
    """Yield all SELECT branches of the main query (handles UNION)."""
    if isinstance(query_body, exp.Select):
        yield query_body
    elif isinstance(query_body, exp.Union):
        yield from _iter_main_selects(query_body.this)
        yield from _iter_main_selects(query_body.expression)
    elif isinstance(query_body, exp.Subquery):
        yield from _iter_main_selects(query_body.this)


def _get_query_body(statement: exp.Expression) -> exp.Expression | None:
    """Extract the main query body from a statement (handles CREATE VIEW)."""
    if isinstance(statement, exp.Create):
        # expression holds the SELECT/UNION after AS
        body = statement.args.get("expression")
        if body:
            return body
        # Fallback: first Select/Union in the tree (skip the table definition)
        return statement.find(exp.Select)
    if isinstance(statement, (exp.Select, exp.Union)):
        return statement
    return statement.find(exp.Select)


def extract_from_statement(statement: exp.Expression, filename: str) -> list[dict]:
    view_name = get_view_name(statement)       # None → blank in output
    cte_registry = build_cte_registry(statement)
    query_body = _get_query_body(statement)
    if query_body is None:
        return []

    rows: list[dict] = []
    for select_node in _iter_main_selects(query_body):
        rows.extend(process_select(select_node, view_name, cte_registry))
    return rows


# ---------------------------------------------------------------------------
# File preprocessing (encoding + T-SQL preamble removal)
# ---------------------------------------------------------------------------

# Lines whose uppercased text starts with any of these are stripped before parsing.
_TSQL_PREAMBLE_PREFIXES = ("SET ANSI_NULLS", "SET QUOTED_IDENTIFIER", "USE ")

def _preprocess_sql(raw_bytes: bytes) -> tuple[str, str | None]:
    """
    Decode a raw SQL file (handles UTF-16 SSMS exports),
    strip T-SQL batch separators (GO) and SET/USE preambles.

    Returns (cleaned_sql, default_database).
    default_database is extracted from USE [db] before removal.
    """
    import re

    # Detect BOM and decode accordingly
    if raw_bytes.startswith(b"\xff\xfe") or raw_bytes.startswith(b"\xfe\xff"):
        text = raw_bytes.decode("utf-16")
    elif raw_bytes.startswith(b"\xef\xbb\xbf"):
        text = raw_bytes.decode("utf-8-sig")
    else:
        text = raw_bytes.decode("utf-8", errors="replace")

    default_database: str | None = None
    cleaned: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        # Remove standalone GO batch separators
        if upper == "GO":
            continue
        # Capture database name from USE [db] before removing
        if upper.startswith("USE "):
            m = re.match(r"USE\s+\[?(\w+)\]?", stripped, re.IGNORECASE)
            if m:
                default_database = m.group(1)
            continue
        # Remove SET ANSI_NULLS, SET QUOTED_IDENTIFIER
        if upper.startswith("SET ANSI_NULLS") or upper.startswith("SET QUOTED_IDENTIFIER"):
            continue
        cleaned.append(line)

    return "\n".join(cleaned), default_database


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

def process_file(filepath: Path) -> tuple[list[dict], int, int]:
    filename = filepath.name
    try:
        raw_bytes = filepath.read_bytes()
        sql_text, default_database = _preprocess_sql(raw_bytes)
    except OSError as e:
        logger.warning(f"Could not read {filename}: {e}")
        return [], 0, 1

    # Try T-SQL dialect first (most SSMS exports), then generic fallback
    statements = None
    for dialect in ("tsql", None):
        try:
            statements = sqlglot.parse(sql_text, dialect=dialect)
            break
        except Exception:
            pass
    if statements is None:
        logger.warning(f"Could not parse {filename} in any supported dialect — skipping.")
        return [], 0, 1

    rows: list[dict] = []
    for stmt in statements:
        if stmt is None:
            continue
        rows.extend(extract_from_statement(stmt, filename))

    # Apply default database (from USE [db]) to rows that have no Database value
    if default_database:
        for row in rows:
            if row.get("Database") is None:
                row["Database"] = default_database

    return rows, 1, 0


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(rows: list[dict], folder_path: Path) -> Path:
    folder_name = folder_path.resolve().name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = folder_path.resolve() / f"{folder_name}_columns_{timestamp}.xlsx"

    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    df = df.drop_duplicates()
    df = df.sort_values(
        ["View Name", "Join Ordinal Sequence", "View Column Name"],
        na_position="last",
    ).reset_index(drop=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Column Lineage")
        ws = writer.sheets["Column Lineage"]
        for col_cells in ws.columns:
            max_len = max(
                (len(str(c.value)) if c.value is not None else 0) for c in col_cells
            )
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 80)

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
            logger.info(f"  Parsed: {sql_file.name}  ({len(rows)} lineage rows)")

    logger.info(
        f"Done — processed {total_success} file(s), "
        f"skipped {total_skip} file(s) due to errors."
    )

    if not all_rows:
        logger.warning("No lineage data found. No output file written.")
        sys.exit(0)

    output_path = write_output(all_rows, folder_path)
    logger.info(f"Output saved to: {output_path}")


if __name__ == "__main__":
    main()
