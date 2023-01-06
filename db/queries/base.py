from frozendict import frozendict
from sqlalchemy import select

from db.records.operations import select as records_select
from db.columns.base import MathesarColumn
from db.columns.operations.select import get_column_name_from_attnum
from db.tables.operations.select import reflect_table_from_oid
from db.transforms.operations.apply import apply_transformations
from db.metadata import get_empty_metadata


class DBQuery:
    def __init__(
            self,
            base_table_oid,
            initial_columns,
            engine,
            transformations=None,
            name=None,
            # The same metadata will be used by all the methods within DBQuery
            # So make sure to change the metadata in case the DBQuery methods are called
            # after a mutation to the database object that could make the existing metadata invalid.
            metadata=None
    ):
        self.base_table_oid = base_table_oid
        for initial_col in initial_columns:
            assert isinstance(initial_col, InitialColumn)
        self.initial_columns = initial_columns
        self.engine = engine
        if transformations is None:
            # Less states to consider if no transformations is just an empty sequence
            transformations = tuple()
        self.transformations = transformations
        self.name = name
        self.metadata = metadata if metadata else get_empty_metadata()

    def get_input_aliases(self, ix_of_transform):
        """
        Each transformation in a DBQuery has its own list of input aliases; this returns it.
        """
        initial_aliases = self.initial_aliases
        if ix_of_transform == 0:
            return initial_aliases
        input_aliases = initial_aliases
        previous_transforms = self.transformations[:ix_of_transform]
        for transform in previous_transforms:
            output_aliases = transform.get_output_aliases(input_aliases)
            input_aliases = output_aliases
        return input_aliases

    def get_initial_column_by_input_alias(self, ix_of_transform, input_alias):
        """
        Retraces the chain of input aliases until it gets to an initial column.

        Returns None if the alias does not originate from an initial column in a way that would
        preserve a unique constraint. E.g. if it is generated by an aggregation.
        """
        initial_col_alias = \
            self._get_initial_alias_by_input_alias(ix_of_transform, input_alias)
        if initial_col_alias is None:
            return None
        initial_column = \
            self._get_initial_column_by_initial_column_alias(initial_col_alias)
        return initial_column

    def _get_initial_alias_by_input_alias(self, ix_of_transform, input_alias):
        if ix_of_transform == 0:
            return input_alias
        transforms = self.transformations[:ix_of_transform]
        initial_aliases = self.initial_aliases
        input_aliases = initial_aliases
        uc_mappings_for_each_transform = [
            transform.get_unique_constraint_mappings(input_aliases)
            for transform in transforms
        ]
        for uc_mappings in reversed(uc_mappings_for_each_transform):
            for uc_mapping in uc_mappings:
                if uc_mapping.output_alias == input_alias:
                    input_alias = uc_mapping.input_alias
                    if input_alias is None:
                        return None
                    break
        initial_alias = input_alias
        return initial_alias

    def _get_initial_column_by_initial_column_alias(self, alias):
        """
        Looks up an initial column by initial column alias; no recursive logic.
        """
        for initial_column in self.initial_columns:
            if initial_column.alias == alias:
                return initial_column

    @property
    def initial_aliases(self):
        return [
            initial_column.alias
            for initial_column
            in self.initial_columns
        ]

    # mirrors a method in db.records.operations.select
    def get_records(self, **kwargs):
        # NOTE how through this method you can perform a second batch of
        # transformations.  this reflects fact that we can form a query, and
        # then apply temporary transforms on it, like how you can apply
        # temporary transforms to a table when in a table view.
        return records_select.get_records_with_default_order(
            table=self.transformed_relation, engine=self.engine, **kwargs,
        )

    # mirrors a method in db.records.operations.select
    def get_count(self, **kwargs):
        return records_select.get_count(
            table=self.transformed_relation, engine=self.engine, **kwargs,
        )

    # NOTE if too expensive, can be rewritten to parse DBQuery spec, instead of leveraging sqlalchemy
    @property
    def all_sa_columns_map(self):
        """
        Expensive! use with care.
        """
        initial_columns_map = {
            col.name: MathesarColumn.from_column(col, engine=self.engine)
            for col in self.initial_relation.columns
        }
        output_columns_map = {
            col.name: col for col in self.sa_output_columns
        }
        transforms_columns_map = {} if self.transformations is None else {
            col.name: MathesarColumn.from_column(col, engine=self.engine)
            for i in range(len(self.transformations))
            for col in DBQuery(
                base_table_oid=self.base_table_oid,
                initial_columns=self.initial_columns,
                engine=self.engine,
                transformations=self.transformations[:i],
                name=f'{self.name}_{i}'
            ).transformed_relation.columns
        }
        map_of_alias_to_sa_col = initial_columns_map | transforms_columns_map | output_columns_map
        return map_of_alias_to_sa_col

    @property
    def sa_output_columns(self):
        """
        Sequence of SQLAlchemy columns representing the output columns of the
        relation described by this query.
        """
        return tuple(
            MathesarColumn.from_column(sa_col, engine=self.engine)
            for sa_col
            in self.transformed_relation.columns
        )

    @property
    def transformed_relation(self):
        """
        A query describes a relation. This property is the result of parsing a
        query into a relation.
        """
        transformations = self.transformations
        if transformations:
            transformed = apply_transformations(
                self.initial_relation,
                transformations,
            )
            return transformed
        else:
            return self.initial_relation

    @property
    def initial_relation(self):
        metadata = self.metadata
        base_table = reflect_table_from_oid(
            self.base_table_oid, self.engine, metadata=metadata
        )
        from_clause = base_table
        # We cache this to avoid copies of the same join path to a given table
        jp_path_alias_map = {(): base_table}
        jp_path_unique_set = set()

        def _get_table(oid):
            """
            We use the function-scoped metadata so all involved tables are aware
            of each other.
            """
            return reflect_table_from_oid(oid, self.engine, metadata=metadata, keep_existing=True)

        def _get_column_name(oid, attnum):
            return get_column_name_from_attnum(oid, attnum, self.engine, metadata=metadata)

        def _process_initial_column(col):
            nonlocal from_clause
            col_name = _get_column_name(col.reloid, col.attnum)
            # Make the path hashable so it can be a dict key
            jp_path = _guarantee_jp_path_tuples(col.jp_path)
            right = base_table

            for i, jp in enumerate(jp_path):
                left = jp_path_alias_map[jp_path[:i]]
                right_table = jp_path[:i + 1]
                if right_table in jp_path_alias_map:
                    right = jp_path_alias_map[right_table]
                else:
                    right = _get_table(jp[1][0]).alias()
                    jp_path_alias_map[jp_path[:i + 1]] = right
                left_col_name = _get_column_name(jp[0][0], jp[0][1])
                right_col_name = _get_column_name(jp[1][0], jp[1][1])
                left_col = left.columns[left_col_name]
                right_col = right.columns[right_col_name]
                join_columns = f"{left_col}, {right_col}"
                if join_columns not in jp_path_unique_set:
                    jp_path_unique_set.add(join_columns)
                    from_clause = from_clause.join(
                        right, onclause=left_col == right_col, isouter=True,
                    )

            return right.columns[col_name].label(col.alias)

        stmt = select(
            [_process_initial_column(col) for col in self.initial_columns]
        ).select_from(from_clause)
        return stmt.cte()

    def get_input_alias_for_output_alias(self, output_alias):
        return self.map_of_output_alias_to_input_alias.get(output_alias)

    # TODO consider caching; not urgent, since redundant calls don't trigger IO, it seems
    @property
    def map_of_output_alias_to_input_alias(self):
        m = dict()
        transforms = self.transformations
        if transforms:
            for transform in transforms:
                m = m | transform.map_of_output_alias_to_input_alias
        return m


class InitialColumn:
    def __init__(
            self,
            # TODO consider renaming to oid; reloid is not a term we use,
            # even if it's what postgres uses; or use reloid more
            reloid,
            attnum,
            alias,
            jp_path=None,
    ):
        # alias mustn't be an empty string
        assert isinstance(alias, str) and alias.strip() != ""
        self.reloid = reloid
        self.attnum = attnum
        self.alias = alias
        self.jp_path = _guarantee_jp_path_tuples(jp_path)

    @property
    def is_base_column(self):
        """
        A base column is an initial column on a query's base table.
        """
        return self.jp_path is None

    def __eq__(self, other):
        """Instances are equal when attributes are equal."""
        if type(other) is type(self):
            return self.__dict__ == other.__dict__
        return False

    def __hash__(self):
        """Hashes are equal when attributes are equal."""
        return hash(frozendict(self.__dict__))


def _guarantee_jp_path_tuples(jp_path):
    """
    Makes sure that jp_path is made up of tuples or is an empty tuple.
    """
    if jp_path is not None:
        return tuple(
            (
                tuple(edge[0]),
                tuple(edge[1]),
            )
            for edge
            in jp_path
        )
    else:
        return tuple()
