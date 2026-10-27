"""SQL hypergraphs and independently checked standard hypertree certificates."""
import itertools
import tempfile
import unittest
from pathlib import Path

try:
    import networkx as nx
    import sqlglot
except ModuleNotFoundError:
    raise unittest.SkipTest("hypertree tests require: uv sync")

from query_width.hypertreewidth import (
    ROOT, join_tree, load_schema, measure_hypergraph, sql_hypergraph,
    verify_decomposition, verify_sources,
)


def gyo_acyclic(raw_edges):
    """Independent acyclicity oracle via Graham-Yu-Ozsoyoglu reduction."""
    edges = set(map(frozenset, raw_edges)) - {frozenset()}
    while edges:
        reduced = {edge for edge in edges if not any(edge < other for other in edges)}
        vertices = set().union(*reduced)
        private = {v for v in vertices if sum(v in edge for edge in reduced) == 1}
        reduced = {edge - private for edge in reduced} - {frozenset()}
        if reduced == edges:
            return False
        edges = reduced
    return True


class SQLHypergraphTests(unittest.TestCase):
    schema = {'r': ('x', 'y', 'payload'), 's': ('x', 'y'), 't': ('x', 'y')}

    def test_transitive_equijoins_and_private_columns(self):
        graph = sql_hypergraph('SELECT min(a.payload) FROM r a, s b, t c '
                              'WHERE a.x=b.x AND b.x=c.y AND a.payload=\'AND ignored\'', self.schema)
        self.assertEqual(len(graph['edges']), 3)
        self.assertEqual(len(graph['variables']), 5)
        self.assertIn(['a.x', 'b.x', 'c.y'], graph['variables'])
        self.assertEqual(len(graph['ignored_predicates']), 1)
        self.assertTrue(any(group == ['a.payload'] for group in graph['variables']))

    def test_self_joins_remain_distinct_atoms(self):
        graph = sql_hypergraph('SELECT count(*) FROM r a JOIN r b ON a.x=b.y', self.schema)
        self.assertEqual([e['alias'] for e in graph['edges']], ['a', 'b'])
        self.assertEqual(len(graph['variables']), 5)

    def test_or_and_inequalities_do_not_create_equivalence_classes(self):
        graph = sql_hypergraph('SELECT count(*) FROM r a, s b WHERE '
                              '(a.x=b.x OR a.y=b.y) AND a.x != b.y', self.schema)
        self.assertEqual(graph['equalities'], [])
        self.assertEqual(len(graph['variables']), 5)
        self.assertEqual(len(graph['ignored_predicates']), 2)

    def test_optional_constraints_do_not_leak_into_mandatory_core(self):
        sql = 'SELECT count(*) FROM r a CROSS JOIN s b LEFT JOIN t c ON c.x=a.x AND c.x=b.x'
        all_ = sql_hypergraph(sql, self.schema)
        core = sql_hypergraph(sql, self.schema, mandatory_only=True)
        self.assertIn(['a.x', 'b.x', 'c.x'], all_['variables'])
        self.assertEqual(len(core['edges']), 2)
        self.assertEqual(core['equalities'], [])
        self.assertIn(['a.x'], core['variables'])
        self.assertIn(['b.x'], core['variables'])

    def test_negative_atom_and_its_columns_are_excluded(self):
        graph = sql_hypergraph('SELECT count(*) FROM r a LEFT JOIN s b ON a.x=b.x WHERE b.x IS NULL', self.schema)
        self.assertEqual([edge['alias'] for edge in graph['edges']], ['a'])
        self.assertEqual(graph['excluded_relations'], [{'alias': 'b', 'table': 's', 'role': 'negative'}])
        self.assertEqual(graph['equalities'], [])
        self.assertEqual(len(graph['ignored_predicates']), 2)
        self.assertTrue(all(col.startswith('a.') for group in graph['variables'] for col in group))

    def test_negative_atom_constraints_do_not_merge_surviving_columns(self):
        sql = ('SELECT count(*) FROM r a CROSS JOIN s b '
               'LEFT JOIN t c ON c.x=a.x AND c.x=b.x AND a.y=b.y WHERE c.x IS NULL')
        graph = sql_hypergraph(sql, self.schema)
        self.assertEqual([edge['alias'] for edge in graph['edges']], ['a', 'b'])
        self.assertEqual(graph['equalities'], [])
        self.assertEqual(len(graph['variables']), 5)

    def test_nullable_payload_filter_is_not_a_negative_atom(self):
        graph = sql_hypergraph('SELECT count(*) FROM r a LEFT JOIN s b ON a.x=b.x WHERE b.y IS NULL', self.schema)
        self.assertEqual(len(graph['edges']), 2)
        self.assertEqual(graph['edges'][1]['role'], 'left_join')
        self.assertEqual(graph['excluded_relations'], [])

    def test_unsupported_queries_fail_loudly(self):
        queries = ['SELECT count(*) FROM r a JOIN s b USING (x)',
                   'SELECT count(*) FROM (SELECT * FROM r) a',
                   'SELECT missing FROM r', 'SELECT x FROM r,s',
                   'SELECT * FROM r; SELECT * FROM s']
        for query in queries:
            with self.subTest(query=query), self.assertRaises(ValueError):
                sql_hypergraph(query, self.schema)

    def test_schema_and_union_view_output_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'schema.sql'
            path.write_text('CREATE TABLE r (id INT PRIMARY KEY, value TEXT); '
                            'CREATE VIEW v AS SELECT id AS key FROM r UNION ALL SELECT id FROM r;')
            self.assertEqual(load_schema([path]), {'r': ('id', 'value'), 'v': ('key',)})


