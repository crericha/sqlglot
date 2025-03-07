import itertools
from collections import defaultdict
from enum import Enum, auto

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.helper import find_new_name


class ScopeType(Enum):
    ROOT = auto()
    SUBQUERY = auto()
    DERIVED_TABLE = auto()
    CTE = auto()
    UNION = auto()
    UDTF = auto()


class Scope:
    """
    Selection scope.

    Attributes:
        expression (exp.Select|exp.Union): Root expression of this scope
        sources (dict[str, exp.Table|Scope]): Mapping of source name to either
            a Table expression or another Scope instance. For example:
                SELECT * FROM x                     {"x": Table(this="x")}
                SELECT * FROM x AS y                {"y": Table(this="x")}
                SELECT * FROM (SELECT ...) AS y     {"y": Scope(...)}
        lateral_sources (dict[str, exp.Table|Scope]): Sources from laterals
            For example:
                SELECT c FROM x LATERAL VIEW EXPLODE (a) AS c;
            The LATERAL VIEW EXPLODE gets x as a source.
        outer_column_list (list[str]): If this is a derived table or CTE, and the outer query
            defines a column list of it's alias of this scope, this is that list of columns.
            For example:
                SELECT * FROM (SELECT ...) AS y(col1, col2)
            The inner query would have `["col1", "col2"]` for its `outer_column_list`
        parent (Scope): Parent scope
        scope_type (ScopeType): Type of this scope, relative to it's parent
        subquery_scopes (list[Scope]): List of all child scopes for subqueries
        cte_scopes (list[Scope]): List of all child scopes for CTEs
        derived_table_scopes (list[Scope]): List of all child scopes for derived_tables
        udtf_scopes (list[Scope]): List of all child scopes for user defined tabular functions
        table_scopes (list[Scope]): derived_table_scopes + udtf_scopes, in the order that they're defined
        union_scopes (list[Scope, Scope]): If this Scope is for a Union expression, this will be
            a list of the left and right child scopes.
    """

    def __init__(
        self,
        expression,
        sources=None,
        outer_column_list=None,
        parent=None,
        scope_type=ScopeType.ROOT,
        lateral_sources=None,
    ):
        self.expression = expression
        self.sources = sources or {}
        self.lateral_sources = lateral_sources.copy() if lateral_sources else {}
        self.sources.update(self.lateral_sources)
        self.outer_column_list = outer_column_list or []
        self.parent = parent
        self.scope_type = scope_type
        self.subquery_scopes = []
        self.derived_table_scopes = []
        self.table_scopes = []
        self.cte_scopes = []
        self.union_scopes = []
        self.udtf_scopes = []
        self.clear_cache()

    def clear_cache(self):
        self._collected = False
        self._raw_columns = None
        self._derived_tables = None
        self._udtfs = None
        self._tables = None
        self._ctes = None
        self._subqueries = None
        self._selected_sources = None
        self._columns = None
        self._external_columns = None
        self._join_hints = None

    def branch(self, expression, scope_type, chain_sources=None, **kwargs):
        """Branch from the current scope to a new, inner scope"""
        return Scope(
            expression=expression.unnest(),
            sources={**self.cte_sources, **(chain_sources or {})},
            parent=self,
            scope_type=scope_type,
            **kwargs,
        )

    def _collect(self):
        self._tables = []
        self._ctes = []
        self._subqueries = []
        self._derived_tables = []
        self._udtfs = []
        self._raw_columns = []
        self._join_hints = []

        for node, parent, _ in self.walk(bfs=False):
            if node is self.expression:
                continue
            elif isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
                self._raw_columns.append(node)
            elif isinstance(node, exp.Table) and not isinstance(node.parent, exp.JoinHint):
                self._tables.append(node)
            elif isinstance(node, exp.JoinHint):
                self._join_hints.append(node)
            elif isinstance(node, exp.UDTF):
                self._udtfs.append(node)
            elif isinstance(node, exp.CTE):
                self._ctes.append(node)
            elif isinstance(node, exp.Subquery) and isinstance(parent, (exp.From, exp.Join)):
                self._derived_tables.append(node)
            elif isinstance(node, exp.Subqueryable):
                self._subqueries.append(node)

        self._collected = True

    def _ensure_collected(self):
        if not self._collected:
            self._collect()

    def walk(self, bfs=True):
        return walk_in_scope(self.expression, bfs=bfs)

    def find(self, *expression_types, bfs=True):
        """
        Returns the first node in this scope which matches at least one of the specified types.

        This does NOT traverse into subscopes.

        Args:
            expression_types (type): the expression type(s) to match.
            bfs (bool): True to use breadth-first search, False to use depth-first.

        Returns:
            exp.Expression: the node which matches the criteria or None if no node matching
            the criteria was found.
        """
        return next(self.find_all(*expression_types, bfs=bfs), None)

    def find_all(self, *expression_types, bfs=True):
        """
        Returns a generator object which visits all nodes in this scope and only yields those that
        match at least one of the specified expression types.

        This does NOT traverse into subscopes.

        Args:
            expression_types (type): the expression type(s) to match.
            bfs (bool): True to use breadth-first search, False to use depth-first.

        Yields:
            exp.Expression: nodes
        """
        for expression, *_ in self.walk(bfs=bfs):
            if isinstance(expression, expression_types):
                yield expression

    def replace(self, old, new):
        """
        Replace `old` with `new`.

        This can be used instead of `exp.Expression.replace` to ensure the `Scope` is kept up-to-date.

        Args:
            old (exp.Expression): old node
            new (exp.Expression): new node
        """
        old.replace(new)
        self.clear_cache()

    @property
    def tables(self):
        """
        List of tables in this scope.

        Returns:
            list[exp.Table]: tables
        """
        self._ensure_collected()
        return self._tables

    @property
    def ctes(self):
        """
        List of CTEs in this scope.

        Returns:
            list[exp.CTE]: ctes
        """
        self._ensure_collected()
        return self._ctes

    @property
    def derived_tables(self):
        """
        List of derived tables in this scope.

        For example:
            SELECT * FROM (SELECT ...) <- that's a derived table

        Returns:
            list[exp.Subquery]: derived tables
        """
        self._ensure_collected()
        return self._derived_tables

    @property
    def udtfs(self):
        """
        List of "User Defined Tabular Functions" in this scope.

        Returns:
            list[exp.UDTF]: UDTFs
        """
        self._ensure_collected()
        return self._udtfs

    @property
    def subqueries(self):
        """
        List of subqueries in this scope.

        For example:
            SELECT * FROM x WHERE a IN (SELECT ...) <- that's a subquery

        Returns:
            list[exp.Subqueryable]: subqueries
        """
        self._ensure_collected()
        return self._subqueries

    @property
    def columns(self):
        """
        List of columns in this scope.

        Returns:
            list[exp.Column]: Column instances in this scope, plus any
                Columns that reference this scope from correlated subqueries.
        """
        if self._columns is None:
            self._ensure_collected()
            columns = self._raw_columns

            external_columns = [
                column
                for scope in itertools.chain(self.subquery_scopes, self.udtf_scopes)
                for column in scope.external_columns
            ]

            named_selects = set(self.expression.named_selects)

            self._columns = []
            for column in columns + external_columns:
                ancestor = column.find_ancestor(exp.Qualify, exp.Order, exp.Having, exp.Hint)
                if (
                    not ancestor
                    # Window functions can have an ORDER BY clause
                    or not isinstance(ancestor.parent, exp.Select)
                    or column.table
                    or (column.name not in named_selects and not isinstance(ancestor, exp.Hint))
                ):
                    self._columns.append(column)

        return self._columns

    @property
    def selected_sources(self):
        """
        Mapping of nodes and sources that are actually selected from in this scope.

        That is, all tables in a schema are selectable at any point. But a
        table only becomes a selected source if it's included in a FROM or JOIN clause.

        Returns:
            dict[str, (exp.Table|exp.Select, exp.Table|Scope)]: selected sources and nodes
        """
        if self._selected_sources is None:
            referenced_names = []

            for table in self.tables:
                referenced_names.append((table.alias_or_name, table))
            for expression in itertools.chain(self.derived_tables, self.udtfs):
                referenced_names.append((expression.alias, expression.unnest()))
            result = {}

            for name, node in referenced_names:
                if name in result:
                    raise OptimizeError(f"Alias already used: {name}")
                if name in self.sources:
                    result[name] = (node, self.sources[name])

            self._selected_sources = result
        return self._selected_sources

    @property
    def cte_sources(self):
        """
        Sources that are CTEs.

        Returns:
            dict[str, Scope]: Mapping of source alias to Scope
        """
        return {
            alias: scope
            for alias, scope in self.sources.items()
            if isinstance(scope, Scope) and scope.is_cte
        }

    @property
    def selects(self):
        """
        Select expressions of this scope.

        For example, for the following expression:
            SELECT 1 as a, 2 as b FROM x

        The outputs are the "1 as a" and "2 as b" expressions.

        Returns:
            list[exp.Expression]: expressions
        """
        if isinstance(self.expression, exp.Union):
            return self.expression.unnest().selects
        return self.expression.selects

    @property
    def external_columns(self):
        """
        Columns that appear to reference sources in outer scopes.

        Returns:
            list[exp.Column]: Column instances that don't reference
                sources in the current scope.
        """
        if self._external_columns is None:
            self._external_columns = [
                c for c in self.columns if c.table not in self.selected_sources
            ]
        return self._external_columns

    @property
    def unqualified_columns(self):
        """
        Unqualified columns in the current scope.

        Returns:
             list[exp.Column]: Unqualified columns
        """
        return [c for c in self.columns if not c.table]

    @property
    def join_hints(self):
        """
        Hints that exist in the scope that reference tables

        Returns:
            list[exp.JoinHint]: Join hints that are referenced within the scope
        """
        if self._join_hints is None:
            return []
        return self._join_hints

    def source_columns(self, source_name):
        """
        Get all columns in the current scope for a particular source.

        Args:
            source_name (str): Name of the source
        Returns:
            list[exp.Column]: Column instances that reference `source_name`
        """
        return [column for column in self.columns if column.table == source_name]

    @property
    def is_subquery(self):
        """Determine if this scope is a subquery"""
        return self.scope_type == ScopeType.SUBQUERY

    @property
    def is_derived_table(self):
        """Determine if this scope is a derived table"""
        return self.scope_type == ScopeType.DERIVED_TABLE

    @property
    def is_union(self):
        """Determine if this scope is a union"""
        return self.scope_type == ScopeType.UNION

    @property
    def is_cte(self):
        """Determine if this scope is a common table expression"""
        return self.scope_type == ScopeType.CTE

    @property
    def is_root(self):
        """Determine if this is the root scope"""
        return self.scope_type == ScopeType.ROOT

    @property
    def is_udtf(self):
        """Determine if this scope is a UDTF (User Defined Table Function)"""
        return self.scope_type == ScopeType.UDTF

    @property
    def is_correlated_subquery(self):
        """Determine if this scope is a correlated subquery"""
        return bool(self.is_subquery and self.external_columns)

    def rename_source(self, old_name, new_name):
        """Rename a source in this scope"""
        columns = self.sources.pop(old_name or "", [])
        self.sources[new_name] = columns

    def add_source(self, name, source):
        """Add a source to this scope"""
        self.sources[name] = source
        self.clear_cache()

    def remove_source(self, name):
        """Remove a source from this scope"""
        self.sources.pop(name, None)
        self.clear_cache()

    def __repr__(self):
        return f"Scope<{self.expression.sql()}>"

    def traverse(self):
        """
        Traverse the scope tree from this node.

        Yields:
            Scope: scope instances in depth-first-search post-order
        """
        for child_scope in itertools.chain(
            self.cte_scopes, self.union_scopes, self.table_scopes, self.subquery_scopes
        ):
            yield from child_scope.traverse()
        yield self

    def ref_count(self):
        """
        Count the number of times each scope in this tree is referenced.

        Returns:
            dict[int, int]: Mapping of Scope instance ID to reference count
        """
        scope_ref_count = defaultdict(lambda: 0)

        for scope in self.traverse():
            for _, source in scope.selected_sources.values():
                scope_ref_count[id(source)] += 1

        return scope_ref_count


