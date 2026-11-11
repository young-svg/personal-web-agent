"""Thin command-line entry point."""

import argparse
import asyncio
from importlib.metadata import version


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal Web Agent")
    parser.add_argument("--version", action="version", version=version("personal-web-agent"))
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("smoke", help="Run the real DeepSeek / visible local Chrome smoke (uses API credits)")
    prepare = commands.add_parser("apply-prepare", help="Fixture preparation, or fail-closed Greenhouse offline checks")
    prepare.add_argument('--site', choices=['fixture', 'greenhouse'], default='fixture')
    prepare.add_argument('--snapshot', help='Local P2-B JSON page snapshot; required for Greenhouse')
    prepare.add_argument('--check-only', action='store_true', help='Offline Greenhouse data matching only; no browser/model')
    prepare.add_argument("--url", required=True)
    prepare.add_argument("--profile", required=True, help="Synthetic profile JSON")
    prepare.add_argument("--resume", required=True, help="Fixture-generated test PDF")
    prepare.add_argument("--instruction", required=True)
    prepare.add_argument("--handoff", action="store_true", help="Keep the same page for a real person's one-time manual submit")
    prepare.add_argument('--confirm-no-side-effects', action='store_true',
                         help='Explicitly confirm a previous FAILED/CANCELLED task has no external effects before retry')
    publish = commands.add_parser('publish-prepare', help='Local fixture preparation or blocked CSDN offline checks')
    publish.add_argument('--site', choices=['fixture', 'csdn'], default='fixture')
    publish.add_argument('--check-only', action='store_true', help='Offline CSDN structural validation; no browser/model')
    publish.add_argument('--snapshot', help='Local CSDN structural JSON from P3-C1 or a synthetic snapshot')
    publish.add_argument('--local-user-id', help='Opaque stable local account alias, never a login credential')
    publish.add_argument('--cdp-url', help='Existing Edge loopback endpoint (default http://127.0.0.1:9223)')
    publish.add_argument('--allow-csdn-draft', action='store_true',
                         help='Explicit user consent for CSDN test draft/auto-save, specified image upload and DeepSeek page transmission; NEVER publication')
    publish.add_argument('--url', required=True)
    publish.add_argument('--title', required=True)
    publish.add_argument('--body-file', required=True)
    publish.add_argument('--images', nargs='+', required=True)
    publish.add_argument('--instruction', required=True)
    publish.add_argument('--handoff', action='store_true')
    publish.add_argument('--confirm-no-side-effects', action='store_true')
    tasks = commands.add_parser('tasks', help='Query private SQLite task records (no API calls)')
    tasks.add_argument('--id', help='Show one task ID; omit to list all')
    args = parser.parse_args()
    if args.command == 'tasks':
        import json
        from personal_web_agent.records import TaskStore
        try:
            store = TaskStore()
            print(json.dumps(store.get(args.id) if args.id else store.list(), ensure_ascii=False, indent=2))
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command == 'publish-prepare':
        from personal_web_agent.runner import run_publish_prepare
        try:
            if args.site == 'csdn':
                if not args.snapshot or not args.local_user_id:
                    parser.error('--site csdn requires --snapshot and --local-user-id')
                from personal_web_agent.csdn import load_inputs
                inputs = load_inputs(args.url, args.title, args.body_file, args.images, args.instruction,
                                     snapshot=args.snapshot, local_user_id=args.local_user_id,
                                     cdp_url=args.cdp_url or 'http://127.0.0.1:9223')
            else:
                if args.check_only or args.snapshot or args.local_user_id or args.cdp_url or args.allow_csdn_draft:
                    parser.error('--check-only/--snapshot/--local-user-id/--cdp-url require --site csdn')
                from personal_web_agent.publishing import load_inputs
                inputs = load_inputs(args.url, args.title, args.body_file, args.images, args.instruction)
            result = asyncio.run(run_publish_prepare(inputs, human_handoff=args.handoff,
                                                      confirm_no_side_effects=args.confirm_no_side_effects,
                                                      check_only=args.check_only, allow_csdn_draft=args.allow_csdn_draft))
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        raise SystemExit(result)
    elif args.command == "smoke":
        from personal_web_agent.runner import run_smoke

        raise SystemExit(asyncio.run(run_smoke()))
    elif args.command == "apply-prepare":
        from personal_web_agent.runner import run_apply_prepare
        from personal_web_agent.task_inputs import load_prepare_inputs

        try:
            if args.site == 'greenhouse':
                if not args.snapshot:
                    parser.error('--site greenhouse requires a local --snapshot in P2-C1')
                from personal_web_agent.greenhouse import load_inputs
                inputs = load_inputs(args.url, args.profile, args.resume, args.instruction, args.snapshot)
            else:
                if args.snapshot or args.check_only:
                    parser.error('--snapshot/--check-only require --site greenhouse')
                inputs = load_prepare_inputs(args.url, args.profile, args.resume, args.instruction)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        try:
            result = asyncio.run(run_apply_prepare(inputs, human_handoff=args.handoff,
                                                  confirm_no_side_effects=args.confirm_no_side_effects,
                                                  check_only=args.check_only))
        except ValueError as exc:
            parser.error(str(exc))
        raise SystemExit(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
