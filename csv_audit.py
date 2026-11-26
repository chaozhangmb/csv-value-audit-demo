"""Offline, bounded CSV preservation and audit sample."""

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys


MAX_INPUT_BYTES = 5_000_000
MAX_COLUMNS = 30
MAX_RECORDS = 10_000
FORMULA_PREFIXES = frozenset('=+-@\t\r\n')
SPREADSHEET_WARNING = (
    'Output preserves untrusted spreadsheet values. Formula-like prefixes are '
    'reported, not escaped; this is not a spreadsheet-safe export.'
)


class AuditError(Exception):
    """Expected input or output validation failure."""


def read_snapshot(source):
    try:
        with source.open('rb') as handle:
            original_bytes = handle.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise AuditError('could not read input file')
    if len(original_bytes) > MAX_INPUT_BYTES:
        raise AuditError('input exceeds the 5,000,000-byte limit')

    try:
        decoded = original_bytes.decode('utf-8-sig', errors='strict')
    except UnicodeDecodeError:
        raise AuditError('input is not valid UTF-8')

    previous_field_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(MAX_INPUT_BYTES)
        reader = csv.reader(io.StringIO(decoded, newline=''), strict=True)
        try:
            header = next(reader)
        except StopIteration:
            raise AuditError('input must contain a header row')

        if not header or len(header) > MAX_COLUMNS:
            raise AuditError('header must contain between 1 and 30 columns')
        if any(not name.strip() for name in header):
            raise AuditError('header names must not be empty or whitespace-only')
        if len(set(header)) != len(header):
            raise AuditError('header names must be unique')

        records = []
        for row in reader:
            if len(row) != len(header):
                raise AuditError('a data record has a different number of fields than the header')
            if len(records) >= MAX_RECORDS:
                raise AuditError('input exceeds the 10,000-record limit')
            records.append(row)
    except csv.Error:
        raise AuditError('input contains malformed CSV or a field exceeds the parser limit')
    finally:
        csv.field_size_limit(previous_field_limit)

    return original_bytes, header, records


def count_empty(header, records):
    by_column = {column: 0 for column in header}
    for row in records:
        for column, value in zip(header, row):
            if value == '':
                by_column[column] += 1
    return {'total': sum(by_column.values()), 'by_column': by_column}


def duplicate_groups(records):
    occurrences = {}
    for record_number, row in enumerate(records, 1):
        occurrences.setdefault(tuple(row), []).append(record_number)
    return [
        {'record_numbers': record_numbers}
        for record_numbers in occurrences.values()
        if len(record_numbers) > 1
    ]


def formula_prefix(value):
    inspected = value.lstrip(' ')
    if inspected and inspected[0] in FORMULA_PREFIXES:
        return inspected[0]
    return None


def build_outputs(header, records, trim_column):
    if trim_column is not None and trim_column not in header:
        raise AuditError('trim column must exactly match a header name')

    selected_index = header.index(trim_column) if trim_column is not None else None
    output_records = []
    changes = []
    formula_values = []

    for record_number, original_row in enumerate(records, 1):
        output_row = list(original_row)
        if selected_index is not None:
            before = original_row[selected_index]
            after = before.strip(' \t')
            output_row[selected_index] = after
            if before != after:
                changes.append({
                    'record_number': record_number,
                    'column': trim_column,
                    'before': before,
                    'after': after,
                })

        for column_index, column in enumerate(header):
            original_prefix = formula_prefix(original_row[column_index])
            output_prefix = formula_prefix(output_row[column_index])
            if original_prefix is not None or output_prefix is not None:
                formula_values.append({
                    'record_number': record_number,
                    'column': column,
                    'original_prefix': original_prefix,
                    'output_prefix': output_prefix,
                })
        output_records.append(output_row)

    audit = {
        'schema_version': 1,
        'input_sha256': None,
        'columns': list(header),
        'record_count': len(records),
        'trim_column': trim_column,
        'empty_fields': {
            'original': count_empty(header, records),
            'output': count_empty(header, output_records),
        },
        'exact_duplicate_input_groups': duplicate_groups(records),
        'changes': changes,
        'spreadsheet_warning': SPREADSHEET_WARNING,
        'formula_like_values': formula_values,
    }
    return output_records, audit