class DecompositionTests(unittest.TestCase):
    def check_width(self, edges, expected):
        result = measure_hypergraph(edges)
        self.assertEqual((result['hypertree_lower'], result['hypertree_upper']), (expected, expected))
        self.assertEqual(verify_decomposition(list(map(frozenset, edges)), result['decomposition']), expected)

    def test_known_hypergraphs(self):
        self.check_width([], 0)
        self.check_width([[]], 0)
        self.check_width([list(range(30))], 1)  # Primal treewidth is 29, hypertree width is 1.
        self.check_width([[0, 1], [1, 2], [2, 3]], 1)
        self.check_width([[0, 1], [1, 2], [0, 2]], 2)
        self.check_width([[i, j] for i in range(5) for j in range(i + 1, 5)], 3)
        self.check_width([[0, 1], [2, 3], [0, 1]], 1)

    def test_nontrivial_standard_special_condition(self):
        # This satisfies all GHD axioms, but its root guards mention z, which
        # appears only below the root. It is not a standard HD.
        edges = [frozenset([0, 1]), frozenset([1, 2]), frozenset([0, 2])]
        invalid = {'bag': [0, 1], 'guards': [0, 1], 'children': [
            {'bag': [0, 1, 2], 'guards': [0, 1], 'children': []}]}
        with self.assertRaisesRegex(ValueError, 'special condition'):
            verify_decomposition(edges, invalid)

    def test_rejects_uncovered_edges_and_disconnected_variable_occurrences(self):
        edges = [frozenset([0, 1]), frozenset([1, 2]), frozenset([2, 3])]
        disconnected = {'bag': [0, 1], 'guards': [0], 'children': [
            {'bag': [2, 3], 'guards': [2], 'children': [
                {'bag': [1, 2], 'guards': [1], 'children': []}]}]}
        with self.assertRaisesRegex(ValueError, 'running intersection'):
            verify_decomposition(edges, disconnected)
        uncovered = {'bag': [0, 1], 'guards': [0], 'children': []}
        with self.assertRaises(ValueError):
            verify_decomposition(edges, uncovered)

    def test_bounded_search_fallback_stays_an_upper_bound(self):
        edges = [[0, 1], [1, 2], [2, 0]]
        result = measure_hypergraph(edges, max_width=1)
        self.assertEqual((result['hypertree_lower'], result['hypertree_upper']), (2, 3))
        self.assertFalse(result['exact'])
        self.assertEqual(verify_decomposition(list(map(frozenset, edges)), result['decomposition']), 3)

    def test_join_tree_criterion_agrees_with_gyo_for_all_four_vertex_hypergraphs(self):
        possible = [frozenset(c) for size in range(1, 5) for c in itertools.combinations(range(4), size)]
        # All hypergraphs with at most four distinct nonempty hyperedges.
        for count in range(1, 5):
            for edges in itertools.combinations(possible, count):
                self.assertEqual(join_tree(list(edges)) is not None, gyo_acyclic(edges), edges)


class BenchmarkTests(unittest.TestCase):
    def test_pinned_source_checksums(self):
        self.assertEqual(verify_sources('job')['query_count'], 113)
        self.assertEqual(verify_sources('lsqb')['query_count'], 9)

    def test_job_all_queries_have_valid_width_one_certificates(self):
        root = ROOT / 'benchmarks' / 'job'
        schema = load_schema([root / 'schema.sql'])
        paths = sorted(p for p in root.glob('*.sql') if p.stem[0].isdigit())
        self.assertEqual(len(paths), 113)
        for path in paths:
            with self.subTest(query=path.stem):
                graph = sql_hypergraph(path.read_text(), schema)
                edges = [e['vertices'] for e in graph['edges']]
                self.assertTrue(gyo_acyclic(edges))
                self.assertEqual(measure_hypergraph(edges)['hypertree_upper'], 1)

    def test_lsqb_positive_patterns_keep_optional_atoms_and_exclude_negative_atoms(self):
        root = ROOT / 'benchmarks' / 'lsqb' / 'sql'
        schema = load_schema([root / 'schema.sql', root / 'views.sql'])
        for number in range(1, 10):
            for mandatory in (False, True):
                with self.subTest(query=number, mandatory=mandatory):
                    graph = sql_hypergraph((root / f'q{number}.sql').read_text(), schema, mandatory)
                    edges = [e['vertices'] for e in graph['edges']]
                    expected = 2 if number in {2, 3} else 1
                    if number == 7:
                        self.assertEqual(len(edges), 2 if mandatory else 4)
                    if number in {8, 9}:
                        self.assertEqual(len(edges), 3)
                        self.assertEqual(sum(r['role'] == 'negative' for r in graph['excluded_relations']), 1)
                    self.assertEqual(gyo_acyclic(edges), expected == 1)
                    result = measure_hypergraph(edges)
                    self.assertEqual((result['hypertree_lower'], result['hypertree_upper']), (expected, expected))
                    self.assertEqual(verify_decomposition(list(map(frozenset, edges)), result['decomposition']), expected)


if __name__ == '__main__':
    unittest.main()
