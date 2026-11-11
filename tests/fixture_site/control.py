"""Local fixture state/reset/revoke CLI; never prints its private credential."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
import httpx


def main():
    parser = argparse.ArgumentParser(description='Control a local fixture between runs')
    parser.add_argument('action', choices=['state', 'reset', 'cancel', 'authorize'])
    parser.add_argument('--site', choices=['recruit', 'publish'], default='recruit')
    parser.add_argument('--draft-id', help='Current saved publishing draft ID; required for authorize')
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost'} or not url.port
            or url.path not in {'', '/'} or url.username or url.password or url.query or url.fragment):
        parser.error('Only an explicit loopback fixture origin is allowed')
    if args.action == 'authorize' and (args.site != 'publish' or not args.draft_id):
        parser.error('authorize requires --site publish --draft-id; recruitment handoff is unchanged')
    if args.draft_id and args.action != 'authorize':
        parser.error('--draft-id only applies to authorize')
    prefix = '/api/publish/' if args.site == 'publish' else '/api/'
    origin = f'{url.scheme}://{url.netloc}'
    with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
        if args.action == 'state':
            response = client.get(prefix + 'state')
        else:
            token = (Path(__file__).resolve().parents[2] / '.local/fixture-control-token').read_text().strip()
            response = client.post(prefix + args.action, headers={'X-Fixture-Control': token},
                                   json={'draft_id': args.draft_id} if args.action == 'authorize' else None)
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0 if response.is_success else 1


if __name__ == '__main__':
    raise SystemExit(main())
