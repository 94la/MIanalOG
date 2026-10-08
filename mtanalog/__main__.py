import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tomllib

from .collector import collect
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description='Personal BTCUSDT Spot liquidity archive')
    parser.add_argument('--config', default='config.toml')
    sub = parser.add_subparsers(dest='command', required=True)
    collect_args = sub.add_parser('collect')
    collect_args.add_argument('--seconds', type=float, help='Bounded live smoke test')
    sub.add_parser('status')
    sub.add_parser('prune')
    sub.add_parser('verify')
    sub.add_parser('backup')
    sub.add_parser('web')
    sub.add_parser('web-start')
    sub.add_parser('web-stop')
    invitation = sub.add_parser('web-invite', help='Create a shared expiring link without printing credentials')
    invitation.add_argument('--base-url', required=True)
    invitation.add_argument('--hours', type=float, default=12)
    invitation.add_argument('--output', required=True)
    invitation.add_argument('--owner-output')
    for command in ('start', 'stop', 'serve'):
        sub.add_parser(command)
    args = parser.parse_args()
    path = Path(args.config).resolve()
    config = tomllib.loads(path.read_text())
    if config['symbol'] != 'BTCUSDT' or config['retention_days'] != 14:
        parser.error('This project supports BTCUSDT Spot and 14-day retention only.')
    config['data_dir'] = str(path.parent / config['data_dir'])
    if config['base_price_step'] <= 0 or config['price_step'] <= 0 or config['min_liquidity_usdt'] <= 0:
        parser.error('Price steps and minimum liquidity must be positive.')
    Path(config['data_dir']).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'collect':
        if args.seconds is None:
            handler = RotatingFileHandler(Path(config['data_dir'])/'collector.log', maxBytes=5_000_000, backupCount=2)
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            logging.getLogger().handlers[:] = [handler]
        asyncio.run(collect(config, args.seconds))
    elif args.command == 'web-invite':
        from .access import invite
        from datetime import datetime, timezone
        try:
            expires = invite(config, args.base_url, args.output, args.owner_output, args.hours)
        except ValueError as error:
            parser.error(str(error))
        print('Guest link saved to:', args.output)
        print('Expires UTC:', datetime.fromtimestamp(expires, timezone.utc).isoformat())
    elif args.command in ('web', 'web-start', 'web-stop'):
        from .web import run, start, stop
        if args.command == 'web':
            run(config)
        elif args.command == 'web-start':
            print('Web PID:', start(config, path))
        else:
            print('Web stop requested:', stop(config))
    elif args.command in ('start', 'stop', 'serve'):
        from . import supervisor
        if args.command == 'start':
            print(f'Supervisor PID: {supervisor.start(config, path)}')
        elif args.command == 'stop':
            print('Stop requested' if supervisor.stop(config) else 'Supervisor is not running')
        else:
            supervisor.serve(config, path)
    elif args.command == 'backup':
        from .backup import backup
        print(backup(config, path.parent))
    elif args.command == 'verify':
        from .replay import verify
        result = verify(config)
        print(json.dumps(result, indent=2))
        if not result['book_matches_checkpoint'] or not result['cvd_matches_sqlite'] or result['active_or_truncated_gzip']:
            raise SystemExit(1)
    else:
        store = Store(config['data_dir'], read_only=args.command == 'status')
        try:
            if args.command == 'status':
                print(json.dumps(store.status(), indent=2, ensure_ascii=False))
            else:
                store.prune(config['retention_days'])
        finally:
            store.close()


if __name__ == '__main__':
    main()
