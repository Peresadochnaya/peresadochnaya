"""Apply deployed itinerary filters without modifying an immutable graph version."""
import argparse
import json
from pathlib import Path


def configure(base: Path, defaults: Path, output: Path) -> None:
    config = json.loads(base.read_text())
    settings = json.loads(defaults.read_text())
    filters = settings['routingDefaults']['itineraryFilters']
    if not isinstance(filters, dict) or not filters:
        raise ValueError('Expected nonempty itineraryFilters')
    config.setdefault('routingDefaults', {}).setdefault('itineraryFilters', {}).update(filters)
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--defaults', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    configure(args.base, args.defaults, args.output)
