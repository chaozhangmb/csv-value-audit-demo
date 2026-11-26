import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('csv_audit.py')


class CsvAuditTests(unittest.TestCase):
    def call(self, source, destination, *args):
        return subprocess.run(
            [sys.executable, '-I', str(SCRIPT), str(source),
             '--output-dir', str(destination), *args],
            capture_output=True, text=True,
        )

    def rows_from(self, path):
        with path.open(encoding='utf-8', newline='') as handle:
            return list(csv.reader(handle))

    def difference_list(self, before, after):
        return [
            {'record_number': number, 'column': column,
             'before': old_value, 'after': new_value}
            for number, (old_row, new_row) in enumerate(zip(before[1:], after[1:]), 1)
            for column, old_value, new_value in zip(before[0], old_row, new_row)
            if old_value != new_value
        ]

    def test_report_only_preserves_values(self):
        rows = [
            ['SKU', 'Name', 'Notes', 'Quantity', 'Date'],
            ['00123', '  Widget A  ', 'one, "two"\r\nthree', '0', '03/04/2026'],
            ['00000', '   ', '', '', ''],
            ['00123', '  Widget A  ', 'one, "two"\r\nthree', '0', '03/04/2026'],
        ]
        stream = io.StringIO(newline='')
        csv.writer(stream, lineterminator='\r\n').writerows(rows)
        original = b'\xef\xbb\xbf' + stream.getvalue().encode('utf-8')
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'input.csv', base / 'out'
            source.write_bytes(original)
            result = self.call(source, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_bytes(), original)
            output_rows = self.rows_from(destination / 'cleaned.csv')
            self.assertEqual(output_rows, rows)
            self.assertFalse((destination / 'cleaned.csv').read_bytes().startswith(b'\xef\xbb\xbf'))
            report = json.loads((destination / 'audit.json').read_text())
            self.assertEqual(report['input_sha256'], hashlib.sha256(original).hexdigest())
            self.assertEqual(report['record_count'], 3)
            self.assertEqual(report['changes'], self.difference_list(rows, output_rows))
            self.assertEqual(report['empty_fields']['original']['total'], 3)
            self.assertEqual(report['empty_fields']['output']['total'], 3)
            self.assertEqual(report['exact_duplicate_input_groups'], [{'record_numbers': [1, 3]}])

    def test_trim_is_ascii_space_and_tab_only_and_audit_matches_output(self):
        rows = [
            ['Name', 'name', 'SKU'],
            [' \tAlice\t ', '  keep  ', '00123'],
            [' \t ', '\t', '00000'],
            ['0', ' 0 ', '0'],
            ['\r\nAlice\r\n', 'x', '00001'],
            ['\u00a0Alice\u00a0', 'y', '00002'],
        ]
        stream = io.StringIO(newline='')
        csv.writer(stream, lineterminator='\r\n').writerows(rows)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'in.csv', base / 'out'
            source.write_bytes(stream.getvalue().encode('utf-8'))
            original = source.read_bytes()
            result = self.call(source, destination, '--trim-column', 'Name')
            self.assertEqual(result.returncode, 0, result.stderr)
            output = self.rows_from(destination / 'cleaned.csv')
            expected = [rows[0]] + [[row[0].strip(' \t'), *row[1:]] for row in rows[1:]]
            self.assertEqual(output, expected)
            report = json.loads((destination / 'audit.json').read_text())
            differences = self.difference_list(rows, output)
            self.assertEqual(report['changes'], differences)
            self.assertTrue(all(item['column'] == 'Name' for item in differences))
            self.assertEqual(report['empty_fields']['original']['total'], 0)
            self.assertEqual(report['empty_fields']['output']['total'], 1)
            self.assertEqual(source.read_bytes(), original)

    def test_distinct_rows_that_become_equal_are_not_reported_as_original_duplicates(self):
        rows = [['Name'], [' Alice '], ['Alice']]
        stream = io.StringIO(newline='')
        csv.writer(stream, lineterminator='\r\n').writerows(rows)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'in.csv', base / 'out'
            source.write_bytes(stream.getvalue().encode('utf-8'))
            result = self.call(source, destination, '--trim-column', 'Name')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((destination / 'audit.json').read_text())
            self.assertEqual(report['exact_duplicate_input_groups'], [])
            self.assertEqual(report['record_count'], 2)
            self.assertEqual(len(self.rows_from(destination / 'cleaned.csv')), 3)

    def test_header_only_and_case_distinct_headers_are_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'in.csv', base / 'out'
            source.write_bytes(b'Name,name\r\n')
            result = self.call(source, destination, '--trim-column', 'Name')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((destination / 'audit.json').read_text())
            self.assertEqual(report['columns'], ['Name', 'name'])
            self.assertEqual(report['record_count'], 0)
            self.assertEqual(report['changes'], [])
            self.assertEqual(report['exact_duplicate_input_groups'], [])
            self.assertEqual(report['empty_fields']['output']['total'], 0)

    def test_invalid_input_has_no_outputs_or_traceback(self):
        invalid = {
            'empty': b'',
            'invalid_utf8': b'Name\n\xff\n',
            'duplicate_header': b'A,A\nx,y\n',
            'blank_header': b'A,   \nx,y\n',
            'unequal_width': b'A,B\nx,y\nz\n',
            'blank_logical_row': b'A,B\n\n',
            'unterminated_quote': b'A,B\n"unterminated,x\n',
        }
        for name, content in invalid.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                source, destination = base / 'in.csv', base / 'out'
                source.write_bytes(content)
                original = source.read_bytes()
                result = self.call(source, destination)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(destination.exists())
                self.assertEqual(source.read_bytes(), original)
                self.assertNotIn('Traceback', result.stderr)

    def test_late_invalid_row_is_rejected_before_output_creation(self):
        for content in (b'A,B\n1,2\n3,4\n5\n', b'A,B\n1,2\n3,4\n"late\n'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                source, destination = base / 'in.csv', base / 'out'
                source.write_bytes(content)
                result = self.call(source, destination)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(destination.exists())

    def test_column_and_record_limits_and_exact_valid_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'in.csv', base / 'out'
            source.write_text(','.join('c%d' % i for i in range(31)) + '\n', encoding='utf-8')
            result = self.call(source, destination)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(destination.exists())

            source.write_text(','.join('c%d' % i for i in range(30)) + '\n', encoding='utf-8')
            thirty_column_output = base / 'thirty-columns'
            result = self.call(source, thirty_column_output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads((thirty_column_output / 'audit.json').read_text())['columns']), 30)

            source.write_bytes(b'A\n' + b'x\n' * 10001)
            result = self.call(source, destination)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(destination.exists())

            source.write_bytes(b'A\n' + b'x\n' * 10000)
            result = self.call(source, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((destination / 'audit.json').read_text())
            self.assertEqual(report['record_count'], 10000)

    def test_byte_limit_and_exact_five_million_byte_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'in.csv', base / 'out'
            source.write_bytes(b'A\n' + b'x' * 4_999_998 + b'\n')
            self.assertEqual(source.stat().st_size, 5_000_001)
            result = self.call(source, destination)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(destination.exists())

            source.write_bytes(b'A\n' + b'x' * 4_999_997 + b'\n')
            self.assertEqual(source.stat().st_size, 5_000_000)
            result = self.call(source, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((destination / 'audit.json').read_text())['record_count'], 1)

    def test_unknown_or_repeated_trim_option_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'in.csv', base / 'out'
            source.write_bytes(b'Name\nAlice\n')
            for args in (('--trim-column', 'Missing'),
                         ('--trim-column', 'Name', '--trim-column', 'Name')):
                result = self.call(source, destination, *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(destination.exists())

    def test_existing_output_paths_and_input_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'input.csv', base / 'out'
            original = b'SKU\n00123\n'
            source.write_bytes(original)
            destination.mkdir()
            sentinel = destination / 'sentinel.txt'
            sentinel.write_text('keep exactly', encoding='utf-8')
            result = self.call(source, destination)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(sentinel.read_text(), 'keep exactly')
            self.assertEqual(sorted(p.name for p in destination.iterdir()), ['sentinel.txt'])

            empty = base / 'empty'
            empty.mkdir()
            result = self.call(source, empty)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(list(empty.iterdir()), [])

            result = self.call(source, source)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(source.read_bytes(), original)

    def test_symlink_output_paths_are_refused_without_modifying_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, target = base / 'in.csv', base / 'target'
            source.write_bytes(b'A\nx\n')
            target.mkdir()
            sentinel = target / 'keep'
            sentinel.write_text('unchanged', encoding='utf-8')
            link = base / 'link'
            link.symlink_to(target, target_is_directory=True)
            result = self.call(source, link)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertTrue(link.is_symlink())
            self.assertEqual(sentinel.read_text(), 'unchanged')

            dangling = base / 'dangling'
            dangling.symlink_to(base / 'missing')
            result = self.call(source, dangling)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertTrue(dangling.is_symlink())

    def test_missing_parent_is_not_created_but_new_sibling_child_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / 'in.csv'
            source.write_bytes(b'A\nx\n')
            missing_parent = base / 'missing' / 'out'
            result = self.call(source, missing_parent)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse((base / 'missing').exists())

            sibling = base / 'example-output'
            sibling.mkdir()
            result = self.call(source, sibling / 'sample.csv')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_formula_prefixes_warn_on_original_and_output_without_neutralizing(self):
        values = ['=1+1', '+SUM(A1:A2)', '-1', '@example', '\t=1+1', ' =1+1']
        rows = [['Value']] + [[value] for value in values]
        stream = io.StringIO(newline='')
        csv.writer(stream, lineterminator='\r\n').writerows(rows)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'in.csv', base / 'out'
            source.write_bytes(stream.getvalue().encode('utf-8'))
            result = self.call(source, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((destination / 'audit.json').read_text())
            formulas = report['formula_like_values']
            self.assertEqual(len(formulas), len(values))
            self.assertEqual([item['record_number'] for item in formulas], list(range(1, 7)))
            self.assertEqual([item['original_prefix'] for item in formulas], ['=', '+', '-', '@', '\t', '='])
            self.assertEqual([item['output_prefix'] for item in formulas], ['=', '+', '-', '@', '\t', '='])
            self.assertEqual(self.rows_from(destination / 'cleaned.csv'), rows)
            self.assertIn('not a spreadsheet-safe export', report['spreadsheet_warning'])

    def test_formula_prefix_transition_is_reported_both_sides(self):
        rows = [['Value'], ['\t=1+1']]
        stream = io.StringIO(newline='')
        csv.writer(stream, lineterminator='\r\n').writerows(rows)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, destination = base / 'in.csv', base / 'out'
            source.write_bytes(stream.getvalue().encode('utf-8'))
            result = self.call(source, destination, '--trim-column', 'Value')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((destination / 'audit.json').read_text())
            self.assertEqual(report['formula_like_values'], [{
                'record_number': 1, 'column': 'Value',
                'original_prefix': '\t', 'output_prefix': '='}])
            self.assertEqual(report['changes'], self.difference_list(rows, self.rows_from(destination / 'cleaned.csv')))


if __name__ == '__main__':
    unittest.main()
