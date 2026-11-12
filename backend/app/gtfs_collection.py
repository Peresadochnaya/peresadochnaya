"""Expose optional namespaced rail GTFS alongside the surface feed."""
from contextlib import ExitStack, contextmanager

from .gtfs_store import GtfsStore


class MultiFeedGtfsStore:
    def __init__(self, root):
        self.root = root
        self.surface = GtfsStore(root)
        self.rail = GtfsStore(root, feed='rail')

    def preload(self):
        for store in (self.surface, self.rail):
            if (self.root / store.feed / 'current.json').exists():
                with store.open() as snapshot:
                    snapshot.index

    @contextmanager
    def open(self, dummy=False):
        with ExitStack() as stack:
            surface = stack.enter_context(self.surface.open(dummy=dummy))
            rail = None
            if not dummy and (self.root / 'rail/current.json').exists():
                rail = stack.enter_context(self.rail.open())
            yield FeedCollection(surface, rail)


class FeedCollection:
    def __init__(self, surface, rail):
        self.surface, self.rail = surface, rail
        self.selected = None
        self._merged = {}

    @property
    def metadata(self):
        meta = (self.selected or self.surface).metadata
        if self.rail is not None:
            meta['warnings'].append(f'rail_gtfs_version: {self.rail.version}; exact dated services, no calendar extension')
        return meta

    def _combined(self, name):
        if name in self._merged:
            return self._merged[name]
        result = dict(getattr(self.surface, name))
        if self.rail is not None:
            for key, value in getattr(self.rail, name).items():
                if key in result:
                    raise ValueError('Colliding GTFS identifiers across feeds')
                result[key] = value
        self._merged[name] = result
        return result

    @property
    def stops(self):
        return self._combined('stops')

    @property
    def routes(self):
        return self._combined('routes')

    @property
    def trips(self):
        return self._combined('trips')

    def _select(self, identifier):
        self.selected = self.rail if self.rail is not None and identifier.startswith('rail:') else self.surface
        return self.selected

    def get_stop(self, identifier):
        return self._select(identifier).get_stop(identifier)

    def get_route(self, identifier):
        return self._select(identifier).get_route(identifier)

    def trip(self, identifier):
        return self._select(identifier).trip(identifier)

    def departures(self, identifier, *args):
        return self._select(identifier).departures(identifier, *args)

    def stop_routes(self):
        if self.selected is not None:
            return self.selected.stop_routes()
        result = dict(self.surface.stop_routes())
        if self.rail is not None:
            result.update(self.rail.stop_routes())
        return result