def actual_differences(header, original_records, output_records):
    if len(original_records) != len(output_records):
        raise AuditError('output readback changed the number of data records')
    differences = []
    for record_number, (original_row, output_row) in enumerate(
            zip(original_records, output_records), 1):
        if len(original_row) != len(header) or len(output_row) != len(header):
            raise AuditError('output readback changed the number of fields')
        for column, before, after in zip(header, original_row, output_row):
            if before != after:
                differences.append({
                    'record_number': record_number,
                    'column': column,
                    'before': before,
                    'after': after,
                })
    return differences


def write_and_verify(destination, header, original_records, expected_records, audit):
    if os.path.lexists(str(destination)):
        raise AuditError('output path already exists or is a symlink')
    parent = destination.parent
    if not parent.is_dir():
        raise AuditError('output parent directory must already exist')

    created_directory = False
    created_csv = False
    created_audit = False
    try:
        destination.mkdir(exist_ok=False)
        created_directory = True

        cleaned_path = destination / 'cleaned.csv'
        audit_path = destination / 'audit.json'
        with cleaned_path.open('x', encoding='utf-8', newline='') as handle:
            created_csv = True
            writer = csv.writer(handle, lineterminator='\r\n')
            writer.writerow(header)
            writer.writerows(expected_records)

        with audit_path.open('x', encoding='utf-8', newline='') as handle:
            created_audit = True
            json.dump(audit, handle, ensure_ascii=False, indent=2)
            handle.write('\n')

        previous_field_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(MAX_INPUT_BYTES)
            with cleaned_path.open('r', encoding='utf-8', newline='') as handle:
                actual_rows = list(csv.reader(handle, strict=True))
        finally:
            csv.field_size_limit(previous_field_limit)
        with audit_path.open('r', encoding='utf-8') as handle:
            saved_audit = json.load(handle)

        if saved_audit != audit:
            raise AuditError('audit JSON did not pass semantic readback')
        if not actual_rows or actual_rows[0] != header:
            raise AuditError('CSV readback changed the header')
        actual_records = actual_rows[1:]
        if actual_records != expected_records:
            raise AuditError('CSV readback differs from the validated output')
        if actual_differences(header, original_records, actual_records) != saved_audit['changes']:
            raise AuditError('CSV differences do not exactly match the saved change audit')
    except Exception:
        if created_csv:
            try:
                (destination / 'cleaned.csv').unlink()
            except OSError:
                pass
        if created_audit:
            try:
                (destination / 'audit.json').unlink()
            except OSError:
                pass
        if created_directory:
            try:
                destination.rmdir()
            except OSError:
                pass
        raise


def make_parser():
    parser = argparse.ArgumentParser(
        description='Audit a bounded UTF-8 comma CSV offline; optionally trim one column.'
    )
    parser.add_argument('input_csv', help='input CSV file')
    parser.add_argument('--output-dir', required=True, help='new output directory (must not exist)')
    parser.add_argument('--trim-column', action='append', help='exact header name; at most once')
    return parser


def main(argv=None):
    args = make_parser().parse_args(argv)
    trim_values = args.trim_column or []
    if len(trim_values) > 1:
        print('csv_audit: --trim-column may be supplied at most once', file=sys.stderr)
        return 2

    source = Path(args.input_csv)
    destination = Path(args.output_dir)
    try:
        original_bytes, header, records = read_snapshot(source)
        trim_column = trim_values[0] if trim_values else None
        output_records, audit = build_outputs(header, records, trim_column)
        audit['input_sha256'] = hashlib.sha256(original_bytes).hexdigest()
        write_and_verify(destination, header, records, output_records, audit)
    except AuditError as error:
        print('csv_audit: ' + str(error), file=sys.stderr)
        return 2
    except OSError:
        print('csv_audit: filesystem operation failed', file=sys.stderr)
        return 2
    except (UnicodeError, csv.Error, json.JSONDecodeError, ValueError):
        print('csv_audit: input or output could not be validated', file=sys.stderr)
        return 2

    print('OK: audited %d logical data records.' % len(records))
    return 0


if __name__ == '__main__':
    sys.exit(main())
