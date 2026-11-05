import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from API_TC_Preview2 import (
        FOLDER_PATH,
        build_definitions,
        parse_row_numbers,
        resolve_workbook,
        write_and_print_preview,
    )
    from API_TC_Preview2 import fail
except ImportError:
    from .API_TC_Preview2 import (
        FOLDER_PATH,
        build_definitions,
        parse_row_numbers,
        resolve_workbook,
        write_and_print_preview,
    )
    from .API_TC_Preview2 import fail


BASE_URL = 'https://trackspace.lhsystems.com'
PROJECT_KEY = 'FSD'
REFERENCE_ISSUE_KEY = 'FSD-12945'
DEFAULT_WORKBOOK = 'Data/RefreshScenarios.xlsx'
FOLDER_ROOT_ID = -1


def request_json(base_url, path, headers, method='GET', body=None):
    request = Request(
        base_url.rstrip('/') + path,
        headers=headers,
        method=method,
        data=json.dumps(body).encode('utf-8') if body is not None else None,
    )
    try:
        with urlopen(request) as response:
            content = response.read()
            return json.loads(content) if content else None
    except (HTTPError, URLError) as error:
        raise RuntimeError(str(error)) from error


def load_token():
    env_path = Path(__file__).resolve().parent.parent / '.env'
    if not env_path.exists():
        fail('<<CHECK: TRACKSPACE_ACCESS_TOKEN is missing from .env>>')
    for line in env_path.read_text(encoding='utf-8').splitlines():
        if line.startswith('TRACKSPACE_ACCESS_TOKEN='):
            return line.split('=', 1)[1].strip()
    fail('<<CHECK: TRACKSPACE_ACCESS_TOKEN is missing from .env>>')


def discover_schema():
    token = load_token()
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    reference = request_json(
        BASE_URL,
        f'/rest/api/2/issue/{REFERENCE_ISSUE_KEY}?fields=issuetype',
        headers,
    )
    if reference.get('fields', {}).get('issuetype', {}).get('name') != 'Test':
        fail('<<CHECK: reference issue type is not Test>>')
    edit_meta = request_json(
        BASE_URL, f'/rest/api/2/issue/{REFERENCE_ISSUE_KEY}/editmeta', headers
    )
    test_level_field = next(
        (
            {'id': field_id, 'value': field}
            for field_id, field in edit_meta.get('fields', {}).items()
            if field.get('name', '').lower() == 'test level'
        ),
        None,
    )
    test_type_field = next(
        (
            {'id': field_id, 'value': field}
            for field_id, field in edit_meta.get('fields', {}).items()
            if field.get('name', '').lower() == 'test case type'
        ),
        None,
    )
    regression = next(
        (
            item
            for item in (test_level_field or {}).get('value', {}).get('allowedValues', [])
            if item.get('value') == 'Regression'
        ),
        None,
    )
    functional = next(
        (
            item
            for item in (test_type_field or {}).get('value', {}).get('allowedValues', [])
            if item.get('value') == 'Functional'
        ),
        None,
    )
    if not test_level_field or not regression:
        fail('<<CHECK: could not identify Test Level or Regression>>')
    if not test_type_field or not functional:
        fail('<<CHECK: could not identify Test Case Type or Functional>>')
    myself = request_json(BASE_URL, '/rest/api/2/myself', headers)
    assignee = myself.get('name') or myself.get('key')
    schema = {
        'test_level_field': test_level_field,
        'test_type_field': test_type_field,
        'regression': regression,
        'functional': functional,
    }
    return schema, assignee, headers


def folder_children(folder):
    for key in ('folders', 'children', 'subFolders', 'subfolders'):
        children = folder.get(key)
        if isinstance(children, list):
            return children
    return []


def folder_tree(headers):
    response = request_json(
        BASE_URL,
        f'/rest/raven/1.0/api/testrepository/{PROJECT_KEY}/folders',
        headers,
    )
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in ('folders', 'children', 'subFolders', 'subfolders'):
            if isinstance(response.get(key), list):
                return response[key]
        if 'id' in response:
            return [response]
    fail('<<CHECK: unexpected Trackspace folder tree response>>')


def folder_name(folder):
    return str(folder.get('name', '')).strip()


def find_child(folder, name):
    name = name.casefold()
    return next(
        (child for child in folder_children(folder) if folder_name(child).casefold() == name),
        None,
    )


def resolve_base_folder(tree):
    parts = [part.strip() for part in FOLDER_PATH.split('/') if part.strip()]
    if parts and parts[0].casefold() == 'test repository':
        parts = parts[1:]
    current = {'id': FOLDER_ROOT_ID, 'name': 'Test Repository', 'folders': tree}
    for part in parts:
        current = find_child(current, part)
        if current is None:
            fail(f'<<CHECK: base folder not found: {FOLDER_PATH}>>')
    return current


