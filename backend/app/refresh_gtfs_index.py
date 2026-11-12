"""Run the existing source updater, then prepare its shared disk index."""
import argparse
import json
from pathlib import Path

from .gtfs_index import warm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('feed', choices=['gtfs', 'metro', 'rail'])
    parser.add_argument('--data-root', type=Path, default=Path('/data'))
    args = parser.parse_args()
    if args.feed == 'gtfs':
        from data.gtfs.versioned_static import refresh
        result = refresh(args.data_root / 'gtfs')
    elif args.feed == 'metro':
        from data.metro.refresh import refresh
        result = refresh(args.data_root / 'metro')
    else:
        from data.rail.refresh import refresh_range, HUBS, MOSCOW
        from datetime import datetime
        import os
        result = refresh_range(args.data_root / 'rail', datetime.now(MOSCOW).date(), 7,
                               HUBS, os.environ['YANDEX_RASP_API_KEY'],
                               raw_dir=args.data_root / 'rail-raw')
    print(json.dumps({'refresh': result, 'indexes': warm(args.data_root)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
