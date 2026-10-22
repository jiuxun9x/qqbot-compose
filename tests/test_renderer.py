"""Actual malformed math structure and unchanged rendering security policy."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('qqbot_renderer_test', Path(__file__).resolve().parents[1]/'renderer/server.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class MathNumberTests(unittest.TestCase):
    def test_nested_equation_number_keeps_expression_and_exact_label(self):
        expression = r'\begin{aligned}x&=a+b\\y&=c+d\tag{2}\end{aligned}'
        expected = r'\begin{aligned}x&=a+b\\y&=c+d\end{aligned}'+'\n'+r'\tag{2}'
        self.assertEqual(renderer.normalize_math_tags(expression, True), expected)

    def test_tag_star_and_nested_argument_are_preserved(self):
        expression = r'\begin{gathered}x=y\tag*{\text{条件 A}}\end{gathered}'
        result = renderer.normalize_math_tags(expression, True)
        self.assertTrue(result.endswith(r'\tag*{\text{条件 A}}'))
        self.assertIn(r'\begin{gathered}x=y\end{gathered}', result)

    def test_top_level_inline_multiple_and_malformed_tags_are_not_guessed(self):
        for text, display in ((r'x=y\tag{1}',True),
            (r'\begin{aligned}x=y\tag{1}\end{aligned}',False),
            (r'\begin{aligned}x=y\tag{1}\\y=z\tag{2}\end{aligned}',True),
            (r'\begin{aligned}x=y\tag{1\end{aligned}',True)):
            self.assertEqual(renderer.normalize_math_tags(text, display), text)

    def test_math_ast_repair_retains_kind_and_does_not_mutate_source(self):
        expression = r'\begin{aligned}x&=y\tag{1}\end{aligned}'
        node = {'t':'Math','c':[{'t':'DisplayMath'},expression]}
        result = renderer.sanitize_ast(node)
        self.assertEqual(result['c'][0], {'t':'DisplayMath'})
        self.assertEqual(node['c'][1], expression)
        self.assertTrue(result['c'][1].endswith(r'\tag{1}'))

    def test_unsafe_command_and_disallowed_environment_still_rejected(self):
        for text in (r'\begin{aligned}x=y\tag{\input{/etc/passwd}}\end{aligned}',
                     r'\begin{equation}x=y\end{equation}', r'\csname input\endcsname{x}'):
            with self.assertRaises(renderer.RenderError):
                renderer.sanitize_ast({'t':'Math','c':[{'t':'DisplayMath'},text]})

    def test_literal_code_containing_tag_is_never_normalized(self):
        code = r'\begin{aligned}x=y\tag{2}\end{aligned}'
        node = renderer.sanitize_ast({'t':'CodeBlock','c':[['', ['tex'], []], code]})
        self.assertEqual(node['t'], 'RawBlock')
        self.assertIn(code, node['c'][1])


if __name__ == '__main__':
    unittest.main()
