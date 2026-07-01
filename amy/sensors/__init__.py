"""Sensors — external data sources that publish normalized events to the bus.

A Sensor is NOT an agent. It authenticates to an external system, normalizes
incoming data into canonical events, and publishes them to the Event Bus. Agents
then subscribe and react. This keeps external integrations decoupled from
reasoning.

    External system  ->  Sensor  ->  Event Bus  ->  Relevant Agents
"""
from .github_models import (
    GitHubEvent, GITHUB_EVENT_TYPES,
    NEW_REPOSITORY, NEW_COMMIT, NEW_PULL_REQUEST, NEW_ISSUE, NEW_RELEASE, CI_FAILURE,
)
from .github_service import GitHubService
from .github_sensor import GitHubSensor
