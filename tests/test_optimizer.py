import unittest
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial

import duckdb
from pandas.testing import assert_frame_equal

import sqlglot
from sqlglot import exp, optimizer, parse_one
from sqlglot.errors import OptimizeError, SchemaError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.scope import build_scope, traverse_scope, walk_in_scope
from sqlglot.schema import MappingSchema
from tests.helpers import (
    TPCDS_SCHEMA,
    TPCH_SCHEMA,
    load_sql_fixture_pairs,
    load_sql_fixtures,
    string_to_bool,
)


def parse_and_optimize(func, sql, dialect, **kwargs):
    return func(parse_one(sql, read=dialect), **kwargs)


def qualify_columns(expression, **kwargs):
    expression = optimizer.qualify_tables.qualify_tables(expression)
    expression = optimizer.qualify_columns.qualify_columns(expression, **kwargs)
    return expression


def pushdown_projections(expression, **kwargs):
    expression = optimizer.qualify_tables.qualify_tables(expression)
    expression = optimizer.qualify_columns.qualify_columns(expression, **kwargs)
    expression = optimizer.pushdown_projections.pushdown_projections(expression, **kwargs)
    return expression


def normalize(expression, **kwargs):
    expression = optimizer.normalize.normalize(expression, dnf=False)
    return optimizer.simplify.simplify(expression)