def traverse_scope(expression):
    """
    Traverse an expression by it's "scopes".

    "Scope" represents the current context of a Select statement.

    This is helpful for optimizing queries, where we need more information than
    the expression tree itself. For example, we might care about the source
    names within a subquery. Returns a list because a generator could result in
    incomplete properties which is confusing.

    Examples:
        >>> import sqlglot
        >>> expression = sqlglot.parse_one("SELECT a FROM (SELECT a FROM x) AS y")
        >>> scopes = traverse_scope(expression)
        >>> scopes[0].expression.sql(), list(scopes[0].sources)
        ('SELECT a FROM x', ['x'])
        >>> scopes[1].expression.sql(), list(scopes[1].sources)
        ('SELECT a FROM (SELECT a FROM x) AS y', ['y'])

    Args:
        expression (exp.Expression): expression to traverse
    Returns:
        list[Scope]: scope instances
    """
    return list(_traverse_scope(Scope(expression)))


def build_scope(expression):
    """
    Build a scope tree.

    Args:
        expression (exp.Expression): expression to build the scope tree for
    Returns:
        Scope: root scope
    """
    return traverse_scope(expression)[-1]


def _traverse_scope(scope):
    if isinstance(scope.expression, exp.Select):
        yield from _traverse_select(scope)
    elif isinstance(scope.expression, exp.Union):
        yield from _traverse_union(scope)
    elif isinstance(scope.expression, exp.Subquery):
        yield from _traverse_subqueries(scope)
    elif isinstance(scope.expression, exp.Table):
        # This case corresponds to a "join construct", i.e. (tbl1 JOIN tbl2 ON ..)
        yield from _traverse_tables(scope)
    elif isinstance(scope.expression, exp.UDTF):
        pass
    else:
        raise OptimizeError(f"Unexpected expression type: {type(scope.expression)}")
    yield scope


