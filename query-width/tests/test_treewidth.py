"""Query lowering and graph bounds checked against independently known widths."""
import contextlib
import io
import itertools
import json
import tempfile
import unittest
from pathlib import Path

try:
    import networkx as nx
except ModuleNotFoundError:
    raise unittest.SkipTest("treewidth tests require: uv sync")

from query_width.treewidth import (
    Environment, QueryBuilder, SourceError, Sources, analyze_suite, extract_queries,
    main, measure_graph, parse_program, query_graph, render,
)


def lower(text):
    env, queries = Environment(), []
    for form in parse_program(text):
        env.declare(form)
        queries.extend(QueryBuilder(env).build(q) for q in extract_queries(form))
    return queries


def bounds(text, model="primal"):
    return measure_graph(query_graph(lower(text)[0], model))


class ParsingTests(unittest.TestCase):
    def test_comments_strings_and_locations(self):
        forms = list(parse_program('; (rule (ignored))\n(rule () ((print "a; (b) \\\"q\\\""))) ; tail\n'))
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0].line, 2)
        self.assertIn('"a; (b)', render(forms[0]))

    def test_deep_initialization_is_iterative(self):
        text = '(let x ' + '(F ' * 2000 + '0' + ')' * 2000 + ')'
        self.assertEqual(len(list(parse_program(text))), 1)

    def test_bad_input_has_location(self):
        for text in (')', '(rule () ()', '(rule () ((print "unterminated)))'):
            with self.subTest(text=text), self.assertRaisesRegex(SourceError, 'input.egg:1:'):
                list(parse_program(text, 'input.egg'))

    def test_unknown_rule_option_fails(self):
        with self.assertRaisesRegex(SourceError, 'unsupported'):
            lower('(rule () () :made-up)')


class QueryTests(unittest.TestCase):
    def test_path_and_triangle(self):
        schema = '(relation R (i64 i64))'
        path = bounds(schema + '(rule ((R x y) (R y z)) ())')
        cycle = bounds(schema + '(rule ((R x y) (R y z) (R z x)) ())')
        self.assertEqual((path['treewidth_lower'], path['treewidth_upper']), (1, 1))
        self.assertEqual((cycle['treewidth_lower'], cycle['treewidth_upper']), (2, 2))

    def test_hyperedge_primal_and_incidence(self):
        source = '(relation R (i64 i64 i64 i64)) (rule ((R a b c d)) ())'
        query = lower(source)[0]
        self.assertEqual(query['hyperedges'], [[0, 1, 2, 3]])
        self.assertEqual(bounds(source)['treewidth_upper'], 3)
        self.assertEqual(bounds(source, 'incidence')['treewidth_upper'], 1)

    def test_nested_constructor_outputs_are_variables(self):
        query = lower('(datatype E (Add E E)) (rewrite (Add (Add a b) c) c)')[0]
        self.assertEqual(query['variables'], 5)
        self.assertEqual(sorted(map(len, query['hyperedges'])), [3, 3])
        self.assertEqual(measure_graph(query_graph(query))['treewidth_upper'], 2)

    def test_repeated_variables_and_fixed_values(self):
        source = '(constructor F (i64 i64 String bool Unit f64) E)'
        query = lower(source + '(rewrite (F x x "a; b" true () 1.2e-3) (Other x))')[0]
        self.assertEqual(query['variables'], 2)
        self.assertEqual(query['hyperedges'], [[0, 1]])

    def test_equality_aliases_and_ground_bindings(self):
        source = '(relation R (i64 i64 i64))'
        query = lower(source + '(rule ((R x y z) (= x y) (= z 1)) ())')[0]
        self.assertEqual(query['hyperedges'], [[0]])
        self.assertEqual(measure_graph(query_graph(query))['treewidth_upper'], 0)

    def test_equal_call_results_are_shared(self):
        source = '(constructor F (i64) E) (constructor G (E) E)'
        query = lower(source + '(rule ((= a (G (F x))) (= b (G (F y))) (= x y)) ())')[0]
        self.assertEqual(len(query['atoms']), 2)
        self.assertEqual(query['variables'], 3)

    def test_calls_on_both_sides_of_equality(self):
        source = '(constructor F (i64) E) (constructor G (i64) E)'
        query = lower(source + '(rule ((= (F x) (G y))) ())')[0]
        self.assertEqual(query['variables'], 3)
        self.assertEqual(len(query['hyperedges']), 2)
        self.assertEqual(measure_graph(query_graph(query))['treewidth_upper'], 1)

    def test_rewrite_conditions_are_excluded_including_relations(self):
        source = '(constructor F (i64) E) (relation R (i64 i64 i64 i64))'
        plain = lower(source + '(rewrite (F x) x)')[0]
        conditioned = lower(source + '(rewrite (F x) x :when ((R x y z w) (> x 0)))')[0]
        self.assertEqual(plain['query_id'], conditioned['query_id'])
        self.assertEqual(conditioned['ignored_when_facts'], 2)

    def test_builtins_and_lookups_inside_predicates_are_excluded(self):
        source = '(relation R (i64 i64)) (function f (i64) i64 :merge new)'
        query = lower(source + '(rule ((R x y) (< (f a) (f b)) (= z (+ x y))) ())')[0]
        self.assertEqual([a['head'] for a in query['atoms']], ['R'])
        self.assertEqual(query['variables'], 2)
        self.assertEqual(len(query['ignored_body_facts']), 2)

    def test_nested_primitive_adds_no_hyperedge(self):
        source = '(constructor F (i64 i64) E)'
        query = lower(source + '(rewrite (F (+ x y) z) z)')[0]
        self.assertEqual([a['head'] for a in query['atoms']], ['F'])
        self.assertEqual(query['variables'], 3)
        self.assertEqual(query['ignored_primitive_calls'], 1)

    def test_birewrite_is_two_queries_and_actions_do_not_count(self):
        source = '(datatype E (A E) (B E E E))'
        queries = lower(source + '(birewrite (A x) (B x y z))')
        self.assertEqual(len(queries), 2)
        self.assertEqual([measure_graph(query_graph(q))['treewidth_upper'] for q in queries], [1, 3])
        self.assertEqual(bounds(source + '(rule ((= a (A x))) ((union a (B x y z))))')['treewidth_upper'], 1)

    def test_globals_are_fixed_but_rule_local_lets_do_not_shadow(self):
        source = '(constructor F (i64 i64) E) (let g 1)'
        query = lower(source + '(rule ((= e (F g x))) ((let x 2)))')[0]
        self.assertEqual(query['variables'], 2)

    def test_empty_ground_unit_and_nullary(self):
        self.assertEqual(bounds('(rule () ())')['treewidth_upper'], 0)
        self.assertEqual(bounds('(relation R (i64)) (rule ((R 1)) ())')['nodes'], 0)
        self.assertEqual(bounds('(function P (i64) Unit) (rule ((P x)) ())')['nodes'], 1)
        self.assertEqual(bounds('(datatype E (Nil)) (rewrite (Nil) (Nil))')['nodes'], 1)

    def test_alpha_renaming_and_rhs_are_ignored_for_deduplication(self):
        source = '(datatype E (A E) (B E E))'
        first = lower(source + '(rewrite (B x (A y)) x)')[0]
        second = lower(source + '(rewrite (B renamed (A other)) (A renamed))')[0]
        self.assertEqual(first['query_id'], second['query_id'])