class TestOptimizer(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        sqlglot.schema = MappingSchema()
        cls.conn = duckdb.connect()
        cls.conn.execute(
            """
        CREATE TABLE x (a INT, b INT);
        CREATE TABLE y (b INT, c INT);
        CREATE TABLE z (b INT, c INT);
        CREATE TABLE w (d TEXT, e TEXT);

        INSERT INTO x VALUES (1, 1);
        INSERT INTO x VALUES (2, 2);
        INSERT INTO x VALUES (2, 2);
        INSERT INTO x VALUES (3, 3);
        INSERT INTO x VALUES (null, null);

        INSERT INTO y VALUES (2, 2);
        INSERT INTO y VALUES (2, 2);
        INSERT INTO y VALUES (3, 3);
        INSERT INTO y VALUES (4, 4);
        INSERT INTO y VALUES (null, null);

        INSERT INTO y VALUES (3, 3);
        INSERT INTO y VALUES (3, 3);
        INSERT INTO y VALUES (4, 4);
        INSERT INTO y VALUES (5, 5);
        INSERT INTO y VALUES (null, null);

        INSERT INTO w VALUES ('a', 'b');
        """
        )

    def setUp(self):
        self.schema = {
            "x": {
                "a": "INT",
                "b": "INT",
            },
            "y": {
                "b": "INT",
                "c": "INT",
            },
            "z": {
                "b": "INT",
                "c": "INT",
            },
            "w": {
                "d": "TEXT",
                "e": "TEXT",
            },
        }

    def check_file(self, file, func, pretty=False, execute=False, **kwargs):
        with ProcessPoolExecutor() as pool:
            results = {}

            for i, (meta, sql, expected) in enumerate(
                load_sql_fixture_pairs(f"optimizer/{file}.sql"), start=1
            ):
                title = meta.get("title") or f"{i}, {sql}"
                dialect = meta.get("dialect")
                leave_tables_isolated = meta.get("leave_tables_isolated")

                func_kwargs = {**kwargs}
                if leave_tables_isolated is not None:
                    func_kwargs["leave_tables_isolated"] = string_to_bool(leave_tables_isolated)

                future = pool.submit(parse_and_optimize, func, sql, dialect, **func_kwargs)
                results[future] = (
                    sql,
                    title,
                    expected,
                    dialect,
                    execute if meta.get("execute") is None else False,
                )

        for future in as_completed(results):
            optimized = future.result()
            sql, title, expected, dialect, execute = results[future]

            with self.subTest(title):
                self.assertEqual(
                    expected,
                    optimized.sql(pretty=pretty, dialect=dialect),
                )

            if string_to_bool(execute):
                with self.subTest(f"(execute) {title}"):
                    df1 = self.conn.execute(
                        sqlglot.transpile(sql, read=dialect, write="duckdb")[0]
                    ).df()
                    df2 = self.conn.execute(optimized.sql(pretty=pretty, dialect="duckdb")).df()
                    assert_frame_equal(df1, df2)

    def test_optimize(self):
        schema = {
            "x": {"a": "INT", "b": "INT"},
            "y": {"b": "INT", "c": "INT"},
            "z": {"a": "INT", "c": "INT"},
        }

        self.check_file("optimizer", optimizer.optimize, pretty=True, execute=True, schema=schema)

    def test_isolate_table_selects(self):
        self.check_file(
            "isolate_table_selects",
            optimizer.isolate_table_selects.isolate_table_selects,
            schema=self.schema,
        )

    def test_qualify_tables(self):
        self.check_file(
            "qualify_tables",
            optimizer.qualify_tables.qualify_tables,
            db="db",
            catalog="c",
        )

    def test_normalize(self):
        self.assertEqual(
            optimizer.normalize.normalize(
                parse_one("x AND (y OR z)"),
                dnf=True,
            ).sql(),
            "(x AND y) OR (x AND z)",
        )

        self.assertEqual(
            optimizer.normalize.normalize(
                parse_one("x AND (y OR z)"),
            ).sql(),
            "x AND (y OR z)",
        )

        self.check_file("normalize", normalize)

    def test_qualify_columns(self):
        self.check_file("qualify_columns", qualify_columns, execute=True, schema=self.schema)

    def test_qualify_columns__with_invisible(self):
        schema = MappingSchema(self.schema, {"x": {"a"}, "y": {"b"}, "z": {"b"}})
        self.check_file("qualify_columns__with_invisible", qualify_columns, schema=schema)

    def test_qualify_columns__invalid(self):
        for sql in load_sql_fixtures("optimizer/qualify_columns__invalid.sql"):
            with self.subTest(sql):
                with self.assertRaises((OptimizeError, SchemaError)):
                    expression = optimizer.qualify_columns.qualify_columns(
                        parse_one(sql), schema=self.schema
                    )
                    optimizer.qualify_columns.validate_qualify_columns(expression)

    def test_lower_identities(self):
        self.check_file("lower_identities", optimizer.lower_identities.lower_identities)

    def test_pushdown_projection(self):
        self.check_file("pushdown_projections", pushdown_projections, schema=self.schema)

    def test_simplify(self):
        self.check_file("simplify", optimizer.simplify.simplify)

        expression = parse_one("TRUE AND TRUE AND TRUE")
        self.assertEqual(exp.true(), optimizer.simplify.simplify(expression))
        self.assertEqual(exp.true(), optimizer.simplify.simplify(expression.this))

    def test_unnest_subqueries(self):
        self.check_file(
            "unnest_subqueries",
            optimizer.unnest_subqueries.unnest_subqueries,
            pretty=True,
        )

    def test_pushdown_predicates(self):
        self.check_file("pushdown_predicates", optimizer.pushdown_predicates.pushdown_predicates)

    def test_expand_laterals(self):
        # check order of lateral expansion with no schema
        self.assertEqual(
            optimizer.optimize("SELECT a + 1 AS d, d + 1 AS e FROM x " "").sql(),
            'SELECT "x"."a" + 1 AS "d", "x"."a" + 2 AS "e" FROM "x" AS "x"',
        )

        self.check_file(
            "expand_laterals",
            optimizer.expand_laterals.expand_laterals,
            pretty=True,
            execute=True,
        )

    def test_expand_multi_table_selects(self):
        self.check_file(
            "expand_multi_table_selects",
            optimizer.expand_multi_table_selects.expand_multi_table_selects,
        )

    def test_optimize_joins(self):
        self.check_file(
            "optimize_joins",
            optimizer.optimize_joins.optimize_joins,
        )

    def test_eliminate_joins(self):
        self.check_file(
            "eliminate_joins",
            optimizer.eliminate_joins.eliminate_joins,
            pretty=True,
        )

    def test_eliminate_ctes(self):
        self.check_file(
            "eliminate_ctes",
            optimizer.eliminate_ctes.eliminate_ctes,
            pretty=True,
        )

    def test_merge_subqueries(self):
        optimize = partial(
            optimizer.optimize,
            rules=[
                optimizer.qualify_tables.qualify_tables,
                optimizer.qualify_columns.qualify_columns,
                optimizer.merge_subqueries.merge_subqueries,
            ],
        )

        self.check_file("merge_subqueries", optimize, execute=True, schema=self.schema)

    def test_eliminate_subqueries(self):
        self.check_file("eliminate_subqueries", optimizer.eliminate_subqueries.eliminate_subqueries)

    def test_canonicalize(self):
        optimize = partial(
            optimizer.optimize,
            rules=[
                optimizer.qualify_tables.qualify_tables,
                optimizer.qualify_columns.qualify_columns,
                annotate_types,
                optimizer.canonicalize.canonicalize,
            ],
        )
        self.check_file("canonicalize", optimize, schema=self.schema)

    def test_tpch(self):
        self.check_file("tpc-h/tpc-h", optimizer.optimize, schema=TPCH_SCHEMA, pretty=True)

    def test_tpcds(self):
        self.check_file("tpc-ds/tpc-ds", optimizer.optimize, schema=TPCDS_SCHEMA, pretty=True)

    def test_file_schema(self):
        expression = parse_one(
            """
            SELECT *
            FROM READ_CSV('tests/fixtures/optimizer/tpc-h/nation.csv.gz', 'delimiter', '|')
            """
        )
        self.assertEqual(
            """
SELECT
  "_q_0"."n_nationkey" AS "n_nationkey",
  "_q_0"."n_name" AS "n_name",
  "_q_0"."n_regionkey" AS "n_regionkey",
  "_q_0"."n_comment" AS "n_comment"
FROM READ_CSV('tests/fixtures/optimizer/tpc-h/nation.csv.gz', 'delimiter', '|') AS "_q_0"
""".strip(),
            optimizer.optimize(expression).sql(pretty=True),
        )

    def test_scope(self):
        sql = """
        WITH q AS (
          SELECT x.b FROM x
        ), r AS (
          SELECT y.b FROM y
        ), z as (
          SELECT cola, colb FROM (VALUES(1, 'test')) AS tab(cola, colb)
        )
        SELECT
          r.b,
          s.b
        FROM r
        JOIN (
          SELECT y.c AS b FROM y
        ) s
        ON s.b = r.b
        WHERE s.b > (SELECT MAX(x.a) FROM x WHERE x.b = s.b)
        """
        expression = parse_one(sql)
        for scopes in traverse_scope(expression), list(build_scope(expression).traverse()):
            self.assertEqual(len(scopes), 7)
            self.assertEqual(scopes[0].expression.sql(), "SELECT x.b FROM x")
            self.assertEqual(scopes[1].expression.sql(), "SELECT y.b FROM y")
            self.assertEqual(scopes[2].expression.sql(), "(VALUES (1, 'test')) AS tab(cola, colb)")
            self.assertEqual(
                scopes[3].expression.sql(),
                "SELECT cola, colb FROM (VALUES (1, 'test')) AS tab(cola, colb)",
            )
            self.assertEqual(scopes[4].expression.sql(), "SELECT y.c AS b FROM y")
            self.assertEqual(scopes[5].expression.sql(), "SELECT MAX(x.a) FROM x WHERE x.b = s.b")
            self.assertEqual(scopes[6].expression.sql(), parse_one(sql).sql())

            self.assertEqual(set(scopes[6].sources), {"q", "z", "r", "s"})
            self.assertEqual(len(scopes[6].columns), 6)
            self.assertEqual({c.table for c in scopes[6].columns}, {"r", "s"})
            self.assertEqual(scopes[6].source_columns("q"), [])
            self.assertEqual(len(scopes[6].source_columns("r")), 2)
            self.assertEqual({c.table for c in scopes[6].source_columns("r")}, {"r"})

            self.assertEqual({c.sql() for c in scopes[-1].find_all(exp.Column)}, {"r.b", "s.b"})
            self.assertEqual(scopes[-1].find(exp.Column).sql(), "r.b")
            self.assertEqual({c.sql() for c in scopes[0].find_all(exp.Column)}, {"x.b"})

        # Check that we can walk in scope from an arbitrary node
        self.assertEqual(
            {
                node.sql()
                for node, *_ in walk_in_scope(expression.find(exp.Where))
                if isinstance(node, exp.Column)
            },
            {"s.b"},
        )

    def test_literal_type_annotation(self):
        tests = {
            "SELECT 5": exp.DataType.Type.INT,
            "SELECT 5.3": exp.DataType.Type.DOUBLE,
            "SELECT 'bla'": exp.DataType.Type.VARCHAR,
            "5": exp.DataType.Type.INT,
            "5.3": exp.DataType.Type.DOUBLE,
            "'bla'": exp.DataType.Type.VARCHAR,
        }

        for sql, target_type in tests.items():
            expression = annotate_types(parse_one(sql))
            self.assertEqual(expression.find(exp.Literal).type.this, target_type)

    def test_boolean_type_annotation(self):
        tests = {
            "SELECT TRUE": exp.DataType.Type.BOOLEAN,
            "FALSE": exp.DataType.Type.BOOLEAN,
        }

        for sql, target_type in tests.items():
            expression = annotate_types(parse_one(sql))
            self.assertEqual(expression.find(exp.Boolean).type.this, target_type)

    def test_cast_type_annotation(self):
        expression = annotate_types(parse_one("CAST('2020-01-01' AS TIMESTAMPTZ(9))"))
        self.assertEqual(expression.type.this, exp.DataType.Type.TIMESTAMPTZ)
        self.assertEqual(expression.this.type.this, exp.DataType.Type.VARCHAR)
        self.assertEqual(expression.args["to"].type.this, exp.DataType.Type.TIMESTAMPTZ)
        self.assertEqual(expression.args["to"].expressions[0].type.this, exp.DataType.Type.INT)

        expression = annotate_types(parse_one("ARRAY(1)::ARRAY<INT>"))
        self.assertEqual(expression.type, parse_one("ARRAY<INT>", into=exp.DataType))

        expression = annotate_types(parse_one("CAST(x AS INTERVAL)"))
        self.assertEqual(expression.type.this, exp.DataType.Type.INTERVAL)
        self.assertEqual(expression.this.type.this, exp.DataType.Type.UNKNOWN)
        self.assertEqual(expression.args["to"].type.this, exp.DataType.Type.INTERVAL)

    def test_cache_annotation(self):
        expression = annotate_types(
            parse_one("CACHE LAZY TABLE x OPTIONS('storageLevel' = 'value') AS SELECT 1")
        )
        self.assertEqual(expression.expression.expressions[0].type.this, exp.DataType.Type.INT)

    def test_binary_annotation(self):
        expression = annotate_types(parse_one("SELECT 0.0 + (2 + 3)")).expressions[0]

        self.assertEqual(expression.type.this, exp.DataType.Type.DOUBLE)
        self.assertEqual(expression.left.type.this, exp.DataType.Type.DOUBLE)
        self.assertEqual(expression.right.type.this, exp.DataType.Type.INT)
        self.assertEqual(expression.right.this.type.this, exp.DataType.Type.INT)
        self.assertEqual(expression.right.this.left.type.this, exp.DataType.Type.INT)
        self.assertEqual(expression.right.this.right.type.this, exp.DataType.Type.INT)

    def test_lateral_annotation(self):
        expression = optimizer.optimize(
            parse_one("SELECT c FROM (select 1 a) as x LATERAL VIEW EXPLODE (a) AS c")
        ).expressions[0]
        self.assertEqual(expression.type.this, exp.DataType.Type.INT)

    def test_derived_tables_column_annotation(self):
        schema = {"x": {"cola": "INT"}, "y": {"cola": "FLOAT"}}
        sql = """
            SELECT a.cola AS cola
            FROM (
                SELECT x.cola + y.cola AS cola
                FROM (
                    SELECT x.cola AS cola
                    FROM x AS x
                ) AS x
                JOIN (
                    SELECT y.cola AS cola
                    FROM y AS y
                ) AS y
            ) AS a
        """

        expression = annotate_types(parse_one(sql), schema=schema)
        self.assertEqual(
            expression.expressions[0].type.this, exp.DataType.Type.FLOAT
        )  # a.cola AS cola

        addition_alias = expression.args["from"].expressions[0].this.expressions[0]
        self.assertEqual(
            addition_alias.type.this, exp.DataType.Type.FLOAT
        )  # x.cola + y.cola AS cola

        addition = addition_alias.this
        self.assertEqual(addition.type.this, exp.DataType.Type.FLOAT)
        self.assertEqual(addition.this.type.this, exp.DataType.Type.INT)
        self.assertEqual(addition.expression.type.this, exp.DataType.Type.FLOAT)

    def test_cte_column_annotation(self):
        schema = {"x": {"cola": "CHAR"}, "y": {"colb": "TEXT", "colc": "BOOLEAN"}}
        sql = """
            WITH tbl AS (
                SELECT x.cola + 'bla' AS cola, y.colb AS colb, y.colc AS colc
                FROM (
                    SELECT x.cola AS cola
                    FROM x AS x
                ) AS x
                JOIN (
                    SELECT y.colb AS colb, y.colc AS colc
                    FROM y AS y
                ) AS y
            )
            SELECT tbl.cola + tbl.colb + 'foo' AS col
            FROM tbl AS tbl
            WHERE tbl.colc = True
        """

        expression = annotate_types(parse_one(sql), schema=schema)
        self.assertEqual(
            expression.expressions[0].type.this, exp.DataType.Type.TEXT
        )  # tbl.cola + tbl.colb + 'foo' AS col

        outer_addition = expression.expressions[0].this  # (tbl.cola + tbl.colb) + 'foo'
        self.assertEqual(outer_addition.type.this, exp.DataType.Type.TEXT)
        self.assertEqual(outer_addition.left.type.this, exp.DataType.Type.TEXT)
        self.assertEqual(outer_addition.right.type.this, exp.DataType.Type.VARCHAR)

        inner_addition = expression.expressions[0].this.left  # tbl.cola + tbl.colb
        self.assertEqual(inner_addition.left.type.this, exp.DataType.Type.VARCHAR)
        self.assertEqual(inner_addition.right.type.this, exp.DataType.Type.TEXT)

        # WHERE tbl.colc = True
        self.assertEqual(expression.args["where"].this.type.this, exp.DataType.Type.BOOLEAN)

        cte_select = expression.args["with"].expressions[0].this
        self.assertEqual(
            cte_select.expressions[0].type.this, exp.DataType.Type.VARCHAR
        )  # x.cola + 'bla' AS cola
        self.assertEqual(
            cte_select.expressions[1].type.this, exp.DataType.Type.TEXT
        )  # y.colb AS colb
        self.assertEqual(
            cte_select.expressions[2].type.this, exp.DataType.Type.BOOLEAN
        )  # y.colc AS colc

        cte_select_addition = cte_select.expressions[0].this  # x.cola + 'bla'
        self.assertEqual(cte_select_addition.type.this, exp.DataType.Type.VARCHAR)
        self.assertEqual(cte_select_addition.left.type.this, exp.DataType.Type.CHAR)
        self.assertEqual(cte_select_addition.right.type.this, exp.DataType.Type.VARCHAR)

        # Check that x.cola AS cola and y.colb AS colb have types CHAR and TEXT, respectively
        for d, t in zip(
            cte_select.find_all(exp.Subquery), [exp.DataType.Type.CHAR, exp.DataType.Type.TEXT]
        ):
            self.assertEqual(d.this.expressions[0].this.type.this, t)

    def test_function_annotation(self):
        schema = {"x": {"cola": "VARCHAR", "colb": "CHAR"}}
        sql = "SELECT x.cola || TRIM(x.colb) AS col FROM x AS x"

        concat_expr_alias = annotate_types(parse_one(sql), schema=schema).expressions[0]
        self.assertEqual(concat_expr_alias.type.this, exp.DataType.Type.VARCHAR)

        concat_expr = concat_expr_alias.this
        self.assertEqual(concat_expr.type.this, exp.DataType.Type.VARCHAR)
        self.assertEqual(concat_expr.left.type.this, exp.DataType.Type.VARCHAR)  # x.cola
        self.assertEqual(concat_expr.right.type.this, exp.DataType.Type.VARCHAR)  # TRIM(x.colb)
        self.assertEqual(concat_expr.right.this.type.this, exp.DataType.Type.CHAR)  # x.colb

        sql = "SELECT CASE WHEN 1=1 THEN x.cola ELSE x.colb END AS col FROM x AS x"

        case_expr_alias = annotate_types(parse_one(sql), schema=schema).expressions[0]
        self.assertEqual(case_expr_alias.type.this, exp.DataType.Type.VARCHAR)

        case_expr = case_expr_alias.this
        self.assertEqual(case_expr.type.this, exp.DataType.Type.VARCHAR)
        self.assertEqual(case_expr.args["default"].type.this, exp.DataType.Type.CHAR)

        case_ifs_expr = case_expr.args["ifs"][0]
        self.assertEqual(case_ifs_expr.type.this, exp.DataType.Type.VARCHAR)
        self.assertEqual(case_ifs_expr.args["true"].type.this, exp.DataType.Type.VARCHAR)

    def test_unknown_annotation(self):
        schema = {"x": {"cola": "VARCHAR"}}
        sql = "SELECT x.cola || SOME_ANONYMOUS_FUNC(x.cola) AS col FROM x AS x"

        concat_expr_alias = annotate_types(parse_one(sql), schema=schema).expressions[0]
        self.assertEqual(concat_expr_alias.type.this, exp.DataType.Type.UNKNOWN)

        concat_expr = concat_expr_alias.this
        self.assertEqual(concat_expr.type.this, exp.DataType.Type.UNKNOWN)
        self.assertEqual(concat_expr.left.type.this, exp.DataType.Type.VARCHAR)  # x.cola
        self.assertEqual(
            concat_expr.right.type.this, exp.DataType.Type.UNKNOWN
        )  # SOME_ANONYMOUS_FUNC(x.cola)
        self.assertEqual(
            concat_expr.right.expressions[0].type.this, exp.DataType.Type.VARCHAR
        )  # x.cola (arg)

        annotate_types(parse_one("select x from y lateral view explode(y) as x")).expressions[0]

    def test_null_annotation(self):
        expression = annotate_types(parse_one("SELECT NULL + 2 AS col")).expressions[0].this
        self.assertEqual(expression.left.type.this, exp.DataType.Type.NULL)
        self.assertEqual(expression.right.type.this, exp.DataType.Type.INT)

        # NULL <op> UNKNOWN should yield NULL
        sql = "SELECT NULL || SOME_ANONYMOUS_FUNC() AS result"

        concat_expr_alias = annotate_types(parse_one(sql)).expressions[0]
        self.assertEqual(concat_expr_alias.type.this, exp.DataType.Type.NULL)

        concat_expr = concat_expr_alias.this
        self.assertEqual(concat_expr.type.this, exp.DataType.Type.NULL)
        self.assertEqual(concat_expr.left.type.this, exp.DataType.Type.NULL)
        self.assertEqual(concat_expr.right.type.this, exp.DataType.Type.UNKNOWN)

    def test_nullable_annotation(self):
        nullable = exp.DataType.build("NULLABLE", expressions=exp.DataType.build("BOOLEAN"))
        expression = annotate_types(parse_one("NULL AND FALSE"))

        self.assertEqual(expression.type, nullable)
        self.assertEqual(expression.left.type.this, exp.DataType.Type.NULL)
        self.assertEqual(expression.right.type.this, exp.DataType.Type.BOOLEAN)

    def test_predicate_annotation(self):
        expression = annotate_types(parse_one("x BETWEEN a AND b"))
        self.assertEqual(expression.type.this, exp.DataType.Type.BOOLEAN)

        expression = annotate_types(parse_one("x IN (a, b, c, d)"))
        self.assertEqual(expression.type.this, exp.DataType.Type.BOOLEAN)

    def test_aggfunc_annotation(self):
        schema = {"x": {"cola": "SMALLINT", "colb": "FLOAT", "colc": "TEXT", "cold": "DATE"}}

        tests = {
            ("AVG", "cola"): exp.DataType.Type.DOUBLE,
            ("SUM", "cola"): exp.DataType.Type.BIGINT,
            ("SUM", "colb"): exp.DataType.Type.DOUBLE,
            ("MIN", "cola"): exp.DataType.Type.SMALLINT,
            ("MIN", "colb"): exp.DataType.Type.FLOAT,
            ("MAX", "colc"): exp.DataType.Type.TEXT,
            ("MAX", "cold"): exp.DataType.Type.DATE,
            ("COUNT", "colb"): exp.DataType.Type.BIGINT,
            ("STDDEV", "cola"): exp.DataType.Type.DOUBLE,
        }

        for (func, col), target_type in tests.items():
            expression = annotate_types(
                parse_one(f"SELECT {func}(x.{col}) AS _col_0 FROM x AS x"), schema=schema
            )
            self.assertEqual(expression.expressions[0].type.this, target_type)

    def test_concat_annotation(self):
        expression = annotate_types(parse_one("CONCAT('A', 'B')"))
        self.assertEqual(expression.type.this, exp.DataType.Type.VARCHAR)

    def test_recursive_cte(self):
        query = parse_one(
            """
            with recursive t(n) AS
            (
              select 1
              union all
              select n + 1
              FROM t
              where n < 3
            ), y AS (
              select n
              FROM t
              union all
              select n + 1
              FROM y
              where n < 2
            )
            select * from y
            """
        )

        scope_t, scope_y = build_scope(query).cte_scopes
        self.assertEqual(set(scope_t.cte_sources), {"t"})
        self.assertEqual(set(scope_y.cte_sources), {"t", "y"})

    def test_schema_with_spaces(self):
        schema = {
            "a": {
                "b c": "text",
                '"d e"': "text",
            }
        }

        self.assertEqual(
            optimizer.optimize(parse_one("SELECT * FROM a"), schema=schema),
            parse_one('SELECT "a"."b c" AS "b c", "a"."d e" AS "d e" FROM "a" AS "a"'),
        )
