import argparse
import re
from pathlib import Path

from openpyxl import load_workbook


DEFAULT_WORKBOOK = 'Data/RefreshScenarios.xlsx'
FOLDER_PATH = 'Test Repository/The INTcredibles/API -Manual'
NO_DATA = 'Nil'
HAPPY_PREFIX = 'Happy case_ '
UNHAPPY_PREFIX = 'Unhappy case_ '
SCOPE_PREFIX = 'The scope of the test case is to validate '
STATUS_PATTERN = re.compile(r'status(?:\s*code)?\s*[=:]?\s*(\d{3})', re.IGNORECASE)
LEADING_VALIDATE_PATTERN = re.compile(
    r'^\s*validat\w*\s+(?:response\s+)?(?:for\s+)?', re.IGNORECASE
)


def fail(message):
    raise RuntimeError(message)


def get_cell_text(cell):
    if cell.value is None:
        return ''
    if isinstance(cell.value, str):
        return cell.value
    return str(cell.value)


def resolve_workbook(name):
    path = Path(name)
    if path.suffix.lower() not in ('.xlsx', '.xlsm'):
        path = Path(str(path) + '.xlsx')
    for candidate in (path, Path('Data') / path):
        if candidate.is_file():
            return str(candidate)
    fail(f'<<CHECK: workbook not found: {name}>>')


def get_scenario_rows(workbook_path, sheet_name, row_numbers):
    workbook = load_workbook(workbook_path, data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            fail(f'Sheet not found: {sheet_name}')
        worksheet = workbook[sheet_name]
        headers = [get_cell_text(cell) for cell in worksheet[1]]
        while headers and not headers[-1].strip():
            headers.pop()
        if not headers:
            fail('<<CHECK: no column headers found in row 1>>')
        for index, header in enumerate(headers, start=1):
            if not header.strip():
                fail(f'<<CHECK: header of column {index}>>')
        if len(headers) % 2:
            fail(
                '<<CHECK: column count must be even (action/validation pairs), '
                f'found {len(headers)}>>'
            )
        scenarios = []
        for row_number in row_numbers:
            if row_number > worksheet.max_row:
                fail(f'Row not found: {row_number}')
            columns = [
                {
                    'Header': header,
                    'Value': get_cell_text(worksheet.cell(row_number, index)),
                }
                for index, header in enumerate(headers, start=1)
            ]
            scenarios.append({'Row': row_number, 'Columns': columns})
        return scenarios
    finally:
        workbook.close()


def normalize_words(text):
    return ' '.join(text.replace('_', ' ').split())


def remove_repeated_words(text):
    words = []
    for word in text.split():
        if not words or words[-1].lower() != word.lower():
            words.append(word)
    return ' '.join(words)


def get_case_prefix(value):
    match = STATUS_PATTERN.search(value)
    if not match:
        return ''
    code = int(match.group(1))
    if 200 <= code <= 299:
        return HAPPY_PREFIX
    if 400 <= code <= 599:
        return UNHAPPY_PREFIX
    return ''


def describe_values(value):
    parts = []
    for part in re.split(r'[,;\n]+', value):
        part = ' '.join(part.split())
        if not part:
            continue
        if '=' in part:
            key, _, key_value = part.partition('=')
            key, key_value = key.strip(), key_value.strip()
            if key.lower() in ('status', 'status code') and key_value:
                part = f'status code {key_value}'
            elif key and key_value:
                part = f'{key} with {key_value}'
        parts.append(part)
    if len(parts) > 1:
        return ', '.join(parts[:-1]) + ' and ' + parts[-1]
    return parts[0]


def new_step(action_column, check_column):
    check_value = check_column['Value']
    if not check_value.strip():
        fail(f"<<CHECK: {check_column['Header']}>>")
    data = action_column['Value']
    has_data = bool(data.strip()) and data.strip().lower() != NO_DATA.lower()
    header = ' '.join(action_column['Header'].split())
    action = f'Trigger {header} endpoint' + (' with given data' if has_data else '')
    expected = LEADING_VALIDATE_PATTERN.sub('', check_value, count=1)
    return {
        'step': action,
        'data': data if has_data else NO_DATA,
        'result': f'Validate response for {expected}',
        'attachments': [],
    }


def new_scenario_definition(sheet_name, scenario):
    columns = scenario['Columns']
    steps = [
        new_step(columns[index], columns[index + 1])
        for index in range(0, len(columns), 2)
    ]
    endpoint_header = ' '.join(columns[-2]['Header'].replace('_', ' ').split())
    last_value = columns[-1]['Value']
    prefix = get_case_prefix(last_value)
    scope = remove_repeated_words(
        f'{normalize_words(sheet_name).lower()} for {endpoint_header} endpoint '
        f'for {describe_values(last_value)}'
    )
    return {
        'Row': scenario['Row'],
        'Title': f'{prefix}To validate {scope}',
        'Description': f'{SCOPE_PREFIX}{scope}.',
        'Steps': steps,
    }


def parse_row_numbers(rows):
    row_parts = rows.split('-')
    return (
        [int(row_parts[0])]
        if len(row_parts) == 1
        else list(range(int(row_parts[0]), int(row_parts[1]) + 1))
    )


def build_definitions(sheet_name, row_numbers, workbook_path=DEFAULT_WORKBOOK):
    scenarios = get_scenario_rows(workbook_path, sheet_name, row_numbers)
    return [new_scenario_definition(sheet_name, scenario) for scenario in scenarios]


def format_preview(definition, assignee, folder_display):
    lines = [
        'Type: Test',
        f"Title: {definition['Title']}",
        f"Description: {definition['Description']}",
        f'Assignee: {assignee}',
        'Test Level: Regression',
        'Test Case Type: Functional',
        'Manual Steps:',
    ]
    for index, step in enumerate(definition['Steps'], start=1):
        lines.append(
            f"{index}. Action: {step['step']} | Data: {step['data']} "
            f"| Expected Result: {step['result']}"
        )
    lines.extend([f'Target Folder: {folder_display}', ''])
    return '\n'.join(lines)


def write_and_print_preview(
    definitions, assignee, sheet_name, row_numbers, folder_display=None
):
    folder_display = folder_display or f'{FOLDER_PATH}/{sheet_name}'
    preview = ''.join(
        format_preview(definition, assignee, folder_display)
        for definition in definitions
    )
    preview += f'Phase 1 dry run complete: {len(definitions)} full row block(s) printed.\n'
    preview_path = (
        Path('Data/_generated')
        / f'preview_{sheet_name}_{row_numbers[0]}-{row_numbers[-1]}.txt'
    )
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_text(preview, encoding='utf-8')
    print(preview, end='')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sheet', required=True)
    parser.add_argument('--rows', required=True)
    parser.add_argument('--workbook', default=DEFAULT_WORKBOOK)
    args = parser.parse_args()

    row_numbers = parse_row_numbers(args.rows)
    definitions = build_definitions(
        args.sheet, row_numbers, resolve_workbook(args.workbook)
    )
    try:
        from API_TC_Create2 import discover_schema, resolve_target_folder
    except ImportError:
        from .API_TC_Create2 import discover_schema, resolve_target_folder

    _, assignee, headers = discover_schema()
    target = resolve_target_folder(headers, args.sheet)
    write_and_print_preview(
        definitions, assignee, args.sheet, row_numbers, target['display']
    )


if __name__ == '__main__':
    main()