def _traverse_select(scope):
    yield from _traverse_ctes(scope)
    yield from _traverse_tables(scope)
    yield from _traverse_subqueries(scope)


def _traverse_union(scope):
    yield from _traverse_ctes(scope)

    # The last scope to be yield should be the top most scope
    left = None
    for left in _traverse_scope(scope.branch(scope.expression.left, scope_type=ScopeType.UNION)):
        yield left

    right = None
    for right in _traverse_scope(scope.branch(scope.expression.right, scope_type=ScopeType.UNION)):
        yield right

    scope.union_scopes = [left, right]


def _traverse_ctes(scope):
    sources = {}

    for cte in scope.ctes:
        recursive_scope = None

        # if the scope is a recursive cte, it must be in the form of
        # base_case UNION recursive. thus the recursive scope is the first
        # section of the union.
        if scope.expression.args["with"].recursive:
            union = cte.this

            if isinstance(union, exp.Union):
                recursive_scope = scope.branch(union.this, scope_type=ScopeType.CTE)

        for child_scope in _traverse_scope(
            scope.branch(
                cte.this,
                chain_sources=sources,
                outer_column_list=cte.alias_column_names,
                scope_type=ScopeType.CTE,
            )
        ):
            yield child_scope

            alias = cte.alias
            sources[alias] = child_scope

            if recursive_scope:
                child_scope.add_source(alias, recursive_scope)

        # append the final child_scope yielded
        scope.cte_scopes.append(child_scope)

    scope.sources.update(sources)