def resolve_target_folder(headers, sheet_name):
    tree = folder_tree(headers)
    base = resolve_base_folder(tree)
    target = find_child(base, sheet_name)
    display = f'{FOLDER_PATH}/{sheet_name}'
    if target is None:
        return {'id': None, 'display': f'{display} (will be created)', 'base_id': base['id']}
    return {
        'id': target.get('id'),
        'display': f'{display} (exists, id {target.get("id")})',
        'base_id': base['id'],
    }


def ensure_target_folder(headers, sheet_name, target=None):
    target = target or resolve_target_folder(headers, sheet_name)
    if target['id'] is not None:
        return target
    try:
        created = request_json(
            BASE_URL,
            f'/rest/raven/1.0/api/testrepository/{PROJECT_KEY}/folders/{target["base_id"]}',
            headers,
            'POST',
            {'name': sheet_name},
        )
    except RuntimeError as error:
        raise RuntimeError(
            f'<<CHECK: could not create folder {FOLDER_PATH}/{sheet_name}: {error}>>'
        ) from error
    folder_id = created.get('id') if isinstance(created, dict) else created
    if folder_id is None:
        fail('<<CHECK: folder creation returned no folder id>>')
    return {
        'id': folder_id,
        'display': f'{FOLDER_PATH}/{sheet_name} (created, id {folder_id})',
        'base_id': target['base_id'],
    }


def test_trackspace_duplicate(base_url, headers, title, description, new_steps):
    if not new_steps:
        fail('Duplicate check requires at least 1 new step.')
    jql = f'project=FSD AND summary~"{title.replace(chr(34), chr(92) + chr(34))}"'
    query = urlencode({'jql': jql}) + '&fields=summary,description&maxResults=100'
    search = request_json(base_url, f'/rest/api/2/search?{query}', headers)
    for candidate in search.get('issues', []):
        key = candidate['key']
        issue = request_json(
            base_url, f'/rest/api/2/issue/{key}?fields=summary,description', headers
        )
        fields = issue.get('fields', {})
        if fields.get('summary') != title or fields.get('description') != description:
            continue
        response = request_json(base_url, f'/rest/raven/1.0/api/test/{key}/step', headers)
        if isinstance(response, list):
            candidate_steps = response
        elif isinstance(response, dict) and response.get('steps'):
            candidate_steps = response['steps']
        else:
            candidate_steps = [response]
        if len(candidate_steps) != len(new_steps):
            continue
        if all(
            candidate_steps[index].get('step') == new_steps[index]['step']
            and candidate_steps[index].get('data') == new_steps[index]['data']
            and candidate_steps[index].get('result') == new_steps[index]['result']
            for index in range(len(new_steps))
        ):
            return key
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sheet', required=True)
    parser.add_argument('--rows', required=True)
    parser.add_argument('--workbook', default=DEFAULT_WORKBOOK)
    parser.add_argument('--create', action='store_true')
    args = parser.parse_args()

    row_numbers = parse_row_numbers(args.rows)
    definitions = build_definitions(
        args.sheet, row_numbers, resolve_workbook(args.workbook)
    )
    schema, assignee, headers = discover_schema()
    target = resolve_target_folder(headers, args.sheet)
    write_and_print_preview(
        definitions, assignee, args.sheet, row_numbers, target['display']
    )
    if not args.create:
        return

    created_count = 0
    for definition in definitions:
        duplicate = test_trackspace_duplicate(
            BASE_URL,
            headers,
            definition['Title'],
            definition['Description'],
            definition['Steps'],
        )
        if duplicate:
            print(f"{definition['Title']} -> skipped as duplicate {duplicate}")
            continue
        target = ensure_target_folder(headers, args.sheet, target)
        fields = {
            'project': {'key': PROJECT_KEY},
            'issuetype': {'name': 'Test'},
            'summary': definition['Title'],
            'description': definition['Description'],
            schema['test_level_field']['id']: [{'id': schema['regression']['id']}],
            schema['test_type_field']['id']: [{'id': schema['functional']['id']}],
            'assignee': {'name': assignee},
        }
        created = request_json(
            BASE_URL,
            '/rest/api/2/issue',
            headers,
            'POST',
            {'fields': fields},
        )
        key = created['key']
        try:
            request_json(
                BASE_URL,
                f'/rest/raven/1.0/api/testrepository/{PROJECT_KEY}/folders/{target["id"]}/tests',
                headers,
                'PUT',
                {'add': [key]},
            )
        except RuntimeError:
            try:
                request_json(BASE_URL, f'/rest/api/2/issue/{key}', headers, 'DELETE')
            except RuntimeError:
                pass
            raise
        for step in definition['Steps']:
            try:
                request_json(
                    BASE_URL,
                    f'/rest/raven/1.0/api/test/{key}/step',
                    headers,
                    'PUT',
                    step,
                )
            except RuntimeError as error:
                raise RuntimeError(
                    f'<<CHECK: step-add failed for {definition["Row"]}, issue {key} '
                    f'was created and filed but steps require manual entry>>'
                ) from error
        print(f"{definition['Title']} -> created as {key}, filed under {target['display']}")
        created_count += 1
    print(f'Total: {len(definitions)} rows processed - {created_count} fully created.')


if __name__ == '__main__':
    main()