def exhaustive_width(graph):
    best = max(0, len(graph) - 1)
    for order in itertools.permutations(graph):
        adjacency = {v: set(graph[v]) for v in graph}
        width = 0
        for v in order:
            neighbors = adjacency.pop(v)
            width = max(width, len(neighbors))
            for x in neighbors:
                adjacency[x].discard(v)
                adjacency[x].update(neighbors - {x})
        best = min(best, width)
    return best


class GraphBoundTests(unittest.TestCase):
    def test_known_graph_families(self):
        for graph, width in [(nx.path_graph(9), 1), (nx.cycle_graph(7), 2),
                             (nx.complete_graph(6), 5), (nx.complete_bipartite_graph(3, 5), 3),
                             (nx.convert_node_labels_to_integers(nx.grid_2d_graph(3, 3)), 3)]:
            result = measure_graph(graph)
            self.assertLessEqual(result['treewidth_lower'], width)
            self.assertEqual(result['treewidth_upper'], width)

    def test_bounds_against_exhaustive_search_on_all_graphs_up_to_five_nodes(self):
        for graph in nx.graph_atlas_g():
            if len(graph) > 5:
                continue
            with self.subTest(nodes=len(graph), edges=list(graph.edges)):
                exact = exhaustive_width(graph)
                result = measure_graph(graph)
                self.assertLessEqual(result['treewidth_lower'], exact)
                self.assertGreaterEqual(result['treewidth_upper'], exact)


class CorpusTests(unittest.TestCase):
    def test_includes_are_relative_and_source_definitions_count_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / 'header.egg'
            header.write_text('(relation R (i64 i64))\n(rule ((R x y)) ())\n')
            paths = [root / 'a.egg', root / 'b.egg', header]
            for path in paths[:2]:
                path.write_text('(include "header.egg")\n(rule ((R y z)) ())')
            unique = {}
            rows, inventory = analyze_suite('test', paths, Sources(), 'primal', {}, unique)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(unique), 1)
            self.assertEqual(inventory['source_files'], 3)

    def test_include_cycles_and_missing_files_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cycle.egg'
            path.write_text('(include "cycle.egg")')
            with self.assertRaisesRegex(SourceError, 'include cycle'):
                list(Sources().walk(path))
            path.write_text('(include "missing.egg")')
            with self.assertRaises(FileNotFoundError):
                list(Sources().walk(path))

    def test_cli_outputs_queries_hypergraphs_and_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input.egg'
            source.write_text('(relation R (i64 i64)) (rule ((R x y)) ()) (rule () ())')
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--suite', f'test={source}', '--out', str(root / 'out'), '--no-plot']), 0)
            report = json.loads((root / 'out' / 'summary.json').read_text())
            stats = report['statistics']['test']['occurrences']
            self.assertEqual(stats['count'], 2)
            self.assertEqual(stats['mean'], 0.5)
            self.assertEqual(stats['histogram'], {'0': 1, '1': 1})
            unique = [json.loads(line) for line in (root / 'out' / 'unique_queries.jsonl').read_text().splitlines()]
            self.assertEqual(unique[0]['hyperedges'], [[0, 1]])
            self.assertTrue((root / 'out' / 'queries.csv').exists())


if __name__ == '__main__':
    unittest.main()
