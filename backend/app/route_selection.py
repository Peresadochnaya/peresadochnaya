"""Shared classification of walking itineraries."""
from .schemas import Route


def is_walking_route(route: Route) -> bool:
    """A walking itinerary has a walk leg and no transport legs."""
    return (bool(route.legs)
            and all(leg.mode in {"walk", "wait"} for leg in route.legs)
            and any(leg.mode == "walk" for leg in route.legs))
