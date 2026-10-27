from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_support import ast_cache


class StripDocstrings(ast.NodeTransformer):
    """The same new-list-then-assign shape used by adapter architecture guards."""

    def visit_Module(self, node):  # noqa: N802 - ast API
        self.generic_visit(node)
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr):
            node.body = body[1:]
        return node


class ASTCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "sample.py"
        self.path.write_text('"""docs"""\nVALUE = 1\n', encoding="utf-8")
        ast_cache.clear()

    def tearDown(self):
        ast_cache.clear()
        self.tempdir.cleanup()

    def test_source_and_tree_share_one_read_and_parse(self):
        with mock.patch.object(ast_cache.ast, "parse", wraps=ast.parse) as parse:
            source = ast_cache.source(self.path)
            tree = ast_cache.tree(self.path)
            self.assertEqual(source, ast_cache.source(self.path))
            self.assertIs(tree, ast_cache.tree(self.path))
        self.assertEqual(1, parse.call_count)
        self.assertIn("VALUE = 1", ast.unparse(tree))

    def test_reloads_when_size_changes(self):
        ast_cache.tree(self.path)
        self.path.write_text("VALUE = 22\n", encoding="utf-8")
        self.assertEqual("VALUE = 22\n", ast_cache.source(self.path))
        self.assertEqual("VALUE = 22", ast.unparse(ast_cache.tree(self.path)))

    def test_reloads_when_mtime_ns_changes_at_same_size(self):
        ast_cache.tree(self.path)
        stat = self.path.stat()
        self.path.write_text("VALUE = 2\n", encoding="utf-8")
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.assertEqual("VALUE = 2", ast.unparse(ast_cache.tree(self.path)))

    def test_paths_have_independent_snapshots(self):
        other = self.path.with_name("other.py")
        other.write_text("VALUE = 9\n", encoding="utf-8")
        self.assertIn("VALUE = 1", ast.unparse(ast_cache.tree(self.path)))
        self.assertIn("VALUE = 9", ast.unparse(ast_cache.tree(other)))
        self.assertIn("VALUE = 1", ast.unparse(ast_cache.tree(self.path)))

    def test_standard_ast_readers_work(self):
        tree = ast_cache.tree(self.path)
        self.assertEqual(7, len(list(ast.walk(tree))))
        self.assertEqual(["body", "type_ignores"], [name for name, _ in ast.iter_fields(tree)])
        self.assertIn("VALUE", ast.dump(tree, include_attributes=True))
        self.assertIn("VALUE", ast.dump(tree))

    def test_attribute_and_child_list_mutation_are_rejected(self):
        tree = ast_cache.tree(self.path)
        assignment = tree.body[1]
        with self.assertRaises(ast_cache.ReadOnlyASTError):
            tree.body = []
        with self.assertRaises(ast_cache.ReadOnlyASTError):
            tree.body.append(assignment)
        with self.assertRaises(ast_cache.ReadOnlyASTError):
            tree.body[0] = assignment
        with self.assertRaises(ast_cache.ReadOnlyASTError):
            assignment.targets = []
        with self.assertRaises(ast_cache.ReadOnlyASTError):
            assignment.targets[0].id = "CHANGED"
        with self.assertRaises(ast_cache.ReadOnlyASTError):
            assignment.targets[0].ctx = ast.Load()
        with self.assertRaises(ast_cache.ReadOnlyASTError):
            del assignment.value
        self.assertIn("VALUE = 1", ast.unparse(ast_cache.tree(self.path)))

    def test_mutating_transformer_cannot_pollute_later_reader(self):
        with self.assertRaises(ast_cache.ReadOnlyASTError):
            StripDocstrings().visit(ast_cache.tree(self.path))
        self.assertEqual(2, len(ast_cache.tree(self.path).body))

    def test_copy_supports_transformer_that_builds_a_new_tree(self):
        import copy

        transformed = StripDocstrings().visit(copy.deepcopy(ast_cache.tree(self.path)))
        self.assertEqual("VALUE = 1", ast.unparse(transformed))
        self.assertEqual(2, len(ast_cache.tree(self.path).body))

    def test_dynamic_string_parsing_is_not_implicitly_cached(self):
        # The helper accepts paths only; inline probes retain ast.parse semantics.
        with self.assertRaises((TypeError, ValueError, OSError)):
            ast_cache.tree("VALUE = 1\n")


if __name__ == "__main__":
    unittest.main()
