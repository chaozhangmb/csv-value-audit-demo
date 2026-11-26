# Offline CSV audit demo

Python 3.9+ standard library only; implemented and tested with Python 3.9.6. It reads a bounded UTF-8 comma CSV, reports data-quality facts, and optionally trims ASCII spaces and tabs from one exact header column.

```bash
python3 -I csv_audit.py sample.csv --output-dir run-output --trim-column Name
```

`example-output/` is the saved demo result included with these files; inspect it as a read-only reference. Every CLI run needs a new output directory. If `run-output/` already exists, choose another new path; the CLI refuses existing paths, and prior results should not be deleted to rerun.

Report-only mode preserves parsed field values and uses a different, new output directory:

```bash
python3 -I csv_audit.py sample.csv --output-dir report-only-output
```

The input may be at most 5,000,000 bytes, with at most 10,000 logical data records and 30 columns. A UTF-8 BOM is accepted and included in the original-byte SHA-256, but removed from the parsed header. Headers must be nonblank and exactly unique. Every record must have the header width. Invalid UTF-8, malformed CSV, invalid options, and limit violations exit with status 2. Validation completes before output creation; the output directory must be new and its parent must already exist.

No value types are inferred: `00123` stays text, an empty field differs from `0`, and date-looking strings are unchanged. Original exact duplicate rows are reported by logical record numbers starting at 1 (header excluded) and are retained. Optional trimming is exactly `.strip(' \t')` on the selected column; it does not trim other columns, CR/LF, or Unicode whitespace. The audit lists every actual change with its original and output values.

The output CSV uses UTF-8 without a BOM and CRLF record terminators. Parsed values and embedded newlines round-trip, but original quoting, BOM, and record-terminator bytes need not match. The audit JSON includes a copy of input values in the change list, so treat it as data-bearing. Formula-like prefixes are reported and preserved, not neutralized; the CSV is not spreadsheet-safe for opening or executing as a spreadsheet. These indicators are not a complete security check. Running this offline CLI does not establish how a future AI-assisted service would handle customer data.

## Scoped paid audits

AI-assisted CSV structure audits are available for discussion. This synthetic sample demonstrates empty-field counts, reports of exact duplicate input records, spreadsheet formula-prefix warnings, and optional ASCII space/tab trimming in one specified column.

A quote depends on the agreed input format, requested rule and expected output. A general enquiry can describe column names and the desired result. Do not post real files, personal details or sensitive data in public issues. The example demonstrates functionality; it is not a customer case study.
