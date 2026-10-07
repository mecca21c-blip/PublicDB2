"""Compatibility imports for the shared semantic discovery projector."""

from app.services.semantic_discovery_service import (
    DEFAULT_DISCOVERY_PAGE_SIZE,
    MAX_DISCOVERY_PAGE_SIZE,
    DiscoveryCategory,
    RunDiscoveryNotFound,
    SemanticDiscoveryProjector,
)

DiscoveryReadService = SemanticDiscoveryProjector