def _traverse_tables(scope):
    sources = {}

    # Traverse FROMs, JOINs, and LATERALs in the order they are defined
    expressions = []
    from_ = scope.expression.args.get("from")
    if from_:
        expressions.extend(from_.expressions)

    for join in scope.expression.args.get("joins") or []:
        expressions.append(join.this)

    if isinstance(scope.expression, exp.Table):
        expressions.append(scope.expression)

    expressions.extend(scope.expression.args.get("laterals") or [])

    for expression in expressions:
        if isinstance(expression, exp.Table):
            table_name = expression.name
            source_name = expression.alias_or_name

            if table_name in scope.sources:
                # This is a reference to a parent source (e.g. a CTE), not an actual table.
                sources[source_name] = scope.sources[table_name]
            elif source_name in sources:
                sources[find_new_name(sources, table_name)] = expression
            else:
                sources[source_name] = expression
            continue

        if isinstance(expression, exp.UDTF):
            lateral_sources = sources
            scope_type = ScopeType.UDTF
            scopes = scope.udtf_scopes
        else:
            lateral_sources = None
            scope_type = ScopeType.DERIVED_TABLE
            scopes = scope.derived_table_scopes

        for child_scope in _traverse_scope(
            scope.branch(
                expression,
                lateral_sources=lateral_sources,
                outer_column_list=expression.alias_column_names,
                scope_type=scope_type,
            )
        ):
            yield child_scope

            # Tables without aliases will be set as ""
            # This shouldn't be a problem once qualify_columns runs, as it adds aliases on everything.
            # Until then, this means that only a single, unaliased derived table is allowed (rather,
            # the latest one wins.
            alias = expression.alias
            sources[alias] = child_scope

        # append the final child_scope yielded
        scopes.append(child_scope)
        scope.table_scopes.append(child_scope)

    scope.sources.update(sources)


def _traverse_subqueries(scope):
    for subquery in scope.subqueries:
        top = None
        for child_scope in _traverse_scope(scope.branch(subquery, scope_type=ScopeType.SUBQUERY)):
            yield child_scope
            top = child_scope
        scope.subquery_scopes.append(top)


def walk_in_scope(expression, bfs=True):
    """
    Returns a generator object which visits all nodes in the syntrax tree, stopping at
    nodes that start child scopes.

    Args:
        expression (exp.Expression):
        bfs (bool): if set to True the BFS traversal order will be applied,
            otherwise the DFS traversal will be used instead.

    Yields:
        tuple[exp.Expression, Optional[exp.Expression], str]: node, parent, arg key
    """
    # We'll use this variable to pass state into the dfs generator.
    # Whenever we set it to True, we exclude a subtree from traversal.
    prune = False

    for node, parent, key in expression.walk(bfs=bfs, prune=lambda *_: prune):
        prune = False

        yield node, parent, key

        if node is expression:
            continue
        if (
            isinstance(node, exp.CTE)
            or (isinstance(node, exp.Subquery) and isinstance(parent, (exp.From, exp.Join)))
            or isinstance(node, exp.UDTF)
            or isinstance(node, exp.Subqueryable)
        ):
            prune = True
